from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import click
from flask import current_app, g


SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_CONTENTION_MARKERS = ("locked", "busy")


def is_contention(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONTENTION_MARKERS)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            timeout=current_app.config["SQLITE_BUSY_TIMEOUT_MS"] / 1000,
            isolation_level=None,  # explicit BEGIN/COMMIT for short, defined txns
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            f"PRAGMA busy_timeout = {int(current_app.config['SQLITE_BUSY_TIMEOUT_MS'])}"
        )
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    """Create the schema.  Every statement is idempotent."""
    conn = get_db()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def initialise() -> None:
    """Create schema and fixed dictionary, tolerating multi-worker startup.

    Gunicorn workers may start at the same time against one SQLite file.
    Contention is retried; SQLite has a single writer, so the retries
    serialise cleanly.  All DDL/seed statements are idempotent.
    """
    from . import seed

    delay = 0.05
    last_error: sqlite3.Error | None = None
    for _attempt in range(40):
        try:
            init_db()
            seed.seed_fixed_dictionary(get_db())
            return
        except sqlite3.OperationalError as exc:
            if not is_contention(exc):
                raise
            last_error = exc
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)
    raise RuntimeError(f"数据库初始化在超时前未能完成：{last_error}")


@click.command("init-db")
def init_db_command() -> None:
    """Create tables and seed the fixed card/item dictionary."""
    initialise()
    click.echo("Initialised database.")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
