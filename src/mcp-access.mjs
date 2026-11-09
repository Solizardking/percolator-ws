// Access helpers for /mcp. Public read tools are free; gateMcp (x402 / $CLAWD holder) applies only to tools a server
// explicitly marks protected. SIWS sessions authenticate MCP Events subscriptions.
// Shared pricing/402/holder logic lives in ./mcp-public-gate.mjs (same file as musebook-private cloudflare/).
import crypto from 'node:crypto';
import bs58 from 'bs58';
import { holdsClawd, paymentRequirements, paymentRequired, priceForCalls, settleX402, isFreeTool } from './mcp-public-gate.mjs';

export const CANONICAL_ORIGIN = 'https://percolator.tech';
export const RESOURCE = CANONICAL_ORIGIN + '/mcp';
const FACILITATOR = 'https://musebook.trade/api/x402';
const TOKEN_TTL = 3600, CHALLENGE_TTL = 600;
const ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const b64u = b => Buffer.from(b).toString('base64url');
// (the ed25519 SPKI prefix moved to ./shared-ed25519.mjs)

function secret(env) {
  if (typeof env.MCP_AUTH_SECRET !== 'string' || env.MCP_AUTH_SECRET.length < 24) throw new Error('MCP_AUTH_SECRET not configured');
  return env.MCP_AUTH_SECRET;
}
const mac = (env, s) => b64u(crypto.createHmac('sha256', secret(env)).update(s).digest());
const sealed = (env, obj) => { const p = b64u(JSON.stringify(obj)); return p + '.' + mac(env, p); };
function unseal(env, token) {
  const [p, s] = String(token || '').split('.');
  if (!p || !s) return null;
  const want = Buffer.from(mac(env, p)), got = Buffer.from(s);
  if (want.length !== got.length || !crypto.timingSafeEqual(want, got)) return null;
  try { return JSON.parse(Buffer.from(p, 'base64url').toString()); } catch { return null; }
}

// Stateless Sign-In With Solana. Challenge = HMAC-sealed {wallet, nonce, exp}; the message embeds it.
export function siwsChallenge(env, wallet, now = Date.now()) {
  if (!ADDRESS.test(wallet || '')) return { ok: false, error: 'invalid Solana wallet address' };
  const exp = Math.floor(now / 1000) + CHALLENGE_TTL;
  const nonce = sealed(env, { w: wallet, n: b64u(crypto.randomBytes(16)), exp, k: 'c' });
  const message = siwsChallengeMessage(wallet, nonce);
  return { ok: true, wallet, nonce, message, expiresAt: exp };
}

// verifyEd25519 lives in ./shared-ed25519.mjs (vendored by the Discord bot);
// re-exported here for existing importers.
import { verifyEd25519 } from './shared-ed25519.mjs';
export { verifyEd25519 };

const usedNonces = new Map();
export function siwsVerify(env, { wallet, nonce, signature } = {}, now = Date.now()) {
  const c = unseal(env, nonce);
  const t = Math.floor(now / 1000);
  if (!c || c.k !== 'c' || c.w !== wallet || c.exp < t) return { ok: false, error: 'challenge not found or expired' };
  for (const [n, e] of usedNonces) if (e < t) usedNonces.delete(n);
  if (usedNonces.has(nonce)) return { ok: false, error: 'challenge already used' };
  if (!verifyEd25519(wallet, siwsChallengeMessage(wallet, nonce), signature)) return { ok: false, error: 'signature verification failed' };
  usedNonces.set(nonce, c.exp);
  return { ok: true, wallet, token: sealed(env, { w: wallet, k: 's', exp: t + TOKEN_TTL }), expiresIn: TOKEN_TTL };
}
const siwsChallengeMessage = (wallet, nonce) => ['Percolator MCP: sign in with Solana', '', 'Sign to prove you control this wallet. It costs nothing and grants no on-chain authority.', '',
  `Wallet: ${wallet}`, 'Domain: percolator.tech', `Nonce: ${nonce}`].join('\n');

export function sessionWallet(env, authorization, now = Date.now()) {
  const m = /^Bearer\s+(\S+)$/i.exec(authorization || '');
  if (!m) return null;
  const s = unseal(env, m[1]);
  return s && s.k === 's' && s.exp > Math.floor(now / 1000) && ADDRESS.test(s.w) ? s.w : null;
}

// Principal for MCP Events: the SIWS session wallet plus the session's expiry (subscriptions never outlive it).
export function sessionPrincipal(env, authorization, now = Date.now()) {
  const m = /^Bearer\s+(\S+)$/i.exec(authorization || '');
  if (!m) return null;
  const s = unseal(env, m[1]);
  return s && s.k === 's' && s.exp > Math.floor(now / 1000) && ADDRESS.test(s.w) ? { principal: 'siws:' + s.w, wallet: s.w, authExp: s.exp * 1000 } : null;
}

const holderCache = new Map();
const memCache = { get: async k => { const v = holderCache.get(k); return v && v.e > Date.now() ? v.v : undefined; }, put: async (k, v, o) => { holderCache.set(k, { v, e: Date.now() + (o?.expirationTtl || 300) * 1000 }); } };

export function facilitatorDeps(env, fetchImpl = fetch) {
  const base = env.MCP_X402_FACILITATOR_URL || FACILITATOR;
  const post = async (path, body) => { const r = await fetchImpl(base + path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); return { status: r.status, body: await r.json().catch(() => ({})) }; };
  return {
    verify: async payment => (await post('/verify', payment)).body,
    settle: async payment => post('/settle', payment),
    signature: tx => { const b = Buffer.from(tx, 'base64'); if (!b.length || b[0] < 1 || b[0] > 127) throw new Error('bad tx'); return bs58.encode(b.subarray(1, 65)); },
    status: async sig => {
      if (!env.SOLANA_RPC_URL) throw new Error('SOLANA_RPC_URL not configured');
      const r = await fetchImpl(env.SOLANA_RPC_URL, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'getSignatureStatuses', params: [[sig], { searchTransactionHistory: true }] }) });
      return (await r.json())?.result?.value?.[0] || null;
    },
  };
}

export function wwwAuthenticate() { return `Bearer realm="percolator", resource_metadata="${CANONICAL_ORIGIN}/.well-known/oauth-protected-resource/mcp", scope="read"`; }

export function protectedResourceMetadata() {
  return { resource: RESOURCE, scopes_supported: ['read'], bearer_methods_supported: ['header'],
    authorization_servers: [], resource_documentation: CANONICAL_ORIGIN + '/mcp',
    'x-sign-in-with-solana': { challenge: CANONICAL_ORIGIN + '/auth/siws/challenge', verify: CANONICAL_ORIGIN + '/auth/siws/verify', purpose: 'Percolator MCP session ($CLAWD holders free)' },
    'x-access': {
      publicReadTools: 'free, no authentication',
      eventsSubscribe: 'requires a Sign-In With Solana session bearer token',
      walletTools: 'optional; when WALLET_TOOLS_ENABLED, requires SIWS bearer (not x402). Custodial OWS signer is separate infrastructure.',
    },
    'x-sign-in-with-solana-wallet': { challenge: CANONICAL_ORIGIN + '/auth/siws/challenge', verify: CANONICAL_ORIGIN + '/auth/siws/verify', purpose: 'Authenticated wallet/trade tools when enabled' },
  };
}

// Decide access for an MCP JSON-RPC message. Returns { allow: true, receipt?, via } or { allow: false, response }.
export async function gateMcp(env, headers, msg, { fetchImpl = fetch, resource = RESOURCE } = {}) {
  const msgs = Array.isArray(msg) ? msg : [msg];
  const called = msgs.filter(m => m?.method === 'tools/call').map(m => String(m?.params?.name || ''));
  if (!called.length) return { allow: true, via: 'public' };
  const toolNames = called.filter(n => !isFreeTool(n)); // free tools are never priced or holder-checked
  if (!toolNames.length) return { allow: true, via: 'free' };
  let wallet = null;
  try { wallet = sessionWallet(env, headers.authorization); } catch {}
  if (wallet) {
    try { if (await holdsClawd(wallet, { rpcUrl: env.SOLANA_RPC_URL, fetch: fetchImpl, cache: memCache })) return { allow: true, via: 'clawd_holder', wallet }; }
    catch { return { allow: false, response: new Response(JSON.stringify({ error: 'holder_check_unavailable' }), { status: 503, headers: { 'content-type': 'application/json' } }) }; }
  }
  const { total, top } = priceForCalls(toolNames, env);
  const requirements = { ...paymentRequirements(env, { toolName: top.name, resource }), maxAmountRequired: total };
  const opts = { wwwAuthenticate: wwwAuthenticate(), signIn: CANONICAL_ORIGIN + '/auth/siws/challenge', oauthMetadata: CANONICAL_ORIGIN + '/.well-known/oauth-protected-resource/mcp' };
  const header = headers['x-payment'];
  if (!header) return { allow: false, response: paymentRequired(requirements, opts) };
  const paid = await settleX402(header, requirements, env, facilitatorDeps(env, fetchImpl), opts);
  return paid.ok ? { allow: true, via: 'x402', receipt: paid.receipt, wallet: paid.payer } : { allow: false, response: paid.response };
}

export async function sendWebResponse(res, response, extraHeaders = {}) {
  const headers = { ...extraHeaders };
  response.headers.forEach((v, k) => { headers[k] = v; });
  res.writeHead(response.status, headers);
  res.end(Buffer.from(await response.arrayBuffer()));
}
