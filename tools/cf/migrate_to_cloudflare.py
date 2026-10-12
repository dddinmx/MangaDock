#!/usr/bin/env python3
"""Migrate a MangaDock install to Cloudflare D1 + R2.

Reads the local SQLite DB and comic library, and writes D1-compatible INSERT
SQL plus a staged directory of page images for rclone upload to R2.

Usage:
  python3 tools/cf/migrate_to_cloudflare.py export-d1 \\
      --db instance/download_tasks.db --out d1_data.sql
  python3 tools/cf/migrate_to_cloudflare.py export-d1 \\
      --db instance/download_tasks.db --out d1_data.sql --skip-table extra_table
  python3 tools/cf/migrate_to_cloudflare.py stage-r2 \\
      --comic-root data/comic --out /tmp/r2-stage --sql-out lib.sql
  python3 tools/cf/migrate_to_cloudflare.py stage-r2 \\
      --comic-root data/comic --out /tmp/r2-stage --sql-out lib.sql \\
      --state /tmp/r2-state.json
  python3 tools/cf/migrate_to_cloudflare.py print-upload-commands \\
      --stage /tmp/r2-stage --bucket mangadock-library

Subcommands:
  export-d1   Dump every user table as INSERT OR REPLACE (no CREATE TABLE,
              BEGIN, COMMIT, or PRAGMA). Schema comes from D1 migrations.
              password_hash values are copied unchanged (werkzeug
              pbkdf2:sha256:260000$salt$hex).
  stage-r2    Unpack each comic's .cbz / image-chapter folders into
              STAGE_DIR/comics/<name>/<chapter>/<NNN><ext> and emit cf_comic /
              cf_chapter INSERT SQL. Optional --state skips unchanged sources.
  print-upload-commands
              Print rclone + wrangler commands (does not run them).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

MAX_STATEMENT_BYTES = 90 * 1024
DEFAULT_SKIP_TABLES = ("login_log", "login_failure", "background_command")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
COVER_NAMES = ("cover.jpg", "cover.png", "cover.webp")
SKIP_ARCHIVE_EXTENSIONS = {".pdf", ".epub"}

# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def natural_sort_key(text: str):
    """Numeric-aware sort key so 'ch2' sorts before 'ch10'."""
    parts = re.split(r"(\d+)", str(text))
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part.casefold()))
    return key


def sql_ident(name: str) -> str:
    """Quote an SQL identifier with double quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(value) -> str:
    """Render a Python value as an SQL literal (NULL / int / float / text / X'hex')."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "NULL"
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex().upper() + "'"
    if isinstance(value, datetime):
        text = value.isoformat()
        return "'" + text.replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def iter_rows(conn: sqlite3.Connection, table: str) -> Iterator[tuple]:
    """Yield each row of ``table`` as a tuple (SELECT * column order)."""
    cur = conn.execute(f"SELECT * FROM {sql_ident(table)}")
    for row in cur:
        yield tuple(row)


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"SELECT * FROM {sql_ident(table)} LIMIT 0")
    return [d[0] for d in cur.description]


def batch_statements(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    max_bytes: int = MAX_STATEMENT_BYTES,
) -> list[str]:
    """Build INSERT OR REPLACE statements, each at most ``max_bytes`` UTF-8 bytes.

    A single row that itself exceeds the limit is still emitted as its own
    statement (a row cannot be split).
    """
    col_sql = ", ".join(sql_ident(c) for c in columns)
    prefix = f"INSERT OR REPLACE INTO {sql_ident(table)} ({col_sql}) VALUES "
    suffix = ";"
    prefix_n = len(prefix.encode("utf-8"))
    suffix_n = len(suffix.encode("utf-8"))

    statements: list[str] = []
    parts: list[str] = []
    size = prefix_n + suffix_n

    def flush() -> None:
        nonlocal parts, size
        if not parts:
            return
        statements.append(prefix + ", ".join(parts) + suffix)
        parts = []
        size = prefix_n + suffix_n

    for row in rows:
        tuple_sql = "(" + ", ".join(sql_literal(v) for v in row) + ")"
        n = len(tuple_sql.encode("utf-8"))
        sep = 2 if parts else 0  # ", "
        if parts and size + sep + n > max_bytes:
            flush()
            sep = 0
        parts.append(tuple_sql)
        size += sep + n

    flush()
    return statements


def list_user_tables(conn: sqlite3.Connection, skip: set[str]) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    tables = []
    for (name,) in rows:
        if name.startswith("sqlite_"):
            continue
        if name.startswith("alembic"):
            continue
        if name in skip:
            continue
        tables.append(name)
    return tables


# ---------------------------------------------------------------------------
# export-d1
# ---------------------------------------------------------------------------


def cmd_export_d1(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    out_path = Path(args.out)
    skip = set(DEFAULT_SKIP_TABLES)
    skip.update(args.skip_table or [])

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = list_user_tables(conn, skip)
        statements: list[str] = []
        for table in tables:
            columns = table_columns(conn, table)
            rows = list(iter_rows(conn, table))
            print(f"export-d1: {table}: {len(rows)} rows", file=sys.stderr)
            if not rows:
                continue
            statements.extend(batch_statements(table, columns, rows))
    finally:
        conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if statements:
        text = "\n".join(statements) + "\n"
    out_path.write_text(text, encoding="utf-8")
    print(
        f"export-d1: wrote {len(statements)} statement(s) to {out_path}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# stage-r2
# ---------------------------------------------------------------------------


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def r2_key(*parts: str) -> str:
    """Join path parts with forward slashes; keep unicode as-is."""
    cleaned = []
    for part in parts:
        piece = str(part).replace("\\", "/").strip("/")
        if piece:
            cleaned.append(piece)
    return "/".join(cleaned)


def is_image_name(name: str) -> bool:
    if not name or name.endswith("/"):
        return False
    bits = name.replace("\\", "/").split("/")
    if any(part == "__MACOSX" or part.startswith("._") for part in bits):
        return False
    base = bits[-1]
    if base.startswith("."):
        return False
    ext = os.path.splitext(base)[1].lower()
    return ext in IMAGE_EXTENSIONS


def load_state(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"state file must be a JSON object: {path}")
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def file_fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime}


def is_unchanged(path: Path, state: dict) -> bool:
    key = str(path.resolve())
    prev = state.get(key)
    if not isinstance(prev, dict):
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    try:
        prev_size = int(prev.get("size"))
        prev_mtime = float(prev.get("mtime"))
    except (TypeError, ValueError):
        return False
    return prev_size == st.st_size and abs(prev_mtime - st.st_mtime) < 1e-6


def record_state(state: dict, path: Path) -> None:
    try:
        state[str(path.resolve())] = file_fingerprint(path)
    except OSError:
        pass


def list_cbz_image_entries(zf: zipfile.ZipFile) -> list[str]:
    entries = [name for name in zf.namelist() if is_image_name(name)]
    entries.sort(key=natural_sort_key)
    return entries


def list_dir_images(directory: Path) -> list[Path]:
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and is_image_name(p.name)
    ]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return files


def copy_bytes(src_fh, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(src_fh, out)
        return out.tell()


def copy_file(src: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.stat().st_size


def staged_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def find_cover(comic_dir: Path) -> Path | None:
    available = {p.name.lower(): p for p in comic_dir.iterdir() if p.is_file()}
    for name in COVER_NAMES:
        hit = available.get(name)
        if hit is not None:
            return hit
    return None


def iter_comic_dirs(comic_root: Path) -> list[Path]:
    dirs = [
        p
        for p in comic_root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "__MACOSX"
    ]
    dirs.sort(key=lambda p: natural_sort_key(p.name))
    return dirs


def collect_chapters(comic_dir: Path) -> list[dict]:
    chapters = []
    for entry in comic_dir.iterdir():
        if entry.name.startswith(".") or entry.name == "__MACOSX":
            continue
        if entry.is_file():
            ext = entry.suffix.lower()
            if ext == ".cbz":
                chapters.append(
                    {
                        "base": entry.stem,
                        "kind": "cbz",
                        "path": entry,
                    }
                )
            elif ext in SKIP_ARCHIVE_EXTENSIONS:
                print(
                    f"stage-r2: skip PDF/EPUB {entry}",
                    file=sys.stderr,
                )
        elif entry.is_dir():
            if list_dir_images(entry):
                chapters.append(
                    {
                        "base": entry.name,
                        "kind": "dir",
                        "path": entry,
                    }
                )
    chapters.sort(key=lambda c: natural_sort_key(c["base"]))
    return chapters


def stage_cbz_chapter(
    cbz_path: Path,
    dest_dir: Path,
    state: dict,
) -> tuple[list[str], int]:
    """Extract images from a .cbz into dest_dir as NNN<ext>.

    Returns (dest filenames in order, total bytes of dest files).
    """
    with zipfile.ZipFile(cbz_path) as zf:
        entries = list_cbz_image_entries(zf)
        skip_write = is_unchanged(cbz_path, state)
        names: list[str] = []
        total = 0
        for index, entry in enumerate(entries, start=1):
            ext = os.path.splitext(entry)[1]
            dest_name = f"{index:03d}{ext}"
            dest = dest_dir / dest_name
            if skip_write and dest.is_file():
                total += staged_file_size(dest)
            else:
                with zf.open(entry) as src:
                    total += copy_bytes(src, dest)
            names.append(dest_name)
    record_state(state, cbz_path)
    return names, total


def stage_dir_chapter(
    chapter_dir: Path,
    dest_dir: Path,
    state: dict,
) -> tuple[list[str], int]:
    images = list_dir_images(chapter_dir)
    names: list[str] = []
    total = 0
    for index, src in enumerate(images, start=1):
        dest_name = f"{index:03d}{src.suffix}"
        dest = dest_dir / dest_name
        if is_unchanged(src, state) and dest.is_file():
            total += staged_file_size(dest)
        else:
            total += copy_file(src, dest)
        record_state(state, src)
        names.append(dest_name)
    return names, total


def cmd_stage_r2(args: argparse.Namespace) -> int:
    comic_root = Path(args.comic_root)
    stage_dir = Path(args.out)
    sql_out = Path(args.sql_out)
    state_path = Path(args.state) if args.state else None

    if not comic_root.is_dir():
        print(f"stage-r2: comic root is not a directory: {comic_root}", file=sys.stderr)
        return 1

    state = load_state(state_path)
    created_at = iso_utc_now()
    stage_dir.mkdir(parents=True, exist_ok=True)

    comic_rows: list[tuple] = []
    chapter_rows: list[tuple] = []
    n_comics = 0
    n_chapters = 0
    n_pages = 0
    n_bytes = 0

    for comic_dir in iter_comic_dirs(comic_root):
        n_comics += 1
        comic_name = comic_dir.name
        cover = find_cover(comic_dir)
        cover_key = None
        if cover is not None:
            dest_cover = stage_dir / "comics" / comic_name / f"cover{cover.suffix}"
            if is_unchanged(cover, state) and dest_cover.is_file():
                n_bytes += staged_file_size(dest_cover)
            else:
                n_bytes += copy_file(cover, dest_cover)
            record_state(state, cover)
            cover_key = r2_key("comics", comic_name, f"cover{cover.suffix}")

        comic_rows.append(
            (comic_name, comic_name, "migrated", None, cover_key, created_at)
        )

        for order, chapter in enumerate(collect_chapters(comic_dir), start=1):
            n_chapters += 1
            chapter_base = chapter["base"]
            dest_chapter = stage_dir / "comics" / comic_name / chapter_base
            if chapter["kind"] == "cbz":
                dest_names, nbytes = stage_cbz_chapter(
                    chapter["path"], dest_chapter, state
                )
            else:
                dest_names, nbytes = stage_dir_chapter(
                    chapter["path"], dest_chapter, state
                )
            n_bytes += nbytes
            n_pages += len(dest_names)
            page_keys = [
                r2_key("comics", comic_name, chapter_base, name) for name in dest_names
            ]
            chapter_rows.append(
                (
                    comic_name,
                    order,
                    chapter_base,
                    chapter_base,
                    len(dest_names),
                    json.dumps(page_keys, ensure_ascii=False),
                )
            )

    statements: list[str] = []
    statements.extend(
        batch_statements(
            "cf_comic",
            ["name", "title", "provider", "source_url", "cover_key", "created_at"],
            comic_rows,
        )
    )
    statements.extend(
        batch_statements(
            "cf_chapter",
            [
                "comic_name",
                "chapter_order",
                "filename_base",
                "title",
                "page_count",
                "page_keys",
            ],
            chapter_rows,
        )
    )

    sql_out.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if statements:
        text = "\n".join(statements) + "\n"
    sql_out.write_text(text, encoding="utf-8")

    if state_path is not None:
        save_state(state_path, state)

    print(
        f"stage-r2: comics={n_comics} chapters={n_chapters} "
        f"pages={n_pages} bytes={n_bytes}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# print-upload-commands
# ---------------------------------------------------------------------------


def cmd_print_upload_commands(args: argparse.Namespace) -> int:
    stage = os.path.abspath(args.stage)
    bucket = args.bucket
    rclone_cmd = shlex.join(
        [
            "rclone",
            "copy",
            stage,
            f"r2:{bucket}",
            "--checksum",
            "--transfers",
            "16",
            "--progress",
        ]
    )
    print(
        """# Upload staged pages to Cloudflare R2, then apply D1 SQL.
#
# rclone remote "r2" talks to R2's S3-compatible API. Configure it with
# environment variables so no secrets are embedded in the command or in
# rclone.conf:
#
#   export AWS_ACCESS_KEY_ID='<R2 access key id>'
#   export AWS_SECRET_ACCESS_KEY='<R2 secret access key>'
#   export R2_ACCOUNT_ID='<ACCOUNT_ID>'
#
#   rclone config create r2 s3 \\
#     provider Cloudflare \\
#     env_auth true \\
#     region auto \\
#     endpoint "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
#
# Endpoint form: https://<ACCOUNT_ID>.r2.cloudflarestorage.com
#
# Copy the staged tree (STAGE_DIR contents become object keys, e.g.
# comics/<name>/<chapter>/001.jpg):
"""
    )
    print(rclone_cmd)
    print(
        """
# Load the SQL produced by export-d1 / stage-r2 into D1. Schema must already
# exist from wrangler migrations. Repeat for each SQL file (OUT.sql then
# LIB.sql). Binding name is the wrangler d1_databases.binding (often "DB"):
#
#   npx wrangler d1 execute DB --remote --file OUT.sql
"""
    )
    print("npx wrangler d1 execute DB --remote --file OUT.sql")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_to_cloudflare.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser(
        "export-d1",
        help="Dump SQLite tables as D1 INSERT OR REPLACE statements",
    )
    p_export.add_argument("--db", required=True, help="Path to download_tasks.db")
    p_export.add_argument("--out", required=True, help="Output .sql path")
    p_export.add_argument(
        "--skip-table",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Additional table to skip (repeatable). Always skipped: "
            + ", ".join(DEFAULT_SKIP_TABLES)
            + ", sqlite_*, alembic*"
        ),
    )
    p_export.set_defaults(func=cmd_export_d1)

    p_stage = sub.add_parser(
        "stage-r2",
        help="Stage comic pages for R2 and emit cf_comic/cf_chapter SQL",
    )
    p_stage.add_argument("--comic-root", required=True, help="COMIC_ROOT directory")
    p_stage.add_argument("--out", required=True, dest="out", help="Staging directory")
    p_stage.add_argument("--sql-out", required=True, help="Library index .sql path")
    p_stage.add_argument(
        "--state",
        default=None,
        help="JSON fingerprint file for incremental re-runs",
    )
    p_stage.set_defaults(func=cmd_stage_r2)

    p_print = sub.add_parser(
        "print-upload-commands",
        help="Print rclone / wrangler commands (does not run them)",
    )
    p_print.add_argument("--stage", required=True, help="Staging directory")
    p_print.add_argument("--bucket", required=True, help="R2 bucket name")
    p_print.set_defaults(func=cmd_print_upload_commands)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
