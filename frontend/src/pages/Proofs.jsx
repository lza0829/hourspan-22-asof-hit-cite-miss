import React, { useEffect, useState } from "react";
import { api, asofHitCites, asofCiteVerify } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [cites, setCites] = useState(null);
  const [citeVerify, setCiteVerify] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () => {
    api("/api/proofs").then(setRows).catch((e) => setErr(String(e.message || e)));
    Promise.resolve(asofHitCites()).then(setCites).catch(() => setCites(null));
  };

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const runCiteVerify = async () => {
    setErr("");
    try {
      setCiteVerify(await asofCiteVerify());
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；失败须可观测。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建核证</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      
      <div className="panel">
        <h2>as-of 命中引用</h2>
        <div className="actions">
          <button className="secondary" onClick={runCiteVerify}>核证命中引用（只读）</button>
          <button className="secondary" onClick={() => Promise.resolve(asofHitCites()).then(setCites)}>刷新引用</button>
          {citeVerify && (
            <span className={`badge ${citeVerify.failed ? "bad" : "ok"}`}>
              核证 {citeVerify.checked} 条 · 失败 {citeVerify.failed} · 缺引用 {citeVerify.missing}
            </span>
          )}
        </div>
        {citeVerify && citeVerify.failed > 0 && (
          <pre className="msg">
            {JSON.stringify(citeVerify.results.filter((r) => !r.valid), null, 2)}
          </pre>
        )}
        {cites ? (
          <>
            <p className="muted">
              共 {cites.total} 条（命中 {cites.hit_n} · 未命中 {cites.miss_n}）· 源 {cites.source} ·
              cite_ready={String(cites.cite_ready)}
            </p>
            <table>
              <thead>
                <tr>
                  <th>id</th><th>proof_id</th><th>门店</th><th>as_of</th>
                  <th>命中</th><th>hist_id</th><th>valid 区间</th><th>属性 hash</th>
                </tr>
              </thead>
              <tbody>
                {(cites.cites || []).map((c) => (
                  <tr key={c.cite_id}>
                    <td>{c.cite_id}</td>
                    <td>{c.proof_id}</td>
                    <td>{c.store_id}</td>
                    <td className="mono">{c.as_of_ts}</td>
                    <td>
                      {c.hit
                        ? <span className="badge ok">命中</span>
                        : <span className="badge bad">未命中 · {c.miss_reason}</span>}
                    </td>
                    <td className="mono">{c.hist_id || "—"}</td>
                    <td className="mono">{c.hit ? `${c.valid_from} ~ ${c.valid_to || "∞"}` : "—"}</td>
                    <td className="mono">{c.row_hash || "—"}</td>
                  </tr>
                ))}
                {(cites.cites || []).length === 0 && (
                  <tr><td colSpan={8} className="muted">尚无命中引用——先做一次时点查询。</td></tr>
                )}
              </tbody>
            </table>
          </>
        ) : (
          <p className="muted">（空）</p>
        )}
      </div>
      <div className="panel">
        <h2>scd_proofs</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
