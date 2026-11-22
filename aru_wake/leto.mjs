import { createHash, randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { deliverToPhone } from './transport.mjs';

// The upstream run() reuses its wake object on retries. Some notifications
// (for example friend requests) have no invocation ID and may repeat verbatim.
export function createDelivery(loadBundle, options, send = deliverToPhone) {
  const deliveries = new WeakMap();
  return async (_injector, wake) => {
    let id = deliveries.get(wake);
    if (!id) {
      // The upstream recovery notice uses the same server-generated ID line as
      // the live mention. Quoted group text can span several preceding lines.
      const calls = wake.reason === 'leto_mention'
        ? [...wake.message.matchAll(/^调用编号：(inv_[A-Za-z0-9_-]+)（北京时间 [0-9]{2}:[0-9]{2} 前有效）\r?$/gm)]
        : [];
      const invocation = calls.at(-1)?.[1];
      if (invocation) {
        const bytes = createHash('sha256').update('leto:invocation:' + invocation).digest();
        bytes[6] = (bytes[6] & 15) | 128;
        bytes[8] = (bytes[8] & 63) | 128;
        const hex = bytes.subarray(0, 16).toString('hex');
        id = [hex.slice(0,8), hex.slice(8,12), hex.slice(12,16), hex.slice(16,20), hex.slice(20)].join('-');
      } else {
        id = randomUUID();
      }
      deliveries.set(wake, id);
    }
    return send(wake.message, id, await loadBundle(), options);
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const { loadConfig, run } = await import('./upstream/bridge.mjs');
    // run() requires an injector name. Supply its in-process delivery hook so
    // retries of events without invocation IDs retain their Aru event identity.
    const config = loadConfig({ ...process.env, LETO_INJECTOR_EXECUTABLE: 'aru' });
    const loadBundle = async () => JSON.parse(await readFile('/etc/nian-aru-bridge/sender.json', 'utf8'));
    await run(config, {
      deliver: createDelivery(loadBundle, { spool: process.env.STATE_DIRECTORY }),
      log: line => console.log(line.replaceAll(config.token, '[redacted]')),
    });
  } catch (err) {
    const detail = String(err.message).replace(/lmt_[A-Za-z0-9_-]+/g, '[redacted]');
    console.error(detail);
    process.exitCode = 1;
  }
}
