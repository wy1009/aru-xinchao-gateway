import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { parseRuntimeEnvelope } from './runtime/src/protocol.js';
import { aruNotificationText } from './notification.mjs';
import { deliverToPhone } from './transport.mjs';

export async function deliver(input, bundle, options = {}) {
  const acceptSelfSignals = /^(1|true|yes|on)$/i.test(
    (process.env.XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS ?? '').trim()
  );
  const delivery = parseRuntimeEnvelope(input, null, { acceptSelfSignals });
  const eventId = /^delivery-([a-f0-9-]{36})$/i.exec(delivery.deliveryId)?.[1];
  if (!eventId) throw new Error('unexpected delivery identity');
  return deliverToPhone(aruNotificationText(delivery), eventId, bundle, {
    spool: '/var/lib/nian-aru-bridge', ...options,
  });
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
