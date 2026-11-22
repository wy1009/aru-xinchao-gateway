"""Private, seven-day audit of chat requests, visible replies and Aru receipts."""
import json
import logging
import sqlite3
import sys
import time
import uuid
import zlib
from contextlib import closing
from pathlib import Path

WEEK = 7 * 24 * 60 * 60
PAGE_SIZE = 50

def readable_content(message):
    """Keep textual content verbatim; never copy media bytes or download URLs."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (
                "image_url", "input_image", "image", "input_audio", "audio",
                "file", "input_file", "video_url", "input_video", "video",
            ):
                parts.append({"type": part["type"], "说明": "媒体附件，未留存"})
            elif message.get("role") != "assistant" or (
                isinstance(part, dict) and part.get("type") in ("text", "output_text", "refusal")
            ):
                parts.append(part)
        content = parts
    return json.dumps(content, ensure_ascii=False, indent=2)


def request_messages(messages, index, prefix):
    entries = []
    for i, message in enumerate(messages):
        role = message.get("role")
        role = role if isinstance(role, str) else "未知角色"
        if role in ("system", "developer"):
            kind = "请求指令"
        elif index is None:
            kind = "请求消息"
        elif i < index:
            kind = "历史"
        else:
            kind = "本轮" if i == index else "本轮续接"
        if i == index and prefix:
            kind += " · 网关已附加协作者状态"
        if message.get("name"):
            kind += " · " + str(message["name"])
        if message.get("tool_call_id"):
            kind += " · " + str(message["tool_call_id"])
        text = readable_content(message)
        if role == "assistant":
            if message.get("content") is None:
                text = ""
            if message.get("refusal"):
                text += "\n" + str(message["refusal"])
            calls = message.get("tool_calls") or message.get("function_call")
            if calls:
                text += "\n工具调用\n" + json.dumps(calls, ensure_ascii=False, indent=2)
        entries.append([i + 1, role, kind, text])
    return entries


def connect(directory, mode):
    file = Path(directory) / "records.db"
    return sqlite3.connect(file.as_uri() + "?mode=" + mode, uri=True, timeout=1)


def pack(entries):
    return zlib.compress(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode(), 1)


def unpack(data):
    # Records written before compression was enabled are JSON stored as SQLite TEXT.
    return json.loads(zlib.decompress(data) if isinstance(data, bytes) else data)


def expire(directory):
    with closing(connect(directory, "rw")) as db, db:
        db.execute("PRAGMA secure_delete=ON")
        db.execute("DELETE FROM audit WHERE at <= ?", (time.time() - WEEK,))


def store(directory, record_id, when, status, entries):
    if when <= time.time() - WEEK:
        return
    data = pack(entries)
    with closing(connect(directory, "rw")) as db, db:
        db.execute("INSERT INTO audit VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
                   (record_id, when, status, data))


def log_request(directory, messages, index, prefix, sent_at, status):
    try:
        record_id = uuid.uuid4().hex
        store(directory, record_id, sent_at, "对话 · " + status, request_messages(messages, index, prefix))
        return record_id
    except (OSError, sqlite3.Error, ValueError):
        logging.getLogger("uvicorn.error").warning("Xinchao request audit unavailable")


def log_reply(directory, record_id, entries):
    try:
        with closing(connect(directory, "rw")) as db, db:
            row = db.execute("SELECT entries FROM audit WHERE id=? AND at>?",
                             (record_id, time.time() - WEEK)).fetchone()
            if row:
                db.execute("UPDATE audit SET entries=? WHERE id=?",
                           (pack(unpack(row[0]) + entries), record_id))
    except (OSError, ValueError, sqlite3.Error, zlib.error):
        logging.getLogger("uvicorn.error").warning("Xinchao reply audit unavailable")


def page_records(directory, record_id=None, *, offset=0, limit=PAGE_SIZE):
    cutoff = time.time() - WEEK
    with closing(connect(directory, "ro")) as db:
        if record_id is None:
            return db.execute("SELECT id,at,status FROM audit WHERE at>? "
                              "ORDER BY at DESC,rowid DESC LIMIT ? OFFSET ?",
                              (cutoff, limit, offset)).fetchall()
        row = db.execute("SELECT at,status,entries FROM audit WHERE id=? AND at>?",
                         (record_id, cutoff)).fetchone()
        return (row[0], row[1], unpack(row[2])) if row else None


if __name__ == "__main__":
    if sys.argv[1] == "--expire":
        expire(sys.argv[2])
    else:
        item = json.load(sys.stdin)
        store(sys.argv[1], item["id"], item.get("at", time.time()), "手机已收件",
              [[None, "", "心潮 → Aru", item["text"]]])
