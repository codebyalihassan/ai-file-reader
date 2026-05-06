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
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import cv2
import pypdfium2 as pdfium  # replaced fitz due to DLL block policy
import numpy as np
import requests
import zxingcpp


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


def _download(url: str, dest: Path, timeout: int = 60) -> None:
    """Download file with timeout (reduced from 120s to 60s)."""
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):  # Larger chunks
                if chunk:
                    f.write(chunk)


def _extract_archive(archive: Path, out_dir: Path) -> None:
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(out_dir)
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    seven = _seven_zip()
    if seven:
        cmd = [seven, "x", str(archive), f"-o{out_dir}", "-y", "-bb0"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        # If 7z fails, try tar as fallback
    
    # Fallback to system tar
    tar = shutil.which("tar")
    if tar:
        # tar -xf archive -C out_dir
        # Note: tar requires the directory to exist
        cmd = [tar, "-xf", str(archive), "-C", str(out_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return
        
    raise RuntimeError(
        f"Failed to extract {archive.name}. "
        "The bundled extraction tool (7-Zip) or system 'tar' could not handle this format. "
        "Ensure the file is a valid archive and not password-protected."
    )


def _iter_ai_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".ai":
            files.append(p)
    return sorted(files, key=lambda x: str(x).lower())


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
    print("Archive URL: ", archive_url)
    parsed = urlparse(archive_url)
    name = Path(unquote(parsed.path)).name or "download.bin"
    
    # Check if it's a direct PDF/AI file
    is_direct_pdf = re.search(r'\.(pdf|ai)$', name, re.I) is not None

    print("is_direct_pdf: ", is_direct_pdf)
    
    if is_direct_pdf:
        # Direct PDF/AI file — download and scan directly
        with tempfile.TemporaryDirectory(prefix="ai_qr_") as tmp:
            tmp_path = Path(tmp)
            file_path = tmp_path / name
            
            _download(archive_url, file_path)
            
            # Extract SKU name from filename
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
    
    # Archive file (RAR/ZIP) — extract and scan all .ai files
    if not re.search(r"\.(zip|rar|7z)$", name, re.I):
        name = "download.rar"

    with tempfile.TemporaryDirectory(prefix="ai_qr_") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / name
        extract_dir = tmp_path / "extracted"

        _download(archive_url, archive_path)
        _extract_archive(archive_path, extract_dir)

        ai_files = _iter_ai_files(extract_dir)
        
        all_qr_codes: list[dict[str, Any]] = []
        total_qr_count = 0

        for f in ai_files:
            rel = str(f.relative_to(extract_dir)).replace("\\", "/")
            sku_name = f.stem
            if sku_name.startswith("Order # "):
                sku_name = sku_name[8:]
            
            qr_codes = _scan_ai_file(f)
            total_qr_count += len(qr_codes)
            for qr in qr_codes:
                all_qr_codes.append({
                    "page": qr["page"],
                    "qr_data": qr["qr_data"],
                    "file": rel
                })

        # Archive SKU name from archive filename
        archive_sku = archive_path.stem
        if archive_sku.startswith("Order # "):
            archive_sku = archive_sku[8:]

        return {
            "archive_url": archive_url,
            "file_name": name,
            "sku_name": archive_sku,
            "qr_count": total_qr_count,
            "qr_codes": all_qr_codes,
        }


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
