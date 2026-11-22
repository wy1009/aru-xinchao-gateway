import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const url = pathToFileURL(resolve(process.argv[2], 'xinchao/src/mcp-protocol.js'));
for (const inSystem of [true, false]) {
  process.env.XINCHAO_MCP_STATE_IN_SYSTEM = inSystem ? '1' : '0';
  const { handleMcpMessage } = await import(url.href + '?system=' + inSystem);
  let reads = 0;
  const content = '这封信最后一句是我的原话。\n此刻：作者自己的措辞';
  const handlers = {
    nowLine: async () => { reads++; return '此刻：测试状态'; },
    cabinInbox: async () => ({ letters: [{ createdAt: 'T', content }], lockedCount: 1, lockedLatestAt: 'T' }),
    cabinNote: async () => ({ note: { id: 'demo' } }),
    boardRead: async () => ({ ok: true, messages: [] }),
  };
  async function request(method, params) {
    const reply = await handleMcpMessage({ jsonrpc: '2.0', id: 1, method, params }, handlers);
    assert.equal(reply.status, 200);
    return reply.body.result;
  }
  const { instructions } = await request('initialize', {});
  assert.equal(instructions.includes('每个工具回应末尾'), !inSystem);
  for (const name of ['xinchao_cabin_inbox', 'xinchao_cabin_note', 'board_read']) {
    const result = await request('tools/call', { name, arguments: { event_id: 'test-note', content: '测试' } });
    assert.equal(result.isError, false, name + " " + result.content[0].text);
    const text = result.content[0].text;
    assert.equal(text.endsWith('\n\n此刻：测试状态'), !inSystem && name.startsWith('xinchao_'));
    if (name === 'xinchao_cabin_inbox') {
      assert.ok(text.includes(content));
      assert.ok(text.includes('1 封上锁的信'));
    }
  }
  assert.equal(reads, inSystem ? 0 : 2);
}
console.log('MCP state routing passed for gateway and standalone modes');
