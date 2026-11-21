"""as_of_query_proof 验收：两段历史切点前后各查一次、未命中、失败闭、只读核证。"""

from app.db import connect
from conftest import hist_rows, proof_count, run_pipeline

STORE = "ST-EAST-01"
BEFORE_CUT = "2026-11-10T00:00:00Z"
CUT = "2026-11-15T00:00:00Z"
AFTER_CUT = "2026-11-20T00:00:00Z"
BEFORE_COVERAGE = "2026-10-01T00:00:00Z"


def query(client, store_id, as_of_ts, **extra):
    return client.post(
        "/api/as-of/query",
        json={"store_id": store_id, "as_of_ts": as_of_ts, **extra},
    )


def test_two_segments_before_after_cut_match_hist(client):
    run_pipeline(client)
    rows = hist_rows(client, STORE)
    assert len(rows) == 2, f"须至少两段历史: {rows}"
    seg1, seg2 = rows[0], rows[1]
    assert seg1["valid_to"] == seg2["valid_from"] == CUT
    assert seg1["is_current"] == 0 and seg2["is_current"] == 1

    # 切点前 → 第一段，属性与 hist 行逐字段对上
    r = query(client, STORE, BEFORE_CUT)
    assert r.status_code == 200, r.text
    rc = r.json()
    assert rc["hit"] is True and rc["miss_reason"] is None
    assert rc["segment"]["hist_id"] == seg1["id"]
    assert rc["segment"]["open_time"] == seg1["open_time"] == "09:00"
    assert rc["segment"]["close_time"] == seg1["close_time"] == "21:00"
    assert rc["segment"]["row_hash"] == seg1["row_hash"]
    assert rc["segment"]["valid_from"] == seg1["valid_from"]
    assert rc["segment"]["valid_to"] == CUT
    assert rc["mirror_used"] is False and rc["source"] == "dim_store_hours_hist"

    # 切点后 → 第二段，同样对上 hist
    r = query(client, STORE, AFTER_CUT)
    assert r.status_code == 200, r.text
    rc = r.json()
    assert rc["hit"] is True
    assert rc["segment"]["hist_id"] == seg2["id"]
    assert rc["segment"]["open_time"] == seg2["open_time"] == "09:30"
    assert rc["segment"]["row_hash"] == seg2["row_hash"]
    assert rc["segment"]["valid_to"] is None

    # 证明与查询结果同事务可见：回执 proof_id 在证明表中立即可查且同源
    proofs = client.get("/api/as-of/proofs").json()
    by_id = {p["id"]: p for p in proofs}
    assert rc["proof_id"] in by_id
    p = by_id[rc["proof_id"]]
    assert p["hist_id"] == seg2["id"] and p["row_hash"] == seg2["row_hash"]
    assert p["query_key"] == f"{STORE}|{AFTER_CUT}" and p["hit"] == 1

    # 只读核证：全部通过，且核证本身不落库
    before = proof_count()
    rep = client.get("/api/as-of/verify").json()
    assert rep["checked"] == 2 and rep["failed"] == 0
    assert proof_count() == before


def test_cut_point_boundary(client):
    run_pipeline(client)
    rows = hist_rows(client, STORE)
    seg1, seg2 = rows[0], rows[1]

    # as_of == valid_from（切点）→ 命中新段（半开区间 [from, to)）
    r = query(client, STORE, CUT)
    assert r.json()["segment"]["hist_id"] == seg2["id"]
    # 切点前一秒 → 旧段
    r = query(client, STORE, "2026-11-14T23:59:59Z")
    assert r.json()["segment"]["hist_id"] == seg1["id"]


def test_miss_before_coverage_is_explicit_not_mirror(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_COVERAGE)
    assert r.status_code == 200, r.text
    rc = r.json()
    # 明确未命中：无属性、有原因、绝不拿当前镜顶替
    assert rc["hit"] is False
    assert rc["miss_reason"] == "no_coverage"
    assert rc["segment"] is None
    assert rc["mirror_used"] is False

    # 未命中也落证明（未命中标记可核证）
    proofs = client.get(f"/api/as-of/proofs?store_id={STORE}").json()
    miss = [p for p in proofs if p["as_of_ts"] == BEFORE_COVERAGE]
    assert len(miss) == 1
    assert miss[0]["hit"] == 0 and miss[0]["miss_reason"] == "no_coverage"
    assert miss[0]["hist_id"] is None and miss[0]["row_hash"] is None

    rep = client.get("/api/as-of/verify").json()
    assert rep["failed"] == 0


def test_unknown_store_is_explicit_miss(client):
    r = query(client, "ST-NOPE-99", BEFORE_CUT)
    assert r.status_code == 200, r.text
    rc = r.json()
    assert rc["hit"] is False and rc["miss_reason"] == "unknown_store"
    assert rc["segment"] is None


def test_query_never_touches_current_mirror(client):
    run_pipeline(client)
    # 删掉当前镜整行：点查仍须命中 hist；覆盖外仍须未命中（禁止镜顶替）
    conn = connect()
    conn.execute("DELETE FROM dim_store_hours_current WHERE store_id=?", (STORE,))
    conn.commit()
    conn.close()

    r = query(client, STORE, BEFORE_CUT)
    assert r.status_code == 200, r.text
    assert r.json()["hit"] is True
    assert r.json()["segment"]["open_time"] == "09:00"

    r = query(client, STORE, BEFORE_COVERAGE)
    assert r.json()["hit"] is False and r.json()["segment"] is None


def test_fault_injection_leaves_no_fake_hit(client):
    run_pipeline(client)
    assert proof_count() == 0

    # 夹具故障：proof 半截写失败 → 整次查询失败，事务回滚
    r = query(client, STORE, BEFORE_CUT, fault="proof_insert")
    assert r.status_code == 500, r.text
    assert proof_count() == 0, "半截写失败不得留下假命中 proof"

    # 故障后管道不瘸：正常点查照常落证明
    r = query(client, STORE, BEFORE_CUT)
    assert r.status_code == 200 and r.json()["hit"] is True
    assert proof_count() == 1
    rep = client.get("/api/as-of/verify").json()
    assert rep["failed"] == 0


def test_overlap_fails_whole_query_without_proof(client):
    run_pipeline(client)
    # 人为制造 hist 重叠（与切点前区间相交的脏段）
    conn = connect()
    conn.execute(
        """INSERT INTO dim_store_hours_hist(
             id, store_id, open_time, close_time, weekday_mask, biz_status,
             valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
           ) VALUES ('HIST-DIRTY-OVERLAP', ?, '00:00', '23:59', '1111111', 'open',
                     '2026-11-05T00:00:00Z', '2026-11-12T00:00:00Z', 0, NULL, 'deadbeef', NULL)""",
        (STORE,),
    )
    conn.commit()
    conn.close()

    r = query(client, STORE, BEFORE_CUT)
    assert r.status_code == 409, r.text
    assert proof_count() == 0, "校验失败则整次查询失败，不得落 proof"


def test_verify_catches_proof_pointing_to_foreign_hist(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_CUT)
    proof_id = r.json()["proof_id"]

    # 篡改：回执有属性，proof 却指他键 hist（禁例）→ 核证必须逮到
    foreign = hist_rows(client, "ST-SOUTH-01")[0]
    conn = connect()
    conn.execute("UPDATE as_of_proofs SET hist_id=? WHERE id=?", (foreign["id"], proof_id))
    conn.commit()
    conn.close()

    rep = client.get("/api/as-of/verify").json()
    assert rep["failed"] == 1
    bad = [x for x in rep["results"] if not x["valid"]][0]
    assert bad["proof_id"] == proof_id
    errors = {e["error"] for e in bad["detail"]["errors"]}
    assert "hist_id_mismatch" in errors


def test_verify_catches_hash_tamper(client):
    run_pipeline(client)
    r = query(client, STORE, AFTER_CUT)
    proof_id = r.json()["proof_id"]

    conn = connect()
    conn.execute("UPDATE as_of_proofs SET row_hash='tampered' WHERE id=?", (proof_id,))
    conn.commit()
    conn.close()

    rep = client.get(f"/api/as-of/verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errors = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "row_hash_mismatch" in errors


def test_input_validation(client):
    assert query(client, "", BEFORE_CUT).status_code == 400
    assert query(client, STORE, "not-a-time").status_code == 400
    # 无时区拒绝猜测
    assert query(client, STORE, "2026-11-10 08:00:00").status_code == 400
    # 带偏移时区可解析并归一（+08:00 的 08:00 == UTC 00:00）
    run_pipeline(client)
    r = query(client, STORE, "2026-11-15T08:00:00+08:00")
    assert r.status_code == 200
    assert r.json()["query"]["as_of_ts"] == CUT
