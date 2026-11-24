from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


class DomainError(Exception):
    """A submission/correction was rejected; nothing was written."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    """Short write transaction serialised across processes and threads.

    BEGIN IMMEDIATE takes SQLite's single writer lock before any validation
    read.  Concurrent submissions/corrections therefore queue up and each one
    validates against the state committed by its predecessor.  Any
    DomainError rolls the whole operation back.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise DomainError(
                "数据库正忙，本次操作未写入，请稍后重试。", status=503
            ) from exc
        raise
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _require_card(conn: sqlite3.Connection, card_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, submitted_batch_count FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None:
        raise DomainError("作业卡不存在。", status=404)
    return row


def _require_items(conn: sqlite3.Connection, item_ids: set[int]) -> None:
    if not item_ids:
        return
    rows = conn.execute(
        "SELECT id FROM items WHERE id IN (%s)" % ",".join("?" for _ in item_ids),
        tuple(sorted(item_ids)),
    ).fetchall()
    if len(rows) != len(item_ids):
        raise DomainError("项目编号不存在。", status=404)


def _listed_items(conn: sqlite3.Connection, card_id: int) -> set[int]:
    return {
        int(r["item_id"])
        for r in conn.execute(
            "SELECT item_id FROM card_items WHERE card_id = ?", (card_id,)
        )
    }


def card_state(conn: sqlite3.Connection, card_id: int) -> dict:
    card = _require_card(conn, card_id)
    items = [
        dict(row)
        for row in conn.execute(
            "SELECT i.id, i.code, i.name, "
            "       EXISTS(SELECT 1 FROM card_items ci "
            "              WHERE ci.card_id = ? AND ci.item_id = i.id) AS on_list "
            "FROM items i ORDER BY i.code",
            (card_id,),
        )
    ]
    batches = [
        dict(row)
        for row in conn.execute(
            "SELECT id, batch_no, submitted_at FROM batches "
            "WHERE card_id = ? AND status = 'submitted' ORDER BY id DESC",
            (card_id,),
        )
    ]
    return {"card": dict(card), "items": items, "batches": batches}


def submit_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    card_id: int,
    abnormalities: dict[int, str] | None = None,
) -> int:
    """Store one submitted batch; update this card's fixed-size counters.

    The batch's inspected items are exactly the card's registered inspection
    list.  ``abnormalities`` maps abnormal item id -> original detail text.
    Every abnormal item must be on that card list; otherwise the whole batch
    is rejected and nothing is written.
    """
    abnormalities = abnormalities or {}
    batch_no = (batch_no or "").strip()
    if not batch_no:
        raise DomainError("批号不能为空。")

    with write_txn(conn):
        _require_card(conn, card_id)
        _require_items(conn, set(abnormalities))
        listed = _listed_items(conn, card_id)

        out_of_list = sorted(set(abnormalities) - listed)
        if out_of_list:
            raise DomainError(
                "异常项目不在本卡实检清单内，整批退回：项目 %s"
                % ", ".join(map(str, out_of_list)),
                status=409,
            )

        try:
            cur = conn.execute(
                "INSERT INTO batches(batch_no, card_id, status) "
                "VALUES (?, ?, 'submitted')",
                (batch_no, card_id),
            )
        except sqlite3.IntegrityError as exc:
            if "batches.batch_no" in str(exc):
                raise DomainError("批号已存在，整批退回。", status=409) from exc
            raise
        batch_id = int(cur.lastrowid)

        # This batch is inspected-and-clean for listed items and not
        # inspected for all other items.  Then move abnormal ones over.
        conn.execute(
            "UPDATE card_item_stats SET clean_count = clean_count + 1 "
            "WHERE card_id = ? AND item_id IN "
            "(SELECT item_id FROM card_items WHERE card_id = ?)",
            (card_id, card_id),
        )
        conn.execute(
            "UPDATE card_item_stats SET not_inspected_count = not_inspected_count + 1 "
            "WHERE card_id = ? AND item_id NOT IN "
            "(SELECT item_id FROM card_items WHERE card_id = ?)",
            (card_id, card_id),
        )
        for item_id in sorted(abnormalities):
            conn.execute(
                "INSERT INTO batch_abnormalities(batch_id, item_id, detail) "
                "VALUES (?, ?, ?)",
                (batch_id, item_id, abnormalities[item_id]),
            )
            conn.execute(
                "UPDATE card_item_stats SET "
                "abnormal_count = abnormal_count + 1, clean_count = clean_count - 1 "
                "WHERE card_id = ? AND item_id = ?",
                (card_id, item_id),
            )

        conn.execute(
            "UPDATE cards SET submitted_batch_count = submitted_batch_count + 1 "
            "WHERE id = ?",
            (card_id,),
        )
        return batch_id


def correct_card_items(
    conn: sqlite3.Connection, *, card_id: int, item_ids: set[int]
) -> dict[str, int]:
    """Replace one card's inspection list and re-count its received batches.

    Original abnormality details are never updated or deleted.  The recount
    uses only the card's fixed-size counter rows plus the batch count, never
    the batch or detail rows.  If an existing abnormality would become
    out-of-list, the whole correction is rejected and nothing changes.
    """
    item_ids = set(item_ids)
    with write_txn(conn):
        card = _require_card(conn, card_id)
        _require_items(conn, item_ids)
        n = int(card["submitted_batch_count"])

        old_items = _listed_items(conn, card_id)

        # One read of this card's fixed-size counter rows.  abnormal_count is
        # the authoritative count of preserved abnormal details per item.
        stats = {
            int(r["item_id"]): (
                int(r["abnormal_count"]),
                int(r["clean_count"]),
                int(r["not_inspected_count"]),
            )
            for r in conn.execute(
                "SELECT item_id, abnormal_count, clean_count, not_inspected_count "
                "FROM card_item_stats WHERE card_id = ?",
                (card_id,),
            )
        }
        out_of_list = sorted(
            item_id
            for item_id, (abnormal, _clean, _ni) in stats.items()
            if abnormal > 0 and item_id not in item_ids
        )
        if out_of_list:
            raise DomainError(
                "更正后这些项目仍有原异常明细，整次退回：项目 %s"
                % ", ".join(map(str, out_of_list)),
                status=409,
            )

        # Only touch list rows that actually change.  Retained rows are left
        # in place (a backstop trigger forbids deleting an item that still
        # has abnormal details, and re-writing them would be pointless).
        removed = sorted(old_items - item_ids)
        added = sorted(item_ids - old_items)
        conn.executemany(
            "DELETE FROM card_items WHERE card_id = ? AND item_id = ?",
            [(card_id, item_id) for item_id in removed],
        )
        conn.executemany(
            "INSERT INTO card_items(card_id, item_id) VALUES (?, ?)",
            [(card_id, item_id) for item_id in added],
        )

        # Defensive: every (card, item) pair is normally pre-seeded.  This
        # keeps the fixed row set complete if an item was ever added later.
        conn.executemany(
            "INSERT INTO card_item_stats"
            "(card_id, item_id, abnormal_count, clean_count, not_inspected_count) "
            "VALUES (?, ?, 0, 0, 0) ON CONFLICT DO NOTHING",
            [(card_id, item_id) for item_id in sorted(item_ids - set(stats))],
        )
        for item_id in item_ids - set(stats):
            stats[item_id] = (0, 0, 0)

        # Re-count every item of this card from the new list and n:
        #   listed:     abnormal unchanged (preserved details), clean = n - abnormal
        #   unlisted:   abnormal 0, clean 0, not_inspected = n
        # Writes exactly one fixed row per (card, item), never per batch.
        for item_id, (abnormal, _clean, _ni) in stats.items():
            if item_id in item_ids:
                conn.execute(
                    "UPDATE card_item_stats SET clean_count = ?, "
                    "not_inspected_count = 0 WHERE card_id = ? AND item_id = ?",
                    (n - abnormal, card_id, item_id),
                )
            else:
                conn.execute(
                    "UPDATE card_item_stats SET abnormal_count = 0, clean_count = 0, "
                    "not_inspected_count = ? WHERE card_id = ? AND item_id = ?",
                    (n, card_id, item_id),
                )

        return {
            "added": len(item_ids - old_items),
            "removed": len(old_items - item_ids),
            "listed": len(item_ids),
            "batches": n,
        }


def query_item(conn: sqlite3.Connection, item_id: int) -> dict[str, int]:
    """Batch counts for one item across all submitted batches.

    Reads exactly one row per card from the fixed-size counter table; no
    batch or abnormality row is scanned.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(abnormal_count), 0) AS abnormal, "
        "       COALESCE(SUM(clean_count), 0) AS clean, "
        "       COALESCE(SUM(not_inspected_count), 0) AS not_inspected "
        "FROM card_item_stats WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    return {
        "abnormal": int(row["abnormal"]),
        "clean": int(row["clean"]),
        "not_inspected": int(row["not_inspected"]),
    }


def list_cards(conn: sqlite3.Connection) -> list[dict]:
    cards = [
        dict(row)
        for row in conn.execute(
            "SELECT id, code, name, submitted_batch_count FROM cards ORDER BY code"
        )
    ]
    for card in cards:
        card["item_ids"] = sorted(_listed_items(conn, card["id"]))
    return cards


def list_items(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY code")]


def list_batches(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT b.id, b.batch_no, b.submitted_at, c.code AS card_code, "
            "       c.name AS card_name, "
            "       (SELECT COUNT(*) FROM batch_abnormalities ba "
            "        WHERE ba.batch_id = b.id) AS abnormal_count "
            "FROM batches b JOIN cards c ON c.id = b.card_id "
            "WHERE b.status = 'submitted' ORDER BY b.id DESC LIMIT ?",
            (limit,),
        )
    ]


def batch_detail(conn: sqlite3.Connection, batch_id: int) -> dict | None:
    batch = conn.execute(
        "SELECT b.id, b.batch_no, b.submitted_at, b.card_id, "
        "       c.code AS card_code, c.name AS card_name "
        "FROM batches b JOIN cards c ON c.id = b.card_id WHERE b.id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None
    result = dict(batch)
    result["abnormalities"] = [
        dict(row)
        for row in conn.execute(
            "SELECT i.code AS item_code, i.name AS item_name, ba.detail "
            "FROM batch_abnormalities ba JOIN items i ON i.id = ba.item_id "
            "WHERE ba.batch_id = ? ORDER BY i.code",
            (batch_id,),
        )
    ]
    return result


def audit_counters(conn: sqlite3.Connection) -> dict[str, int]:
    """Reference recount from raw batch/detail rows, for verification only.

    Deliberately scans all batches; never used by query or correction.
    Also checks two invariants: the per-card batch counter matches stored
    submitted batches, and no stored abnormality is outside its card's
    current inspection list.
    """
    expected: dict[tuple[int, int], list[int]] = {}
    card_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM cards")]
    item_ids = [int(r["id"]) for r in conn.execute("SELECT id FROM items")]
    counts = {
        int(r["id"]): int(r["submitted_batch_count"])
        for r in conn.execute("SELECT id, submitted_batch_count FROM cards")
    }
    for card_id in card_ids:
        for item_id in item_ids:
            expected[(card_id, item_id)] = [0, 0, counts[card_id]]
    for row in conn.execute(
        "SELECT b.card_id, ci.item_id "
        "FROM batches b JOIN card_items ci ON ci.card_id = b.card_id "
        "WHERE b.status = 'submitted'"
    ):
        key = (int(row["card_id"]), int(row["item_id"]))
        if key in expected:
            expected[key][2] -= 1
            expected[key][1] += 1
    for row in conn.execute(
        "SELECT b.card_id, ba.item_id FROM batches b "
        "JOIN batch_abnormalities ba ON ba.batch_id = b.id "
        "WHERE b.status = 'submitted'"
    ):
        key = (int(row["card_id"]), int(row["item_id"]))
        if key in expected:
            expected[key][1] -= 1
            expected[key][0] += 1

    actual = {
        (int(r["card_id"]), int(r["item_id"])): [
            int(r["abnormal_count"]),
            int(r["clean_count"]),
            int(r["not_inspected_count"]),
        ]
        for r in conn.execute(
            "SELECT card_id, item_id, abnormal_count, clean_count, "
            "not_inspected_count FROM card_item_stats"
        )
    }
    mismatches = sum(
        1
        for key in set(expected) | set(actual)
        if expected.get(key) != actual.get(key)
    )

    batch_count_mismatches = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT c.id, c.submitted_batch_count, "
        "         (SELECT COUNT(*) FROM batches b "
        "          WHERE b.card_id = c.id AND b.status = 'submitted') AS actual "
        "  FROM cards c"
        ") WHERE submitted_batch_count <> actual"
    ).fetchone()[0]

    out_of_list_details = conn.execute(
        "SELECT COUNT(*) FROM batch_abnormalities ba "
        "JOIN batches b ON b.id = ba.batch_id "
        "WHERE b.status = 'submitted' AND NOT EXISTS ("
        "  SELECT 1 FROM card_items ci "
        "  WHERE ci.card_id = b.card_id AND ci.item_id = ba.item_id)"
    ).fetchone()[0]

    return {
        "mismatches": mismatches,
        "batch_count_mismatches": int(batch_count_mismatches),
        "out_of_list_details": int(out_of_list_details),
        "abnormal": sum(v[0] for v in expected.values()),
        "clean": sum(v[1] for v in expected.values()),
        "not_inspected": sum(v[2] for v in expected.values()),
    }
