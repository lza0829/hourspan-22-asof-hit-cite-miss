from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules import as_of_query_proof as asof
from app.modules import asof_hit_cite as hit_cite
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    run_id = body.get("detect_run_id")
    if not run_id:
        raise HTTPException(400, "需要 detect_run_id")
    return scd.apply_type2(run_id)


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.post("/as-of/query")
def as_of_query(body: dict = Body(...)):
    """时点投影点查：store_id+as_of_ts → 覆盖该时点的 hist 段（未命中明确标记）。

    落库形态甲：proof 写入 as_of_proofs；失败闭路径甲：proof 与结果同事务，
    异常整体回滚。fault 为夹具故障注入（仅测试失败闭用）。
    """
    try:
        return asof.query_as_of(
            body.get("store_id"),
            body.get("as_of_ts"),
            fault=body.get("fault"),
        )
    except asof.AsOfInputError as exc:
        raise HTTPException(400, str(exc))
    except asof.AsOfOverlapError as exc:
        raise HTTPException(409, str(exc))
    except asof.AsOfFault as exc:
        raise HTTPException(500, str(exc))
    except (hit_cite.CiteMissing, hit_cite.CiteMismatch) as exc:
        raise HTTPException(500, str(exc))


@router.get("/as-of/proofs")
def as_of_proofs(store_id: str | None = None):
    """只读：as-of 证明清单（命中 hist_id / valid 区间 / 属性 hash / 未命中标记）。"""
    return asof.list_proofs(store_id)


@router.get("/as-of/cite-verify")
def as_of_cite_verify(proof_id: int | None = None):
    """as-of 命中引用核证（只读）：缺引用显式失败，绝不把缺引用当通过。"""
    try:
        return hit_cite.verify_cites(proof_id)
    except asof.AsOfInputError as exc:
        raise HTTPException(404, str(exc))


@router.get("/as-of/verify")
def as_of_verify(proof_id: int | None = None):
    """as-of 核证只读接口：对已提交 hist 重推导，逐条比对 proof，不落库。"""
    try:
        return asof.verify_proofs(proof_id)
    except asof.AsOfInputError as exc:
        raise HTTPException(404, str(exc))


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}

# --- plant routes ---
@router.get("/as-of/hit-cites")
def asof_hit_cites(store_id: str | None = None, proof_id: int | None = None):
    conn = connect()
    try:
        return hit_cite.cites_of(conn, store_id, proof_id=proof_id)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

