from __future__ import annotations

import sqlite3
import threading

import pytest

from app import create_app
from app import service


@pytest.fixture()
def app(tmp_path):
    db_path = str(tmp_path / "test.sqlite3")
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "SECRET_KEY": "test",
            "SQLITE_BUSY_TIMEOUT_MS": 300,
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def connect(app) -> sqlite3.Connection:
    conn = sqlite3.connect(
        app.config["DATABASE"],
        timeout=20,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def item_id(conn, code: str) -> int:
    return int(conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()[0])


def card_id(conn, code: str) -> int:
    return int(conn.execute("SELECT id FROM cards WHERE code = ?", (code,)).fetchone()[0])


def set_list(conn, card: int, codes: list[str]) -> dict[str, int]:
    ids = {item_id(conn, code) for code in codes}
    return service.correct_card_items(conn, card_id=card, item_ids=ids)


def submit(conn, batch_no: str, card: int, abnormalities: dict[str, str] | None = None):
    ids = {
        item_id(conn, code): detail
        for code, detail in (abnormalities or {}).items()
    }
    return service.submit_batch(
        conn, batch_no=batch_no, card_id=card, abnormalities=ids
    )


def counts(conn, code: str) -> dict[str, int]:
    return service.query_item(conn, item_id(conn, code))


def test_card_list_must_exist_before_abnormal_submission(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    with pytest.raises(service.DomainError):
        submit(conn, "B-1", card, {"I01": "幅宽不足"})
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert service.query_item(conn, item_id(conn, "I01")) == {
        "abnormal": 0,
        "clean": 0,
        "not_inspected": 0,
    }


def test_counts_are_disjoint_and_sum_to_submitted_batches(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01", "I02"])

    submit(conn, "B-1", card)  # both clean
    submit(conn, "B-2", card, {"I01": "幅宽超差"})  # I01 abnormal
    submit(conn, "B-3", card, {"I01": "污渍", "I02": "克重偏轻"})

    total = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    for code in ["I01", "I02", "I03"]:
        row = counts(conn, code)
        assert sum(row.values()) == total
    assert counts(conn, "I01") == {"abnormal": 2, "clean": 1, "not_inspected": 0}
    assert counts(conn, "I02") == {"abnormal": 1, "clean": 2, "not_inspected": 0}
    assert counts(conn, "I03") == {"abnormal": 0, "clean": 0, "not_inspected": 3}
    assert service.audit_counters(conn)["mismatches"] == 0
    assert service.audit_counters(conn)["batch_count_mismatches"] == 0
    assert service.audit_counters(conn)["out_of_list_details"] == 0


def test_correction_preserves_detail_and_recounts_received_batches(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01", "I02"])
    submit(conn, "B-1", card, {"I01": "原异常明细 A"})
    submit(conn, "B-2", card)
    submit(conn, "B-3", card, {"I01": "原异常明细 B"})
    before = {code: counts(conn, code) for code in ["I01", "I02", "I03"]}
    details_before = [
        tuple(row)
        for row in conn.execute(
            "SELECT batch_id, item_id, detail FROM batch_abnormalities ORDER BY batch_id"
        )
    ]

    # Keep I01 (has abnormalities), drop I02, add I03.
    result = set_list(conn, card, ["I01", "I03"])
    assert result == {"added": 1, "removed": 1, "listed": 2, "batches": 3}

    # Original abnormal detail rows are byte-for-byte untouched.
    details_after = [
        tuple(row)
        for row in conn.execute(
            "SELECT batch_id, item_id, detail FROM batch_abnormalities ORDER BY batch_id"
        )
    ]
    assert details_after == details_before

    # New list: I01 retains its 2 abnormalities and 1 clean; I02 becomes
    # not-inspected for all received batches; I03 is clean for all of them.
    assert counts(conn, "I01") == {"abnormal": 2, "clean": 1, "not_inspected": 0}
    assert counts(conn, "I02") == {"abnormal": 0, "clean": 0, "not_inspected": 3}
    assert counts(conn, "I03") == {"abnormal": 0, "clean": 3, "not_inspected": 0}
    # Before correction: B-1 and B-3 had I01 abnormal; I02 was on the card
    # list and clean in all three batches; I03 was not on the list at all.
    assert before["I02"] == {"abnormal": 0, "clean": 3, "not_inspected": 0}
    assert before["I03"] == {"abnormal": 0, "clean": 0, "not_inspected": 3}
    assert service.audit_counters(conn)["mismatches"] == 0
    assert service.audit_counters(conn)["batch_count_mismatches"] == 0
    assert service.audit_counters(conn)["out_of_list_details"] == 0


def test_correction_that_would_drop_abnormal_item_is_rejected_whole(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01", "I02"])
    submit(conn, "B-1", card, {"I01": "幅差"})
    submit(conn, "B-2", card)

    snapshot = {
        "stats": [
            tuple(r)
            for r in conn.execute(
                "SELECT * FROM card_item_stats WHERE card_id = ? ORDER BY item_id",
                (card,),
            )
        ],
        "items": [
            tuple(r)
            for r in conn.execute(
                "SELECT item_id FROM card_items WHERE card_id = ? ORDER BY item_id",
                (card,),
            )
        ],
        "batches": conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
        "details": conn.execute(
            "SELECT COUNT(*) FROM batch_abnormalities"
        ).fetchone()[0],
    }
    with pytest.raises(service.DomainError) as excinfo:
        set_list(conn, card, ["I02", "I03"])
    assert excinfo.value.status == 409

    assert snapshot["items"] == [
        tuple(r)
        for r in conn.execute(
            "SELECT item_id FROM card_items WHERE card_id = ? ORDER BY item_id",
            (card,),
        )
    ]
    assert snapshot["stats"] == [
        tuple(r)
        for r in conn.execute(
            "SELECT * FROM card_item_stats WHERE card_id = ? ORDER BY item_id",
            (card,),
        )
    ]
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == snapshot["batches"]
    assert (
        conn.execute("SELECT COUNT(*) FROM batch_abnormalities").fetchone()[0]
        == snapshot["details"]
    )
    assert counts(conn, "I01") == {"abnormal": 1, "clean": 1, "not_inspected": 0}


def test_submission_with_out_of_list_abnormality_is_rejected_whole(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01"])
    with pytest.raises(service.DomainError) as excinfo:
        submit(conn, "B-BAD", card, {"I02": "克重异常"})
    assert excinfo.value.status == 409
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM batch_abnormalities").fetchone()[0] == 0
    assert counts(conn, "I01") == {"abnormal": 0, "clean": 0, "not_inspected": 0}
    assert service.audit_counters(conn)["mismatches"] == 0
    assert service.audit_counters(conn)["batch_count_mismatches"] == 0
    assert service.audit_counters(conn)["out_of_list_details"] == 0


def test_duplicate_batch_number_rejected_without_counter_change(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01"])
    submit(conn, "SAME", card)
    before = counts(conn, "I01")
    with pytest.raises(service.DomainError) as excinfo:
        submit(conn, "SAME", card, {"I01": "x"})
    assert excinfo.value.status == 409
    assert counts(conn, "I01") == before
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert service.audit_counters(conn)["mismatches"] == 0
    assert service.audit_counters(conn)["batch_count_mismatches"] == 0
    assert service.audit_counters(conn)["out_of_list_details"] == 0


def test_concurrent_same_batch_number_only_one_commits(app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01"])
    conn.close()

    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        c = connect(app)
        try:
            barrier.wait()
            submit(c, "RACE", card, {"I01": "并发"})
            outcome = "ok"
        except service.DomainError as exc:
            outcome = f"rejected:{exc.status}"
        finally:
            c.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = connect(app)
    assert sorted(results) == ["ok", "rejected:409"]
    assert check.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert counts(check, "I01") == {"abnormal": 1, "clean": 0, "not_inspected": 0}
    assert service.audit_counters(check)["mismatches"] == 0
    check.close()


def test_concurrent_correction_and_submission_never_leave_out_of_list_detail(app):
    """Race a correction that removes I01 against a submission abnormal on I01.

    Whichever order the writer lock grants, the invariant holds: stored
    abnormality details are always inside the card's current list, and no
    partial batch is written.
    """
    for round_no in range(10):
        conn = connect(app)
        card = card_id(conn, "C001")
        set_list(conn, card, ["I01", "I02"])
        conn.close()

        barrier = threading.Barrier(2)
        errors: list[str] = []
        lock = threading.Lock()

        def submitter() -> None:
            c = connect(app)
            try:
                barrier.wait()
                submit(c, f"R-{round_no}", card, {"I01": f"detail-{round_no}"})
            except service.DomainError as exc:
                with lock:
                    errors.append(f"submit:{exc.status}")
            finally:
                c.close()

        def corrector() -> None:
            c = connect(app)
            try:
                barrier.wait()
                set_list(c, card, ["I02"])
            except service.DomainError as exc:
                with lock:
                    errors.append(f"correct:{exc.status}")
            finally:
                c.close()

        threads = [
            threading.Thread(target=submitter),
            threading.Thread(target=corrector),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check = connect(app)
        listed = {
            int(r[0])
            for r in check.execute(
                "SELECT item_id FROM card_items WHERE card_id = ?", (card,)
            )
        }
        abnormal_items = {
            int(r[0])
            for r in check.execute(
                "SELECT ba.item_id FROM batch_abnormalities ba "
                "JOIN batches b ON b.id = ba.batch_id WHERE b.card_id = ?",
                (card,),
            )
        }
        assert abnormal_items <= listed, (round_no, errors)
        assert service.audit_counters(check)["mismatches"] == 0
        # Exactly one of the two operations must have succeeded.
        assert len(errors) == 1, errors
        check.close()


def test_query_and_correction_work_is_constant_in_batch_count(app):
    """Exact SQLite progress-handler steps must not grow with received batches."""

    def steps(conn, fn) -> int:
        counter = {"n": 0}

        def progress() -> int:
            counter["n"] += 1
            return 0

        conn.set_progress_handler(progress, 1)
        try:
            fn(conn)
        finally:
            conn.set_progress_handler(None, 0)
        return counter["n"]

    conn = connect(app)
    card = card_id(conn, "C001")
    item_i01 = item_id(conn, "I01")
    item_i02 = item_id(conn, "I02")
    item_i03 = item_id(conn, "I03")
    full_list = {item_i01, item_i02, item_i03}
    service.correct_card_items(conn, card_id=card, item_ids=full_list)
    for i in range(2):
        submit(conn, f"SMALL-{i}", card, {"I01": "d"})

    def measure(c) -> tuple[int, int, int]:
        # Always measure from the same inspection-list state: full list.
        if service._listed_items(c, card) != full_list:
            service.correct_card_items(c, card_id=card, item_ids=full_list)
        query = steps(c, lambda x: service.query_item(x, item_i02))
        noop = steps(
            c, lambda x: service.correct_card_items(x, card_id=card, item_ids=full_list)
        )
        remove = steps(
            c,
            lambda x: service.correct_card_items(
                x, card_id=card, item_ids={item_i01, item_i02}
            ),
        )
        service.correct_card_items(conn, card_id=card, item_ids=full_list)
        return query, noop, remove

    small = measure(conn)

    # Grow received batches by 300 without changing cards, items or lists.
    for i in range(300):
        submit(conn, f"BIG-{i}", card)
    large = measure(conn)

    # Identical statements => identical instruction counts, exactly.
    assert small == large, (small, large)

    # The correct value after a final remove: I02 is off-list.
    service.correct_card_items(conn, card_id=card, item_ids={item_i01, item_i03})
    assert counts(conn, "I01") == {"abnormal": 2, "clean": 300, "not_inspected": 0}
    assert counts(conn, "I02") == {"abnormal": 0, "clean": 0, "not_inspected": 302}
    assert service.audit_counters(conn)["mismatches"] == 0
    assert service.audit_counters(conn)["batch_count_mismatches"] == 0
    assert service.audit_counters(conn)["out_of_list_details"] == 0
    conn.close()


def test_web_flow_submit_query_correct_and_rejections(client, app):
    conn = connect(app)
    card = card_id(conn, "C001")
    item_i01 = item_id(conn, "I01")
    item_i02 = item_id(conn, "I02")
    item_i03 = item_id(conn, "I03")
    conn.close()

    # Register the card's inspection list through the web form.
    response = client.post(
        f"/cards/{card}/items",
        data={"item_ids": [str(item_i01), str(item_i02)]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "实检清单已更正".encode() in response.data

    response = client.post(
        "/batches",
        data={
            "batch_no": "W-1",
            "card_id": str(card),
            "abnormal_item": [str(item_i01)],
            f"detail_{item_i01}": "幅宽异常",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "已登记入库".encode() in response.data

    response = client.get(f"/query?item_id={item_i01}")
    assert response.status_code == 200
    assert b'data-count="abnormal">1<' in response.data
    assert b'data-count="clean">0<' in response.data
    assert b'data-count="not_inspected">0<' in response.data

    # Removing the item carrying an abnormality is rejected on the web page.
    response = client.post(
        f"/cards/{card}/items",
        data={"item_ids": [str(item_i02)]},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "整次退回".encode() in response.data

    # Submitting an out-of-list abnormality is rejected as a whole.
    response = client.post(
        "/batches",
        data={
            "batch_no": "W-BAD",
            "card_id": str(card),
            "abnormal_item": [str(item_i03)],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "整批退回".encode() in response.data

    response = client.get(f"/query?item_id={item_i01}")
    assert b'data-count="abnormal">1<' in response.data
    response = client.get(f"/query?item_id={item_i03}")
    assert b'data-count="not_inspected">1<' in response.data


def test_batch_detail_shows_preserved_original_detail(client, app):
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01", "I02"])
    submit(conn, "D-1", card, {"I01": "保留的原明细"})
    conn.close()

    response = client.get("/batches/1")
    assert response.status_code == 200
    assert "保留的原明细".encode() in response.data


def test_database_triggers_backstop_both_invariants(app):
    """Bypass the service and write directly: triggers must still abort."""
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01", "I02"])
    submit(conn, "T-1", card, {"I01": "原明细"})

    # Direct insert of an out-of-list abnormality must abort.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO batch_abnormalities(batch_id, item_id, detail) "
            "VALUES (1, ?, 'x')",
            (item_id(conn, "I03"),),
        )
    assert conn.execute("SELECT COUNT(*) FROM batch_abnormalities").fetchone()[0] == 1

    # Direct removal of an item that still has preserved details must abort.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM card_items WHERE card_id = ? AND item_id = ?",
            (card, item_id(conn, "I01")),
        )
    assert item_id(conn, "I01") in service._listed_items(conn, card)

    # Removing an item with no abnormalities is allowed.
    conn.execute(
        "DELETE FROM card_items WHERE card_id = ? AND item_id = ?",
        (card, item_id(conn, "I02")),
    )
    assert item_id(conn, "I02") not in service._listed_items(conn, card)
    conn.close()


def test_busy_writer_surfaces_retryable_domain_error(app):
    """A second writer holding the lock yields a 503-style domain error,
    never a silent partial write."""
    conn = connect(app)
    card = card_id(conn, "C001")
    set_list(conn, card, ["I01"])
    conn.execute("PRAGMA busy_timeout = 200")
    other = connect(app)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(service.DomainError) as excinfo:
            service.submit_batch(conn, batch_no="BUSY-1", card_id=card)
        assert excinfo.value.status == 503
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    finally:
        other.rollback()
        other.close()
        conn.close()
