// Shared $CLAWD balance primitives for the main server and the Discord bot.
//
// This module exists as a standalone file on purpose: the bot deploys on
// Railway with root directory discord-bot/, so anything the bot imports must
// live under discord-bot/src/ OR be vendored in — the bot vendors THIS file
// byte-for-byte at discord-bot/src/shared-clawd-balance.mjs (a test fails
// if the two copies drift). The main server keeps importing from here via
// mcp-public-gate.mjs (which re-exports these).

export const CLAWD_MINT = '8cHzQHUS2s2h8TzCmfqPKYiM4dSt4roa3n7MyRLApump';
// Token-2022 program id — note the real id ends …AhPNxuK1qEb (an earlier
// transcription of this constant read …EPFLC1PE6AhPNxuK1qKJh, a blend of the
// two program ids that matched nothing; verified on-chain 2026-11-08: the
// $CLAWD mint account's owner is TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb).
export const TOKEN_PROGRAMS = ['TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'];
const ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const HOLDER_TTL = 300;

// Total $CLAWD balance across the wallet's token accounts (SPL Token or
// Token-2022). Returns { amount: BigInt atomic units, uiAmount: number }.
// Throws on RPC misconfiguration/failure so callers can fail closed.
export async function clawdBalance(wallet, { rpcUrl, fetch: fetchImpl = fetch } = {}) {
  if (!ADDRESS.test(wallet || '')) return { amount: 0n, uiAmount: 0 };
  if (!rpcUrl) throw new Error('solana_rpc_not_configured');
  // A mint filter returns accounts from whichever program owns the mint (SPL Token or Token-2022).
  const res = await fetchImpl(rpcUrl, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({
    jsonrpc: '2.0', id: 1, method: 'getTokenAccountsByOwner', params: [wallet, { mint: CLAWD_MINT }, { encoding: 'jsonParsed', commitment: 'confirmed' }] }) });
  const body = await res.json();
  if (body?.error) throw new Error('rpc_error');
  let amount = 0n, uiAmount = 0;
  for (const a of body?.result?.value || []) {
    const owner = a?.account?.owner, t = a?.account?.data?.parsed?.info?.tokenAmount;
    if ((!owner || TOKEN_PROGRAMS.includes(owner)) && t && /^\d+$/.test(t.amount)) {
      amount += BigInt(t.amount);
      uiAmount += Number(t.uiAmount || 0);
    }
  }
  return { amount, uiAmount };
}

// Any nonzero $CLAWD under SPL Token or Token-2022. cache: optional {get(k), put(k, v, {expirationTtl})}.
export async function holdsClawd(wallet, { rpcUrl, fetch: fetchImpl = fetch, cache } = {}) {
  if (!ADDRESS.test(wallet || '')) return false;
  const key = 'clawd-holder:' + wallet;
  try { const hit = await cache?.get(key); if (hit === '1') return true; if (hit === '0') return false; } catch {}
  const { amount } = await clawdBalance(wallet, { rpcUrl, fetch: fetchImpl });
  const held = amount > 0n;
  try { await cache?.put(key, held ? '1' : '0', { expirationTtl: HOLDER_TTL }); } catch {}
  return held;
}
