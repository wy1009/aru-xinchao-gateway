"""Read-only observation recall for the Gushou bank, with OpenClaw's prompt format."""
import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

import httpx

ENABLED = os.environ.get("NIAN_HINDSIGHT_ENABLED") == "1"
RECALL_URL = "http://127.0.0.1:18458/v1/default/banks/gushou/memories/recall"
MIN_QUERY_CHARS = 5
MAX_QUERY_CHARS = 800
PREAMBLE = ("Relevant memories from past conversations (prioritize recent when conflicting). "
            "Only use memories that are directly useful to continue this conversation; ignore the rest:")
log = logging.getLogger("uvicorn.error")


def format_observation(row):
    line = f"- {row['text']} [observation]"
    if row.get("mentioned_at"):
        line += f" ({row['mentioned_at']})"
    start, end = row.get("occurred_start"), row.get("occurred_end")
    if start and end:
        window = start if start == end else f"{start} → {end}"
        line += f" [occurred: {window}]"
    elif start:
        line += f" [occurred from: {start}]"
    elif end:
        line += f" [occurred until: {end}]"
    if row.get("document_id"):
        line += f" [doc:{row['document_id']}]"
    return line


async def recall(client, query):
    # The upstream OpenClaw limits are expressed in JavaScript UTF-16 code units.
    encoded = query.strip().encode("utf-16-le", errors="replace")
    if len(encoded) < MIN_QUERY_CHARS * 2:
        return "", "跳过（有效文本不足 5 字符）"
    query = encoded[:MAX_QUERY_CHARS * 2].decode("utf-16-le", errors="ignore")
    try:
        token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "hindsight-key").read_text().strip()
        response = await asyncio.wait_for(client.post(
            RECALL_URL, headers={"Authorization": "Bearer " + token},
            json={"query": query, "types": ["observation"], "budget": "mid",
                  "max_tokens": 1024, "include": {"entities": None}},
        ), timeout=10)
        response.raise_for_status()
        rows = response.json()["results"]
        if not isinstance(rows, list):
            raise ValueError("Invalid Hindsight recall result")
        lines = [format_observation(row) for row in rows if isinstance(row, dict)
                 and row.get("type") == "observation" and isinstance(row.get("text"), str)
                 and row["text"].strip()]
    except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError, asyncio.TimeoutError) as exc:
        log.warning("Hindsight recall failed (%s)", type(exc).__name__)
        return "", f"检索失败（{type(exc).__name__}）"
    if not lines:
        return "", "命中 0 条观察"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return PREAMBLE + "\nCurrent time - " + now + "\n\n" + "\n\n".join(lines), f"注入 {len(lines)} 条观察"


def inject(messages, context, index):
    message = messages[index]
    content = message.get("content")
    prefix = "<hindsight_memories>\n" + context + "\n</hindsight_memories>\n\n"
    if isinstance(content, str):
        prepared = prefix + content
    elif isinstance(content, list):
        prepared = [{"type": "text", "text": prefix}, *content]
    else:
        return messages
    return [*messages[:index], {**message, "content": prepared}, *messages[index+1:]]
