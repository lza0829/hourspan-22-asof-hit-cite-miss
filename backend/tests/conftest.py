import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 必须在 import app 之前落定环境（db.py 在 import 时读取）
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="hourspan-test-")
os.environ["FIXTURES_DIR"] = str(BACKEND_DIR.parent / "fixtures")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402

TABLES = [
    "as_of_proofs",
    "scd_proofs",
    "quarantine",
    "dim_store_hours_current",
    "dim_store_hours_hist",
    "detect_runs",
    "source_snapshots",
    "ingest_batches",
    "stores",
]


@pytest.fixture()
def client():
    """每用例一张干净库：建表后清表重播种，保证 proof 计数可断言。"""
    seed.init_db()  # 首用例先建表
    conn = connect()
    try:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    finally:
        conn.close()
    seed.init_db()  # stores 空 → 重播种
    with TestClient(app) as c:
        yield c


def run_pipeline(client):
    """入档变更夹具 → 检测 → Type2 应用：ST-EAST-01 形成两段历史（切点 2026-11-15）。"""
    r = client.post("/api/ingest/run", json={"fixture": "ingest_change_batch.json"})
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]
    r = client.post("/api/detect/run", json={"batch_id": batch_id})
    assert r.status_code == 200, r.text
    run_id = r.json()["detect_run_id"]
    r = client.post("/api/scd/apply", json={"detect_run_id": run_id})
    assert r.status_code == 200, r.text
    assert r.json()["ok"]


def hist_rows(client, store_id):
    r = client.get(f"/api/history?store_id={store_id}")
    assert r.status_code == 200, r.text
    return r.json()


def proof_count(store_id=None):
    conn = connect()
    try:
        if store_id:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM as_of_proofs WHERE store_id=?", (store_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS c FROM as_of_proofs").fetchone()
        return row["c"]
    finally:
        conn.close()
