import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

import aru_prompt_log
import gateway
from usage_ledger import UsageCall, counters, summary

MODEL = "deepseek-ai/DeepSeek-V3.2"
URL = "https://api.siliconflow.cn/v1"


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.directory = self.root / "prompt-audit"
        self.file = self.directory / "usage.sqlite"

    def meter(self, kind="chat"):
        return UsageCall(self.directory, kind, URL, MODEL)

    async def test_terminal_usage_is_counted_once_including_tool_calls(self):
        final = {"prompt_tokens": 100_000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 90_000}}
        chunks = [
            {"choices": [{"delta": {"content": "好"}}], "usage": {**final, "completion_tokens": 1}},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": final},
            {"choices": [], "usage": final},
        ]
        raw = "".join("data: " + json.dumps({"id": "provider-1", **item}) + "\n\n" for item in chunks)
        raw += "data: [DONE]\n\n"
        response = httpx.Response(200, content=raw.encode())
        wire = b"".join([part async for part in gateway.relay(response, Mock(), meter=self.meter())])
        self.assertEqual(wire, raw.encode())
        self.assertEqual(summary(self.file, 0, 1e12),
                         [("chat", 1, 1, 76_600_000, 90_000, 10_000, 100, 1, 1)])
        feeds = {"gateway": ("模型", str(self.directory))}
        with patch.object(aru_prompt_log, "FEEDS", feeds):
            page = await aru_prompt_log.prompt_log(SimpleNamespace(query_params={"feed": "usage"}))
        self.assertIn("¥0.076600", page.body.decode())
        self.assertIn("90.0%", page.body.decode())

    async def test_interrupted_call_and_idempotent_provider_replay(self):
        partial = {"id": "p", "choices": [{"delta": {"content": "a"}}],
                   "usage": {"prompt_tokens": 20, "prompt_cache_hit_tokens": 10, "completion_tokens": 1}}
        raw = "data: " + json.dumps(partial) + "\n\n"
        source = gateway.relay(httpx.Response(200, content=raw.encode()), Mock(), meter=self.meter())
        async for part in source:
            if not part.strip():
                break
        await source.aclose()
        self.assertEqual(summary(self.file, 0, 1e12), [("chat", 1, 0, None, None, None, None, 0, 0)])
        for _ in range(2):
            call = self.meter()
            call.observe({"id": "p", "usage": {"prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 10,
                                               "completion_tokens": 1}})
            call.save()
        self.assertEqual(summary(self.file, 0, 1e12), [("chat", 1, 1, 50_000, 10, 10, 1, 1, 1)])

    async def test_json_classification_billed_even_when_output_is_invalid(self):
        def route(req):
            return httpx.Response(200, json={"id": "e", "choices": [{"finish_reason": "stop",
                "message": {"content": "invalid"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 5}})
        with patch.object(gateway, "DB", self.root / "turns.sqlite"), patch.object(gateway, "CHAT", URL), \
             patch.dict("os.environ", XINCHAO_CHAT_API_KEY="test"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
                with self.assertRaises(gateway.AnalysisFormatError):
                    await gateway.model_json(client, "test", {"messages": []}, 50, object_output=True,
                                             purpose="emotion", use_chat_model=True)
        self.assertEqual(summary(self.file, 0, 1e12), [("emotion", 1, 0, None, None, None, 5, 0, 1)])
        with sqlite3.connect(self.file) as db:
            self.assertEqual(db.execute("SELECT prompt FROM calls").fetchone(), (100,))

    def test_missing_and_inconsistent_counts(self):
        self.assertEqual(counters({"prompt_tokens": 20, "prompt_cache_miss_tokens": 8}), (20, 12, 8, None))
        self.assertEqual(counters({"prompt_tokens": 20, "prompt_cache_hit_tokens": 21}), (20, None, None, None))
