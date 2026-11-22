import { createDecipheriv } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export async function recordReceipt(envelope, bundle, at = Date.now() / 1000) {
  const raw = Buffer.from(envelope.sealedPayload, 'base64');
  const dec = createDecipheriv('aes-256-gcm', Buffer.from(bundle.encryptionKey, 'base64'), raw.subarray(0,12));
  dec.setAuthTag(raw.subarray(-16));
  const payload = JSON.parse(Buffer.concat([dec.update(raw.subarray(12,-16)), dec.final()]));
  if (payload.triggerId !== bundle.triggerId || payload.eventId !== envelope.eventId || typeof payload.content !== 'string')
    throw new Error('unexpected sealed notification');
  const script = fileURLToPath(new URL('./prompt_audit.py', import.meta.url));
  await new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/python3', [script, '/var/lib/nian-aru-bridge/prompt-audit'],
                        {stdio: ['pipe', 'ignore', 'ignore']});
    child.once('error', reject);
    child.stdin.on('error', reject);
    child.once('close', code => code === 0 ? resolve() : reject(new Error('receipt audit unavailable')));
    child.stdin.end(JSON.stringify({id:payload.eventId, text:payload.content, at}));
  });
}
