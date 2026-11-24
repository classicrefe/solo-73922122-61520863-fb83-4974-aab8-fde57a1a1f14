from __future__ import annotations

import sqlite3


CARDS = [
    ("C001", "验布作业卡 A"),
    ("C002", "验布作业卡 B"),
    ("C003", "验布作业卡 C"),
    ("C004", "验布作业卡 D"),
]

ITEMS = [
    ("I01", "幅宽"),
    ("I02", "克重"),
    ("I03", "纬斜"),
    ("I04", "色差"),
    ("I05", "污点"),
    ("I06", "破洞"),
]


def seed_fixed_dictionary(conn: sqlite3.Connection) -> None:
    """Cards and items are a fixed dictionary; insert idempotently.

    A counter row exists for every (card, item) pair from the start and is
    never added or removed by batch activity, so the table size is fixed at
    cards x items.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for code, name in ITEMS:
            conn.execute(
                "INSERT INTO items(code, name) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
                (code, name),
            )
        for code, name in CARDS:
            conn.execute(
                "INSERT INTO cards(code, name) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
                (code, name),
            )
        conn.execute(
            "INSERT OR IGNORE INTO card_item_stats"
            "(card_id, item_id, abnormal_count, clean_count, not_inspected_count) "
            "SELECT c.id, i.id, 0, 0, 0 FROM cards c CROSS JOIN items i"
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
