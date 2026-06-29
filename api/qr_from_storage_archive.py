#!/usr/bin/env python3
"""
Download a Firebase Storage (or any HTTP) archive URL, extract .ai files,
scan each page for QR codes and barcodes using zxing-cpp + PyMuPDF,
and return results as JSON.

Requires for .rar: 7-Zip installed (https://www.7-zip.org/).
Install deps: pip install requests pymupdf opencv-contrib-python-headless zxing-cpp
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import cv2
import pypdfium2 as pdfium  # replaced fitz due to DLL block policy
import numpy as np
import requests
import zxingcpp

# Vercel /tmp is capped at ~512 MB. Leave headroom for one extracted .ai at a time.
_DEFAULT_MAX_ARCHIVE_BYTES = 450 * 1024 * 1024


def _is_serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _tmp_root() -> Path:
    if sys.platform != "win32":
        return Path("/tmp")
    return Path(tempfile.gettempdir())


def _configure_temp_dir() -> None:
    if sys.platform == "win32":
        return
    root = _tmp_root()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)


def _max_archive_bytes() -> int | None:
    raw = os.environ.get("AI_QR_MAX_ARCHIVE_BYTES", "").strip()
    if raw:
        return int(raw)
    return _DEFAULT_MAX_ARCHIVE_BYTES if _is_serverless() else None


def _disk_limit_error(size_hint: str = "") -> RuntimeError:
    detail = f" ({size_hint})" if size_hint else ""
    return RuntimeError(
        "Storage limit reached on Vercel (~512 MB /tmp). "
        f"The archive or extracted files are too large{detail}. "
        "Try a smaller archive, upload individual .ai files, or run this job outside Vercel."
    )


def _seven_zip() -> str | None:
    # 1. Check for bundled binaries in api/bin (self-contained for Vercel/Local)
    base_dir = Path(__file__).parent
    
    # Prioritize 7zz (Full version that supports RAR)
    bundled_7zz_win = base_dir / "bin" / "7zz.exe"
    bundled_7zz_linux = base_dir / "bin" / "7zz"
    
    # Fallback to 7za (Archive-only version, no RAR support)
    bundled_7za_win = base_dir / "bin" / "7za.exe"
    bundled_7za_linux = base_dir / "bin" / "7za"
    
    if sys.platform == "win32":
        if bundled_7zz_win.exists(): return str(bundled_7zz_win)
        if bundled_7za_win.exists(): return str(bundled_7za_win)
    else:
        # Linux/Vercel
        target = bundled_7zz_linux if bundled_7zz_linux.exists() else bundled_7za_linux
        if target.exists():
            try:
                os.chmod(target, 0o755)
            except Exception:
                pass
            return str(target)

    # 2. Check for 7z variants in system path
    for name in ("7zz", "7z", "7za", "7zr", "7z.exe", "7zz.exe"):
        path = shutil.which(name)
        if path:
            return path
    
    # 3. Check common Windows installation paths
    windows_paths = [
        "C:\\Program Files\\7-Zip\\7z.exe",
        "C:\\Program Files (x86)\\7-Zip\\7z.exe"
    ]
    for p in windows_paths:
        if os.path.exists(p):
            return p
            
    return None


def _download(url: str, dest: Path, timeout: int = 120, max_bytes: int | None = None) -> None:
    """Download file to disk, optionally enforcing a max size (for Vercel /tmp limits)."""
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
        r.raise_for_status()
        if max_bytes is not None:
            content_length = r.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise _disk_limit_error(f"archive is {int(content_length) // (1024 * 1024)} MB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise _disk_limit_error(f"archive exceeds {max_bytes // (1024 * 1024)} MB")
                    f.write(chunk)
        except OSError as e:
            if e.errno == 28:
                dest.unlink(missing_ok=True)
                raise _disk_limit_error() from e
            raise


def _list_ai_members_zip(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive, "r") as zf:
        return sorted(
            name
            for name in zf.namelist()
            if not name.endswith("/") and PurePosixPath(name).suffix.lower() == ".ai"
        )


def _list_ai_members_7z(archive: Path, seven: str) -> list[str]:
    proc = subprocess.run(
        [seven, "l", "-slt", str(archive)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to list archive contents")

    members: list[str] = []
    path: str | None = None
    is_folder = False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Path = "):
            path = stripped[len("Path = ") :]
            is_folder = False
        elif stripped.startswith("Folder = +"):
            is_folder = True
        elif stripped == "" and path:
            if not is_folder and path.lower().endswith(".ai"):
                members.append(path)
            path = None
            is_folder = False

    if path and not is_folder and path.lower().endswith(".ai"):
        members.append(path)
    return sorted(set(members), key=str.lower)


def _clear_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for item in path.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item, ignore_errors=True)


def _extract_ai_member_zip(archive: Path, member: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / PurePosixPath(member).name
    with zipfile.ZipFile(archive, "r") as zf, zf.open(member) as src, dest.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return dest


def _extract_ai_member_7z(archive: Path, member: str, seven: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [seven, "x", str(archive), member, f"-o{out_dir}", "-y", "-bb0"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"Failed to extract {member}")

    matches = [p for p in out_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".ai"]
    if not matches:
        raise RuntimeError(f"Extracted member not found: {member}")
    return matches[0]


def _scan_ai_members_from_archive(
    archive_path: Path,
    members: list[str],
    *,
    extract_member: Callable[[str, Path], Path],
) -> list[dict[str, Any]]:
    """Extract and scan one .ai file at a time to stay within /tmp limits."""
    all_qr_codes: list[dict[str, Any]] = []
    scratch_dir = archive_path.parent / "scratch"

    for member in members:
        _clear_dir(scratch_dir)
        ai_path = extract_member(member, scratch_dir)
        try:
            qr_codes = _scan_ai_file(ai_path)
        finally:
            ai_path.unlink(missing_ok=True)

        rel = member.replace("\\", "/")
        for qr in qr_codes:
            all_qr_codes.append(
                {
                    "page": qr["page"],
                    "qr_data": qr["qr_data"],
                    "file": rel,
                }
            )

    shutil.rmtree(scratch_dir, ignore_errors=True)
    return all_qr_codes


def _process_archive_file(archive_path: Path, archive_url: str, name: str) -> dict[str, Any]:
    seven = _seven_zip()
    suffix = archive_path.suffix.lower()

    if suffix == ".zip":
        members = _list_ai_members_zip(archive_path)
        if not members:
            raise RuntimeError("No .ai files found in archive")

        def extract_member(member: str, out_dir: Path) -> Path:
            return _extract_ai_member_zip(archive_path, member, out_dir)

    elif seven:
        members = _list_ai_members_7z(archive_path, seven)
        if not members:
            raise RuntimeError("No .ai files found in archive")

        def extract_member(member: str, out_dir: Path) -> Path:
            return _extract_ai_member_7z(archive_path, member, seven, out_dir)

    else:
        raise RuntimeError(
            f"Cannot process {archive_path.name} without 7-Zip. "
            "Bundle api/bin/7zz for Vercel or install 7-Zip locally."
        )

    all_qr_codes = _scan_ai_members_from_archive(archive_path, members, extract_member=extract_member)
    archive_path.unlink(missing_ok=True)

    archive_sku = Path(name).stem
    if archive_sku.startswith("Order # "):
        archive_sku = archive_sku[8:]

    return {
        "archive_url": archive_url,
        "file_name": name,
        "sku_name": archive_sku,
        "qr_count": len(all_qr_codes),
        "qr_codes": all_qr_codes,
    }


def _scan_ai_file(ai_path: Path, max_dpi: int = 100) -> list[dict[str, Any]]:
    """Scan all pages of a PDF-based .ai file for QR codes only (no barcodes).
    Uses 100 DPI for fast processing — sufficient for QR detection."""
    pdf = pdfium.PdfDocument(str(ai_path))
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for page_num in range(len(pdf)):
        page = pdf[page_num]
        # Render the page to a bitmap (scale = DPI / 72)
        bitmap = page.render(scale=max_dpi / 72)
        pil_image = bitmap.to_pil()
        # Convert PIL image to numpy array for OpenCV (RGB to BGR)
        img = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)

        # Fast scan with zxing
        barcodes = zxingcpp.read_barcodes(img)
        for b in barcodes:
            if not b.valid:
                continue
            # Only QR codes, skip EAN-13 and other barcodes
            format_str = str(b.format)
            if "QR" not in format_str.upper():
                continue
            key = f"{b.format}:{b.text}"
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "page": page_num + 1,
                "qr_data": b.text,
            })

    pdf.close()
    return results


def process_archive_url(archive_url: str) -> dict[str, Any]:
    _configure_temp_dir()
    max_bytes = _max_archive_bytes()

    print("Archive URL: ", archive_url)
    parsed = urlparse(archive_url)
    name = Path(unquote(parsed.path)).name or "download.bin"

    # Check if it's a direct PDF/AI file
    is_direct_pdf = re.search(r"\.(pdf|ai)$", name, re.I) is not None

    print("is_direct_pdf: ", is_direct_pdf)

    with tempfile.TemporaryDirectory(prefix="ai_qr_", dir=str(_tmp_root())) as tmp:
        tmp_path = Path(tmp)

        if is_direct_pdf:
            file_path = tmp_path / name
            _download(archive_url, file_path, max_bytes=max_bytes)

            sku_name = file_path.stem
            if sku_name.startswith("Order # "):
                sku_name = sku_name[8:]

            qr_codes = _scan_ai_file(file_path)

            return {
                "archive_url": archive_url,
                "file_name": name,
                "sku_name": sku_name,
                "qr_count": len(qr_codes),
                "qr_codes": [{**qr, "file": name} for qr in qr_codes],
            }

        if not re.search(r"\.(zip|rar|7z)$", name, re.I):
            name = "download.rar"

        archive_path = tmp_path / name
        _download(archive_url, archive_path, max_bytes=max_bytes)
        return _process_archive_file(archive_path, archive_url, name)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download archive, extract .ai files, scan QR/barcodes."
    )
    parser.add_argument("url", nargs="?", default="", help="Direct download URL")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = parser.parse_args()

    # URL can come from env var (set by api.php to avoid shell escaping issues)
    # Base64 encoded to prevent PHP proc_open from decoding %2F etc.
    url_b64 = os.environ.get("AI_QR_URL_B64", "").strip()
    if url_b64:
        import base64
        url = base64.b64decode(url_b64).decode('utf-8')
    else:
        url = args.url or os.environ.get("AI_QR_URL", "").strip()

    if not url:
        print("Error: url required (pass as argument or set AI_QR_URL env var)", file=sys.stderr)
        return 1

    try:
        result = process_archive_url(url)
    except requests.RequestException as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return 1
    except (zipfile.BadZipFile, RuntimeError, OSError) as e:
        print(f"Extract/process failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # Unified output
    print(f"URL: {result['archive_url']}")
    print(f"Source: {result['file_name']}")
    print(f"SKU: {result['sku_name']}")
    print(f"Total QR codes: {result['qr_count']}\n")
    
    for qr in result["qr_codes"]:
        file_info = f" ({qr['file']})" if qr.get('file') != result['file_name'] else ""
        print(f"  Page {qr['page']}  {qr['qr_data']}{file_info}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
