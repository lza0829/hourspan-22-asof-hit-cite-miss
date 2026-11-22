export async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

export function asofHitCites(storeId, proofId) {
  const params = new URLSearchParams();
  if (storeId) params.set("store_id", storeId);
  if (proofId != null) params.set("proof_id", proofId);
  const q = params.toString();
  return api(`/api/as-of/hit-cites${q ? `?${q}` : ""}`);
}

export function asofCiteVerify(proofId) {
  const q = proofId != null ? `?proof_id=${encodeURIComponent(proofId)}` : "";
  return api(`/api/as-of/cite-verify${q}`);
}
