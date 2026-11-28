import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

import gateway
import hindsight_gateway as hindsight
from aru_prompt_log import FEEDS, compact_log, split_context
from prompt_audit import readable_content


class HindsightGatewayTest(unittest.TestCase):
    def test_outbound_chat_and_compact_log(self):
        user = "主人还记得第一次散步吗？"
        content = [{"type": "text", "text": user + " <sent_at 2026-11-28 12:15>"},
                   {"type": "image_url", "image_url": {"url": "https://example.test/dog"}}]
        messages = [{"role": "assistant", "content": "unrelated previous turn"},
                    {"role": "user", "content": content}]
        rows = [{"text": "first note", "type": "observation", "mentioned_at": "2026-11-28"},
                {"text": "a raw fact", "type": "world"},
                {"text": "second note", "type": "observation", "document_id": "walks"}]
        calls = []

        def dispatch(req):
            calls.append(str(req.url))
            body = json.loads(req.content)
            if str(req.url) == hindsight.RECALL_URL:
                self.assertEqual(body, {"query": user, "types": ["observation"], "budget": "mid",
                                        "max_tokens": 1024, "include": {"entities": None}})
                self.assertEqual(req.headers["Authorization"], "Bearer test-bank")
                return httpx.Response(200, json={"results": rows, "chunks": {"raw": "raw dialogue"}})
            self.assertEqual(str(req.url), gateway.CHAT + "/chat/completions")
            sent = body["messages"]
            self.assertEqual(sent[0], messages[0])
            if len(calls) == 2:
                self.assertEqual(sent[1]["content"][1:], content)
                block, original = split_context(readable_content(sent[1]))
                self.assertIn("- first note [observation] (2026-11-28)\n\n- second note [observation] [doc:walks]", block)
                self.assertIn(user, original)
                self.assertNotIn("raw", block)
            else:
                self.assertEqual(sent[1]["content"], "好 <sent_at 2026-11-28 12:19>")
            return httpx.Response(200, json={"choices": []})

        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {
            "CREDENTIALS_DIRECTORY": path, "XINCHAO_CHAT_API_KEY": "test",
            "NIAN_CLIENT_SHA256": hashlib.sha256(b"test").hexdigest(),
        }), patch.object(gateway, "DB", Path(path) / "queue.db"), patch.object(
            gateway, "MIND_ENABLED", False
        ), patch.object(gateway, "CHAT_MEMORY_IMPORT", False), patch.object(
            hindsight, "ENABLED", True
        ), patch.object(gateway.serein, "ENABLED", True), patch.object(gateway.serein, "LIVE_CAPTURE", True):
            (Path(path) / "hindsight-key").write_text("test-bank")
            audit = Path(path) / "prompt-audit"
            audit.mkdir()
            with sqlite3.connect(audit / "records.db") as db:
                db.execute("CREATE TABLE audit(id TEXT PRIMARY KEY, at REAL, status TEXT, entries BLOB)")
            with TestClient(gateway.app) as client:
                transport = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
                with patch.object(gateway.app.state, "client", transport):
                    for question in (content, "好 <sent_at 2026-11-28 12:19>"):
                        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer test"},
                                               json={"model": gateway.MODEL, "messages": [
                                                   messages[0], {"role": "user", "content": question}]})
                        self.assertEqual(response.status_code, 200)
                client.portal.call(transport.aclose)
            with patch.dict(FEEDS, gateway=("test", str(audit))):
                from prompt_audit import page_records
                records = page_records(audit)
                self.assertEqual(len(records), 2)
                report = compact_log(records[-1][0]).body.decode()
                self.assertIn("Hindsight 注入 2 条观察", report)
                self.assertEqual(report.count("first note"), 1)
                self.assertNotIn("<details open", report)
        self.assertEqual(len(calls), 3)


class HindsightRecallTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_results_and_failed_recall_allow_chat(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, CREDENTIALS_DIRECTORY=path):
            (Path(path) / "hindsight-key").write_text("test")
            for response in (httpx.Response(200, json={"results": []}), httpx.Response(503)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response)) as client:
                    block, status = await hindsight.recall(client, "主人还记得吗？")
                    self.assertEqual(block, "")
                    self.assertIn("0 条" if response.is_success else "失败", status)
