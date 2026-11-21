STATUS = "partial"


def cites_of(conn, store_id: str | None = None) -> dict:
    # 属性命中主链可用；命中引用行未接
    return {
        "store_id": store_id,
        "cites": [],
        "hit_n": 0,
        "cite_ready": False,
    }


def attach_cite(result: dict) -> dict:
    out = dict(result or {})
    out["cite"] = None
    out["cite_ready"] = False
    return out
