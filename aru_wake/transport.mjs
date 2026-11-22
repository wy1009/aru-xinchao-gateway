import { createHash, randomUUID } from 'node:crypto';
import { open, readFile, link, unlink } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { sealWakeEvent } from './aru/wake-send.mjs';

const digest = (value, format = 'hex') => createHash('sha256').update(value).digest(format);

export function receipt(query) {
  return new Promise((resolve, reject) => {
    const sock = createConnection('/run/nian-aru-receipt.sock');
    let reply = '';
    sock.setTimeout(4500, () => sock.destroy(new Error('receipt timed out')));
    sock.once('connect', () => sock.write(JSON.stringify(query) + '\n'));
    sock.setEncoding('utf8');
    sock.on('data', part => { reply += part; });
    sock.once('error', reject);
    sock.once('end', () => resolve(reply.trim()));
  });
}

async function savedEnvelope(file, bundle, content, eventId, target) {
  const load = async () => {
    const saved = JSON.parse(await readFile(file, 'utf8'));
    if (saved.target !== target || saved.envelope?.eventId !== eventId)
      throw new Error('delivery already bound to another target');
    return saved.envelope;
  };
  try { return await load(); } catch (err) { if (err.code !== 'ENOENT') throw err; }
  const envelope = sealWakeEvent(bundle, content, eventId);
  const tmp = file + '.' + randomUUID();
  const handle = await open(tmp, 'wx', 0o600);
  try {
    await handle.writeFile(JSON.stringify({target, envelope}));
    await handle.sync();
    try { await link(tmp, file); } catch (err) { if (err.code !== 'EEXIST') throw err; }
    // Ensure the idempotency record survives power loss before the first network send.
    const dir = await open(join(file, '..'), 'r');
    try { await dir.sync(); } finally { await dir.close(); }
  } finally { await handle.close(); await unlink(tmp); }
  return load();
}

export async function deliverToPhone(content, eventId, bundle, options) {
  const base = new URL(bundle.submitURL);
  const endpoint = /^\/aru\/v1\/wake-bridge\/endpoints\/([A-Za-z0-9_.:-]{1,128})\/events$/.exec(base.pathname)?.[1];
  if (base.protocol !== 'https:' || base.origin !== process.env.ARU_PUBLIC_ORIGIN || !endpoint || base.search || base.hash || base.username || base.password)
    throw new Error('unexpected destination');
  const target = digest(JSON.stringify([endpoint, bundle.triggerId, bundle.encryptionKey]));
  const query = {endpoint, event:eventId, tokenHash:digest(bundle.submitToken,'base64url')};
  const check = options.receipt ?? receipt;
  const file = join(options.spool, eventId + '.json');
  const envelope = await savedEnvelope(file, bundle, content, eventId, target);
  let state = await check(query);
  if (state === 'phone_received') return envelope;
  if (state === 'missing' || state === 'retry_relay') {
    const response = await (options.fetch ?? fetch)('http://127.0.0.1:8787' + base.pathname, {
      method:'POST', redirect:'error', signal:AbortSignal.timeout(4500),
      headers:{authorization:'Bearer ' + bundle.submitToken, 'content-type':'application/json'},
      body:JSON.stringify(envelope),
    });
    if (response.status !== 202) throw new Error('Host admission failed: HTTP ' + response.status);
    const result = await response.json();
    if (result.accepted !== true || result.eventId !== eventId) throw new Error('Host receipt mismatch');
  } else if (state !== 'at_host') throw new Error('registered endpoint or receipt unavailable');
  for (let i=0; i<4; i++) {
    await (options.delay ?? delay)(1500);
    if (await check(query) === 'phone_received') return envelope;
  }
  throw new Error('waiting for phone receipt');
}
