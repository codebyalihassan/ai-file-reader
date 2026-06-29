#!/usr/bin/env python3
"""
Download a Firebase Storage (or any HTTP) URL, extract .ai/.pdf files from
archives when needed, scan each page for QR codes using pypdfium2 + zxing-cpp,
and return results as JSON.

Supports direct .pdf/.ai links and .zip/.rar/.7z archives.
Requires for .rar: 7-Zip installed (https://www.7-zip.org/) or api/bin/7zz bundled.
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


def _filename_from_content_disposition(content_disposition: str) -> str | None:
    if not content_disposition:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.I)
    if match:
        return unquote(match.group(1).strip())
    return None


def _filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = Path(path).name
    if name and name not in {"o", "media"}:
        return name
    return "download.bin"


def _extension_from_name(name: str) -> str:
    return Path(name).suffix.lower()


def _content_type_from_head(url: str) -> tuple[str | None, str | None]:
    """Return (content_type, filename_from_headers)."""
    try:
        response = requests.head(url, allow_redirects=True, timeout=30)
        if response.ok:
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            filename = _filename_from_content_disposition(response.headers.get("Content-Disposition", ""))
            return content_type or None, filename
    except requests.RequestException:
        pass
    return None, None


def _sniff_file_kind(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(8)
    if header.startswith(b"%PDF"):
        return "document"
    if header.startswith(b"PK"):
        return "zip"
    if header.startswith(b"Rar!"):
        return "rar"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    return None


def _kind_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    if "pdf" in content_type:
        return "document"
    if "zip" in content_type:
        return "zip"
    if "vnd.rar" in content_type or content_type == "application/x-rar-compressed":
        return "rar"
    if "7z" in content_type or content_type == "application/x-7z-compressed":
        return "7z"
    return None


def _resolve_input_kind(url: str, downloaded: Path | None = None) -> tuple[str, str]:
    """Return (kind, filename). kind is document | zip | rar | 7z."""
    name = _filename_from_url(url)
    ext = _extension_from_name(name)
    if ext in {".pdf", ".ai"}:
        return "document", name
    if ext == ".zip":
        return "zip", name
    if ext == ".rar":
        return "rar", name
    if ext == ".7z":
        return "7z", name

    content_type, header_name = _content_type_from_head(url)
    if header_name:
        header_ext = _extension_from_name(header_name)
        if header_ext in {".pdf", ".ai"}:
            return "document", header_name
        if header_ext == ".zip":
            return "zip", header_name
        if header_ext == ".rar":
            return "rar", header_name
        if header_ext == ".7z":
            return "7z", header_name

    kind = _kind_from_content_type(content_type)
    if kind == "document":
        return "document", name if ext else f"{Path(name).stem}.pdf"
    if kind in {"zip", "rar", "7z"}:
        return kind, name if ext else f"{Path(name).stem}.{kind}"

    if downloaded is not None and downloaded.exists():
        sniffed = _sniff_file_kind(downloaded)
        if sniffed == "document":
            return "document", name if ext else f"{Path(name).stem}.pdf"
        if sniffed in {"zip", "rar", "7z"}:
            return sniffed, name if ext else f"{Path(name).stem}.{sniffed}"

    return "rar", name if ext else "download.rar"


def _is_scannable_member(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in {".ai", ".pdf"}


def _list_scannable_members_zip(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive, "r") as zf:
        return sorted(
            name
            for name in zf.namelist()
            if not name.endswith("/") and _is_scannable_member(name)
        )


def _list_scannable_members_7z(archive: Path, seven: str) -> list[str]:
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
            if not is_folder and _is_scannable_member(path):
                members.append(path)
            path = None
            is_folder = False

    if path and not is_folder and _is_scannable_member(path):
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


def _extract_member_zip(archive: Path, member: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / PurePosixPath(member).name
    with zipfile.ZipFile(archive, "r") as zf, zf.open(member) as src, dest.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return dest


def _extract_member_7z(archive: Path, member: str, seven: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [seven, "x", str(archive), member, f"-o{out_dir}", "-y", "-bb0"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"Failed to extract {member}")

    matches = [
        p for p in out_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".ai", ".pdf"}
    ]
    if not matches:
        raise RuntimeError(f"Extracted member not found: {member}")
    return matches[0]


def _scan_members_from_archive(
    archive_path: Path,
    members: list[str],
    *,
    extract_member: Callable[[str, Path], Path],
) -> list[dict[str, Any]]:
    """Extract and scan one .ai/.pdf file at a time to stay within /tmp limits."""
    all_qr_codes: list[dict[str, Any]] = []
    scratch_dir = archive_path.parent / "scratch"

    for member in members:
        _clear_dir(scratch_dir)
        ai_path = extract_member(member, scratch_dir)
        try:
            qr_codes = _scan_document(ai_path)
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
        members = _list_scannable_members_zip(archive_path)
        if not members:
            raise RuntimeError("No .ai or .pdf files found in archive")

        def extract_member(member: str, out_dir: Path) -> Path:
            return _extract_member_zip(archive_path, member, out_dir)

    elif seven:
        members = _list_scannable_members_7z(archive_path, seven)
        if not members:
            raise RuntimeError("No .ai or .pdf files found in archive")

        def extract_member(member: str, out_dir: Path) -> Path:
            return _extract_member_7z(archive_path, member, seven, out_dir)

    else:
        raise RuntimeError(
            f"Cannot process {archive_path.name} without 7-Zip. "
            "Bundle api/bin/7zz for Vercel or install 7-Zip locally."
        )

    all_qr_codes = _scan_members_from_archive(archive_path, members, extract_member=extract_member)
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


def _qr_scan_variants(img: np.ndarray) -> list[np.ndarray]:
    variants = [img]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    variants.append(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    sharpened = cv2.addWeighted(
        gray, 1.5, cv2.GaussianBlur(gray, (0, 0), 3), -0.5, 0
    )
    variants.append(cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR))
    return variants


def _read_qr_codes_from_image(img: np.ndarray, seen: set[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for variant in _qr_scan_variants(img):
        for barcode in zxingcpp.read_barcodes(variant):
            if not barcode.valid:
                continue
            if "QR" not in str(barcode.format).upper():
                continue
            key = f"{barcode.format}:{barcode.text}"
            if key in seen:
                continue
            seen.add(key)
            results.append({"qr_data": barcode.text})
    return results


def _scan_document(doc_path: Path) -> list[dict[str, Any]]:
    """Scan all pages of a PDF or PDF-based .ai file for QR codes."""
    pdf = pdfium.PdfDocument(str(doc_path))
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    dpis = (150, 200, 300)

    try:
        for page_num in range(len(pdf)):
            page = pdf[page_num]
            page_results: list[dict[str, Any]] = []

            for dpi in dpis:
                bitmap = page.render(scale=dpi / 72)
                pil_image = bitmap.to_pil()
                img = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
                page_results = _read_qr_codes_from_image(img, seen)
                if page_results:
                    break

            for qr in page_results:
                results.append({
                    "page": page_num + 1,
                    "qr_data": qr["qr_data"],
                })
    finally:
        pdf.close()

    return results


def _scan_ai_file(doc_path: Path, max_dpi: int = 100) -> list[dict[str, Any]]:
    """Backward-compatible alias."""
    return _scan_document(doc_path)


def process_archive_url(archive_url: str) -> dict[str, Any]:
    _configure_temp_dir()
    max_bytes = _max_archive_bytes()

    print("Archive URL: ", archive_url)
    kind_hint, name = _resolve_input_kind(archive_url)
    print("detected kind:", kind_hint, "filename:", name)

    with tempfile.TemporaryDirectory(prefix="ai_qr_", dir=str(_tmp_root())) as tmp:
        tmp_path = Path(tmp)
        download_path = tmp_path / name
        _download(archive_url, download_path, max_bytes=max_bytes)

        kind, resolved_name = _resolve_input_kind(archive_url, download_path)
        if resolved_name != name:
            resolved_path = tmp_path / resolved_name
            download_path.replace(resolved_path)
            download_path = resolved_path
            name = resolved_name

        if kind == "document":
            sku_name = Path(name).stem
            if sku_name.startswith("Order # "):
                sku_name = sku_name[8:]

            qr_codes = _scan_document(download_path)

            return {
                "archive_url": archive_url,
                "file_name": name,
                "sku_name": sku_name,
                "qr_count": len(qr_codes),
                "qr_codes": [{**qr, "file": name} for qr in qr_codes],
            }

        return _process_archive_file(download_path, archive_url, name)


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
