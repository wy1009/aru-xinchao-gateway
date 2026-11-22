import { createHash, randomUUID } from 'node:crypto';
import { open, readFile, link, unlink } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';
import { parseRuntimeEnvelope } from './runtime/src/protocol.js';
import { renderText } from './runtime/examples/render.mjs';
import { sealWakeEvent } from './aru/wake-send.mjs';

const digest = (value, format = 'hex') => createHash('sha256').update(value).digest(format);

function receipt(query) {
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

function aruNotificationText(delivery) {
  if (delivery.reason !== 'self_signal') {
    return renderText(delivery).replace('】', '｜心潮 MCP】');
  }
  return '【此刻，' + (process.env.ARU_COLLABORATOR_NAME || '小禾') + '的状态】\n' + delivery.message;
}

async function savedEnvelope(file, bundle, delivery, eventId, target) {
  const load = async () => {
    const saved = JSON.parse(await readFile(file, 'utf8'));
    if (saved.target !== target || saved.envelope?.eventId !== eventId)
      throw new Error('delivery already bound to another target');
    return saved.envelope;
  };
  try { return await load(); } catch (err) { if (err.code !== 'ENOENT') throw err; }
  const envelope = sealWakeEvent(bundle, aruNotificationText(delivery), eventId);
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

export async function deliver(input, bundle, options = {}) {
  const acceptSelfSignals = /^(1|true|yes|on)$/i.test(
    (process.env.XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS ?? '').trim()
  );
  const delivery = parseRuntimeEnvelope(input, null, { acceptSelfSignals });
  const base = new URL(bundle.submitURL);
  const endpoint = /^\/aru\/v1\/wake-bridge\/endpoints\/([A-Za-z0-9_.:-]{1,128})\/events$/.exec(base.pathname)?.[1];
  if (base.protocol !== 'https:' || base.origin !== process.env.ARU_PUBLIC_ORIGIN || !endpoint || base.search || base.hash || base.username || base.password)
    throw new Error('unexpected destination');
  const eventId = /^delivery-([a-f0-9-]{36})$/i.exec(delivery.deliveryId)?.[1];
  if (!eventId) throw new Error('unexpected delivery identity');
  const target = digest(JSON.stringify([endpoint, bundle.triggerId, bundle.encryptionKey]));
  const query = {endpoint, event:eventId, tokenHash:digest(bundle.submitToken,'base64url')};
  const check = options.receipt ?? receipt;
  const file = join(options.spool ?? '/var/lib/nian-aru-bridge', eventId + '.json');
  const envelope = await savedEnvelope(file, bundle, delivery, eventId, target);
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

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const data = JSON.parse(Buffer.concat(chunks).toString());
    const bundle = JSON.parse(await readFile('/etc/nian-aru-bridge/sender.json','utf8'));
    const received = await deliver(data, bundle);
    try {
      const { recordReceipt } = await import('./receipt_audit.mjs');
      await recordReceipt(received, bundle);
    } catch {
      console.error('Aru receipt audit unavailable');
    }
  } catch (err) {
    const known = ['waiting for phone receipt','registered endpoint or receipt unavailable','Host receipt mismatch','delivery already bound to another target'];
    console.error(known.includes(err.message) ? err.message : 'Aru delivery failed');
    process.exitCode = 1;
  }
}
