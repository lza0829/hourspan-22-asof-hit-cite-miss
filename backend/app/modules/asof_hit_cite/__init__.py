"""as-of 命中引用（全局架构：甲 · 引用齐套为成功条件）

口径择一（全局只贯彻甲）：
  - 甲（本模块）：以引用齐套为成功条件——无引用不得返回成功体。
    点查成功体返回之前，须在同一事务内写 `as_of_hit_cites` 行，并与本次实命中
    hist 段逐字段对撞（store_id / as_of_ts / hist_id / valid_from / valid_to /
    row_hash）；引用缺行或与命中段分叉即整次查询失败、事务回滚，不存在
    「有成功回执、无命中引用」的可见状态。
  - 乙（未采用）：以区间命中为成功条件、引用仅旁路审计；缺引用仍可返回属性，
    但核证须标引用缺失且不得当通过。

禁区（违反即缺陷）：
  - 禁止点查成功而引用缺行（成功 ⇔ 引用齐套）。
  - 禁止引用 hist / valid 区间与实命中段分叉。
  - 禁止用当前镜 hist_id 冒充历史命中引用。
  - 禁止邻键（他店 / 他时点）引用被串改后仍核证通过。

核证（只读）：verify_cites 对每条已落 proof 重推导实命中段，逐字段比对引用；
缺引用显式记 `cite_missing` 且 valid=False，绝不把缺引用当通过。
"""

import json

from app.db import connect
from app.seed import row_hash

MISS = {
    "hist_id": None,
    "valid_from": None,
    "valid_to": None,
    "row_hash": None,
}


class CiteMissing(RuntimeError):
    """点查成功后引用缺行——甲口径下不允许提交。"""


class CiteMismatch(RuntimeError):
    """引用与本次实命中 hist 段不齐（字段分叉）。"""


def _segment_fields(segment: dict | None) -> dict:
    if segment is None:
        return dict(MISS)
    return {
        "hist_id": segment.get("hist_id"),
        "valid_from": segment.get("valid_from"),
        "valid_to": segment.get("valid_to"),
        "row_hash": segment.get("row_hash"),
    }


def write_cite(
    conn,
    *,
    proof_id: int,
    query_key: str,
    store_id: str,
    as_of_ts: str,
    segment: dict | None,
    miss_reason: str | None,
    created_at: str,
) -> int:
    """同事务写入命中引用（草稿）；不提交，由调用方与 proof 一并提交/回滚。"""
    fields = _segment_fields(segment)
    hit = 1 if segment is not None else 0
    cur = conn.execute(
        """INSERT INTO as_of_hit_cites(
             proof_id, query_key, store_id, as_of_ts, hit, miss_reason,
             hist_id, valid_from, valid_to, row_hash, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            proof_id,
            query_key,
            store_id,
            as_of_ts,
            hit,
            miss_reason,
            fields["hist_id"],
            fields["valid_from"],
            fields["valid_to"],
            fields["row_hash"],
            created_at,
        ),
    )
    return cur.lastrowid


def cite_by_proof(conn, proof_id: int):
    return conn.execute(
        "SELECT * FROM as_of_hit_cites WHERE proof_id=?", (proof_id,)
    ).fetchone()


def cite_view(row) -> dict | None:
    if row is None:
        return None
    return {
        "cite_id": row["id"],
        "proof_id": row["proof_id"],
        "query_key": row["query_key"],
        "store_id": row["store_id"],
        "as_of_ts": row["as_of_ts"],
        "hit": bool(row["hit"]),
        "miss_reason": row["miss_reason"],
        "hist_id": row["hist_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "row_hash": row["row_hash"],
        "created_at": row["created_at"],
    }


def assert_cite_matches(
    conn,
    *,
    proof_id: int,
    store_id: str,
    as_of_ts: str,
    segment: dict | None,
    miss_reason: str | None,
) -> "object":
    """写后即对撞：引用草稿必须与本次实命中段全等，否则拒绝提交。

    「先写引用草稿，再与命中段对撞，对撞通过才返回成功体」——缺行或分叉都会
    抛出，由调用方回滚整个事务。
    """
    row = cite_by_proof(conn, proof_id)
    if row is None:
        raise CiteMissing(f"proof #{proof_id} 命中引用缺行，拒绝返回成功体")

    fields = _segment_fields(segment)
    hit = 1 if segment is not None else 0
    problems = []
    if row["store_id"] != store_id:
        problems.append("store_id")
    if row["as_of_ts"] != as_of_ts:
        problems.append("as_of_ts")
    if int(row["hit"]) != hit:
        problems.append("hit")
    if hit:
        if row["miss_reason"] is not None:
            problems.append("miss_reason_on_hit")
        for key in ("hist_id", "valid_from", "valid_to", "row_hash"):
            if row[key] != fields[key]:
                problems.append(key)
    else:
        if (row["miss_reason"] or None) != miss_reason:
            problems.append("miss_reason")
        if row["hist_id"] or row["valid_from"] or row["valid_to"] or row["row_hash"]:
            problems.append("miss_carrying_attrs")
    if problems:
        raise CiteMismatch(
            "命中引用与实命中段不齐: " + ",".join(problems)
        )
    return row


def cites_of(conn, store_id: str | None = None, proof_id: int | None = None) -> dict:
    """只读：命中引用清单（store_id / as_of / hist_id / valid 区间 / row_hash）。

    proof_id 可用于从单条证明下钻到其命中引用（引用下钻不再空白）。
    """
    if proof_id is not None:
        rows = conn.execute(
            "SELECT * FROM as_of_hit_cites WHERE proof_id=? ORDER BY id DESC",
            (proof_id,),
        ).fetchall()
    elif store_id:
        rows = conn.execute(
            "SELECT * FROM as_of_hit_cites WHERE store_id=? ORDER BY id DESC LIMIT 200",
            (store_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM as_of_hit_cites ORDER BY id DESC LIMIT 200"
        ).fetchall()
    cites = [cite_view(r) for r in rows]
    if store_id:
        orphaned = conn.execute(
            """SELECT COUNT(*) AS c FROM as_of_proofs p
               WHERE p.store_id=?
                 AND NOT EXISTS (SELECT 1 FROM as_of_hit_cites c WHERE c.proof_id=p.id)""",
            (store_id,),
        ).fetchone()["c"]
    else:
        orphaned = conn.execute(
            """SELECT COUNT(*) AS c FROM as_of_proofs p
               WHERE NOT EXISTS (SELECT 1 FROM as_of_hit_cites c WHERE c.proof_id=p.id)"""
        ).fetchone()["c"]
    return {
        "store_id": store_id,
        "proof_id": proof_id,
        "cites": cites,
        "total": len(cites),
        "hit_n": sum(1 for c in cites if c["hit"]),
        "miss_n": sum(1 for c in cites if not c["hit"]),
        "proofs_without_cite": orphaned,
        "cite_ready": orphaned == 0,
        "source": "dim_store_hours_hist",
    }


def attach_cite(result: dict) -> dict:
    """给已有点查回执补挂其引用（只读查询，用于旁路展示/核证）。"""
    out = dict(result or {})
    proof_id = out.get("proof_id")
    if proof_id is None:
        out["cite"] = None
        out["cite_ready"] = False
        return out
    conn = connect()
    try:
        row = cite_by_proof(conn, proof_id)
    finally:
        conn.close()
    out["cite"] = cite_view(row)
    out["cite_ready"] = row is not None
    return out


def cite_gap(conn, proof) -> str | None:
    """proof 与其引用的粗对齐检查（供 /api/as-of/verify 协同核证，只读）。

    返回错误键；None 表示引用存在且与 proof 记录同源。
    """
    row = cite_by_proof(conn, proof["id"])
    if row is None:
        return "cite_missing"
    if bool(row["hit"]) != bool(proof["hit"]):
        return "cite_hit_diverged"
    if row["store_id"] != proof["store_id"]:
        return "cite_neighbor_store"
    if row["as_of_ts"] != proof["as_of_ts"]:
        return "cite_neighbor_as_of"
    if bool(proof["hit"]):
        if row["hist_id"] != proof["hist_id"]:
            return "cite_hist_diverged"
        if row["valid_from"] != proof["valid_from"] or row["valid_to"] != proof["valid_to"]:
            return "cite_interval_diverged"
        if row["row_hash"] != proof["row_hash"]:
            return "cite_hash_diverged"
    else:
        if row["hist_id"] or row["row_hash"]:
            return "cite_miss_carrying_attrs"
    return None


def _verify_one(conn, proof) -> dict:
    """只读重推导：引用必须存在、邻键不得串改、hist/区间/hash 与实命中段全等。"""
    from app.modules.as_of_query_proof import AsOfOverlapError, _segment_at

    errors = []

    def fail(key, **extra):
        errors.append({"error": key, **extra})

    cite = cite_by_proof(conn, proof["id"])
    if cite is None:
        # 甲口径：无引用不得视为通过
        fail("cite_missing")
        return _result(proof, None, errors)

    expected_key = f"{proof['store_id']}|{proof['as_of_ts']}"
    if cite["query_key"] != expected_key:
        fail("cite_key_diverged", expected=expected_key)
    if cite["store_id"] != proof["store_id"]:
        fail("cite_neighbor_store", expected=proof["store_id"], got=cite["store_id"])
    if cite["as_of_ts"] != proof["as_of_ts"]:
        fail("cite_neighbor_as_of", expected=proof["as_of_ts"], got=cite["as_of_ts"])
    if bool(cite["hit"]) != bool(proof["hit"]):
        fail("cite_hit_diverged", proof_hit=bool(proof["hit"]), cite_hit=bool(cite["hit"]))

    try:
        row, miss_reason = _segment_at(conn, proof["store_id"], proof["as_of_ts"])
    except AsOfOverlapError as exc:
        fail("hist_overlap", message=str(exc))
        return _result(proof, cite, errors)

    expected_hit = row is not None
    if bool(proof["hit"]) != expected_hit:
        fail("proof_hit_mismatch", expected=expected_hit)
    if bool(cite["hit"]) != expected_hit:
        fail("cite_hit_not_covering", expected=expected_hit)
    if (cite["miss_reason"] or None) != miss_reason:
        fail("cite_miss_reason_mismatch", expected=miss_reason)

    if expected_hit:
        if cite["hist_id"] != row["id"]:
            fail("cite_hist_mismatch", expected=row["id"], got=cite["hist_id"])
        if cite["hist_id"] != proof["hist_id"]:
            fail("cite_proof_hist_diverged", proof_hist_id=proof["hist_id"])
        if cite["valid_from"] != row["valid_from"] or cite["valid_to"] != row["valid_to"]:
            fail(
                "cite_valid_interval_mismatch",
                expected={"valid_from": row["valid_from"], "valid_to": row["valid_to"]},
            )
        recomputed = row_hash(
            row["open_time"], row["close_time"], row["weekday_mask"], row["biz_status"]
        )
        if row["row_hash"] != recomputed:
            fail("hist_row_hash_drift", recomputed=recomputed)
        if cite["row_hash"] != row["row_hash"]:
            fail("cite_row_hash_mismatch", expected=row["row_hash"])
        if row["store_id"] != proof["store_id"]:
            fail("cite_hist_foreign_store", hist_store=row["store_id"])

        # 当前镜隔离：引用不得拿当前镜 hist_id 冒充本次历史命中
        mirror = conn.execute(
            "SELECT hist_id FROM dim_store_hours_current WHERE store_id=?",
            (proof["store_id"],),
        ).fetchone()
        if mirror and cite["hist_id"] == mirror["hist_id"] and mirror["hist_id"] != row["id"]:
            fail(
                "cite_mirror_impersonation",
                mirror_hist_id=mirror["hist_id"],
                expected_hist_id=row["id"],
            )
    else:
        if cite["hist_id"] or cite["valid_from"] or cite["valid_to"] or cite["row_hash"]:
            fail("cite_miss_carrying_attrs")

    # 引用所指 hist 必须真实存在、属本店、且自身区间覆盖 as_of（不依赖 proof 记录的二次校验）
    if cite["hist_id"]:
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (cite["hist_id"],)
        ).fetchone()
        if not hist:
            fail("cite_hist_missing", hist_id=cite["hist_id"])
        else:
            if hist["store_id"] != proof["store_id"]:
                fail(
                    "cite_hist_foreign_key",
                    hist_id=hist["id"],
                    hist_store=hist["store_id"],
                )
            covers = hist["valid_from"] <= proof["as_of_ts"] and (
                hist["valid_to"] is None or hist["valid_to"] > proof["as_of_ts"]
            )
            if not covers:
                fail(
                    "cite_hist_not_covering_as_of",
                    hist_id=hist["id"],
                    valid_from=hist["valid_from"],
                    valid_to=hist["valid_to"],
                )

    # 回执快照必须携带同一引用，且标记齐套
    try:
        receipt = json.loads(proof["receipt_json"])
    except (TypeError, ValueError):
        receipt = None
    if not isinstance(receipt, dict):
        fail("receipt_unreadable")
    else:
        rcite = receipt.get("cite")
        if not isinstance(rcite, dict):
            fail("receipt_cite_missing")
        else:
            if rcite.get("cite_id") != cite["id"]:
                fail("receipt_cite_id_mismatch", expected=cite["id"])
            for key in ("hist_id", "valid_from", "valid_to", "row_hash"):
                if rcite.get(key) != cite[key]:
                    fail("receipt_cite_diverged", field=key)
        if receipt.get("cite_ready") is not True:
            fail("receipt_cite_not_ready")

    return _result(proof, cite, errors)


def _result(proof, cite, errors) -> dict:
    return {
        "proof_id": proof["id"],
        "cite_id": cite["id"] if cite is not None else None,
        "store_id": proof["store_id"],
        "as_of_ts": proof["as_of_ts"],
        "hit": bool(proof["hit"]),
        "hist_id": proof["hist_id"],
        "valid_from": proof["valid_from"],
        "valid_to": proof["valid_to"],
        "row_hash": proof["row_hash"],
        "cite_present": cite is not None,
        "cite_ready": cite is not None and not errors,
        "valid": not errors,
        "detail": {"ok": True} if not errors else {"errors": errors},
    }


def verify_cites(proof_id: int | None = None) -> dict:
    """as-of 命中引用核证（只读）：缺引用显式失败，绝不把缺引用当通过。"""
    from app.modules.as_of_query_proof import AsOfInputError

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
            "missing": sum(1 for r in results if not r["cite_present"]),
            "results": results,
        }
    finally:
        conn.close()
