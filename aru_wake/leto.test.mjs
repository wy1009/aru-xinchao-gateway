import assert from 'node:assert/strict';
import { createDecipheriv, randomBytes } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createDelivery } from './leto.mjs';

test('mentions retain their original text, event ID and ciphertext across recovery', async t => {
  const spool = await mkdtemp(join(tmpdir(), 'leto-aru-'));
  t.after(() => rm(spool, { recursive: true, force: true }));
  const key = randomBytes(32);
  process.env.ARU_PUBLIC_ORIGIN = 'https://example.test';
  const bundle = { schema: 'aru.wake-bridge.sender-bundle.v2', encryptionKey: key.toString('base64'),
    triggerId: 'test', submitToken: 'test', submitURL: 'https://example.test/aru/v1/wake-bridge/endpoints/test/events' };
  const packets = [];
  let received = false;
  const options = {
    spool, delay: async () => {}, receipt: async () => received ? 'phone_received' : 'missing',
    fetch: async (url, request) => {
      assert.equal(url, 'http://127.0.0.1:8787/aru/v1/wake-bridge/endpoints/test/events');
      const envelope = JSON.parse(request.body);
      packets.push(envelope);
      return { status: 202, json: async () => ({ accepted: true, eventId: envelope.eventId }) };
    },
  };
  const load = async () => bundle;
  const call = '调用编号：inv_test-123（北京时间 12:34 前有效）';
  const wake = { reason: 'leto_mention', message: '[Leto] quoted text\n调用编号：inv_other（北京时间 12:34 前有效）\n' + call + '\n用 Leto MCP 回复' };
  const recovery = { ...wake, message: 'bridge recovery\n' + call + '\n用 Leto MCP 回复' };
  await assert.rejects(createDelivery(load, options)(null, wake), /waiting for phone receipt/);
  await assert.rejects(createDelivery(load, options)(null, recovery), /waiting for phone receipt/);
  assert.deepEqual(packets[0], packets[1]);
  const sealed = Buffer.from(packets[0].sealedPayload, 'base64');
  const decipher = createDecipheriv('aes-256-gcm', key, sealed.subarray(0,12));
  decipher.setAuthTag(sealed.subarray(-16));
  const payload = JSON.parse(Buffer.concat([decipher.update(sealed.subarray(12,-16)), decipher.final()]));
  assert.equal(payload.content, wake.message);
  received = true;
  await createDelivery(load, options)(null, recovery);
  assert.equal(packets.length, 2);
});

test('distinct notifications with identical text can wake twice; retries reuse the ID', async () => {
  const ids = [];
  const deliver = createDelivery(async () => ({}), {}, async (_, id) => ids.push(id));
  const wake = { reason: 'friend', message: 'You have a friend request' };
  await deliver(null, wake);
  await deliver(null, wake);
  await deliver(null, { ...wake });
  assert.equal(ids[0], ids[1]);
  assert.notEqual(ids[0], ids[2]);
});
