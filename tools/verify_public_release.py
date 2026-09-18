#!/usr/bin/env python3
"""Fail when private Fanqie implementation details enter a public tree."""
from __future__ import annotations

import sys
from pathlib import Path


SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "instance",
    "comic",
    "小说",
    "node_modules",
}
PRIVATE_MODULE_STEM = "fanqie_" + "download"
FORBIDDEN_BYTES = (
    b"SHARED" + b"_KEY",
    b"SIGN" + b"_KEY",
    b"crypt/" + b"registerkey",
    b"def " + b"sign_request(",
    b"def " + b"decrypt_comic_image(",
    b"api5-" + b"sinfonlinec",
    b"reading." + b"snssdk.com",
    b"mangadock.vendor" + b" import fanqie_download",
    b"fanqie_download" + b".",
)
TEXT_SUFFIXES = {".py", ".txt", ".md", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings = []
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        if path.name == f"{PRIVATE_MODULE_STEM}.py" or (
            path.name.startswith(f"{PRIVATE_MODULE_STEM}.")
            and path.suffix.lower() in {".pyc", ".pyo"}
        ):
            findings.append(f"forbidden file: {relative}")
            continue
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2 * 1024 * 1024:
            continue
        data = path.read_bytes()
        for pattern in FORBIDDEN_BYTES:
            if pattern in data:
                findings.append(f"forbidden implementation marker in {relative}")
                break
    if findings:
        print("Public release verification failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"Public release verification passed: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
