// ed25519 signature verification for Solana wallets (bs58 public key,
// message, signature in base64 or bs58). Vendored by the Discord bot at
// discord-bot/src/shared-ed25519.mjs — the bot deploys standalone from
// discord-bot/, so anything it imports must live under discord-bot/src/.
// Keep the two copies byte-identical (a test guards the drift).
import crypto from 'node:crypto';
import bs58 from 'bs58';

// DER prefix for an ed25519 SubjectPublicKeyInfo wrapping a raw 32-byte key.
const SPKI = Buffer.from('302a300506032b6570032100', 'hex');

export function verifyEd25519(wallet, message, signature) {
  let pub, sig;
  try { pub = bs58.decode(wallet); } catch { return false; }
  if (pub.length !== 32) return false;
  for (const dec of [s => Buffer.from(s, 'base64'), s => Buffer.from(bs58.decode(s))]) { try { const b = dec(signature); if (b.length === 64) { sig = b; break; } } catch {} }
  if (!sig) return false;
  const key = crypto.createPublicKey({ key: Buffer.concat([SPKI, Buffer.from(pub)]), format: 'der', type: 'spki' });
  return crypto.verify(null, Buffer.from(message), key, sig);
}
