import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import paimem_gateway as memory


class PaiMemGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledged_write_can_be_replayed_without_saving_twice(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        memory.setup(db)
        memory.queue(db, "turn-1", "window-A", "问题", "答复")
        original = db.execute("SELECT payload FROM paimem_outbox").fetchone()[0]
        sent = []

        def handle(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(memory, "headers", return_value={}):
                task = asyncio.create_task(memory.deliver(SimpleNamespace(state=SimpleNamespace(db=db, client=client))))
                try:
                    for _ in range(25):
                        if sent:
                            break
                        await asyncio.sleep(0.01)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        memory.queue(db, "turn-1", "window-A", "问题", "答复")
        self.assertEqual(sent, [json.loads(original)])
        self.assertEqual(db.execute("SELECT payload FROM paimem_outbox").fetchall(), [(None,)])

    async def test_query_uses_current_message_and_recent_dialogue(self):
        history = [("user", "过久的内容")] * 8 + [("assistant", "我们的称呼是小银帆"), ("user", "你今天又这么叫我了")]
        query = memory.query_for(history)
        self.assertIn("我们的称呼是小银帆", query)
        self.assertTrue(query.endswith("当前用户消息：你今天又这么叫我了"))
        self.assertEqual(len(query.splitlines()), 8)

    async def test_unavailable_store_returns_empty_context(self):
        def unavailable(request):
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            with patch.object(memory, "headers", return_value={}):
                self.assertIsNone(await memory.recall(client, [("user", "你好")]))


if __name__ == "__main__":
    unittest.main()
