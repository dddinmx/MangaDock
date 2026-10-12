#!/usr/bin/env python3
"""Tests for migrate_to_cloudflare.py (stdlib unittest, no third-party deps)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import migrate_to_cloudflare as migrate  # noqa: E402


PASSWORD_HASH = "pbkdf2:sha256:260000$saltsalt$0123456789abcdef"


class TestNaturalSortKey(unittest.TestCase):
    def test_numeric_order(self):
        names = ["ch10.cbz", "ch2.cbz", "ch1.cbz"]
        ordered = sorted(names, key=migrate.natural_sort_key)
        self.assertEqual(ordered, ["ch1.cbz", "ch2.cbz", "ch10.cbz"])

    def test_unicode_chapter_titles(self):
        names = ["第10话", "第2话", "第1话"]
        ordered = sorted(names, key=migrate.natural_sort_key)
        self.assertEqual(ordered, ["第1话", "第2话", "第10话"])


class TestSqlLiteral(unittest.TestCase):
    def test_null(self):
        self.assertEqual(migrate.sql_literal(None), "NULL")

    def test_int_and_bool(self):
        self.assertEqual(migrate.sql_literal(42), "42")
        self.assertEqual(migrate.sql_literal(True), "1")
        self.assertEqual(migrate.sql_literal(False), "0")

    def test_float(self):
        self.assertEqual(migrate.sql_literal(1.5), repr(1.5))
        self.assertEqual(migrate.sql_literal(float("nan")), "NULL")
        self.assertEqual(migrate.sql_literal(float("inf")), "NULL")

    def test_text_escapes_quotes(self):
        self.assertEqual(migrate.sql_literal("O'Reilly"), "'O''Reilly'")
        self.assertEqual(migrate.sql_literal("plain"), "'plain'")

    def test_unicode_kept(self):
        self.assertEqual(migrate.sql_literal("三體"), "'三體'")

    def test_bytes_as_hex(self):
        self.assertEqual(migrate.sql_literal(b"\x00\xff"), "X'00FF'")
        self.assertEqual(migrate.sql_literal(bytearray(b"hi")), "X'6869'")


class TestIterRowsAndBatch(unittest.TestCase):
    def test_iter_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        rows = list(migrate.iter_rows(conn, "t"))
        self.assertEqual(rows, [(1, "a"), (2, "b")])
        conn.close()

    def test_batch_statements_single(self):
        stmts = migrate.batch_statements(
            "t", ["id", "name"], [(1, "a"), (2, "O'Reilly")]
        )
        self.assertEqual(len(stmts), 1)
        self.assertTrue(stmts[0].startswith('INSERT OR REPLACE INTO "t"'))
        self.assertIn("(1, 'a')", stmts[0])
        self.assertIn("(2, 'O''Reilly')", stmts[0])
        self.assertTrue(stmts[0].endswith(";"))

    def test_batch_statements_splits_at_limit(self):
        payload = "x" * 40_000
        rows = [(1, payload), (2, payload), (3, payload)]
        stmts = migrate.batch_statements("t", ["id", "body"], rows, max_bytes=90 * 1024)
        self.assertGreater(len(stmts), 1)
        for stmt in stmts:
            self.assertLessEqual(len(stmt.encode("utf-8")), 90 * 1024 + 50_000)
            # each statement is independently valid
            self.assertTrue(stmt.startswith("INSERT OR REPLACE INTO"))
            self.assertTrue(stmt.endswith(";"))
        # three rows all present across the statements
        joined = "\n".join(stmts)
        self.assertIn("(1, ", joined)
        self.assertIn("(2, ", joined)
        self.assertIn("(3, ", joined)

    def test_quoted_identifiers(self):
        stmts = migrate.batch_statements("group", ["key"], [("v",)])
        self.assertIn('"group"', stmts[0])
        self.assertIn('"key"', stmts[0])


def _build_sample_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE user (
            id INTEGER PRIMARY KEY,
            username TEXT,
            password_hash TEXT,
            role TEXT
        );
        CREATE TABLE login_log (id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE login_failure (id INTEGER PRIMARY KEY);
        CREATE TABLE background_command (id INTEGER PRIMARY KEY);
        CREATE TABLE alembic_version (version_num TEXT);
        CREATE TABLE "group" (id INTEGER, name TEXT);
        CREATE TABLE blobs (id INTEGER, data BLOB, note TEXT);
        CREATE TABLE extra_skip (id INTEGER, v TEXT);
        """
    )
    conn.execute(
        "INSERT INTO user VALUES (1, 'admin', ?, 'admin')",
        (PASSWORD_HASH,),
    )
    conn.execute("INSERT INTO login_log VALUES (1, 'admin')")
    conn.execute("INSERT INTO login_failure VALUES (1)")
    conn.execute("INSERT INTO background_command VALUES (1)")
    conn.execute("INSERT INTO alembic_version VALUES ('rev1')")
    conn.execute('INSERT INTO "group" VALUES (1, ?)', ("默认分组",))
    conn.execute("INSERT INTO blobs VALUES (1, ?, NULL)", (b"\x00\xff",))
    conn.execute("INSERT INTO extra_skip VALUES (1, 'nope')")
    conn.commit()
    conn.close()


class TestExportD1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "download_tasks.db"
        _build_sample_db(self.db)
        self.out = self.root / "out.sql"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, extra=None):
        argv = ["export-d1", "--db", str(self.db), "--out", str(self.out)]
        if extra:
            argv.extend(extra)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = migrate.main(argv)
        self.assertEqual(rc, 0)
        return self.out.read_text(encoding="utf-8"), err.getvalue()

    def test_inserts_and_skips(self):
        sql, err = self._run(["--skip-table", "extra_skip"])
        self.assertIn('INSERT OR REPLACE INTO "user"', sql)
        self.assertIn(PASSWORD_HASH, sql)
        self.assertIn("'默认分组'", sql)
        self.assertIn("X'00FF'", sql)
        self.assertIn("NULL", sql)
        self.assertIn('"group"', sql)

        self.assertNotIn("login_log", sql)
        self.assertNotIn("login_failure", sql)
        self.assertNotIn("background_command", sql)
        self.assertNotIn("alembic_version", sql)
        self.assertNotIn("extra_skip", sql)
        self.assertNotIn("sqlite_sequence", sql)

        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("BEGIN", sql)
        self.assertNotIn("COMMIT", sql)
        self.assertNotIn("PRAGMA", sql)

        self.assertIn("user:", err)
        self.assertIn("blobs:", err)

    def test_default_skip_without_extra_flag(self):
        sql, err = self._run()
        self.assertIn("extra_skip", sql)
        self.assertNotIn("login_log", sql)
        self.assertIn("1 rows", err)


def _write_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("nested/page-b.jpg", b"JPEG-B")
        zf.writestr("nested/page-a.jpg", b"JPEG-A")
        zf.writestr("ComicInfo.xml", b"<ComicInfo></ComicInfo>")
        zf.writestr("readme.txt", b"ignore me")


class TestStageR2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comic_root = self.root / "comicsrc"
        self.stage = self.root / "stage"
        self.sql_out = self.root / "lib.sql"
        self.state = self.root / "state.json"

        comic = self.comic_root / "三體"
        comic.mkdir(parents=True)
        (comic / "cover.jpg").write_bytes(b"COVER")
        _write_cbz(comic / "01.cbz")

        ch_dir = comic / "02"
        ch_dir.mkdir()
        (ch_dir / "z.png").write_bytes(b"PNG-Z")
        (ch_dir / "a.png").write_bytes(b"PNG-A")
        (ch_dir / "notes.txt").write_text("not an image", encoding="utf-8")

        (comic / "book.pdf").write_bytes(b"%PDF")
        (comic / "story.epub").write_bytes(b"EPUB")

        other = self.comic_root / "Alpha"
        other.mkdir()
        _write_cbz(other / "ch10.cbz")
        _write_cbz(other / "ch2.cbz")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, with_state=True):
        argv = [
            "stage-r2",
            "--comic-root",
            str(self.comic_root),
            "--out",
            str(self.stage),
            "--sql-out",
            str(self.sql_out),
        ]
        if with_state:
            argv.extend(["--state", str(self.state)])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = migrate.main(argv)
        self.assertEqual(rc, 0)
        return self.sql_out.read_text(encoding="utf-8"), err.getvalue()

    def test_extracts_cbz_ignores_xml_and_stages_dir_chapter(self):
        sql, err = self._run()

        page_a = self.stage / "comics" / "三體" / "01" / "001.jpg"
        page_b = self.stage / "comics" / "三體" / "01" / "002.jpg"
        self.assertEqual(page_a.read_bytes(), b"JPEG-A")
        self.assertEqual(page_b.read_bytes(), b"JPEG-B")
        self.assertFalse((self.stage / "comics" / "三體" / "01" / "ComicInfo.xml").exists())
        xml_hits = list((self.stage / "comics" / "三體").rglob("ComicInfo.xml"))
        self.assertEqual(xml_hits, [])

        self.assertEqual(
            (self.stage / "comics" / "三體" / "cover.jpg").read_bytes(), b"COVER"
        )
        self.assertEqual(
            (self.stage / "comics" / "三體" / "02" / "001.png").read_bytes(), b"PNG-A"
        )
        self.assertEqual(
            (self.stage / "comics" / "三體" / "02" / "002.png").read_bytes(), b"PNG-Z"
        )

        self.assertIn("skip PDF/EPUB", err)
        self.assertIn("book.pdf", err)
        self.assertIn("story.epub", err)
        self.assertIn("comics=", err)
        self.assertIn("chapters=", err)
        self.assertIn("pages=", err)
        self.assertIn("bytes=", err)

        self.assertIn('INSERT OR REPLACE INTO "cf_comic"', sql)
        self.assertIn("'migrated'", sql)
        self.assertIn("NULL", sql)
        self.assertIn("comics/三體/cover.jpg", sql)
        self.assertIn("comics/三體/01/001.jpg", sql)
        self.assertIn("comics/三體/01/002.jpg", sql)
        self.assertIn("comics/三體/02/001.png", sql)
        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("BEGIN", sql)

        # Alpha chapters naturally ordered: ch2 then ch10
        # chapter_order 1 = ch2, 2 = ch10
        self.assertIn("'ch2'", sql)
        self.assertIn("'ch10'", sql)
        self.assertRegex(
            sql,
            r'"cf_chapter".*\'Alpha\', 1, \'ch2\'',
        )
        self.assertRegex(
            sql,
            r'"cf_chapter".*\'Alpha\', 2, \'ch10\'',
        )

        self.assertTrue(self.state.is_file())
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(any("01.cbz" in k for k in state))

    def test_incremental_skips_unchanged_sources(self):
        self._run()
        dest = self.stage / "comics" / "三體" / "01" / "001.jpg"
        dest.write_bytes(b"STALE")

        sql, _ = self._run()
        self.assertEqual(dest.read_bytes(), b"STALE")
        self.assertIn("comics/三體/01/001.jpg", sql)

        cbz = self.comic_root / "三體" / "01.cbz"
        os.utime(cbz, (cbz.stat().st_atime, cbz.stat().st_mtime + 5))
        self._run()
        self.assertEqual(dest.read_bytes(), b"JPEG-A")


class TestPrintUploadCommands(unittest.TestCase):
    def test_prints_rclone_and_wrangler(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = migrate.main(
                [
                    "print-upload-commands",
                    "--stage",
                    "/tmp/r2-stage",
                    "--bucket",
                    "mangadock-library",
                ]
            )
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn(
            "rclone copy /tmp/r2-stage r2:mangadock-library --checksum --transfers 16 --progress",
            text,
        )
        self.assertIn("npx wrangler d1 execute DB --remote --file OUT.sql", text)
        self.assertIn("https://<ACCOUNT_ID>.r2.cloudflarestorage.com", text)
        self.assertIn("AWS_ACCESS_KEY_ID", text)
        self.assertIn("env_auth", text)
        self.assertNotRegex(text, r"sk-[A-Za-z0-9]{10,}")
        self.assertNotIn("SECRET_VALUE", text)


class TestCliHelp(unittest.TestCase):
    def test_module_docstring_mentions_subcommands(self):
        self.assertIn("export-d1", migrate.__doc__)
        self.assertIn("stage-r2", migrate.__doc__)
        self.assertIn("print-upload-commands", migrate.__doc__)


if __name__ == "__main__":
    unittest.main()
