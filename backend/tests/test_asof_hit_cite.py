"""as_of 命中引用验收：甲口径（引用齐套为成功条件）。

覆盖：
  - 跨段点查后逐条核引用（hist_id / valid 区间 / row_hash 与实命中段全等）；
  - 成功体必带 cite 且 citation 已同事务落库；
  - 再造「无引用成功回执」：甲不得成功；乙式核证须暴露缺失且不当通过；
  - 引用 hist / 区间与实命中分叉、邻键引用串改、当前镜冒充均被核证逮到；
  - 半截写引用失败整体回滚，不留无引用成功回执。
"""

from app.db import connect
from conftest import hist_rows, proof_count, run_pipeline

STORE = "ST-EAST-01"
NEIGHBOR = "ST-SOUTH-01"
BEFORE_CUT = "2026-11-10T00:00:00Z"
CUT = "2026-11-15T00:00:00Z"
AFTER_CUT = "2026-11-20T00:00:00Z"
BEFORE_COVERAGE = "2026-10-01T00:00:00Z"


def query(client, store_id, as_of_ts, **extra):
    return client.post(
        "/api/as-of/query",
        json={"store_id": store_id, "as_of_ts": as_of_ts, **extra},
    )


def cite_count(store_id=None):
    conn = connect()
    try:
        if store_id:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM as_of_hit_cites WHERE store_id=?", (store_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM as_of_hit_cites").fetchone()
        return row["c"]
    finally:
        conn.close()


def cites_by_proof(proof_id):
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM as_of_hit_cites WHERE proof_id=?", (proof_id,)
        ).fetchone()
    finally:
        conn.close()


def test_cross_segment_queries_carry_cites_equal_to_hit(client):
    run_pipeline(client)
    rows = hist_rows(client, STORE)
    assert len(rows) == 2
    seg1, seg2 = rows[0], rows[1]

    # 跨段各查一次，成功体必带 cite，且 cite 与实命中段逐字段全等
    receipts = {}
    for ts, expected in ((BEFORE_CUT, seg1), (AFTER_CUT, seg2)):
        r = query(client, STORE, ts)
        assert r.status_code == 200, r.text
        rc = r.json()
        assert rc["hit"] is True
        assert rc["cite_ready"] is True
        cite = rc["cite"]
        assert cite is not None, "甲口径：成功体必须带命中引用"
        assert cite["proof_id"] == rc["proof_id"]
        assert cite["store_id"] == STORE and cite["as_of_ts"] == ts
        assert cite["hist_id"] == expected["id"]
        assert cite["valid_from"] == expected["valid_from"]
        assert cite["valid_to"] == expected["valid_to"]
        assert cite["row_hash"] == expected["row_hash"]
        receipts[rc["proof_id"]] = cite

    # 引用同事务落库，可回查
    for proof_id, cite in receipts.items():
        row = cites_by_proof(proof_id)
        assert row is not None, "成功回执必有引用行"
        assert row["hist_id"] == cite["hist_id"] and row["row_hash"] == cite["row_hash"]
        assert row["valid_from"] == cite["valid_from"] and row["valid_to"] == cite["valid_to"]

    # 只读清单：store 维度可列出这些引用
    listing = client.get(f"/api/as-of/hit-cites?store_id={STORE}").json()
    assert listing["total"] == 2 and listing["hit_n"] == 2
    assert listing["cite_ready"] is True
    listed = {(c["as_of_ts"], c["hist_id"]) for c in listing["cites"]}
    assert listed == {(BEFORE_CUT, seg1["id"]), (AFTER_CUT, seg2["id"])}

    # 按 proof 下钻：引用不再空白
    for proof_id, cite in receipts.items():
        drill = client.get(f"/api/as-of/hit-cites?proof_id={proof_id}").json()
        assert drill["total"] == 1
        assert drill["cites"][0]["hist_id"] == cite["hist_id"]
        assert drill["cite_ready"] is True

    # 引用核证接口：全通过，且不落库
    before = cite_count()
    rep = client.get("/api/as-of/cite-verify").json()
    assert rep["checked"] == 2 and rep["failed"] == 0 and rep["missing"] == 0
    assert cite_count() == before
    for x in rep["results"]:
        assert x["valid"] is True and x["cite_present"] is True


def test_miss_query_still_cites_absence_explicitly(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_COVERAGE)
    rc = r.json()
    assert rc["hit"] is False
    # 未命中也必须有引用行（记录未命中），且不得携带属性
    assert rc["cite_ready"] is True and rc["cite"] is not None
    assert rc["cite"]["hit"] is False
    assert rc["cite"]["miss_reason"] == "no_coverage"
    assert rc["cite"]["hist_id"] is None and rc["cite"]["row_hash"] is None
    rep = client.get("/api/as-of/cite-verify").json()
    assert rep["failed"] == 0


def test_no_cite_success_receipt_cannot_be_produced_jia(client):
    """再造无引用成功回执：甲口径下引用半截写失败，整次查询失败，不得成功。"""
    run_pipeline(client)
    assert proof_count() == 0 and cite_count() == 0

    r = query(client, STORE, BEFORE_CUT, fault="cite_insert")
    assert r.status_code == 500, r.text
    # 甲：不得成功；proof 与 cite 一并回滚，不存在无引用的成功回执
    assert proof_count() == 0
    assert cite_count() == 0

    # 故障后管道不瘸
    ok = query(client, STORE, BEFORE_CUT)
    assert ok.status_code == 200 and ok.json()["cite_ready"] is True
    assert proof_count() == 1 and cite_count() == 1


def test_verify_exposes_missing_cite_and_never_passes_it(client):
    """若历史遗留/篡改出无引用成功 proof，核证须暴露缺失且不得当通过（乙式口径）。"""
    run_pipeline(client)
    r = query(client, STORE, BEFORE_CUT)
    proof_id = r.json()["proof_id"]

    conn = connect()
    conn.execute("DELETE FROM as_of_hit_cites WHERE proof_id=?", (proof_id,))
    conn.commit()
    conn.close()

    rep = client.get("/api/as-of/cite-verify").json()
    assert rep["failed"] == 1 and rep["missing"] == 1
    bad = rep["results"][0]
    assert bad["proof_id"] == proof_id
    assert bad["cite_present"] is False
    assert bad["valid"] is False, "缺引用不得当通过"
    assert "cite_missing" in {e["error"] for e in bad["detail"]["errors"]}

    # 主核证接口协同暴露缺失
    main_rep = client.get("/api/as-of/verify").json()
    assert main_rep["failed"] == 1
    errs = {e["error"] for e in main_rep["results"][0]["detail"]["errors"]}
    assert "cite_missing" in errs


def test_verify_catches_cite_hist_and_interval_divergence(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_CUT)
    proof_id = r.json()["proof_id"]

    # 引用 hist 与实命中分叉
    conn = connect()
    conn.execute(
        "UPDATE as_of_hit_cites SET hist_id='HIST-FAKE' WHERE proof_id=?", (proof_id,)
    )
    conn.commit()
    conn.close()
    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "cite_hist_mismatch" in errs

    # 恢复 hist_id 但区间分叉
    conn = connect()
    conn.execute(
        "UPDATE as_of_hit_cites SET valid_from='2020-01-01T00:00:00Z' WHERE proof_id=?",
        (proof_id,),
    )
    conn.commit()
    conn.close()
    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "cite_valid_interval_mismatch" in errs


def test_verify_catches_neighbor_store_cite_tamper(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_CUT)
    proof_id = r.json()["proof_id"]

    # 邻键串改：把引用指到邻店的 hist
    neighbor = hist_rows(client, NEIGHBOR)[0]
    conn = connect()
    conn.execute(
        "UPDATE as_of_hit_cites SET hist_id=?, store_id=? WHERE proof_id=?",
        (neighbor["id"], NEIGHBOR, proof_id),
    )
    conn.commit()
    conn.close()

    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert errs & {"cite_neighbor_store", "cite_hist_foreign_key"}

    # 主核证接口也须逮到
    main_rep = client.get("/api/as-of/verify").json()
    assert main_rep["failed"] == 1


def test_verify_catches_mirror_impersonation_in_cite(client):
    run_pipeline(client)
    # 切点前查询应命旧段；把引用 hist 改成当前镜 hist_id → 属伪造
    r = query(client, STORE, BEFORE_CUT)
    rc = r.json()
    proof_id = rc["proof_id"]
    mirror = client.get("/api/current").json()
    mirror_hist = [m for m in mirror if m["store_id"] == STORE][0]["hist_id"]
    assert mirror_hist != rc["segment"]["hist_id"]

    conn = connect()
    conn.execute(
        "UPDATE as_of_hit_cites SET hist_id=? WHERE proof_id=?", (mirror_hist, proof_id)
    )
    conn.commit()
    conn.close()

    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "cite_hist_mismatch" in errs


def test_verify_catches_neighbor_as_of_cite_tamper(client):
    run_pipeline(client)
    r = query(client, STORE, BEFORE_CUT)
    proof_id = r.json()["proof_id"]

    conn = connect()
    conn.execute(
        "UPDATE as_of_hit_cites SET as_of_ts=? WHERE proof_id=?",
        (AFTER_CUT, proof_id),
    )
    conn.commit()
    conn.close()

    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "cite_neighbor_as_of" in errs

    main_rep = client.get("/api/as-of/verify").json()
    assert main_rep["failed"] == 1
    assert "cite_neighbor_as_of" in {
        e["error"] for e in main_rep["results"][0]["detail"]["errors"]
    }


def test_receipt_cite_stays_consistent_with_row(client):
    run_pipeline(client)
    r = query(client, STORE, AFTER_CUT)
    proof_id = r.json()["proof_id"]

    conn = connect()
    conn.execute("UPDATE as_of_hit_cites SET row_hash='tampered' WHERE proof_id=?", (proof_id,))
    conn.commit()
    conn.close()

    rep = client.get(f"/api/as-of/cite-verify?proof_id={proof_id}").json()
    assert rep["failed"] == 1
    errs = {e["error"] for e in rep["results"][0]["detail"]["errors"]}
    assert "cite_row_hash_mismatch" in errs
