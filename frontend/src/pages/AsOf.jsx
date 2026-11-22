import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, asofHitCites } from "../api.js";

export default function AsOf() {
  const [stores, setStores] = useState([]);
  const [storeId, setStoreId] = useState("");
  const [asOfTs, setAsOfTs] = useState("");
  const [histRows, setHistRows] = useState([]);
  const [receipt, setReceipt] = useState(null);
  const [proofs, setProofs] = useState([]);
  const [verify, setVerify] = useState(null);
  const [citeVerify, setCiteVerify] = useState(null);
  const [drill, setDrill] = useState({});
  const [drillErr, setDrillErr] = useState({});
  const [err, setErr] = useState("");
  const [searchParams] = useSearchParams();

  const reloadProofs = () =>
    api("/api/as-of/proofs").then(setProofs).catch((e) => setErr(String(e.message || e)));

  useEffect(() => {
    api("/api/stores").then(setStores).catch((e) => setErr(String(e.message || e)));
    reloadProofs();
  }, []);

  const runQuery = async (sid, ts) => {
    setErr("");
    setReceipt(null);
    try {
      const r = await api("/api/as-of/query", {
        method: "POST",
        body: JSON.stringify({ store_id: sid, as_of_ts: ts }),
      });
      setReceipt(r);
      await reloadProofs();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  // 历史维 → 时点投影 协同：带参进入时预填并点查一次
  useEffect(() => {
    const sid = searchParams.get("store_id");
    const ts = searchParams.get("as_of_ts");
    if (sid && ts) {
      setStoreId(sid);
      setAsOfTs(ts);
      runQuery(sid, ts);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // 历史维扫描协同：选中门店即列出其 hist 段，可一键取段首时点
  useEffect(() => {
    if (!storeId) {
      setHistRows([]);
      return;
    }
    api(`/api/history?store_id=${encodeURIComponent(storeId)}`)
      .then(setHistRows)
      .catch(() => setHistRows([]));
  }, [storeId]);

  const drillCite = async (proofId) => {
    setDrillErr((s) => ({ ...s, [proofId]: "" }));
    try {
      const r = await asofHitCites(null, proofId);
      setDrill((s) => ({ ...s, [proofId]: r }));
    } catch (e) {
      setDrillErr((s) => ({ ...s, [proofId]: String(e.message || e) }));
    }
  };

  const runVerify = async () => {
    setErr("");
    try {
      setVerify(await api("/api/as-of/verify"));
      setCiteVerify(await api("/api/as-of/cite-verify"));
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const verdict = {};
  if (verify) {
    for (const r of verify.results) verdict[r.proof_id] = r;
  }
  const citeVerdict = {};
  if (citeVerify) {
    for (const r of citeVerify.results) citeVerdict[r.proof_id] = r;
  }

  return (
    <>
      <div data-plant="22-asof-hit-cite-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>时点投影</h1>
          <p>store_id + as_of_ts → 落在 valid_from≤t&lt;valid_to 的 hist 段；只读历史维，未命中明确标记，禁止当前镜顶替。</p>
        </div>
      </div>

      <div className="panel">
        <h2>点查（落证明 · 形态甲）</h2>
        <div className="actions">
          <select
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            style={{ minWidth: 200, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          >
            <option value="">选门店…</option>
            {stores.map((s) => (
              <option key={s.id} value={s.id}>{s.id} · {s.name}</option>
            ))}
          </select>
          <input
            value={asOfTs}
            onChange={(e) => setAsOfTs(e.target.value)}
            placeholder="as_of_ts，如 2026-11-10T00:00:00Z"
            style={{ minWidth: 260, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={() => runQuery(storeId, asOfTs)} disabled={!storeId || !asOfTs.trim()}>
            时点查询
          </button>
        </div>
        {histRows.length > 0 && (
          <div className="actions" style={{ alignItems: "center" }}>
            <span className="muted">hist 段快捷取点：</span>
            {histRows.map((h) => (
              <button
                key={h.id}
                className="secondary"
                title={`${h.id} [${h.valid_from} ~ ${h.valid_to || "∞"})`}
                onClick={() => setAsOfTs(h.valid_from)}
              >
                {h.valid_from.slice(0, 10)} 段
              </button>
            ))}
            <button className="secondary" onClick={() => setAsOfTs("2026-10-01T00:00:00Z")}>
              覆盖外（未命中演示）
            </button>
          </div>
        )}
        {err && <p className="error">{err}</p>}
      </div>

      {receipt && (
        <div className="panel">
          <h2>
            回执&nbsp;
            <span className={`badge ${receipt.hit ? "ok" : "bad"}`}>
              {receipt.hit ? "命中" : `未命中 · ${receipt.miss_reason}`}
            </span>
          </h2>
          <p className="muted">
            查询键 <span className="mono">{receipt.query.query_key}</span> · 证明 #{receipt.proof_id} 已同事务落库 ·
            源 {receipt.source} · mirror_used={String(receipt.mirror_used)}
          </p>
          {receipt.segment ? (
            <table>
              <thead>
                <tr>
                  <th>hist_id</th><th>valid_from</th><th>valid_to</th>
                  <th>开-闭</th><th>mask</th><th>状态</th><th>属性 hash</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="mono">{receipt.segment.hist_id}</td>
                  <td>{receipt.segment.valid_from}</td>
                  <td>{receipt.segment.valid_to || "—"}</td>
                  <td>{receipt.segment.open_time}–{receipt.segment.close_time}</td>
                  <td className="mono">{receipt.segment.weekday_mask}</td>
                  <td><span className={`badge ${receipt.segment.biz_status}`}>{receipt.segment.biz_status}</span></td>
                  <td className="mono">{receipt.segment.row_hash}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">该时点无 hist 段覆盖——明确未命中，未用当前镜顶替。</p>
          )}
          <h2 style={{ marginTop: "1rem" }}>
            命中引用&nbsp;
            <span className={`badge ${receipt.cite_ready ? "ok" : "bad"}`}>
              {receipt.cite_ready ? "齐套" : "缺失"}
            </span>
          </h2>
          {receipt.cite ? (
            <table>
              <thead>
                <tr>
                  <th>cite_id</th><th>proof_id</th><th>门店</th><th>as_of</th>
                  <th>hist_id</th><th>valid 区间</th><th>属性 hash</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{receipt.cite.cite_id}</td>
                  <td>{receipt.cite.proof_id}</td>
                  <td>{receipt.cite.store_id}</td>
                  <td className="mono">{receipt.cite.as_of_ts}</td>
                  <td className="mono">{receipt.cite.hist_id || "—"}</td>
                  <td className="mono">{receipt.cite.hit ? `${receipt.cite.valid_from} ~ ${receipt.cite.valid_to || "∞"}` : "—"}</td>
                  <td className="mono">{receipt.cite.row_hash || "—"}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="error">引用缺失——甲口径下成功体不得无引用。</p>
          )}
        </div>
      )}

      <div className="panel">
        <div className="actions">
          <button onClick={runVerify}>核证全部证明（只读）</button>
          <button className="secondary" onClick={reloadProofs}>刷新证明</button>
          {verify && (
            <span className={`badge ${verify.failed ? "bad" : "ok"}`}>
              核证 {verify.checked} 条 · 失败 {verify.failed}
            </span>
          )}
          {citeVerify && (
            <span className={`badge ${citeVerify.failed ? "bad" : "ok"}`}>
              引用核证 {citeVerify.checked} 条 · 失败 {citeVerify.failed} · 缺引用 {citeVerify.missing}
            </span>
          )}
        </div>
        {verify && verify.failed > 0 && (
          <pre className="msg">
            {JSON.stringify(verify.results.filter((r) => !r.valid), null, 2)}
          </pre>
        )}
        <h2>as_of_proofs（命中 hist_id / valid 区间 / 属性 hash / 未命中标记）</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>查询键</th><th>命中</th><th>hist_id</th>
              <th>valid 区间</th><th>属性 hash</th><th>核证</th><th>引用核证</th><th>引用</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {proofs.map((p) => (
              <tr key={p.id}>
                <td>{p.id}</td>
                <td className="mono">{p.query_key}</td>
                <td>
                  {p.hit
                    ? <span className="badge ok">命中</span>
                    : <span className="badge bad">未命中 · {p.miss_reason}</span>}
                </td>
                <td className="mono">{p.hist_id || "—"}</td>
                <td className="mono">{p.hit ? `${p.valid_from} ~ ${p.valid_to || "∞"}` : "—"}</td>
                <td className="mono">{p.row_hash || "—"}</td>
                <td>
                  {verdict[p.id] && (
                    <span
                      className={`badge ${verdict[p.id].valid ? "ok" : "bad"}`}
                      title={JSON.stringify(verdict[p.id].detail)}
                    >
                      {verdict[p.id].valid ? "通过" : "失败"}
                    </span>
                  )}
                </td>
                <td>
                  {citeVerdict[p.id] && (
                    <span
                      className={`badge ${citeVerdict[p.id].valid ? "ok" : "bad"}`}
                      title={JSON.stringify(citeVerdict[p.id].detail)}
                    >
                      {citeVerdict[p.id].valid
                        ? "齐套"
                        : citeVerdict[p.id].cite_present ? "分叉" : "缺引用"}
                    </span>
                  )}
                </td>
                <td>
                  <button className="secondary" onClick={() => drillCite(p.id)}>下钻</button>
                  {drillErr[p.id] && <span className="error">{drillErr[p.id]}</span>}
                  {drill[p.id] && (
                    drill[p.id].total === 0
                      ? <p className="error" style={{ margin: "0.3rem 0" }}>引用缺行——不得当通过</p>
                      : drill[p.id].cites.map((c) => (
                          <p key={c.cite_id} className="mono" style={{ margin: "0.3rem 0" }}>
                            #{c.cite_id} · {c.hist_id || "未命中"} · {c.hit ? `${c.valid_from}~${c.valid_to || "∞"}` : c.miss_reason} · {c.row_hash || "—"}
                          </p>
                        ))
                  )}
                </td>
                <td>{p.created_at}</td>
              </tr>
            ))}
            {proofs.length === 0 && (
              <tr><td colSpan={10} className="muted">尚无证明——先做一次时点查询。</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
