#!/usr/bin/env python3
"""Read-only, resumable inventory of a legacy library. Does not upload or modify originals."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path, required=True)
parser.add_argument('--novels', type=Path, required=True)
parser.add_argument('--comics', type=Path)
parser.add_argument('--covers', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
os.chmod(args.output, 0o700)
# SQLite backup handles WAL correctly; copying only the main .db file does not.
snapshot = args.output / 'legacy.sqlite3'
if snapshot.exists():
    parser.error('output snapshot exists; select a new output directory to preserve it')
source = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
destination = sqlite3.connect(snapshot)
source.backup(destination)
destination.close()
os.chmod(snapshot, 0o600)
counts = {name: source.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34)*2)}"').fetchone()[0]
          for (name,) in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
source.close()
objects, total = [], 0
for kind, root in [('novels', args.novels), ('comics', args.comics), ('covers', args.covers)]:
    if root is None:
        continue
    root = root.resolve()
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part.startswith('.') for part in relative.parts):
            continue
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        size = path.stat().st_size
        sha = digest.hexdigest()
        novel = kind == 'novels' and path.suffix.lower() == '.epub'
        # Content-addressed keys avoid unsafe paths and support resumable uploads.
        key = f'{kind}/legacy/{sha}{path.suffix.lower()}'
        objects.append({'kind': kind, 'relative_path': str(relative), 'r2_key': key,
                        'bytes': size, 'sha256': sha, 'legacy_novel_id': path.stem if novel else None,
                        'legacy_progress_key': f'novel:{path.stem}' if novel else None,
                        'phase1_reader_compatible': novel and size <= 33554432})
        total += size
manifest = {'schema_version': 1, 'total_bytes': total, 'legacy_table_counts': counts, 'objects': objects,
            'warning': 'Private backup contains account/settings data. Do not commit or expose it. Import requires preserving group ACLs and resetting incompatible password hashes.'}
manifest_path = args.output / 'manifest.json'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
os.chmod(manifest_path, 0o600)
print(json.dumps({'objects': len(objects), 'bytes': total, 'manifest': str(manifest_path)}, ensure_ascii=False))
