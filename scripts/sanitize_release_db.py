#!/usr/bin/env python3
"""Remove library content records while retaining users and app settings."""
import sqlite3
import sys
from pathlib import Path


CONTENT_TABLES = (
    "admin_hidden_library_item",
    "background_command",
    "comic_group_membership",
    "comic_identity",
    "comic_update_check",
    "download_task",
    "reading_progress",
    "reading_session_state",
    "reading_time",
)


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/instance/download_tasks.db")
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in CONTENT_TABLES:
            if table in tables:
                connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
        connection.execute("VACUUM")


if __name__ == "__main__":
    main()
