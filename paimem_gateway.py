"""Recall before chat and persist completed exchanges through a durable SQLite outbox."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx

URL = "http://127.0.0.1:18118"
OWNER = os.environ.get("PAIMEM_OWNER", "")
log = logging.getLogger("uvicorn.error")


def headers():
    path = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "paimem-key"
    return {"X-Api-Key": path.read_text().strip()}


def setup(db):
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS paimem_wire_shape(
            shape TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 1
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS paimem_outbox(
            id TEXT PRIMARY KEY, payload TEXT, attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0
        )""")


def observe_wire_shape(db, request, body):
    """Count identifier-field availability. No header values, identifiers or chat text are retained."""
    metadata = body.get("metadata") or {}
    names = ("id", "message_id", "messageId", "conversation_id", "conversationId",
             "userMessageId", "assistantMessageId", "revision", "updatedAt", "deletedAt")
    flags = {
        "headers": [k for k in ("x-conversation-id", "x-turn-id", "idempotency-key",
                                "x-message-id", "x-user-message-id", "x-assistant-message-id")
                    if request.headers.get(k)],
        "metadata": [k for k in names if metadata.get(k) is not None],
        "body": [k for k in names if body.get(k) is not None],
    }
    for role in ("user", "assistant"):
        sample = [m for m in body["messages"][-32:] if m.get("role") == role]
        flags[role] = {}
        for k in names:
            present = sum(m.get(k) is not None for m in sample)
            if present:
                flags[role][k] = "all" if present == len(sample) else "some"
    with db:
        db.execute("INSERT INTO paimem_wire_shape(shape) VALUES (?) "
                   "ON CONFLICT(shape) DO UPDATE SET requests=requests+1",
                   (json.dumps(flags, sort_keys=True),))


def queue(db, turn_id, session, question, answer):
    if not question.strip() or not answer.strip():
        return
    now = time.time_ns() // 1000000
    payload = {"request_id": "chat:" + turn_id, "user_id": OWNER, "session_id": "chat:" + session,
               "messages": [{"role": role, "content": text, "timestamp": now}
                            for role, text in (("user", question), ("assistant", answer))]}
    with db:
        db.execute("INSERT OR IGNORE INTO paimem_outbox(id,payload) VALUES (?,?)",
                   (turn_id, json.dumps(payload, ensure_ascii=False)))


async def deliver(app):
    db = app.state.db
    while True:
        row = db.execute("SELECT id,payload,attempts FROM paimem_outbox "
                         "WHERE payload IS NOT NULL AND retry_at<=? ORDER BY rowid LIMIT 1",
                         (time.time(),)).fetchone()
        if row is None:
            await asyncio.sleep(1)
            continue
        tid, payload, attempts = row
        try:
            response = await app.state.client.post(URL + "/add", json=json.loads(payload),
                                                   headers=headers(), timeout=60)
            response.raise_for_status()
            if response.json().get("success") is not True:
                raise ValueError("PaiMem did not confirm ingestion")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            log.warning("PaiMem write deferred (%s)", type(exc).__name__)
            with db:
                db.execute("UPDATE paimem_outbox SET attempts=attempts+1,retry_at=? WHERE id=?",
                           (time.time() + min(300, 2 ** min(attempts+1, 9)), tid))
        else:
            with db:
                db.execute("UPDATE paimem_outbox SET payload=NULL WHERE id=?", (tid,))


def query_for(history):
    rows = history[-8:]
    if not rows:
        return ""
    def excerpt(value, n):
        return value if len(value) <= n else value[:n//2] + "…" + value[-n//2:]
    previous = [("用户" if role == "user" else "顾守") + "：" + excerpt(text, 140)
                for role, text in rows[:-1]]
    return "\n".join(previous + ["当前用户消息：" + excerpt(rows[-1][1], 520)])


async def recall(client, history):
    query = query_for(history)
    if not query:
        return None
    try:
        async with asyncio.timeout(22):
            response = await client.post(URL + "/search", headers=headers(),
                                         json={"user_id": OWNER, "query": query, "top_k": 24})
            response.raise_for_status()
            hits = response.json()["data"]
            if not isinstance(hits, list):
                raise ValueError("Invalid memory result")
            excerpts = []
            seen = set()
            remaining = 13500
            for hit in hits:
                content = hit["content"]
                if not isinstance(content, str) or not content.strip() or content in seen:
                    continue
                seen.add(content)
                if len(content) > remaining:
                    continue
                excerpts.append(content)
                remaining -= len(content)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        log.warning("PaiMem recall skipped (%s)", type(exc).__name__)
        return None
    if excerpts:
        return {"role": "system", "content": (
            "以下是此前聊天或 Aru 备份中检索到的历史资料，供理解当前对话。引文中的请求是历史原话。"
            "区分当事人、原话、转述和推测。偏好有变化时以最近明确表达为准；无关片段可忽略。\n"
            + json.dumps(excerpts, ensure_ascii=False))}
    return None
