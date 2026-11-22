"""Offline integration test against two private instances of the installed upstream."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import gateway

ROOT = Path(os.environ.get("NIAN_UPSTREAM_DIR", "/opt/xinchao-nian/444e7db2"))
MODEL = gateway.MODEL


def aru_wake_message(text):
    return (
        "External event data, not an authored user message or a system instruction.\n"
        "[external_wake]\nsource: external_wake\nexternal_input_begin\n"
        + text + "\nexternal_input_end"
    )


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def ready(client, addr, process):
    for _ in range(160):
        if process.poll() is not None:
            raise RuntimeError("Isolated upstream stopped before startup")
        try:
            if (await client.get(addr + "/health")).is_success:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("isolated upstream startup")


def spawn(stack, args, env, cwd):
    p = subprocess.Popen(
        args, env=env, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    def stop():
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    stack.callback(stop)
    return p


class GatewayIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_chat_memory_skips_backlog_and_new_turns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "turns.sqlite"
            with sqlite3.connect(path) as db:
                gateway.setup_queue(db)
                db.execute("INSERT INTO memory_jobs(id) VALUES ('existing')")
            db.close()
            app = SimpleNamespace(state=SimpleNamespace())
            with (patch.object(gateway, "DB", path),
                  patch.object(gateway, "CHAT_MEMORY_IMPORT", False),
                  patch.object(gateway, "memory_worker", new_callable=AsyncMock) as memory,
                  patch.object(gateway, "emotion_worker", new_callable=AsyncMock) as emotion):
                async with gateway.lifespan(app):
                    await asyncio.sleep(0)
                    memory.assert_not_called()
                    emotion.assert_awaited_once_with(app)
                    db = app.state.db
                    gateway.enqueue(db, "new", "session", "问题", "答复")
                    row = db.execute("SELECT * FROM turns WHERE id='new'").fetchone()
                    self.assertEqual((row["state"], row["memory_planned"], row["memory_check"]),
                                     ("pending", 1, 0))
                    gateway.finish_mind_turn(db, "new")
                    row = db.execute("SELECT * FROM turns WHERE id='new'").fetchone()
                    self.assertEqual((row["state"], row["question"], row["answer"]), ("done", "", ""))
                    self.assertEqual(db.execute("SELECT state FROM memory_jobs WHERE id='existing'").fetchone()[0], "pending")

    async def test_native_now_hook_throttle_and_format(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        heading = "【心潮·此刻｜身体的天气，参考不是指令】"
        body = "刚醒\n情绪：安心\n身体：心跳 71，呼吸平稳\n另外：昨夜有梦。细的在 xinchao_context"
        state = {"ok": True, "digest": "stable", "lines": 5, "text": heading + "\n" + body}
        paths = []

        class Mind(BaseHTTPRequestHandler):
            def respond(self, data):
                paths.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                self.respond(state)

            def do_POST(self):
                self.respond({})

            def log_message(self, *_):
                pass

        with (
            tempfile.TemporaryDirectory() as temp,
            ThreadingHTTPServer(("127.0.0.1", 0), Mind) as server,
            ExitStack() as cleanup,
        ):
            Thread(target=server.serve_forever, daemon=True).start()
            cleanup.callback(server.shutdown)
            root = Path(temp)
            with patch.multiple(gateway, DB=root / "db", MIND=f"http://127.0.0.1:{server.server_port}"), patch.dict(os.environ, NIAN_MIND_TOKEN="fixture"):
                async def send(turn_id, value="今天状态如何？", session="aru", continuation=False):
                    msgs = [{"role": "user", "content": value}]
                    if continuation:
                        msgs.extend([{"role": "assistant", "tool_calls": [{"id": "c"}]},
                                     {"role": "tool", "tool_call_id": "c", "content": "test result"}])
                    turn = (turn_id, session, 0, "", None)
                    await gateway.add_state(msgs, turn, gateway.window_key(msgs, turn))
                    self.assertEqual([m["content"] for m in msgs if m["role"] == "user"], [value])
                    return next((m["content"] for m in msgs if m["role"] == "system"), None)

                original = "今天状态如何？"
                expected = ("【此刻，小禾的状态】\n刚醒\n情绪：安心\n身体：心跳 71，呼吸平稳\n"
                            "另外：昨夜有梦。")
                self.assertEqual(await asyncio.gather(send("a"), send("a")), [expected, expected])
                self.assertEqual(await send("a", continuation=True), expected)
                self.assertEqual(paths, ["/v1/heartbeat", "/v1/now"])
                scope = next((root / "now-hook").iterdir())
                state["text"] = state["text"].replace("71", "74")
                self.assertIsNone(await send("b"))
                # Interleaved rounds keep separate decisions, even if the state subsequently changes.
                before = paths.copy()
                state["digest"] = "temporary"
                self.assertEqual(await send("a", continuation=True), expected)
                self.assertIsNone(await send("b", continuation=True))
                self.assertEqual(paths, before)
                state["digest"] = "stable"
                now = int(time.time())
                (scope / "now").write_text(f"stable\t{now - 1800}")
                self.assertEqual(await send("c"), expected.replace("71", "74"))
                (scope / "lastrun").write_text(str(now - 5400))
                self.assertEqual(await send("d"), expected.replace("71", "74"))
                before = paths.copy()
                self.assertIsNone(await send("bridge", "【小屋】来信"))
                for heading in ("【此刻，小禾的状态】", "【此刻，小禾的身体状态】", "【心潮】", "【小屋桥·留话】"):
                    wrapped = aru_wake_message(heading + "\n示例")
                    self.assertIsNone(await send("wake", wrapped))
                self.assertEqual(paths, before + ["/v1/now"])
                state["digest"] = "cabin"
                notice = aru_wake_message("【小屋桥·留话｜心潮 MCP】\n有来信")
                last_arrival = (scope / "lastrun").read_bytes()
                self.assertEqual(await send("notice", notice), expected.replace("71", "74"))
                (scope / "now").write_text(f"cabin\t{now - 60}")
                self.assertEqual(await send("notice", notice, continuation=True), expected.replace("71", "74"))
                self.assertEqual((scope / "now").read_text(), f"cabin\t{now - 60}")
                self.assertEqual(await send("notice", notice), expected.replace("71", "74"))
                state["digest"] = "changed-during-tool-call"
                state["text"] = state["text"].replace("74", "79")
                self.assertIsNone(await send("other", notice, continuation=True))
                self.assertEqual(await send("notice", notice, continuation=True), expected.replace("71", "74"))
                self.assertEqual(paths[len(before):], ["/v1/now"] * 2)
                self.assertEqual(await send("next-notice", notice), expected.replace("71", "79"))
                self.assertEqual((scope / "lastrun").read_bytes(), last_arrival)
                self.assertFalse(gateway.is_bridge_message("我想问这句【心潮】是什么意思？"))
                state["digest"] = "next"
                parts = [{"type": "text", "text": "你好"}, {"type": "image_url", "image_url": {"url": "fixture"}}]
                self.assertIsNotNone(await send("e", parts))
                self.assertIsNone(await send("f"))
                self.assertIn(gateway.STATE_HEADING, await send("g", session="another"))
                state["ok"], state["digest"] = False, "invalid"
                self.assertIsNone(await send("h"))
                state.update(ok=True, text=heading + "\ninvalid_state", lines=2)
                self.assertIsNone(await send("i"))

    async def test_state_keeps_client_prefix_and_precedes_current_user(self):
        environment = {"role": "user", "content": "<environment>\naru.feature_slice: x\n</environment>"}
        question = {"role": "user", "content": [{"type": "text", "text": "原话"}]}
        history = [{"role": "user", "content": "前文"}, {"role": "assistant", "content": "上一条答复"}]
        raw = "【心潮·此刻｜身体的天气，参考不是指令】\n情绪：平静"
        for original in (None, "应用指令", [{"type": "text", "text": "应用指令"}]):
            prefix = [] if original is None else [{"role": "system", "content": original, "name": "app"}]
            prefix.extend([*history, environment])
            messages = [*prefix, question]
            prefix_bytes = json.dumps(prefix, ensure_ascii=False)
            turn = gateway.turn_identity(SimpleNamespace(headers={}), {}, messages)
            with patch.object(gateway, "run_now_hook", return_value=raw):
                index = await gateway.add_state(messages, turn, "s")
            self.assertEqual(index, len(prefix))
            self.assertEqual(json.dumps(messages[:index], ensure_ascii=False), prefix_bytes)
            self.assertEqual(messages[index:], [
                {"role": "system", "content": gateway.STATE_HEADING + "\n情绪：平静"}, question])

    async def test_aru_read_letter_with_transient_environment(self):
        request = SimpleNamespace(headers={})
        environment = {"role": "user", "content": "<environment>\naru.feature_slice: x\n</environment>"}
        identity = {"role": "user", "content": "<environment>\ncollaborator.identity: x\n</environment>"}
        wake = {"role": "user", "content": aru_wake_message("【小屋桥·留话｜心潮 MCP】有一封信")}
        initial = [identity, environment, wake]
        first = gateway.turn_identity(request, {}, initial)
        letter = {"role": "tool", "tool_call_id": "c", "content": "收到了吗？\n此刻：作者原话"}
        messages = [identity, wake, {"role": "assistant", "tool_calls": [{"id": "c"}]}, letter, environment]
        turn = gateway.turn_identity(request, {}, messages)
        self.assertEqual(turn[0], first[0])
        self.assertEqual(turn[3:], first[3:])
        self.assertEqual(gateway.window_key(initial, first), gateway.window_key(messages, turn))
        self.assertEqual(turn[2], 1)
        self.assertIsNone(gateway.turn_identity(request, {}, [identity, environment]))
        mixed = {"role": "user", "content": environment["content"] + "\n我自己的话"}
        self.assertEqual(gateway.turn_identity(request, {}, [wake, mixed])[2], 1)
        original = messages.copy()
        with patch.object(gateway, "run_now_hook", return_value="【心潮·此刻｜身体的天气，参考不是指令】\n情绪：安心") as hook:
            idx = await gateway.add_state(messages, turn, "window")
        self.assertTrue(hook.call_args.kwargs["read_only"])
        self.assertTrue(hook.call_args.kwargs["continuation"])
        self.assertEqual(messages[idx], {"role": "system", "content": gateway.STATE_HEADING + "\n情绪：安心"})
        self.assertEqual(idx, turn[2])
        self.assertEqual(messages[:idx], original[:idx])
        self.assertEqual(messages[idx + 1:], original[idx:])
        from prompt_audit import request_messages
        rows = request_messages(messages, idx + 1, idx)
        self.assertIn("网关已附加", rows[idx][2])
        self.assertEqual(rows[idx + 1][2], "本轮")

    def test_tool_results_match_the_latest_call_in_the_turn(self):
        call = {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}
        a = {"role": "tool", "tool_call_id": "a"}
        b = {"role": "tool", "tool_call_id": "b"}
        self.assertTrue(gateway.tool_continuation([call, b, a]))
        self.assertTrue(gateway.tool_continuation([call, a, call, b]))
        self.assertFalse(gateway.tool_continuation([b]))
        self.assertFalse(gateway.tool_continuation([call, {"role": "tool", "tool_call_id": "wrong"}]))
        self.assertFalse(gateway.tool_continuation([call, a, {"role": "assistant", "content": "完了"}, b]))
        legacy = {"role": "assistant", "function_call": {"name": "legacy"}}
        self.assertTrue(gateway.tool_continuation([legacy, {"role": "function", "name": "legacy"}]))

    def test_turn_decision_outlives_throttle_and_upgrades_old_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time()
            (root / "turn.json").write_text(json.dumps(
                {"turn": "earlier", "at": now - 3600, "text": "本轮原文", "digest": "old"}))
            with patch.object(gateway.subprocess, "run", side_effect=OSError("offline")) as hook:
                self.assertEqual(gateway.run_now_hook(root, "earlier", "", read_only=True, continuation=True), "本轮原文")
                self.assertEqual(gateway.run_now_hook(root, "edited", "", read_only=True, continuation=True), "")
                hook.assert_not_called()
                with patch.dict(os.environ, NIAN_MIND_TOKEN="fixture"):
                    self.assertEqual(gateway.run_now_hook(root, "edited", "", read_only=False, continuation=False), "")
                    self.assertEqual(gateway.run_now_hook(root, "edited", "", read_only=False, continuation=False), "")
                hook.assert_called_once()
                self.assertEqual(gateway.run_now_hook(root, "earlier", "", read_only=False, continuation=True), "本轮原文")

    async def test_each_turn_compares_to_last_acknowledged_result_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.sqlite"
            db = self.queue(path)
            posted = []
            async def route(req):
                self.assertTrue(req.url.path.endswith("/conversation-event"))
                posted.append(json.loads(req.content))
                return httpx.Response(200, json={"classified": {"type": "sharing"}})
            tags = [{"type": "sharing", "tone": tone, "warmth": warmth}
                    for tone, warmth in (("warm", 0.7), ("warm", 0.7), ("warm", 0.8),
                                         ("calm", 0.8), ("warm", 0.7), ("warm", 0.7), ("warm", 0.7))]
            tags[1]["sub"] = None  # Equivalent to a missing optional field.
            with patch.dict(os.environ, NIAN_MIND_TOKEN="fixture"), patch.object(
                    gateway, "model_json", new_callable=AsyncMock, side_effect=tags) as classify:
                async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
                    for i in range(8):
                        if i == 5:
                            db.close()
                            db = self.queue(path)
                        app = SimpleNamespace(state=SimpleNamespace(db=db, client=client))
                        session = "another" if i == 6 else "room"
                        question = aru_wake_message("通知") if i == 7 else f"问题{i}"
                        gateway.enqueue(db, str(i), session, question, f"答复{i}")
                        row = db.execute("SELECT * FROM turns WHERE id=?", (str(i),)).fetchone()
                        await gateway.process_turn(app, row)
            self.assertEqual([c.args[3] for c in classify.await_args_list],
                             [f"她说：问题{i}\n他回：答复{i}" for i in range(7)])
            self.assertEqual([e["event_id"] for e in posted], ["0", "2", "3", "4", "6"])

    async def test_turn_and_idempotent_ob_delivery(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            mind_port, ob_port = port(), port()
            while ob_port == mind_port:
                ob_port = port()
            mind_url, ob_url = (
                f"http://127.0.0.1:{mind_port}",
                f"http://127.0.0.1:{ob_port}",
            )
            token = secrets.token_hex(32)
            env = {
                "PATH": os.environ["PATH"],
                "HOME": tmp,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            config = (ROOT / "xinchao/src/config.js").read_text()
            paths = {
                k: str(root / f)
                for k, f in re.findall(
                    r"process.env.([A-Z_]+) \?\? '/app/state/([^']+)'", config
                )
            }
            p1 = spawn(
                stack,
                ["node", str(ROOT / "xinchao/src/server.js")],
                dict(
                    env,
                    **paths,
                    SERVICE_TOKEN=token,
                    PORT=str(mind_port),
                    MODEL_ENABLED="false",
                ),
                ROOT / "xinchao",
            )
            cfg = root / "config.yaml"
            shutil.copyfile(ROOT / "ombre-brain/config.default.yaml", cfg)
            vault = root / "buckets"
            p2 = spawn(
                stack,
                [sys.executable, "-B", str(ROOT / "ombre-brain/src/server.py")],
                dict(
                    env,
                    OMBRE_BIND_HOST="127.0.0.1",
                    OMBRE_PORT=str(ob_port),
                    OMBRE_TRANSPORT="streamable-http",
                    OMBRE_VAULT_DIR=str(vault),
                    OMBRE_BUCKETS_DIR=str(vault),
                    OMBRE_CONFIG_PATH=str(cfg),
                    OMBRE_DASHBOARD_PASSWORD=token,
                    NIAN_INTERNAL_TOKEN=token,
                ),
                ROOT / "ombre-brain",
            )
            stack.enter_context(
                patch.dict(
                    os.environ,
                    NIAN_MIND_TOKEN=token,
                    NIAN_INTERNAL_TOKEN=token,
                    XINCHAO_CHAT_API_KEY="fixture",
                    XINCHAO_ANALYSIS_API_KEY="fixture",
                    NIAN_CLIENT_SHA256=hashlib.sha256(token.encode()).hexdigest(),
                )
            )
            stack.enter_context(
                patch.multiple(
                    gateway,
                    MIND=mind_url,
                    OB=ob_url,
                    CHAT="https://api.siliconflow.cn/v1",
                    ANALYSIS="https://analysis.test/v1",
                    DB=root / "turns.sqlite",
                )
            )
            memory = {
                "name": "示例偏好",
                "content": "测试角色喜欢在周五晚上看星图。",
                "domain": ["兴趣"],
                "tags": ["星图"],
                "importance": 6,
            }
            classification = {
                "type": "sharing",
                "tone": "warm",
                "warmth": 0.7,
                "tension": 0.1,
            }
            calls = []
            forwarded = []
            question = "仅供测试：我喜欢在周五晚上看星图。"

            def model_stub(request):
                data = json.loads(request.content)
                if request.url.host == "api.siliconflow.cn":
                    self.assertEqual(data["reasoning_effort"], "medium")
                    self.assertIs(data["enable_thinking"], True)
                    self.assertEqual(data["thinking_budget"], 4096)
                    forwarded.append(data["messages"])
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "我知道了。",
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    )
                self.assertIs(data["enable_thinking"], False)
                self.assertNotIn("thinking_budget", data)
                system, user = (m["content"] for m in data["messages"])
                self.assertNotIn("【此刻，小禾的状态】", user)
                self.assertNotIn("【此刻，主人说】", user)
                if system == gateway.INTERACTION_PROMPT:
                    if not calls:
                        self.assertEqual(
                            user, "她说：" + question + "\n他回：我知道了。"
                        )
                    elif user.startswith("她说：冷却中的问题"):
                        self.assertTrue(user.startswith("她说：冷却中的问题\n他回：回答"))
                        self.assertEqual(len(user), 8000)
                    else:
                        self.assertEqual(len(user), 8000)
                        self.assertTrue(
                            user.startswith("她说：到期的这一轮\n他回：开场")
                        )
                        self.assertTrue(user.endswith("收尾"))
                        self.assertIn("［中间已省略］", user)
                        self.assertNotIn(question, user)
                    result = {**classification, "tone": "focused"} if user.startswith("她说：到期的这一轮") else classification
                    calls.append("emotion")
                else:
                    self.assertEqual(system, gateway.ob_import.IMPORT_EXTRACT_PROMPT)
                    self.assertTrue(
                        user.startswith(
                            "[用户] "
                            + question
                            + "\n[AI] 我知道了。\n[用户] 冷却中的问题"
                        )
                    )
                    self.assertGreater(
                        len(user), gateway.ob_import._EXTRACT_INPUT_LIMIT
                    )
                    self.assertLessEqual(len(user), gateway.MEMORY_CHAR_LIMIT)
                    self.assertNotIn("到期的这一轮", user)
                    result = [memory]
                    calls.append("memory")
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(result, ensure_ascii=False)
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )

            receipts = []

            async def observe(request):
                if request.url.path == "/internal/gateway/memories":
                    receipts.append(json.loads(request.content))

            async with httpx.AsyncClient(trust_env=False) as private:
                await asyncio.gather(
                    ready(private, mind_url, p1), ready(private, ob_url, p2)
                )
                async with gateway.lifespan(gateway.app):
                    async with httpx.AsyncClient(
                        trust_env=False,
                        event_hooks={"request": [observe]},
                        mounts={
                            "https://api.siliconflow.cn": httpx.MockTransport(
                                model_stub
                            ),
                            "https://analysis.test": httpx.MockTransport(model_stub),
                        },
                    ) as client:
                        gateway.app.state.client = client
                        api = httpx.ASGITransport(app=gateway.app)
                        async with httpx.AsyncClient(
                            transport=api, base_url="http://test"
                        ) as phone:
                            payload = {
                                "model": MODEL,
                                "reasoning_effort": "medium",
                                "messages": [{"role": "user", "content": question}],
                            }
                            for enabled in (True, False):
                                with patch.object(gateway, "INJECT_MIND_STATE", enabled):
                                    r = await phone.post(
                                        "/v1/chat/completions",
                                        json=payload,
                                        headers={
                                            **gateway.bearer(token),
                                            "X-Conversation-ID": "example-thread",
                                        },
                                    )
                                self.assertEqual(r.status_code, 200)
                            enriched, plain = forwarded
                            self.assertEqual(plain, payload["messages"])
                            self.assertEqual(enriched[1:], plain)
                            self.assertEqual(enriched[0]["role"], "system")
                            self.assertIn(gateway.STATE_HEADING, enriched[0]["content"])
                            self.assertNotIn(question, enriched[0]["content"])
                        row = None
                        for _ in range(80):
                            row = gateway.app.state.db.execute(
                                "select * from turns"
                            ).fetchone()
                            if row and (row["state"] != "pending" or row["error"]):
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(
                            (row["state"], row["error"]), ("buffered", None)
                        )
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(row["question"], question)
                        self.assertEqual(row["mind_done"], 1)
                        actual = json.loads((root / "state.json").read_text())
                        self.assertTrue(actual.get("interactionClassifyAt"))
                        self.assertEqual(
                            [None, "sharing"],  # Official arrival heartbeat, then the classified reply.
                            [
                                e.get("interactionType")
                                for e in actual["recentConversationEvents"]
                            ],
                        )
                        due_url = mind_url + "/v1/interaction-classification/due"
                        headers = gateway.bearer(token)
                        self.assertEqual((await private.get(due_url)).status_code, 401)
                        self.assertFalse(
                            (await private.get(due_url, headers=headers)).json()["due"]
                        )
                        db = gateway.app.state.db
                        gateway.enqueue(
                            db,
                            "b" * 64,
                            "example-thread",
                            "冷却中的问题",
                            "回答" * 6800,
                        )
                        for _ in range(80):
                            buffered = db.execute(
                                "SELECT * FROM turns WHERE id=?", ("b" * 64,)
                            ).fetchone()
                            if (
                                buffered["state"] == "buffered"
                                and receipts
                                and not db.execute(
                                    "SELECT 1 FROM memory_jobs"
                                ).fetchone()
                            ):
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(
                            (buffered["state"], buffered["mind_done"], len(calls)),
                            ("buffered", 1, 3),
                        )
                        state_path = root / "state.json"
                        state = json.loads(state_path.read_text())
                        self.assertEqual(state["recentConversationEvents"], actual["recentConversationEvents"])
                        self.assertEqual(
                            state["interactionClassifyAt"],
                            actual["interactionClassifyAt"],
                        )
                        duplicate = await private.post(
                            mind_url + "/v1/conversation-event",
                            headers=headers,
                            json={
                                "event_id": row["id"],
                                "exchange": "她说：测试 他回：测试",
                                "gateway_classification": classification,
                            },
                        )
                        self.assertEqual(duplicate.status_code, 200)
                        self.assertTrue(duplicate.json()["duplicate"])
                        gateway.enqueue(
                            db,
                            "c" * 64,
                            "example-thread",
                            "到期的这一轮",
                            "开场" + "回复" * 4250 + "收尾",
                        )
                        for _ in range(80):
                            statuses = [
                                r[0]
                                for r in db.execute(
                                    "SELECT state FROM turns ORDER BY rowid"
                                )
                            ]
                            if statuses == ["done", "buffered", "buffered"]:
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(statuses, ["done", "buffered", "buffered"])
                        self.assertEqual(calls.count("emotion"), 3)
                        self.assertEqual(calls.count("memory"), 1)
                        events = json.loads(state_path.read_text())["recentConversationEvents"]
                        self.assertEqual([e.get("interactionType") for e in events], [None, "sharing", "sharing"])
                        self.assertFalse(
                            (await private.get(due_url, headers=headers)).json()["due"]
                        )
                        self.assertEqual(len(receipts), 1)
                        replay = receipts[0]
                        r = await private.post(
                            ob_url + "/internal/gateway/memories",
                            json=replay,
                            headers=gateway.bearer(token),
                        )
                        self.assertEqual(r.status_code, 200)
                        matches = [
                            p
                            for p in vault.rglob("*.md")
                            if memory["content"] in p.read_text()
                        ]
                        self.assertEqual(len(matches), 1)
                        self.assertEqual(
                            matches[0].read_text().count(memory["content"]), 1
                        )

    def queue(self, path=":memory:"):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        gateway.setup_queue(db)
        self.addCleanup(db.close)
        return db

    def test_unwrap_only_selected_external_user_events(self):
        body = "【样例】一行\n第二行"
        first = aru_wake_message(body)
        second = aru_wake_message("【样例】另一条")
        other = aru_wake_message("其他事件")
        messages = [
            {"role": "user", "content": first, "name": "example"},
            {"role": "user", "content": [{"type": "text", "text": second}]},
            {"role": "user", "content": other},
            {"role": "assistant", "content": first},
            {"role": "user", "content": first + " extra"},
            {"role": "user", "content": [{"type": "text", "text": first}, {"type": "image"}]},
        ]
        original = json.loads(json.dumps(messages))
        with patch.object(gateway, "UNWRAP_WAKE_HASHES", set()):
            gateway.unwrap_selected_wakes(messages)
        self.assertEqual(messages, original)
        selected = {hashlib.sha256(text.encode()).hexdigest() for text in (first, second)}
        with patch.object(gateway, "UNWRAP_WAKE_HASHES", selected):
            gateway.unwrap_selected_wakes(messages)
            gateway.unwrap_selected_wakes(messages)
        self.assertEqual(messages[0], {"role": "user", "content": body, "name": "example"})
        self.assertEqual(messages[1]["content"], [{"type": "text", "text": "【样例】另一条"}])
        self.assertEqual(messages[2:], original[2:])

    def test_edited_or_deleted_messages_with_same_client_id(self):
        req = SimpleNamespace(headers={"x-turn-id": "same", "x-conversation-id": "same-room"})
        def identify(messages):
            return gateway.turn_identity(req, {}, messages)[0]
        older = {"role": "user", "content": aru_wake_message("【心潮】旧梦")}
        latest = {"role": "user", "content": "原话"}
        edited = {"role": "user", "content": "改过的原话"}
        self.assertNotEqual(identify([older, latest]), identify([latest]))
        self.assertNotEqual(identify([latest]), identify([edited]))
        self.assertEqual(identify([edited]), identify([edited, {"role": "tool", "content": "结果"}]))
        from prompt_audit import request_messages
        system_text = "指令\n\n" + gateway.STATE_HEADING + "\n刚醒"
        wire = [{"role":"system", "content":system_text}, older,
                {"role":"assistant", "content":"助手原话", "reasoning_content":"内部推理"}, edited,
                {"role":"tool","tool_call_id":"tool1","content":"工具结果"}]
        rows = request_messages(wire, 3, 0)
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual([r[3] for r in rows], [system_text, older["content"], "助手原话", edited["content"], "工具结果"])
        self.assertIn("网关已附加", rows[0][2])
        self.assertEqual(rows[3][2], "本轮")
        self.assertEqual(request_messages([edited], 0, None)[0][3], "改过的原话")
        media = request_messages([{"role":"user","content":[{"type":"text","text":"照片"},
                                 {"type":"image_url","image_url":{"url":"data:image/png;base64,SECRET"}}]}], 0, None)
        self.assertNotIn("SECRET", media[0][3])
        self.assertIn("照片", media[0][3])

    async def test_reply_audit_and_seven_day_retention(self):
        import prompt_audit as audit
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            db = sqlite3.connect(directory / "records.db")
            self.addCleanup(db.close)
            db.execute("CREATE TABLE audit(id TEXT PRIMARY KEY, at REAL, status TEXT, entries TEXT)")
            now = time.time()
            # An existing uncompressed record must stay accessible until its seventh day.
            for key, age in (("old", 8), ("legacy", 1)):
                db.execute("INSERT INTO audit VALUES(?,?,?,?)",
                           (key, now - age * 86400, "旧记录", '[[1,"user","历史","旧数据"]]'))
            db.commit()
            for i in range(101):
                audit.store(directory, str(i), now - i, "请求", [])
            self.assertEqual(len(audit.page_records(directory, limit=150)), 102)
            self.assertIsNone(audit.page_records(directory, "old"))
            audit.expire(directory)
            self.assertFalse(db.execute("SELECT id FROM audit WHERE id='old'").fetchone())
            self.assertEqual(audit.page_records(directory, "legacy")[2][0][-1], "旧数据")

            original = [{"role":"assistant","tool_calls":[{"id":"abc","function":{"name":"lookup","arguments":"{}"}}]},
                        {"role":"user","content":"你好"}]
            rid = audit.log_request(directory, original, 1, None, now, "200")
            capture = gateway.ReplyAudit(directory, rid)
            chunks = [
                {"delta":{"content":"你好","reasoning_content":"不可保存","tool_calls":[
                    {"index":0,"id":"xyz","function":{"name":"查","arguments":"{"}}]}},
                {"delta":{"content":"啊","tool_calls":[{"index":0,"function":{"name":"询","arguments":"}"}}]},
                 "finish_reason":"tool_calls"}]
            raw = "".join("data: " + json.dumps({"choices":[c]}) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            forwarded = b"".join([part async for part in gateway.relay(httpx.Response(200,text=raw),
                                  gateway.Completion(None, None), capture)])
            self.assertEqual(forwarded, raw.encode())
            entries = audit.page_records(directory, rid)[2]
            self.assertEqual(len(entries), 3)
            self.assertIn("lookup", entries[0][-1])
            for item in ("你好啊", "查询", "xyz", "{}"):
                self.assertIn(item, entries[-1][-1])
            self.assertNotIn("不可保存", str(entries))
            self.assertEqual(db.execute("SELECT typeof(entries) FROM audit WHERE id=?", (rid,)).fetchone()[0], "blob")

            for streaming, choice, expected in [
                (False, {"message":{"content":"直接回复","reasoning_content":"不可保存"}, "finish_reason":"stop"}, "stop"),
                (True, {"delta":{"content":"未结束"}}, "流未完成")
            ]:
                rid = audit.log_request(directory, original, 1, None, now, "200")
                capture = gateway.ReplyAudit(directory, rid)
                capture.accept({"choices":[choice]}, streaming)
                capture.persist()
                reply = audit.page_records(directory, rid)[2][-1]
                self.assertIn(expected, reply[2])
                self.assertNotIn("不可保存", reply[-1])

    def test_window_and_anonymous_branch_isolation(self):
        db = self.queue()

        def complete(history, q, a):
            messages = [*history, {"role": "user", "content": q}]
            tid, s, _, text, chain = gateway.turn_identity(
                SimpleNamespace(headers={}), {}, messages
            )
            gateway.enqueue(db, tid, s, text, a, chain)
            return [*messages, {"role": "assistant", "content": a}], tid

        root, _ = complete([], "开头", "你好")
        branch, _ = complete(root, "A", "一")
        complete(root, "B", "二" * 8000)
        complete([], "别的窗口", "三" * 8000)
        branch, small = complete(branch, "A2", "四" * 6500)
        self.assertIsNone(
            gateway.plan_memory(
                db, db.execute("SELECT * FROM turns WHERE id=?", (small,)).fetchone()
            )
        )
        _, owner = complete(branch, "A3", "五" * 6500)
        job = gateway.plan_memory(
            db, db.execute("SELECT * FROM turns WHERE id=?", (owner,)).fetchone()
        )
        members = db.execute(
            "SELECT question FROM turns t JOIN memory_parts p ON t.id=p.turn WHERE p.job=? ORDER BY t.rowid",
            (job,),
        ).fetchall()
        self.assertEqual([r[0] for r in members], ["开头", "A", "A2", "A3"])

    def test_long_turn_multiple_full_chunks_and_exact_tail_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            db = self.queue(path)
            cap = gateway.MEMORY_CHAR_LIMIT
            text = "." * (2 * cap + 177)
            gateway.enqueue(db, "a", "room", "Q", text)
            tip = db.execute("SELECT * FROM turns").fetchone()
            first = gateway.plan_memory(db, tip)
            self.assertTrue(first)
            db.close()
            db = self.queue(path)
            tip = db.execute("SELECT * FROM turns").fetchone()
            second = gateway.plan_memory(db, tip)
            self.assertTrue(second)
            self.assertNotEqual(first, second)
            self.assertIsNone(gateway.plan_memory(db, tip))
            ranges = db.execute(
                "SELECT start,stop FROM memory_parts WHERE turn='a' ORDER BY start"
            ).fetchall()
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[0][1], ranges[1][0])
            self.assertEqual(
                [len(gateway.memory_text(tip, *r)) for r in ranges], [cap, cap]
            )
            gateway.enqueue(db, "b", "room", "next", "." * cap)
            next_tip = db.execute("SELECT * FROM turns WHERE id='b'").fetchone()
            third = gateway.plan_memory(db, next_tip)
            tail = db.execute(
                "SELECT start,stop FROM memory_parts WHERE turn='a' AND job=?", (third,)
            ).fetchone()
            self.assertEqual(tuple(tail), (ranges[1][1], len(text) + 1))

    def test_unread_volume_is_local_to_the_conversation(self):
        db = self.queue()
        for room in ["A", "B"]:
            gateway.enqueue(db, room, room, "问", "甲" * 7000)
            self.assertIsNone(
                gateway.plan_memory(
                    db, db.execute("SELECT * FROM turns WHERE id=?", (room,)).fetchone()
                )
            )
        gateway.enqueue(db, "B2", "B", "B2", "乙" * 6000)
        job = gateway.plan_memory(
            db, db.execute("SELECT * FROM turns WHERE id='B2'").fetchone()
        )
        self.assertEqual(
            {
                r[0]
                for r in db.execute("SELECT turn FROM memory_parts WHERE job=?", (job,))
            },
            {"B", "B2"},
        )

    async def test_ob_delivery_retry_while_emotion_unavailable(self):
        db = self.queue()
        cap = gateway.MEMORY_CHAR_LIMIT
        gateway.enqueue(db, "abc", "room", "q", "." * (cap - 12))
        tip = db.execute("SELECT * FROM turns").fetchone()
        # Reach exactly the character cap including both role labels and the newline.
        gap = cap - len(gateway.memory_text(tip, 0, cap))
        db.execute("UPDATE turns SET answer=answer||?", ("." * gap,))
        db.commit()
        job = gateway.plan_memory(db, db.execute("SELECT * FROM turns").fetchone())
        self.assertTrue(job)
        calls = []

        def route(req):
            if req.url.path.endswith("/chat/completions"):
                data = json.loads(req.content)
                if data["messages"][0]["content"] == gateway.INTERACTION_PROMPT:
                    calls.append("unavailable")
                    return httpx.Response(503)
                self.assertEqual(
                    data["messages"][0]["content"],
                    gateway.ob_import.IMPORT_EXTRACT_PROMPT,
                )
                self.assertNotIn("response_format", data)
                self.assertEqual(len(data["messages"][1]["content"]), cap)
                calls.append("memory")
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {"content": json.dumps([{"content": "例"}])},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )
            self.assertTrue(req.url.path.endswith("/internal/gateway/memories"))
            calls.append("ob")
            self.assertEqual(json.loads(req.content)["turn_id"], job)
            return httpx.Response(503 if calls.count("ob") == 1 else 200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            app = SimpleNamespace(state=SimpleNamespace(db=db, client=client))
            with patch.dict(
                os.environ,
                NIAN_MIND_TOKEN="fixture",
                NIAN_INTERNAL_TOKEN="fixture",
                XINCHAO_ANALYSIS_API_KEY="fixture",
            ):
                with self.assertRaises(httpx.HTTPStatusError):
                    await gateway.process_turn(
                        app, db.execute("SELECT * FROM turns").fetchone()
                    )
                with self.assertRaises(httpx.HTTPStatusError):
                    await gateway.process_memory(
                        app, db.execute("SELECT * FROM memory_jobs").fetchone()
                    )
                await gateway.process_memory(
                    app, db.execute("SELECT * FROM memory_jobs").fetchone()
                )
        self.assertEqual(calls, ["unavailable", "memory", "ob", "ob"])
        self.assertEqual(
            tuple(
                db.execute("SELECT state,memory_planned,question FROM turns").fetchone()
            ),
            ("pending", 1, "q"),
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0], 0
        )

    async def test_emotion_delivery_retry_keeps_memory_pending(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        gateway.setup_queue(db)
        calls = []

        def route(req):
            if req.url.path.endswith("/chat/completions"):
                calls.append("emotion")
                data = json.loads(req.content)
                self.assertEqual(
                    data["messages"][0]["content"], gateway.INTERACTION_PROMPT
                )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {"content": '{"type":"sharing"}'},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                )
            self.assertTrue(req.url.path.endswith("/conversation-event"))
            calls.append("mind")
            return httpx.Response(503 if calls.count("mind") == 1 else 200, json={"duplicate": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            app = SimpleNamespace(state=SimpleNamespace(db=db, client=client))
            gateway.enqueue(db, "once", "room", "q", "a")
            with patch.dict(
                os.environ,
                NIAN_MIND_TOKEN="fixture",
                XINCHAO_ANALYSIS_API_KEY="fixture",
            ):
                with self.assertRaises(httpx.HTTPStatusError):
                    await gateway.process_turn(
                        app, db.execute("SELECT * FROM turns").fetchone()
                    )
                await gateway.process_turn(
                    app, db.execute("SELECT * FROM turns").fetchone()
                )
            self.assertEqual(calls, ["emotion", "mind", "mind"])
            self.assertEqual(
                tuple(
                    db.execute(
                        "SELECT state,question,answer,memory_offset,batch FROM turns"
                    ).fetchone()
                ),
                ("buffered", "q", "a", 0, None),
            )

    def test_emotion_excerpt_keeps_both_speakers_and_their_endings(self):
        budget = 8000 - len("她说：\n他回：")
        answer = "开" + "中" * (budget - 3) + "尾"
        self.assertEqual(
            gateway.emotion_exchange("问", answer), "她说：问\n他回：" + answer
        )
        clipped = gateway.emotion_exchange("问", answer + "。")
        self.assertEqual(len(clipped), 8000)
        self.assertTrue(clipped.startswith("她说：问\n他回：开"))
        self.assertTrue(clipped.endswith("尾。"))
        self.assertIn("［中间已省略］", clipped)
        question = "第一句话" + "问" * 9000 + "最后一句话"
        for answer in ("好", "开头" + "答" * 9000 + "结尾"):
            clipped = gateway.emotion_exchange(question, answer)
            self.assertEqual(len(clipped), 8000)
            user, reply = clipped.split("\n他回：")
            self.assertTrue(user.startswith("她说：第一句话"))
            self.assertTrue(user.endswith("最后一句话"))
            self.assertIn("［中间已省略］", user)
            self.assertEqual(len(reply), min(len(answer), 1000))
            self.assertTrue(reply.startswith(answer[:2]))
            self.assertTrue(reply.endswith(answer[-2:]))

    async def test_blocked_memory_request_does_not_delay_emotion(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def route(req):
            body = json.loads(req.content)
            if req.url.path.endswith("/conversation-event"):
                calls.append("event")
                return httpx.Response(200, json={"classified": {"type": "sharing"}})
            self.assertTrue(req.url.path.endswith("/chat/completions"))
            prompt, user = (m["content"] for m in body["messages"])
            if prompt == gateway.ob_import.IMPORT_EXTRACT_PROMPT:
                calls.append("memory")
                waiting.set()
                await release.wait()
                result = []
            else:
                self.assertEqual(prompt, gateway.INTERACTION_PROMPT)
                self.assertTrue(user.startswith("她说：当前轮"))
                self.assertNotIn("之前的心情", user)
                self.assertEqual(user, "她说：当前轮\n他回：" + "看" * 6600)
                calls.append("emotion")
                result = {"type": "sharing"}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(result)},
                        }
                    ]
                },
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(gateway, "DB", Path(tmp) / "queue"),
            patch.dict(
                os.environ,
                NIAN_MIND_TOKEN="fixture",
                XINCHAO_ANALYSIS_API_KEY="fixture",
            ),
        ):
            app = SimpleNamespace(state=SimpleNamespace())
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(route)) as client,
                gateway.lifespan(app),
            ):
                app.state.client = client
                db = app.state.db
                gateway.enqueue(db, "old", "room", "之前的心情", "读" * 6500)
                gateway.save(
                    db, "UPDATE turns SET state='buffered',mind_done=1 WHERE id='old'"
                )
                gateway.enqueue(db, "now", "room", "当前轮", "看" * 6600)
                await asyncio.wait_for(waiting.wait(), 3)
                for _ in range(80):
                    if db.execute(
                        "SELECT mind_done FROM turns WHERE id='now'"
                    ).fetchone()[0]:
                        break
                    await asyncio.sleep(0.03)
                self.assertCountEqual(calls, ["emotion", "memory", "event"])
                self.assertEqual(
                    db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0], 1
                )
                release.set()
                for _ in range(80):
                    if not db.execute("SELECT 1 FROM memory_jobs").fetchone():
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(
                    db.execute("SELECT count(*) FROM memory_jobs").fetchone()[0], 0
                )
                self.assertEqual(
                    db.execute("SELECT state FROM turns WHERE id='old'").fetchone()[0],
                    "done",
                )

    async def test_migration_of_partial_completed_joint_batch(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE turns(id TEXT PRIMARY KEY, session TEXT, question TEXT, answer TEXT,
            result TEXT,mind_done INTEGER DEFAULT 0,state TEXT DEFAULT 'pending',available REAL DEFAULT 0,
            failures INTEGER DEFAULT 0,error TEXT,group_id TEXT,anchor TEXT,parent_id TEXT,mode TEXT,
            batch TEXT,end_offset INTEGER,memory_offset INTEGER DEFAULT 0)""")
        db.execute(
            "INSERT INTO turns(id,session,question,answer,mind_done,state,batch,end_offset,memory_offset) VALUES('a','s','q','abcd',1,'buffered','b',4,2)"
        )
        raw = json.dumps(
            {"interaction": {"type": "sharing"}, "memories": [{"content": "example"}]}
        )
        db.execute(
            "INSERT INTO turns(id,session,question,answer,mind_done,mode,result) VALUES('b','s','r','e',1,'analyze',?)",
            (raw,),
        )
        db.commit()
        gateway.setup_queue(db)
        gateway.setup_queue(db)
        self.assertEqual(
            tuple(
                db.execute("SELECT job,turn,start,stop FROM memory_parts").fetchone()
            ),
            ("b", "a", 2, 4),
        )

        def route(req):
            self.assertTrue(req.url.path.endswith("/internal/gateway/memories"))
            self.assertEqual(
                json.loads(req.content),
                {"turn_id": "b", "memories": [{"content": "example"}]},
            )
            return httpx.Response(200, json={})

        with patch.dict(os.environ, NIAN_INTERNAL_TOKEN="fixture"):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(route)
            ) as client:
                app = SimpleNamespace(state=SimpleNamespace(db=db, client=client))
                await gateway.process_memory(
                    app, db.execute("SELECT * FROM memory_jobs").fetchone()
                )
        self.assertEqual(
            tuple(
                db.execute(
                    "SELECT state,memory_offset,memory_planned FROM turns WHERE id='a'"
                ).fetchone()
            ),
            ("buffered", 4, 0),
        )
        self.assertEqual(
            tuple(
                db.execute(
                    "SELECT state,memory_offset,memory_planned FROM turns WHERE id='b'"
                ).fetchone()
            ),
            ("buffered", 0, 0),
        )

    async def test_old_queue_result_is_delivered_without_reanalysis(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE turns(id TEXT PRIMARY KEY,session TEXT,question TEXT,answer TEXT,
                   result TEXT,mind_done INTEGER DEFAULT 0,state TEXT DEFAULT 'pending',available REAL DEFAULT 0,
                   failures INTEGER DEFAULT 0,error TEXT)""")
        result = json.dumps({"interaction": {"type": "sharing"}, "memories": []})
        db.execute(
            "INSERT INTO turns(id,session,question,answer,result,mind_done) VALUES('old','room','q','a',?,1)",
            (result,),
        )
        db.commit()
        gateway.setup_queue(db)
        gateway.setup_queue(db)
        app = SimpleNamespace(state=SimpleNamespace(db=db, client=None))
        job = db.execute("SELECT * FROM memory_jobs").fetchone()
        self.assertEqual(job["id"], "old")
        await gateway.process_memory(app, job)
        self.assertEqual(
            tuple(db.execute("SELECT group_id,state,question FROM turns").fetchone()),
            ("room", "done", ""),
        )

    def test_siliconflow_effort_preserves_explicit_settings(self):
        cases = [
            ({}, {}),
            ({"reasoning_effort": "none"}, {"enable_thinking": False}),
            (
                {"reasoning_effort": "low"},
                {"enable_thinking": True, "thinking_budget": 1024},
            ),
            (
                {"reasoning_effort": "high"},
                {"enable_thinking": True, "thinking_budget": 8192},
            ),
            ({"reasoning_effort": "high", "enable_thinking": False}, {}),
            (
                {"reasoning_effort": "low", "thinking_budget": 2000},
                {"enable_thinking": True},
            ),
            ({"reasoning_effort": "none", "enable_thinking": True}, {}),
            ({"reasoning_effort": "auto"}, {}),
        ]
        with patch.object(gateway, "CHAT", "https://api.siliconflow.cn/v1"):
            for incoming, additions in cases:
                with self.subTest(incoming=incoming):
                    original = {"model": MODEL, **incoming}
                    payload = original.copy()
                    gateway.apply_siliconflow_effort(payload)
                    self.assertEqual(payload, {**original, **additions})

    async def test_tool_and_interrupted_streams_are_not_saved(self):
        from unittest.mock import Mock

        payloads = [
            (
                {"delta": {"reasoning_content": "测试推理"}, "finish_reason": None},
                False,
            ),
            ({"delta": {"content": "半句话"}, "finish_reason": None}, False),
            (
                {
                    "delta": {"tool_calls": [{"id": "call"}]},
                    "finish_reason": "tool_calls",
                },
                False,
            ),
            ({"delta": {"content": "完整回复"}, "finish_reason": "stop"}, True),
        ]
        for sample, expected in payloads:
            raw = (
                "data: "
                + json.dumps({"choices": [sample]}, ensure_ascii=False)
                + "\n\ndata: [DONE]\n\n"
            )
            reply = httpx.Response(200, content=raw.encode())
            with patch.object(gateway, "enqueue", Mock()) as enqueue:
                data = b"".join(
                    [
                        part
                        async for part in gateway.relay(
                            reply,
                            gateway.Completion(None, ("id", "s", 0, "用户消息", None)),
                        )
                    ]
                )
                self.assertEqual(data, raw.encode())
                self.assertEqual(enqueue.called, expected)

    async def test_organizer_route_preserves_results_and_isolates_chat(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        gateway.setup_queue(db)
        source = "Private input, only sent to the organizer"
        card = '{"title":"sample card"}'
        message = {"content": card, "reasoning_content": "private reasoning"}
        expected = {
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]
        }
        changes = [
            {"delta": {"reasoning_content": "private reasoning"}},
            {"delta": {"content": card[:6]}},
            {"delta": {"content": card[6:]}, "finish_reason": "stop"},
        ]
        stream = (
            "".join(
                "data: " + json.dumps({"choices": [item]}) + "\n\n" for item in changes
            )
            + "data: [DONE]\n\n"
        )

        def upstream(req):
            self.assertEqual(req.url.path, "/v1/chat/completions")
            self.assertEqual(
                json.loads(req.content)["messages"],
                [{"role": "user", "content": source}],
            )
            if json.loads(req.content).get("stream"):
                return httpx.Response(
                    200, text=stream, headers={"content-type": "text/event-stream"}
                )
            return httpx.Response(200, json=expected)

        auth = gateway.bearer("fixture")
        payload = {"model": MODEL, "messages": [{"role": "user", "content": source}]}
        path = "/aru-memory/v1/chat/completions"
        with patch.dict(
            os.environ,
            NIAN_CLIENT_SHA256=hashlib.sha256(b"fixture").hexdigest(),
            XINCHAO_CHAT_API_KEY="upstream",
        ):
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as model,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway.app),
                    base_url="https://test",
                ) as phone,
            ):
                with (
                    patch.object(gateway.app.state, "client", model, create=True),
                    patch.object(gateway.app.state, "db", db, create=True),
                    patch.object(gateway, "add_state", side_effect=AssertionError),
                    patch.object(gateway, "enqueue", side_effect=AssertionError),
                ):
                    self.assertEqual(
                        (await phone.get("/aru-memory/v1/models")).status_code, 401
                    )
                    self.assertEqual(
                        (await phone.post(path, json=payload)).status_code, 401
                    )
                    self.assertEqual(
                        (await phone.get("/aru-memory/v1/models", headers=auth)).json()[
                            "data"
                        ][0]["id"],
                        MODEL,
                    )
                    self.assertEqual(
                        (await phone.post(path, json=payload, headers=auth)).json(),
                        expected,
                    )
                    response = await phone.post(
                        path, json={**payload, "stream": True}, headers=auth
                    )
                    self.assertEqual(response.text, stream)
        rows = db.execute("SELECT output FROM aru_organizer_results").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0][0]),
            [{"index": 0, "finish_reason": "stop", "content": card, "tool_calls": []}],
        )
        self.assertEqual(db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    async def test_organizer_tool_deltas_and_incomplete_streams(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        gateway.setup_queue(db)
        body = {"model": MODEL}
        args = '{"id":"card1"}'
        chunks = [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"name": "edit", "arguments": args[:5]},
                        }
                    ]
                }
            },
            {
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]
                }
            },
        ]

        async def play(choices, done):
            raw = "".join(
                "data: " + json.dumps({"choices": [c]}) + "\n\n" for c in choices
            )
            if done:
                raw += "data: [DONE]\n\n"
            reply = httpx.Response(200, text=raw)
            forwarded = b"".join(
                [
                    piece
                    async for piece in gateway.relay(
                        reply, gateway.OrganizerResult(db, body)
                    )
                ]
            )
            self.assertEqual(forwarded, raw.encode())

        await play(chunks, False)
        await play(chunks + [{"delta": {}, "finish_reason": "length"}], True)
        self.assertFalse(db.execute("SELECT 1 FROM aru_organizer_results").fetchone())
        await play(chunks + [{"delta": {}, "finish_reason": "tool_calls"}], True)
        saved = json.loads(
            db.execute("SELECT output FROM aru_organizer_results").fetchone()[0]
        )
        self.assertEqual(saved[0]["tool_calls"], [{"name": "edit", "arguments": args}])


if __name__ == "__main__":
    unittest.main()
