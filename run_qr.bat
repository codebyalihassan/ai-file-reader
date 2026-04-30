@echo off
set PATH=C:\Program Files\7-Zip;C:\Program Files (x86)\7-Zip;%PATH%
python "%~dp0qr_from_storage_archive.py" --json
