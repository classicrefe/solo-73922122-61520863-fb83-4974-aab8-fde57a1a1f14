from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import service
from .db import get_db
from .service import DomainError

bp = Blueprint("main", __name__)


def _int(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


@bp.get("/")
def index():
    conn = get_db()
    return render_template(
        "index.html",
        cards=service.list_cards(conn),
        items=service.list_items(conn),
        batches=service.list_batches(conn),
    )


@bp.post("/cards/<int:card_id>/items")
def correct_card(card_id: int):
    conn = get_db()
    item_ids = {int(v) for v in request.form.getlist("item_ids") if v.isdigit()}
    try:
        result = service.correct_card_items(conn, card_id=card_id, item_ids=item_ids)
    except DomainError as exc:
        flash(exc.message, "error")
        return redirect(url_for("main.index", _anchor=f"card-{card_id}"), 303)
    flash(
        "实检清单已更正：新增 %(added)d 项、移除 %(removed)d 项，"
        "按新清单重计 %(batches)d 个已收批次；原异常明细保留。" % result,
        "success",
    )
    return redirect(url_for("main.index", _anchor=f"card-{card_id}"), 303)


@bp.get("/cards/<int:card_id>")
def card_detail(card_id: int):
    conn = get_db()
    try:
        state = service.card_state(conn, card_id)
    except DomainError:
        abort(404)
    return render_template("card.html", **state)


@bp.post("/batches")
def submit_batch():
    conn = get_db()
    batch_no = request.form.get("batch_no", "")
    card_id = _int(request.form.get("card_id"))
    if card_id is None:
        flash("请选择作业卡。", "error")
        return redirect(url_for("main.index"), 303)

    abnormalities: dict[int, str] = {}
    for raw in request.form.getlist("abnormal_item"):
        item_id = _int(raw)
        if item_id is None:
            flash("异常项目选择无效，整批退回。", "error")
            return redirect(url_for("main.index"), 303)
        abnormalities[item_id] = request.form.get(
            f"detail_{item_id}", ""
        ).strip()

    try:
        service.submit_batch(
            conn,
            batch_no=batch_no,
            card_id=card_id,
            abnormalities=abnormalities,
        )
    except DomainError as exc:
        flash(exc.message, "error")
        return redirect(url_for("main.index", _anchor="submit"), 303)

    flash(f"批次 {batch_no.strip()} 已登记入库。", "success")
    return redirect(url_for("main.index", _anchor="batches"), 303)


@bp.get("/query")
def query():
    conn = get_db()
    item_id = _int(request.args.get("item_id"))
    result = None
    item = None
    if item_id is not None:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is not None:
            result = service.query_item(conn, item_id)
    return render_template(
        "query.html",
        items=service.list_items(conn),
        selected_item=item,
        result=result,
    )


@bp.get("/batches/<int:batch_id>")
def batch_detail(batch_id: int):
    conn = get_db()
    batch = service.batch_detail(conn, batch_id)
    if batch is None:
        abort(404)
    return render_template("batch.html", batch=batch)


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}


@bp.errorhandler(DomainError)
def domain_error(exc: DomainError):
    flash(exc.message, "error")
    return redirect(url_for("main.index"), 303)


@bp.app_errorhandler(DomainError)
def domain_error_app(exc: DomainError):
    # Safety net for any route that forgets its local DomainError handling.
    flash(exc.message, "error")
    return redirect(url_for("main.index"), 303)


@bp.get("/audit")
def audit():
    conn = get_db()
    result = service.audit_counters(conn)
    return render_template("audit.html", result=result)
