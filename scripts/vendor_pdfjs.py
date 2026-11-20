#!/usr/bin/env python3
"""Prepare the pinned official PDF.js distribution for review and vendoring."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import sys
import urllib.request
import zipfile

VERSION = "6.5.438"
SIZE = 6911254
SHA256 = "55138aa33b5f1f6f68809895338b947f395f344eb6381d424b2420d6daa6a844"
URL = f"https://github.com/mozilla/pdf.js/releases/download/v{VERSION}/pdfjs-{VERSION}-legacy-dist.zip"
REQUIRED = {"LICENSE", "build/pdf.mjs", "build/pdf.worker.mjs", "web/viewer.html", "web/viewer.mjs", "web/viewer.css"}

def extract(data, destination):
    if len(data) != SIZE or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("PDF.js archive integrity mismatch")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set()
        total = 0
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename or not path.parts:
                raise ValueError("Unsafe archive path")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive symlink")
            if item.filename in names:
                raise ValueError("Duplicate archive entry")
            names.add(item.filename)
            total += item.file_size
        if total > 100 * 1024 * 1024 or not REQUIRED <= names:
            raise ValueError("Unexpected PDF.js distribution")
        destination.mkdir(parents=True, exist_ok=False)
        archive.extractall(destination)

def main():
    destination = Path(sys.argv[1])
    if destination.exists():
        raise ValueError("Destination already exists")
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(SIZE + 1)
    extract(data, destination)
    print(f"Verified PDF.js {VERSION}: {SHA256}")

if __name__ == "__main__":
    main()
