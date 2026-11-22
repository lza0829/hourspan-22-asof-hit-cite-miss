"""as_of_query_proof · 时点投影查询与证明（去皮=时点投影管道，非运营查询台）

形态择一：【甲 · 独立落库】as_of_proofs 表——查询键 / 命中 hist_id / 属性哈希 /
  未命中标记 / 回执快照，逐次查询追加，可审计。
命中引用形态：【甲 · 引用齐套为成功条件】同事务写 as_of_hit_cites，并与本次实
  命中段逐字段对撞；无引用或引用分叉即整次查询失败——不存在「成功回执无引用」。
失败闭择一：【甲 · 同事务可见】同一事务内读 hist 段、写 proof、写引用、对撞、
  提交；任一环节失败（含注入的半截写故障）整体回滚——不留半截 proof/引用，
  不留假命中，也不留无引用的成功回执。

禁区（违反即缺陷）：
  - 只读 dim_store_hours_hist；禁止读时 JOIN 当前镜冒充历史（receipt 里
    mirror_used=False 为显式标记；引用 hist_id 亦不得取当前镜）。
  - 无覆盖须明确未命中（hit=0 + miss_reason），禁止拿当前镜顶替。
  - 禁止回执有属性而 proof 指他键 hist：proof 与回执由同一行 hist 同事务
    生成，verify 时按 store_id+as_of_ts 重推导比对。
  - 禁止点查成功而命中引用缺行；禁止引用 hist/valid 区间与实命中分叉；
    禁止邻键（他店/他时点）引用被串改后核证通过。

核证（只读）：verify_proofs 对已落 proof 逐条从已提交 hist 重推导期望段，
比对 hist_id / valid 区间 / 属性哈希 / 未命中标记 / 回执一致性；
verify_cites 另对命中引用逐字段核证（缺引用显式失败，详见 asof_hit_cite）。
"""

import json
import sqlite3
from datetime import datetime, timezone

from app.db import connect
from app.modules import asof_hit_cite as hit_cite
from app.seed import now_iso, row_hash

MISS_NO_COVERAGE = "no_coverage"
MISS_UNKNOWN_STORE = "unknown_store"

# 故障注入（仅供夹具/测试验证失败闭）：
#   proof_insert = 写 proof 时半截失败；cite_insert = 写命中引用时半截失败
FAULT_PROOF_INSERT = "proof_insert"
FAULT_CITE_INSERT = "cite_insert"


class AsOfInputError(ValueError):
    """查询入参非法（缺键、时间戳无法解析等）。"""


class AsOfOverlapError(RuntimeError):
    """同一时点命中多段 hist——维表重叠，校验失败则整次查询失败。"""


class AsOfFault(RuntimeError):
    """注入故障：模拟 proof 半截写失败，用于验证失败闭（不留假命中）。"""


def normalize_ts(raw) -> str:
    """解析 ISO-8601（须带时区），归一为 %Y-%m-%dT%H:%M:%SZ 以便与 hist 文本比较。"""
    if not isinstance(raw, str) or not raw.strip():
        raise AsOfInputError("as_of_ts 必填，形如 2026-11-10T00:00:00Z")
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise AsOfInputError(f"as_of_ts 无法解析: {text!r}")
    if dt.tzinfo is None:
        raise AsOfInputError("as_of_ts 须带时区（如 ...Z 或 +08:00），拒绝猜测")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_key_of(store_id: str, as_of_ts: str) -> str:
    return f"{store_id}|{as_of_ts}"


def _segment_at(conn: sqlite3.Connection, store_id: str, as_of_ts: str):
    """半开区间 [valid_from, valid_to) 定位 hist 段；只查历史维，绝不触当前镜。

    返回 (row|None, miss_reason|None)；重叠（>1 段命中）即校验失败。
    """
    known = conn.execute("SELECT 1 FROM stores WHERE id=?", (store_id,)).fetchone()
    if not known:
        return None, MISS_UNKNOWN_STORE
    rows = conn.execute(
        """SELECT * FROM dim_store_hours_hist
           WHERE store_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)
           ORDER BY valid_from, id""",
        (store_id, as_of_ts, as_of_ts),
    ).fetchall()
    if len(rows) > 1:
        raise AsOfOverlapError(
            f"hist 重叠：{store_id} @ {as_of_ts} 命中 {len(rows)} 段 "
            f"({', '.join(r['id'] for r in rows)})，整次查询失败"
        )
    if not rows:
        return None, MISS_NO_COVERAGE
    return rows[0], None


def _segment_receipt(row: sqlite3.Row) -> dict:
    return {
        "hist_id": row["id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "open_time": row["open_time"],
        "close_time": row["close_time"],
        "weekday_mask": row["weekday_mask"],
        "biz_status": row["biz_status"],
        "row_hash": row["row_hash"],
    }


def query_as_of(store_id, as_of_ts, fault: str | None = None) -> dict:
    """时点投影点查：输入 store_id+as_of_ts，返回覆盖该时点的 hist 段属性。

    失败闭（路径甲）：读段、写 proof、写命中引用、对撞、回执补写在同一事务；
    异常即回滚，proof / 引用 / 查询结果要么同时可见、要么都不存在。
    成功 ⇔ 引用齐套：无引用不得返回成功体。
    """
    if not isinstance(store_id, str) or not store_id.strip():
        raise AsOfInputError("store_id 必填")
    store_id = store_id.strip()
    ts = normalize_ts(as_of_ts)
    qkey = query_key_of(store_id, ts)

    conn = connect()
    try:
        row, miss_reason = _segment_at(conn, store_id, ts)
        hit = row is not None
        segment = _segment_receipt(row) if hit else None

        if fault == FAULT_PROOF_INSERT:
            # 夹具故障：段已读出、proof 半截写即崩——事务回滚，不得留下假命中
            raise AsOfFault("注入故障：proof 半截写失败，事务回滚")

        created = now_iso()
        receipt = {
            "ok": True,
            "query": {"store_id": store_id, "as_of_ts": ts, "query_key": qkey},
            "hit": hit,
            "miss_reason": miss_reason,
            "segment": segment,
            "source": "dim_store_hours_hist",
            "mirror_used": False,
        }
        cur = conn.execute(
            """INSERT INTO as_of_proofs(
              query_key, store_id, as_of_ts, hit, miss_reason,
              hist_id, valid_from, valid_to, row_hash, receipt_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                qkey,
                store_id,
                ts,
                1 if hit else 0,
                miss_reason,
                segment["hist_id"] if hit else None,
                segment["valid_from"] if hit else None,
                segment["valid_to"] if hit else None,
                segment["row_hash"] if hit else None,
                json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                created,
            ),
        )
        proof_id = cur.lastrowid
        receipt["proof_id"] = proof_id

        # 命中引用：同事务先写草稿，再与实命中段对撞；缺行/分叉即拒绝提交
        if fault == FAULT_CITE_INSERT:
            raise AsOfFault("注入故障：命中引用半截写失败，事务回滚")
        cite_id = hit_cite.write_cite(
            conn,
            proof_id=proof_id,
            query_key=qkey,
            store_id=store_id,
            as_of_ts=ts,
            segment=segment,
            miss_reason=miss_reason,
            created_at=created,
        )
        if fault:
            raise AsOfInputError(f"未知故障注入: {fault!r}")
        cite_row = hit_cite.assert_cite_matches(
            conn,
            proof_id=proof_id,
            store_id=store_id,
            as_of_ts=ts,
            segment=segment,
            miss_reason=miss_reason,
        )
        cite = hit_cite.cite_view(cite_row)
        if cite["cite_id"] != cite_id:
            raise hit_cite.CiteMismatch("命中引用写入与读回不一致")

        # 回执挂同一引用；成功体只有引用齐套才补 cite/cite_ready=True
        receipt["cite"] = cite
        receipt["cite_ready"] = True

        # 回执补写 proof_id / cite 后同步落库，保证 proof 与回执严格同源同事务
        conn.execute(
            "UPDATE as_of_proofs SET receipt_json=? WHERE id=?",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True), proof_id),
        )
        conn.commit()
        return receipt
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_proofs(store_id: str | None = None) -> list[dict]:
    """只读：已落 as-of 证明（命中 hist_id / valid 区间 / 属性哈希 / 未命中标记）。"""
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM as_of_proofs WHERE store_id=? ORDER BY id DESC LIMIT 200",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM as_of_proofs ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _verify_one(conn: sqlite3.Connection, proof: sqlite3.Row) -> dict:
    """对已提交 hist 重推导，逐字段比对单条 proof（只读，不落库）。"""
    detail = {}
    valid = True

    def fail(key, **extra):
        nonlocal valid
        valid = False
        detail.setdefault("errors", []).append({"error": key, **extra})

    if proof["query_key"] != query_key_of(proof["store_id"], proof["as_of_ts"]):
        fail("query_key_mismatch")

    try:
        row, miss_reason = _segment_at(conn, proof["store_id"], proof["as_of_ts"])
    except AsOfOverlapError as exc:
        fail("hist_overlap", message=str(exc))
        row, miss_reason = None, None

    expected_hit = row is not None
    if bool(proof["hit"]) != expected_hit:
        fail("hit_mismatch", expected=expected_hit)
    if (proof["miss_reason"] or None) != miss_reason:
        fail("miss_reason_mismatch", expected=miss_reason)

    if expected_hit:
        # 禁止回执有属性而 proof 指他键 hist：键、区间、哈希必须与 hist 同源
        if proof["hist_id"] != row["id"]:
            fail("hist_id_mismatch", expected=row["id"])
        if row["store_id"] != proof["store_id"]:
            fail("hist_foreign_key", hist_store=row["store_id"])
        if proof["valid_from"] != row["valid_from"] or proof["valid_to"] != row["valid_to"]:
            fail("valid_interval_mismatch",
                 expected={"valid_from": row["valid_from"], "valid_to": row["valid_to"]})
        recomputed = row_hash(row["open_time"], row["close_time"], row["weekday_mask"], row["biz_status"])
        if row["row_hash"] != recomputed:
            fail("hist_row_hash_drift", recomputed=recomputed)
        if proof["row_hash"] != row["row_hash"]:
            fail("row_hash_mismatch", expected=row["row_hash"])
        expected_segment = _segment_receipt(row)
    else:
        if proof["hist_id"] or proof["row_hash"]:
            fail("miss_proof_carrying_attrs")
        expected_segment = None

    # 回执不可变且与 hist 同源：按重推导结果重建回执，与落库回执逐字段比对
    try:
        receipt = json.loads(proof["receipt_json"])
    except (TypeError, ValueError):
        receipt = None
    if not isinstance(receipt, dict):
        fail("receipt_unreadable")
    else:
        expected_query = {
            "store_id": proof["store_id"],
            "as_of_ts": proof["as_of_ts"],
            "query_key": proof["query_key"],
        }
        if receipt.get("query") != expected_query:
            fail("receipt_query_mismatch", expected=expected_query)
        if receipt.get("segment") != expected_segment:
            fail("receipt_segment_mismatch")
        if receipt.get("hit") != expected_hit or receipt.get("miss_reason") != miss_reason:
            fail("receipt_hit_mismatch")
        if receipt.get("mirror_used") is not False:
            fail("receipt_mirror_tainted")

    # 命中引用协同核证：缺引用显式失败，绝不把缺引用当通过
    gap = hit_cite.cite_gap(conn, proof)
    if gap:
        fail(gap)

    if valid:
        detail["ok"] = True
    return {
        "proof_id": proof["id"],
        "store_id": proof["store_id"],
        "as_of_ts": proof["as_of_ts"],
        "hit": bool(proof["hit"]),
        "miss": not bool(proof["hit"]),
        "miss_reason": proof["miss_reason"],
        "hist_id": proof["hist_id"],
        "valid_from": proof["valid_from"],
        "valid_to": proof["valid_to"],
        "row_hash": proof["row_hash"],
        "cite_present": gap != "cite_missing",
        "valid": valid,
        "detail": detail,
    }


def verify_proofs(proof_id: int | None = None) -> dict:
    """as-of 核证只读接口：命中 hist_id、valid 区间、属性 hash、未命中标记。"""
    conn = connect()
    try:
        if proof_id is not None:
            rows = conn.execute(
                "SELECT * FROM as_of_proofs WHERE id=?", (proof_id,)
            ).fetchall()
            if not rows:
                raise AsOfInputError(f"proof 不存在: {proof_id}")
        else:
            rows = conn.execute("SELECT * FROM as_of_proofs ORDER BY id").fetchall()
        results = [_verify_one(conn, r) for r in rows]
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()
