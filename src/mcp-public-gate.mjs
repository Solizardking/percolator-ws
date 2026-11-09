// Shared public-access gate for paid MCP servers (musebook.trade/mcp-auth, stream.musebook.trade/mcp).
// (a) Solana x402 pay-per-call in USDC with tiered per-tool prices; (b) free for wallets holding any $CLAWD.
// Framework-agnostic: payment verify/settle/status and fetch are injected.

// $CLAWD balance primitives live in ./clawd-balance.mjs so the Discord bot
// can vendor the same file (the bot deploys standalone). Re-exported here
// for every existing importer.
import { CLAWD_MINT, TOKEN_PROGRAMS, clawdBalance, holdsClawd } from './clawd-balance.mjs';
export { CLAWD_MINT, TOKEN_PROGRAMS, clawdBalance, holdsClawd };
export const SOLANA_MAINNET = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp';
export const SOLANA_USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
export const DEFAULT_PAY_TO = '5rxgJXoN2xvoZHcLXav1pEHAhvygLziZsz3xAzNEJfgr';
// Atomic USDC (6 decimals).
export const TIER_PRICES = { read: '2000', live: '10000', heavy: '50000' }; // $0.002 / $0.01 / $0.05
export const DEFAULT_PRICE = TIER_PRICES.live; // unclassified tools: $0.01
const ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const ATOMIC = /^[1-9]\d{0,12}$/;

// Built-in classification. Override per deployment with env.MCP_X402_TOOL_TIERS (JSON {tool: "read"|"live"|"heavy"})
// and env.MCP_X402_TIER_PRICES (JSON {read,live,heavy} atomic USDC); env.MCP_X402_PRICE sets the unclassified default.
export const TOOL_TIERS = {
  heavy: ['market_signal', 'smart_money_flow', 'token_heat_map', 'solana_wallet_pnl', 'solana_top_traders', 'pump_token_scan',
    'scanner_check', 'scanner_holders', 'scanner_daily_picks', 'adam_eve_identity', 'openmarket_snapshot', 'massive_labor_market'],
  live: ['live_launches', 'stream_launches', 'site_launches', 'live_launch_cards', 'pump_live_events', 'pump_stream_snapshot',
    'pump_stream_status', 'pump_recent_launches', 'open_pump_stream', 'backpack_ticker', 'backpack_orderbook', 'backpack_trades',
    'backpack_klines', 'backpack_market_cards', 'backpack_market_snapshot', 'scanner_recent', 'scanner_chart', 'scanner_markets',
    'sol_price', 'solana_price', 'market_regime', 'solana_trending', 'solana_token_info', 'solana_search', 'solana_wallet_tokens',
    'pump_graduation', 'pump_sell_quote', 'pump_buy_quote', 'pump_market_cap', 'pump_top_tokens', 'pump_new_tokens',
    'helius_account_info', 'helius_balance', 'helius_priority_fee', 'stock_snapshot', 'stock_aggregates', 'stock_previous_close',
    'stock_ticker_news', 'stonk_tokens', 'stonk_token', 'stonk_launches', 'stonk_rewards', 'birdeye_token_overview',
    'clawd_holder_check', 'compute_unit_optimizer', 'render_launch_feed', 'post_to_feed', 'request_agent_action', 'work_claim', 'work_release', 'work_log'],
};

// Tools served free to everyone: no x402 payment and no $CLAWD holder check.
export const FREE_TOOLS = new Set(['get_health', 'percolator_list_markets', 'percolator_read_market', 'percolator_oracle_price', 'percolator_quote', 'percolator_detect_venue']);
export const isFreeTool = (name) => FREE_TOOLS.has(name);

function parseJson(value) { try { return value ? JSON.parse(value) : null; } catch { return null; } }

export function priceForTool(name, env = {}) {
  const prices = { ...TIER_PRICES };
  const custom = parseJson(env.MCP_X402_TIER_PRICES);
  if (custom) for (const k of Object.keys(prices)) if (ATOMIC.test(String(custom[k] ?? ''))) prices[k] = String(custom[k]);
  const tiers = parseJson(env.MCP_X402_TOOL_TIERS) || {};
  let tier = typeof tiers[name] === 'string' && prices[tiers[name]] ? tiers[name] : null;
  if (!tier) tier = TOOL_TIERS.heavy.includes(name) ? 'heavy' : TOOL_TIERS.live.includes(name) ? 'live' : null;
  if (!tier && name && /^(percolator_|get_|list_|search_|chess_|validate_|verify_|estimate_|paper_|directory_|x402|massive_|stock_ticker_details|stock_market_status|render_|open_|prompt_|leviathan_|wallet_|whoami|work_check|work_board|trending_|agent_feed|adam_eve_profiles|pumpstream_api|stonk_pairs|stonk_stats|backpack_markets|helius_listener|pump_cashback|get_pump)/.test(name)) tier = 'read';
  if (tier) return { tier, price: prices[tier] };
  return { tier: 'default', price: ATOMIC.test(String(env.MCP_X402_PRICE || '')) ? String(env.MCP_X402_PRICE) : DEFAULT_PRICE };
}

export function paymentRequirements(env = {}, { toolName, resource, description } = {}) {
  const payTo = ADDRESS.test(env.MCP_X402_PAYTO || '') ? env.MCP_X402_PAYTO : DEFAULT_PAY_TO;
  const { price, tier } = priceForTool(toolName, env);
  return { scheme: 'exact', network: SOLANA_MAINNET, asset: SOLANA_USDC, payTo, maxAmountRequired: price, maxTimeoutSeconds: 60,
    resource, mimeType: 'application/json', description: description || `MCP tools/call ${toolName || ''} (${tier} tier). Free for wallets holding any $CLAWD.`,
    extra: { tool: toolName || null, tier } };
}

export function paymentRequired(requirements, { wwwAuthenticate, error, signIn, oauthMetadata } = {}) {
  const challenge = { x402Version: 1, ...(error ? { error } : {}),
    accepts: [{ ...requirements, network: 'solana' }], resource: requirements.resource,
    alternatives: { clawdHolders: { mint: CLAWD_MINT, minimum: 'any nonzero amount', ...(signIn ? { signIn } : {}) }, ...(oauthMetadata ? { oauth: oauthMetadata } : {}) } };
  const headers = { 'content-type': 'application/json', 'cache-control': 'no-store', 'PAYMENT-REQUIRED': btoa(JSON.stringify(challenge)),
    'access-control-expose-headers': 'PAYMENT-REQUIRED, X-PAYMENT-RESPONSE, PAYMENT-RESPONSE, WWW-Authenticate' };
  if (wwwAuthenticate) headers['www-authenticate'] = wwwAuthenticate;
  return new Response(JSON.stringify(challenge), { status: 402, headers });
}

export function decodePayment(encoded, requirements) {
  if (typeof encoded !== 'string' || !encoded || encoded.length > 8192 || !BASE64.test(encoded)) throw new Error('invalid_payment');
  const env = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0))));
  const tx = env?.payload?.transaction;
  // The server owns recipient, mint, price and network; the client only supplies the signed transaction.
  if (env?.x402Version !== 1 || env.scheme !== requirements.scheme || ![requirements.network, 'solana'].includes(env.network)
    || env.paymentRequirements || env.accepted || typeof tx !== 'string' || !tx || tx.length > 4096 || !BASE64.test(tx)) throw new Error('invalid_payment');
  return tx;
}

// deps: { verify(payment, env), settle(payment, env), signature(txBase64), status(sig, env) }
export async function settleX402(headerValue, requirements, env, deps, opts = {}) {
  let paymentHeader;
  try { paymentHeader = decodePayment(headerValue, requirements); }
  catch { return { ok: false, response: paymentRequired(requirements, { ...opts, error: 'invalid_payment' }) }; }
  const payment = { paymentHeader, paymentRequirements: requirements };
  try {
    const verified = await deps.verify(payment, env);
    if (verified?.isValid !== true) return { ok: false, response: paymentRequired(requirements, { ...opts, error: verified?.invalidReason || 'invalid_payment' }) };
    const sig = deps.signature(paymentHeader);
    if (await deps.status(sig, env)) return { ok: false, response: paymentRequired(requirements, { ...opts, error: 'payment_already_used' }) };
    const settled = await deps.settle(payment, env);
    if (settled?.status !== 200 || settled.body?.success !== true || settled.body?.pending)
      return { ok: false, response: paymentRequired(requirements, { ...opts, error: settled?.body?.error || 'settlement_failed' }) };
    return { ok: true, receipt: settled.body, payer: verified.payer || null };
  } catch {
    return { ok: false, response: new Response(JSON.stringify({ error: 'solana_payment_unavailable' }), { status: 503, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } }) };
  }
}

export function withReceipt(response, receipt) {
  const encoded = btoa(JSON.stringify(receipt));
  const out = new Response(response.body, response);
  out.headers.set('X-PAYMENT-RESPONSE', encoded);
  out.headers.set('PAYMENT-RESPONSE', encoded);
  out.headers.append('access-control-expose-headers', 'X-PAYMENT-RESPONSE, PAYMENT-RESPONSE');
  return out;
}

// (clawdBalance / holdsClawd moved to ./clawd-balance.mjs — re-exported at
// the top of this file.)

// Reads the JSON-RPC body of an MCP POST without consuming the original request.
export async function mcpCallInfo(request) {
  if (request.method !== 'POST') return { method: null, toolNames: [] };
  try {
    const body = await request.clone().json();
    const msgs = Array.isArray(body) ? body : [body];
    const calls = msgs.filter(m => m?.method === 'tools/call');
    return { method: msgs.length === 1 ? msgs[0]?.method : 'batch', toolNames: calls.map(m => String(m?.params?.name || '')) };
  } catch { return { method: null, toolNames: [] }; }
}

export function priceForCalls(toolNames, env) {
  // Batch: charge the sum; single: tool price. Describe the most expensive tool.
  let total = 0n, top = null;
  for (const n of toolNames) { const p = priceForTool(n, env); total += BigInt(p.price); if (!top || BigInt(p.price) > BigInt(top.price)) top = { ...p, name: n }; }
  return { total: total.toString(), top };
}
