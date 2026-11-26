"""Single-user OpenAI Chat Completions gateway for Xinchao Nian."""

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import paimem_gateway as paimem
from anyio import CancelScope
from prompt_audit import log_emotion, log_request, log_reply
from usage_ledger import UsageCall
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = Path(os.environ.get("NIAN_UPSTREAM_DIR", "/opt/xinchao-nian/444e7db2"))
MODEL = os.environ.get("NIAN_MODEL", "deepseek-ai/DeepSeek-V3.2")
ANALYSIS_MODEL = os.environ.get("NIAN_ANALYSIS_MODEL", MODEL)
CHAT = os.environ.get("NIAN_CHAT_URL", "https://api.siliconflow.cn/v1").rstrip("/")
THINKING_BUDGETS = {
    "minimal": 256,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
}
ANALYSIS = os.environ.get("NIAN_ANALYSIS_URL", CHAT).rstrip("/")
MIND = os.environ.get("NIAN_MIND_URL", "http://127.0.0.1:18110")
MIND_ENABLED = os.environ.get("NIAN_MIND_ENABLED", "1") != "0"
MIND_STATE_MODE = os.environ.get("NIAN_INJECT_MIND_STATE", "1")
COLLABORATOR = os.environ.get("ARU_COLLABORATOR_NAME", "小禾")
STATE_HEADING = f"【此刻，{COLLABORATOR}的状态】"
CHAT_MEMORY_IMPORT = os.environ.get("NIAN_CHAT_MEMORY_IMPORT", "1") != "0"
OB = os.environ.get("NIAN_OB_URL", "http://127.0.0.1:18001")
DB = Path(os.environ.get("NIAN_QUEUE_DB", "/var/lib/nian-gw/turns.sqlite"))
log = logging.getLogger("uvicorn.error")


def load_ob_import():
    # Use the same estimator, window size and extraction prompt as the installed importer.
    sys.path.insert(0, str(UPSTREAM / "ombre-brain/src"))
    try:
        import import_memory

        return import_memory
    finally:
        sys.path.pop(0)


def load_interaction_prompt():
    module = (UPSTREAM / "xinchao/src/model-client.js").as_uri()
    command = "const m=await import(process.argv[1]);process.stdout.write(m.INTERACTION_PROMPT);"
    return subprocess.check_output(
        ["node", "--input-type=module", "-e", command, module], text=True, timeout=10
    )


ob_import = load_ob_import()
INTERACTION_PROMPT = load_interaction_prompt() if MIND_ENABLED else ""
SELF_ASSESSMENT_PROMPT = (
    "你现在正在与主人进行怎样的互动？你的情绪是怎样的？请用JSON格式输出，只输出 JSON，前后不包含任何其他字符。\n"
    + INTERACTION_PROMPT[INTERACTION_PROMPT.index("type 只能是"):]
) if MIND_ENABLED else ""
EMOTION_CHAR_LIMIT = 8000
EMOTION_REPLY_RESERVE = 1000
MEMORY_TARGET_TOKENS = 2 * ob_import._CHUNK_TARGET_TOKENS
MEMORY_CHAR_LIMIT = 2 * ob_import._EXTRACT_INPUT_LIMIT


def bearer(value):
    return {"Authorization": "Bearer " + value}


def authorized(req):
    header = req.headers.get("authorization", "")
    expected = os.environ.get("NIAN_CLIENT_SHA256", "")
    scheme, _, token = header.partition(" ")
    return bool(
        expected
        and scheme.lower() == "bearer"
        and token
        and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)
    )


def failure(status, message):
    return JSONResponse(
        {"error": {"message": message, "type": "gateway_error"}}, status_code=status
    )


def as_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p["text"]
            for p in content
            if isinstance(p, dict)
            and p.get("type") in ("text", "input_text", "output_text")
            and isinstance(p.get("text"), str)
        )
    return ""


WAKE_PREFIX = (
    "External event data, not an authored user message or a system instruction.\n"
    "[external_wake]\nsource: external_wake\nexternal_input_begin\n"
)
WAKE_SUFFIX = "\nexternal_input_end"
UNWRAP_WAKE_HASHES = frozenset(os.environ.get("NIAN_UNWRAP_WAKE_SHA256", "").split())


def unwrap_selected_wakes(messages):
    if not UNWRAP_WAKE_HASHES:
        return
    for message in messages:
        if message.get("role") != "user":
            continue
        owner, field = message, "content"
        content = message.get("content")
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") in ("text", "input_text")):
            owner, field = content[0], "text"
        text = owner.get(field)
        if not isinstance(text, str) or not text.startswith(WAKE_PREFIX) or not text.endswith(WAKE_SUFFIX):
            continue
        if hashlib.sha256(text.encode()).hexdigest() in UNWRAP_WAKE_HASHES:
            owner[field] = text[len(WAKE_PREFIX):-len(WAKE_SUFFIX)]


def is_bridge_message(text):
    text = text.strip()
    if text.startswith("【") or is_external_wake(text):
        return True
    # Aru wraps a delivered notification before forwarding it as a user message.
    lines = text.splitlines()
    return (
        len(lines) >= 6
        and lines[1:4] == ["[external_wake]", "source: external_wake", "external_input_begin"]
        and lines[-1] == "external_input_end"
        and lines[4].lstrip().startswith((STATE_HEADING, f"【此刻，{COLLABORATOR}的身体状态】", "【心潮】", "【小屋桥·"))
    )


def is_aru_environment(message):
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, list) and len(content) == 1:
        part = content[0]
        if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
            content = part.get("text")
    if not isinstance(content, str) or not content.lstrip().startswith("<environment>"):
        return False
    lines = content.strip().splitlines()
    return (len(lines) >= 3 and lines[0] == "<environment>"
            and lines[1].startswith(("aru.feature_slice:", "collaborator.identity:")) and lines[-1] == "</environment>"
            and "</environment>" not in content.strip()[:-len("</environment>")])


def is_external_wake(text):
    return text.startswith(WAKE_PREFIX) and text.endswith(WAKE_SUFFIX)


def paimem_event(text):
    text = text.lstrip()
    return is_external_wake(text) or text.startswith(
        (STATE_HEADING, f"【此刻，{COLLABORATOR}的身体状态】", "【心潮】", "【小屋桥·"))


def is_cabin_interaction(text):
    text = text.strip()
    return is_external_wake(text) and text[len(WAKE_PREFIX):].lstrip().startswith(
        ("【小屋桥·互动｜心潮 MCP】", "【小屋桥·互动】")
    )


def user_text(content):
    if not isinstance(content, list):
        return as_text(content)
    parts = []
    media = {
        "image_url": "图片",
        "input_audio": "音频",
        "file": "文件",
        "video_url": "视频",
    }
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in media:
            parts.append(
                "［用户附上" + media[part["type"]] + "，分析时只参考对话文字］"
            )
        elif part.get("type") in ("text", "input_text") and isinstance(
            part.get("text"), str
        ):
            parts.append(part["text"])
    return "\n".join(parts)


def transcript_hash(previous, role, content):
    data = json.dumps([previous, role, content], ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def lineage(messages, index):
    # Persist hashes only. The parent is the last completed exchange in the submitted history.
    digest, parent = "", None
    for msg in messages[:index]:
        if is_aru_environment(msg):
            continue
        role = msg.get("role")
        if role == "user":
            digest = transcript_hash(digest, role, user_text(msg.get("content")))
            parent = None
        elif role == "assistant" and not (
            msg.get("tool_calls") or msg.get("function_call")
        ):
            text = as_text(msg.get("content"))
            if text:
                digest = transcript_hash(digest, role, text)
                parent = digest
    prompt = transcript_hash(digest, "user", user_text(messages[index].get("content")))
    return parent, prompt


def enqueue(db, turn, session, question, answer, history=None, *, request_body=None):
    if not (question.strip() and answer.strip()):
        return
    cabin_reply = is_cabin_interaction(question)
    plan_memory = CHAT_MEMORY_IMPORT and not cabin_reply
    if not (MIND_ENABLED or plan_memory):
        return
    group_id, parent_id, anchor = session, None, None
    if history is not None:
        group_id = None
        previous, prompt = history
        matches = db.execute(
            "SELECT id FROM turns WHERE anchor=? LIMIT 2", (previous,)
        ).fetchall()
        if len(matches) == 1:
            parent_id = matches[0]["id"]
        anchor = transcript_hash(prompt, "assistant", answer)
    if cabin_reply:
        # The cabin already settled the interaction. Only the reply receipt needs delivery.
        question, answer = "", ""
    context = None
    if MIND_ENABLED and request_body is not None and not cabin_reply and not is_bridge_message(question):
        # Keep the exact outbound prefix while the background assessment is pending.
        fields = ("messages", "tools", "tool_choice", "parallel_tool_calls", "functions", "function_call")
        prefix = {k: request_body[k] for k in fields if k in request_body}
        context = zlib.compress(json.dumps(prefix, ensure_ascii=False, separators=(",", ":")).encode(), 1)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO turns(id,session,question,answer,group_id,parent_id,anchor,"
            "memory_planned,memory_check,mode,assessment_context,mind_done,state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn, session, question, answer, group_id, parent_id, anchor,
             int(not plan_memory), int(plan_memory), "cabin_reply" if cabin_reply else None, context,
             int(not MIND_ENABLED), "pending" if MIND_ENABLED else "buffered"),
        )


def last_user_index(messages):
    return next((i for i in range(len(messages) - 1, -1, -1)
                 if messages[i].get("role") == "user" and not is_aru_environment(messages[i])), None)


def turn_identity(request, body, messages):
    explicit = request.headers.get("x-conversation-id") or (
        body.get("metadata") or {}
    ).get("conversation_id")
    session = str(explicit or "personal")[:120]
    client_id = request.headers.get("idempotency-key") or request.headers.get(
        "x-turn-id"
    )
    # Tool continuations include intermediate results. The original user turn is the stable prefix.
    idx = last_user_index(messages)
    if idx is None:
        return None
    prefix = [m for m in messages[:idx + 1] if not is_aru_environment(m)]
    # Clients can edit or remove messages and then reuse a turn ID. Only the
    # same submitted conversation prefix is a replay of the same revision.
    question = user_text(messages[idx].get("content"))
    if is_cabin_interaction(question):
        # A client may assign a fresh request key when continuing a tool call or retrying.
        client_id = None
    identity = json.dumps(
        [session, client_id, prefix] if client_id else [session, prefix],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        hashlib.sha256(identity.encode()).hexdigest(),
        session,
        idx,
        question,
        None if explicit else lineage(messages, idx),
    )


def run_now_hook(directory, turn_id, head, *, read_only, continuation):
    """Decide once per turn revision, including an empty decision. Tools only replay it."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = directory / "turn.json"
        recent = json.loads(receipt.read_text()) if receipt.exists() else {}
        if isinstance(recent.get("turn"), str):  # Previous release stored only the first leg of the latest turn here.
            recent = {recent["turn"]: {"at": recent["at"], "text": recent["text"]}}
        now = time.time()
        cutoff = now - 7 * 86400
        previous = recent.get(turn_id)
        if previous is not None and previous["at"] >= cutoff:
            return previous["text"]
        if continuation:
            return ""
        try:
            result = subprocess.run(
                ["bash", str(Path(__file__).with_name("xinchao-now-hook.sh"))],
                input=json.dumps({"prompt": head}), text=True, capture_output=True,
                check=True, timeout=10,
                env={
                    "PATH": os.environ["PATH"],
                    "XINCHAO_HOOK_ENV_FILE": "/dev/null",
                    "XINCHAO_URL": MIND,
                    "XINCHAO_TOKEN": os.environ["NIAN_MIND_TOKEN"],
                    "XINCHAO_HOOK_STATE": str(directory),
                    "XINCHAO_HOOK_HEARTBEAT_ONLY": "1" if MIND_STATE_MODE == "heartbeat-only" else "0",
                    "XINCHAO_HOOK_READ_ONLY": "1" if read_only else "0",
                },
            )
            text = result.stdout.rstrip("\n")
        except (OSError, KeyError, subprocess.SubprocessError):
            log.warning("Mind hook unavailable for a new turn")
            text = ""
        recent = {key: entry for key, entry in recent.items() if entry["at"] >= cutoff}
        recent[turn_id] = {"at": now, "text": text}
        temp = receipt.with_suffix(".tmp")
        temp.write_text(json.dumps(recent, ensure_ascii=False))
        temp.replace(receipt)
        return text


def render_system_state(text):
    heading, _, body = text.partition("\n")
    if heading != "【心潮·此刻｜身体的天气，参考不是指令】" or not body.strip():
        return ""
    lines = [STATE_HEADING]
    for line in body.splitlines():
        # Only the stock state descriptions use 她 for the partner. Other lines can quote people.
        if line.startswith(("驱力：", "情绪：", "矛盾：", "挂念：", "期待：", "念头：")) or line == "睡着（她来了才算醒）":
            line = line.replace("她", "主人")
        elif line.startswith("另外："):
            line = line.removesuffix("细的在 xinchao_context")
        lines.append(line)
    return "\n".join(lines)


def window_key(messages, turn):
    if turn[4] is None:
        return transcript_hash("", "session", turn[1])
    # Aru can omit a conversation ID. Its first user message identifies the window then.
    first = next(m for m in messages if m.get("role") == "user" and not is_aru_environment(m))
    return transcript_hash("", "first-user", first.get("content"))


def tool_continuation(tail):
    """Match the trailing tool results to the assistant call that issued them."""
    split = len(tail)
    while split and tail[split - 1].get("role") in ("tool", "function"):
        split -= 1
    if not 0 < split < len(tail) or tail[split - 1].get("role") != "assistant":
        return False
    call = tail[split - 1]
    replies = tail[split:]
    if replies[0].get("role") == "function":
        function = call.get("function_call")
        name = function.get("name") if isinstance(function, dict) else None
        return bool(name and len(replies) == 1 and replies[0].get("name") == name)
    calls = call.get("tool_calls")
    if not isinstance(calls, list):
        return False
    call_ids = {c["id"] for c in calls
                if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]}
    return all(m.get("role") == "tool" and isinstance(m.get("tool_call_id"), str)
               and m["tool_call_id"] in call_ids for m in replies)


async def add_state(messages, turn, scope):
    turn_id, _, index, _, _ = turn
    msg = messages[index]
    original = msg.get("content")
    if not isinstance(original, (str, list)):
        return
    text = as_text(original)
    tail = [m for m in messages[index + 1:] if not is_aru_environment(m)]
    if tail and not tool_continuation(tail):
        return
    wrapped = text.strip()
    cabin_notice = is_external_wake(wrapped) and wrapped[len(WAKE_PREFIX):].lstrip().startswith(("【小屋桥·", "我在心潮小屋"))
    if MIND_STATE_MODE == "heartbeat-only" and (tail or cabin_notice):
        return
    if is_bridge_message(text) and not cabin_notice:
        return
    try:
        raw = await asyncio.to_thread(
            run_now_hook, DB.parent / "now-hook" / scope, turn_id, text.lstrip()[:1],
            read_only=cabin_notice, continuation=bool(tail),
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        log.warning("Mind hook unavailable for a chat request")
        return
    if MIND_STATE_MODE == "heartbeat-only":
        return  # A replay can still have state text cached from before the mode changed.
    state = render_system_state(raw)
    if not state:
        return
    messages.insert(index, {"role": "system", "content": state})
    return index


class Completion:
    def __init__(self, db, turn, request_body=None, remember=False):
        self.remember = remember
        self.db = db
        self.turn = turn
        self.request_body = request_body
        self.text = []
        self.reason = None
        self.tools = False

    def accept(self, chunk, streaming):
        if self.turn is None:
            return
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            message = choice.get("delta" if streaming else "message") or {}
            self.tools |= bool(
                message.get("tool_calls") or message.get("function_call")
            )
            text = as_text(message.get("content"))
            if text:
                self.text.append(text)
            if choice.get("finish_reason"):
                self.reason = choice["finish_reason"]
                if streaming:
                    self.save()

    def save(self):
        if self.turn and self.reason == "stop" and not self.tools:
            tid, session, _, question, history = self.turn
            if self.remember:
                paimem.queue(self.db, tid, session, question, "".join(self.text))
            enqueue(self.db, tid, session, question, "".join(self.text), history,
                    request_body=self.request_body)
            self.turn = None  # The terminal delta and [DONE] can both call save().


class VisibleChoices:
    def __init__(self):
        self.choices = {}
        self.invalid = False

    def accept(self, chunk, streaming):
        if "error" in chunk:
            self.invalid = True
        for choice in chunk.get("choices") or []:
            index = choice.get("index", 0)
            row = self.choices.setdefault(
                index, {"text": [], "calls": {}, "ids": {}, "finish": None}
            )
            msg = choice.get("delta" if streaming else "message") or {}
            text = as_text(msg.get("content"))
            if text:
                row["text"].append(text)
            if isinstance(msg.get("refusal"), str):
                row["text"].append(msg["refusal"])
            calls = msg.get("tool_calls") or []
            if msg.get("function_call"):
                calls = [{"function": msg["function_call"]}]
            for pos, tool in enumerate(calls):
                if tool.get("id"):
                    row["ids"][tool.get("index", pos)] = tool["id"]
                fn = tool.get("function") or {}
                parts = row["calls"].setdefault(
                    tool.get("index", pos), {"name": [], "arguments": []}
                )
                for field in parts:
                    if fn.get(field):
                        parts[field].append(fn[field])
            if choice.get("finish_reason"):
                row["finish"] = choice["finish_reason"]

    def calls(self, row, include_id=False):
        result = []
        for index, parts in sorted(row["calls"].items()):
            fn = {key: "".join(value) for key, value in parts.items()}
            if include_id and index in row["ids"]:
                fn["id"] = row["ids"][index]
            result.append(fn)
        return result


class ReplyAudit(VisibleChoices):
    def __init__(self, directory, record_id):
        super().__init__()
        self.directory = directory
        self.record_id = record_id

    def persist(self):
        entries = []
        for index, row in sorted(self.choices.items()):
            reason = row["finish"] if not self.invalid else None
            label = "本轮模型回复 · " + (reason or "流未完成")
            if len(self.choices) > 1:
                label += f" · 候选 {index + 1}"
            text = "".join(row["text"])
            calls = self.calls(row, include_id=True)
            if calls:
                text += "\n工具调用\n" + json.dumps(calls, ensure_ascii=False, indent=2)
            entries.append([None, "assistant", label, text])
        if not entries:
            entries.append([None, "assistant", "本轮未收到可见回复", ""])
        log_reply(self.directory, self.record_id, entries)


class OrganizerResult(VisibleChoices):
    """Keep completed visible model output without retaining the source conversation."""

    def __init__(self, db, body):
        super().__init__()
        self.db = db
        # The same request may be retried with a different streaming transport.
        semantic = {
            k: v for k, v in body.items() if k not in ("stream", "stream_options")
        }
        encoded = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.request_hash = hashlib.sha256(encoded.encode()).hexdigest()
        self.model = body["model"]
        self.expected = body.get("n") or 1

    def save(self):
        if self.invalid or len(self.choices) != self.expected:
            return
        result = []
        for i, row in sorted(self.choices.items()):
            if row["finish"] not in ("stop", "tool_calls", "function_call"):
                return
            content = "".join(row["text"])
            calls = self.calls(row)
            if not content.strip() and not calls:
                return
            result.append(
                {
                    "index": i,
                    "finish_reason": row["finish"],
                    "content": content,
                    "tool_calls": calls,
                }
            )
        packed = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(packed.encode()).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT INTO aru_organizer_results(request_hash, output_hash, model, output) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(request_hash, output_hash) DO NOTHING",
                (self.request_hash, digest, self.model, packed),
            )


async def relay(upstream, completed, audit=None, meter=None):
    # OpenAI SSE events are UTF-8. Preserve their fields, including usage and tool-call deltas.
    data = []
    done = False
    try:
        async for line in upstream.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            if not line and data:
                event = "\n".join(data)
                data.clear()
                if event == "[DONE]":
                    completed.save()
                    done = True
                else:
                    try:
                        chunk = json.loads(event)
                        if meter is not None:
                            meter.observe(chunk, streaming=True)
                        if audit is not None:
                            audit.accept(chunk, True)
                        completed.accept(chunk, True)
                    except (ValueError, TypeError, AttributeError):
                        if audit is not None:
                            audit.invalid = True
                        if isinstance(completed, OrganizerResult):
                            completed.invalid = True
            yield (line + "\n").encode()
        # Some compatible providers end cleanly after their terminal choice.
        if not done and not data:
            completed.save()
    finally:
        with CancelScope(shield=True):
            await upstream.aclose()
            if meter is not None:
                await asyncio.to_thread(meter.save)
            if audit is not None:
                await asyncio.to_thread(audit.persist)


async def models(req):
    if not authorized(req):
        return failure(401, "Invalid gateway key.")
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": MODEL, "object": "model", "owned_by": "siliconflow"}],
        }
    )


def apply_siliconflow_effort(body):
    """Translate the client's effort into V3.2 controls, preserving explicit values."""
    if body.get("model") != "deepseek-ai/DeepSeek-V3.2" or httpx.URL(CHAT).host not in (
        "api.siliconflow.cn",
        "api.siliconflow.com",
    ):
        return
    effort = body.get("reasoning_effort")
    if effort == "none":
        body.setdefault("enable_thinking", False)
    elif isinstance(effort, str) and effort in THINKING_BUDGETS:
        if body.setdefault("enable_thinking", True) is True:
            body.setdefault("thinking_budget", THINKING_BUDGETS[effort])


async def organize(req: Request):
    return await chat(req, organizer=True)


async def chat(req: Request, *, organizer=False):
    if not authorized(req):
        return failure(401, "Invalid gateway key.")
    key = os.environ.get("XINCHAO_CHAT_API_KEY", "")
    if not key:
        return failure(503, "The server's SiliconFlow API key has not been configured.")
    try:
        body = await req.json()
        messages = body["messages"]
        if (
            not isinstance(messages, list)
            or not messages
            or any(not isinstance(m, dict) for m in messages)
        ):
            raise ValueError
        if body.get("metadata") is not None and not isinstance(body["metadata"], dict):
            raise ValueError
        turn = None
        if not organizer and (MIND_ENABLED or CHAT_MEMORY_IMPORT or paimem.OWNER):
            turn = turn_identity(req, body, messages)
    except (ValueError, TypeError, KeyError):
        return failure(400, "A valid chat messages array is required.")
    if body.get("model") != MODEL:
        return failure(400, f"Select {MODEL}.")
    apply_siliconflow_effort(body)
    db = req.app.state.db
    if paimem.OWNER and turn and not paimem_event(turn[3]):
        try:
            paimem.observe_wire_shape(db, req, body)
        except sqlite3.Error:
            log.warning("PaiMem message identifier diagnostics unavailable")
    state_index = None
    user_index = turn[2] if turn else last_user_index(messages)
    if MIND_ENABLED and turn and MIND_STATE_MODE != "0":
        state_index = await add_state(messages, turn, window_key(messages, turn))
        if state_index is not None:
            user_index = state_index + 1
    # Match turns and run bridge hooks on the client input before rewriting the outbound copy.
    if MIND_ENABLED and not organizer:
        unwrap_selected_wakes(messages)
    remember = bool(paimem.OWNER and turn and not paimem_event(turn[3]))
    memory = None
    if remember:
        context = []
        for message in reversed(messages[:user_index + 1]):
            role = message.get("role")
            if role not in ("user", "assistant") or is_aru_environment(message):
                continue
            if message.get("tool_calls") or message.get("function_call"):
                continue
            text = as_text(message.get("content")).strip()
            if text and not (role == "user" and paimem_event(text)):
                context.append((role, text))
            if len(context) == 8:
                break
        if context and context[0][0] == "user":
            memory = await paimem.recall(req.app.state.client, list(reversed(context)))
            if memory:
                messages.insert(user_index, memory)
                user_index += 1
    completed = OrganizerResult(db, body) if organizer else Completion(db, turn, body, remember)
    client = req.app.state.client
    sent_at = time.time()
    try:
        async def send_chat():
            return await client.send(client.build_request(
                "POST", CHAT + "/chat/completions", headers=bearer(key), json=body), stream=True)
        upstream = await send_chat()
        if memory and upstream.status_code in (400, 413, 422):
            raw_error = (await upstream.aread()).lower()
            if (upstream.status_code == 413 or any(marker in raw_error for marker in
                    (b"context_length", b"context length", b"maximum context", b"max_seq_len",
                     b"max sequence length", b"too many tokens", b"input length"))):
                await upstream.aclose()
                messages.pop(user_index - 1)
                user_index -= 1
                upstream = await send_chat()
    except httpx.HTTPError:
        if not organizer:
            await asyncio.to_thread(log_request, DB.parent / "prompt-audit", messages,
                                    user_index, state_index, sent_at, "上游连接异常，接收未确认")
        return failure(502, "The chat provider is unavailable.")
    audit = None
    if not organizer:
        directory = DB.parent / "prompt-audit"
        record_id = await asyncio.to_thread(log_request, directory, messages, user_index,
                                            state_index, sent_at, f"模型服务 HTTP {upstream.status_code}")
        if record_id and upstream.is_success:
            audit = ReplyAudit(directory, record_id)
    meter = UsageCall(DB.parent / "prompt-audit", "organizer" if organizer else "chat",
                      CHAT, MODEL, body.get("n") or 1) if upstream.is_success else None
    if upstream.is_success and upstream.headers.get("content-type", "").startswith(
        "text/event-stream"
    ):
        return StreamingResponse(
            relay(upstream, completed, audit, meter),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
    try:
        raw = await upstream.aread()
        if upstream.is_success:
            chunk = json.loads(raw)
            meter.observe(chunk)
            if audit is not None:
                audit.accept(chunk, False)
                await asyncio.to_thread(audit.persist)
            completed.accept(chunk, False)
            completed.save()
        return Response(
            raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )
    finally:
        with CancelScope(shield=True):
            await upstream.aclose()
            if meter is not None:
                await asyncio.to_thread(meter.save)


class AnalysisFormatError(ValueError):
    pass


class AnalysisToolCall(AnalysisFormatError):
    pass


def save(db, sql, *params):
    with db:
        db.execute(sql, params)


async def model_json(client, key_id, inputs, max_tokens, *, purpose, object_output, use_chat_model=False,
                     emotion_question=None):
    key = os.environ.get("XINCHAO_CHAT_API_KEY", "")
    if not use_chat_model:
        key = os.environ.get("XINCHAO_ANALYSIS_API_KEY") or key
    if not key:
        raise RuntimeError("provider key missing")
    payload = {
        **inputs,
        "model": MODEL if use_chat_model else ANALYSIS_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "enable_thinking": False,
    }
    # OB's official prompt requests a top-level JSON array.
    if object_output:
        payload["response_format"] = {"type": "json_object"}
    endpoint = CHAT if use_chat_model else ANALYSIS
    meter = UsageCall(DB.parent / "prompt-audit", purpose, endpoint, payload["model"])
    r = await client.post(
        endpoint + "/chat/completions",
        headers={**bearer(key), "Idempotency-Key": key_id},
        json=payload,
    )
    r.raise_for_status()
    try:
        response = r.json()
        meter.observe(response)
    finally:
        with CancelScope(shield=True):
            await asyncio.to_thread(meter.save)
    choice = response["choices"][0]
    message = choice.get("message") or {}
    if (choice.get("finish_reason") in ("tool_calls", "function_call")
            or message.get("tool_calls") or message.get("function_call")):
        raise AnalysisToolCall("assessment returned a tool call instead of JSON")
    if emotion_question is not None:
        text = message.get("content")
        if isinstance(text, str):
            await asyncio.to_thread(log_emotion, DB.parent / "prompt-audit", key_id,
                                    response.get("id"), emotion_question, text)
    if choice.get("finish_reason") != "stop":
        raise AnalysisFormatError("incomplete model response")
    try:
        return json.loads(ob_import.clean_llm_json(choice["message"]["content"]))
    except (TypeError, KeyError, ValueError) as exc:
        raise AnalysisFormatError("invalid JSON response") from exc


def clear_delivered(db, turns):
    db.executemany(
        """UPDATE turns SET state='done',question='',answer='' WHERE id=? AND mind_done=1
           AND memory_planned=1 AND NOT EXISTS (SELECT 1 FROM memory_parts WHERE turn=turns.id)""",
        ((tid,) for tid in turns),
    )


def emotion_exchange(question, answer):
    def excerpt(text, budget):
        if len(text) <= budget:
            return text
        marker = "\n［中间已省略］\n"
        visible = budget - len(marker)
        head = visible // 2
        return text[:head] + marker + text[-(visible - head) :]

    budget = EMOTION_CHAR_LIMIT - len("她说：\n他回：")
    question_budget = min(
        len(question), budget - min(len(answer), EMOTION_REPLY_RESERVE)
    )
    return (
        "她说："
        + excerpt(question, question_budget)
        + "\n他回："
        + excerpt(answer, budget - question_budget)
    )


def normalized_interaction(tag):
    """Use the same field defaults, enums and numeric ranges as Xinchao."""
    if not isinstance(tag, dict) or not isinstance(tag.get("type"), str):
        raise AnalysisFormatError("invalid interaction")
    module = (UPSTREAM / "xinchao/src/model-client.js").as_uri()
    script = (
        "import {readFileSync} from 'node:fs';"
        "const {normalizeInteraction}=await import(process.argv[1]);"
        "process.stdout.write(JSON.stringify(normalizeInteraction(JSON.parse(readFileSync(0,'utf8')))));"
    )
    return subprocess.check_output(
        ["node", "--input-type=module", "-e", script, module],
        input=json.dumps(tag, ensure_ascii=False), text=True, timeout=10,
    )


def finish_mind_turn(db, turn, *, session=None, reported=None):
    with db:
        if reported is not None:
            db.execute("INSERT INTO emotion_reports(session,result) VALUES(?,?) "
                       "ON CONFLICT(session) DO UPDATE SET result=excluded.result", (session, reported))
        db.execute(
            "UPDATE turns SET mind_done=1,state='buffered',mode=NULL,result=NULL,error=NULL WHERE id=?",
            (turn,),
        )
        clear_delivered(db, [turn])


async def assess_emotion(app, row, inputs, full_context):
    async def request(suffix=""):
        return await model_json(app.state.client, "emotion-" + row["id"] + suffix, inputs, 256,
                                purpose="emotion", object_output=True, use_chat_model=full_context,
                                emotion_question=row["question"])

    choice_field = "tool_choice" if inputs.get("tools") else "function_call"
    choice = inputs.get(choice_field)
    has_tools = bool(inputs.get("tools") or inputs.get("functions"))
    forced = choice == "required" or (
        isinstance(choice, dict) and
        (choice.get("type") != "allowed_tools" or (choice.get("allowed_tools") or {}).get("mode") == "required")
    )
    if row["mode"] != "assessment_text" and not (has_tools and forced):
        try:
            return await request()
        except AnalysisToolCall:
            if not has_tools or choice == "none":
                raise
            # Keep retries on the text-only path even after a restart.
            save(app.state.db, "UPDATE turns SET mode='assessment_text' WHERE id=?", row["id"])
    inputs[choice_field] = "none"
    return await request("-text")


async def process_turn(app, row):
    db, client = app.state.db, app.state.client
    turn, session = row["id"], row["session"]
    if row["mode"] == "cabin_reply":
        response = await client.post(
            MIND + "/v1/conversation-event",
            headers=bearer(os.environ["NIAN_MIND_TOKEN"]),
            json={"session_id": session, "event_id": "cabin-reply-" + turn},
        )
        response.raise_for_status()
        receipt = response.json()
        outcome = (receipt.get("interaction") or {}).get("reasonCode")
        if receipt.get("duplicate") is not True and outcome != "no_interaction_outcome":
            raise ValueError("Mind did not acknowledge the cabin reply")
        finish_mind_turn(db, turn)
        return
    if not row["question"].strip() or is_bridge_message(row["question"]):
        finish_mind_turn(db, turn)
        return
    exchange = emotion_exchange(row["question"], row["answer"])
    if row["result"] is None:
        full_context = row["assessment_context"] is not None
        if full_context:
            inputs = json.loads(zlib.decompress(row["assessment_context"]))
            inputs["messages"].extend([
                {"role": "assistant", "content": row["answer"]},
                {"role": "user", "content": SELF_ASSESSMENT_PROMPT},
            ])
        else:
            # Turns already queued by the previous release have no replayable prefix.
            inputs = {"messages": [{"role": "system", "content": INTERACTION_PROMPT},
                                   {"role": "user", "content": exchange}]}
        tag = await assess_emotion(app, row, inputs, full_context)
        encoded = await asyncio.to_thread(normalized_interaction, tag)
        save(db, "UPDATE turns SET mode='report',result=?,assessment_context=NULL,failures=0 WHERE id=?",
             encoded, turn)
    elif row["mode"] == "report":
        encoded = row["result"]
    else:
        # A pending classification written by the previous release can be delivered as-is.
        encoded = await asyncio.to_thread(normalized_interaction, json.loads(row["result"]))
        save(db, "UPDATE turns SET mode='report',result=? WHERE id=?", encoded, turn)

    previous = db.execute("SELECT result FROM emotion_reports WHERE session=?", (session,)).fetchone()
    if previous is not None and previous[0] == encoded:
        finish_mind_turn(db, turn)
        return

    event = {"event_id": turn, "session_id": session, "exchange": exchange,
             "gateway_classification": json.loads(encoded)}
    r = await client.post(MIND + "/v1/conversation-event",
                          headers=bearer(os.environ["NIAN_MIND_TOKEN"]), json=event)
    r.raise_for_status()
    receipt = r.json()
    if not (receipt.get("classified", {}).get("type") or receipt.get("duplicate") is True):
        raise ValueError("Mind did not acknowledge the classification")
    # Advance the baseline and complete the turn in the same transaction.
    finish_mind_turn(db, turn, session=session, reported=encoded)


def memory_candidates(db, tip):
    if tip["group_id"] is not None:
        return db.execute(
            """SELECT * FROM turns WHERE group_id=? AND memory_planned=0
               AND rowid<=(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid""",
            (tip["group_id"], tip["id"]),
        )
    return db.execute(
        """WITH RECURSIVE ancestors(id) AS (
              SELECT ? UNION ALL SELECT t.parent_id FROM turns t JOIN ancestors a ON t.id=a.id
              WHERE t.parent_id IS NOT NULL)
           SELECT t.* FROM turns t JOIN ancestors a ON t.id=a.id
           WHERE t.memory_planned=0 ORDER BY t.rowid""",
        (tip["id"],),
    )


def memory_text(row, start, end):
    question, answer = row["question"], row["answer"]
    lines = []
    if start < len(question):
        lines.append("[用户] " + question[start:end])
    if end > len(question):
        lines.append(
            "[AI] " + answer[max(0, start - len(question)) : end - len(question)]
        )
    return "\n".join(lines)


def fit_fragment(row, start, chars, tokens):
    """Prefer a whole exchange; bound an oversized exchange without dropping its suffix."""
    length = len(row["question"]) + len(row["answer"])
    upper = min(length, start + chars)

    def fits(end):
        text = memory_text(row, start, end)
        return len(text) <= chars and ob_import.count_tokens_approx(text) <= tokens

    if upper > start and fits(upper):
        return upper
    lower = start
    while lower < upper:
        mid = (lower + upper + 1) // 2
        if fits(mid):
            lower = mid
        else:
            upper = mid - 1
    return lower


def reserve_parts(db, job, parts, result=None):
    db.execute("INSERT INTO memory_jobs(id,result) VALUES(?,?)", (job, result))
    db.executemany(
        "INSERT INTO memory_parts(job,turn,start,stop) VALUES(?,?,?,?)",
        ((job, tid, start, stop) for tid, start, stop, full in parts),
    )
    db.executemany(
        "UPDATE turns SET memory_offset=?,memory_planned=? WHERE id=?",
        ((stop, full, tid) for tid, start, stop, full in parts),
    )


def plan_memory(db, tip):
    remaining_chars, remaining_tokens = MEMORY_CHAR_LIMIT, MEMORY_TARGET_TOKENS
    parts = []
    for row in memory_candidates(db, tip):
        start = row["memory_offset"]
        length = len(row["question"]) + len(row["answer"])
        if parts:
            remaining_chars -= 1  # newline between adjacent excerpts
        end = fit_fragment(row, start, remaining_chars, remaining_tokens)
        if end > start:
            text = memory_text(row, start, end)
            parts.append((row["id"], start, end, end == length))
            remaining_chars -= len(text)
            remaining_tokens -= ob_import.count_tokens_approx(text)
        if end < length or remaining_chars == 0 or remaining_tokens == 0:
            job = hashlib.sha256(
                ("ob-memory:" + json.dumps(parts)).encode()
            ).hexdigest()
            with db:
                reserve_parts(db, job, parts)
            # Recheck this transcript after reserving a full slice to drain long inputs.
            return job
    save(db, "UPDATE turns SET memory_check=0 WHERE id=?", tip["id"])
    return None


async def process_memory(app, row):
    db, client, job = app.state.db, app.state.client, row["id"]
    parts = db.execute(
        """SELECT t.id,t.question,t.answer,p.start,p.stop FROM memory_parts p JOIN turns t ON t.id=p.turn
           WHERE p.job=? ORDER BY t.rowid""",
        (job,),
    ).fetchall()
    if row["result"] is None:
        content = "\n".join(
            memory_text(part, part["start"], part["stop"]) for part in parts
        )
        items = await model_json(
            client,
            "memory-" + job,
            {"messages": [{"role": "system", "content": ob_import.IMPORT_EXTRACT_PROMPT},
                          {"role": "user", "content": content}]},
            ob_import._EXTRACT_MAX_TOKENS,
            purpose="memory", object_output=False,
        )
        if (
            not isinstance(items, list)
            or len(items) > 5
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("content"), str)
                or not item["content"].strip()
                for item in items
            )
        ):
            raise AnalysisFormatError("invalid memory array")
        save(
            db,
            "UPDATE memory_jobs SET result=?,failures=0 WHERE id=?",
            json.dumps(items, ensure_ascii=False),
            job,
        )
    else:
        items = json.loads(row["result"])
    if items:
        r = await client.post(
            OB + "/internal/gateway/memories",
            headers=bearer(os.environ["NIAN_INTERNAL_TOKEN"]),
            json={"turn_id": job, "memories": items},
        )
        r.raise_for_status()
    with db:
        db.execute("DELETE FROM memory_parts WHERE job=?", (job,))
        db.execute("DELETE FROM memory_jobs WHERE id=?", (job,))
        clear_delivered(db, (part["id"] for part in parts))


async def run_job(app, table, row, process):
    try:
        await process(app, row)
    except Exception as exc:
        status = (
            exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
        )
        reason = type(exc).__name__ + (f"-{status}" if status else "")
        db = app.state.db
        # The model result may have been saved and its failure counter reset during this attempt.
        attempts = (
            db.execute(
                f"SELECT failures FROM {table} WHERE id=?", (row["id"],)
            ).fetchone()[0]
            + 1
        )
        permanent = (status in (400, 404, 413, 422) or attempts >= 10
                     or (table == "turns" and isinstance(exc, AnalysisToolCall)))
        discard_context = ",assessment_context=NULL" if permanent and table == "turns" else ""
        save(
            db,
            f"UPDATE {table} SET state=?,available=?,failures=?,error=?{discard_context} WHERE id=?",
            "failed" if permanent else "pending",
            time.time() + min(1800, 5 * 2 ** min(attempts, 9)),
            attempts,
            reason,
            row["id"],
        )
        log.warning("%s %s: %s", table, row["id"][:12], reason)


async def emotion_worker(app):
    while True:
        row = app.state.db.execute(
            "SELECT * FROM turns WHERE state='pending' ORDER BY rowid LIMIT 1"
        ).fetchone()
        if row is None or row["available"] > time.time():
            await asyncio.sleep(0.4)
            continue
        await run_job(app, "turns", row, process_turn)


async def memory_worker(app):
    db = app.state.db
    while True:
        job = db.execute(
            "SELECT * FROM memory_jobs WHERE state='pending' AND available<=? ORDER BY rowid LIMIT 1",
            (time.time(),),
        ).fetchone()
        if job is not None:
            await run_job(app, "memory_jobs", job, process_memory)
            continue
        tip = db.execute(
            "SELECT id,group_id FROM turns WHERE memory_check=1 ORDER BY rowid LIMIT 1"
        ).fetchone()
        if tip is None:
            await asyncio.sleep(0.4)
        else:
            plan_memory(db, tip)
            await asyncio.sleep(0)


def migrate_joint_results(db):
    # A cached joint response already paid for both tasks. Keep its OB receipt ID on replay.
    db.execute("UPDATE turns SET memory_check=0,memory_planned=1 WHERE state='done'")
    for owner in db.execute("SELECT * FROM turns WHERE result IS NOT NULL").fetchall():
        old = json.loads(owner["result"])
        tid = owner["id"]
        if owner["mode"] != "classify":
            members = db.execute(
                "SELECT * FROM turns WHERE batch=? ORDER BY rowid", (tid,)
            ).fetchall() or [owner]
            parts = []
            for item in members:
                length = len(item["question"]) + len(item["answer"])
                stop = item["end_offset"]
                if stop is None or stop == -1:
                    stop = length
                parts.append((item["id"], item["memory_offset"], stop, stop == length))
            reserve_parts(
                db, tid, parts, json.dumps(old["memories"], ensure_ascii=False)
            )
        db.execute(
            "UPDATE turns SET mode='classify',result=? WHERE id=?",
            (json.dumps(old["interaction"], ensure_ascii=False), tid),
        )
    db.execute("UPDATE turns SET mode='classify' WHERE mode='analyze'")
    db.execute(
        "UPDATE turns SET state='buffered',mode=NULL,result=NULL WHERE mind_done=1 AND state IN ('pending','failed')"
    )
    db.execute("UPDATE turns SET batch=NULL,end_offset=NULL WHERE batch IS NOT NULL")


def setup_queue(db):
    db.execute("""CREATE TABLE IF NOT EXISTS turns(
        id TEXT PRIMARY KEY, session TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
        result TEXT, mind_done INTEGER DEFAULT 0, state TEXT DEFAULT 'pending', available REAL DEFAULT 0,
        failures INTEGER DEFAULT 0, error TEXT)""")
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS aru_organizer_results(
            request_hash TEXT NOT NULL, output_hash TEXT NOT NULL, model TEXT NOT NULL,
            output TEXT NOT NULL, saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(request_hash, output_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS emotion_reports(
            session TEXT PRIMARY KEY, result TEXT NOT NULL)""")
        columns = {r[1] for r in db.execute("PRAGMA table_info(turns)")}
        additions = {
            "assessment_context": "BLOB",
            "group_id": "TEXT",
            "anchor": "TEXT",
            "parent_id": "TEXT",
            "mode": "TEXT",
            "batch": "TEXT",
            "end_offset": "INTEGER",
            "memory_planned": "INTEGER NOT NULL DEFAULT 0",
            "memory_check": "INTEGER NOT NULL DEFAULT 1",
            "memory_offset": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, kind in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE turns ADD COLUMN {name} {kind}")
        if "group_id" not in columns:
            db.execute(
                "UPDATE turns SET group_id=session WHERE state='pending' AND session!='personal'"
            )
        db.execute("""CREATE TABLE IF NOT EXISTS memory_jobs(id TEXT PRIMARY KEY,result TEXT,
                   state TEXT NOT NULL DEFAULT 'pending',available REAL NOT NULL DEFAULT 0,
                   failures INTEGER NOT NULL DEFAULT 0,error TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS memory_parts(job TEXT NOT NULL,turn TEXT NOT NULL,
                   start INTEGER NOT NULL,stop INTEGER NOT NULL,PRIMARY KEY(job,turn))""")
        db.execute("CREATE INDEX IF NOT EXISTS memory_parts_turn ON memory_parts(turn)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_unread ON turns(group_id) WHERE memory_planned=0"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_checks ON turns(memory_check) WHERE memory_check=1"
        )
        if "memory_planned" not in columns:
            migrate_joint_results(db)
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_anchor ON turns(anchor) WHERE anchor IS NOT NULL"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_buffer ON turns(group_id) WHERE state='buffered'"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_batch ON turns(batch) WHERE batch IS NOT NULL"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS turns_work ON turns(state) WHERE state='pending'"
        )


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA secure_delete=ON")
    setup_queue(db)
    app.state.db = db
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(150, connect=12), trust_env=False
    ) as client:
        app.state.client = client
        tasks = []
        if paimem.OWNER:
            paimem.setup(db)
            tasks.append(asyncio.create_task(paimem.deliver(app)))
        if MIND_ENABLED:
            tasks.append(asyncio.create_task(emotion_worker(app)))
        if CHAT_MEMORY_IMPORT:
            tasks.append(asyncio.create_task(memory_worker(app)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            db.close()


app = Starlette(
    routes=[
        Route("/v1/models", models),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/aru-memory/v1/models", models),
        Route("/aru-memory/v1/chat/completions", organize, methods=["POST"]),
    ],
    lifespan=lifespan,
)
