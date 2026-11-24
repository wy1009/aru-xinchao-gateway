"""Per-provider-call token receipts; prices are fixed when each receipt is saved."""
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

KINDS = {"chat": "主聊天", "emotion": "情绪自评", "organizer": "Aru 记忆整理", "memory": "聊天记忆提取"}
# Nanoyuan per token. SiliconFlow China lists ¥0.40 / ¥4 / ¥6 per million.
PRICES = {("api.siliconflow.cn", "deepseek-ai/DeepSeek-V3.2"): (400, 4000, 6000)}


def token_count(value):
    return value if type(value) is int and 0 <= value < 2**40 else None


def counters(usage):
    prompt = token_count(usage.get("prompt_tokens"))
    output = token_count(usage.get("completion_tokens"))
    hit = token_count(usage.get("prompt_cache_hit_tokens"))
    miss = token_count(usage.get("prompt_cache_miss_tokens"))
    details = usage.get("prompt_tokens_details")
    if hit is None and isinstance(details, dict):
        hit = token_count(details.get("cached_tokens"))
    if prompt is None and hit is not None and miss is not None:
        prompt = hit + miss
    if prompt is not None:
        if hit is None and miss is not None:
            hit = prompt - miss
        if miss is None and hit is not None:
            miss = prompt - hit
        if hit is not None and miss is not None and (min(hit, miss) < 0 or hit + miss != prompt):
            hit = miss = None
    return prompt, hit, miss, output


class UsageCall:
    def __init__(self, directory, kind, endpoint, model, choices=1):
        self.path = directory / "usage.sqlite"
        self.kind = kind
        self.host = urlsplit(endpoint).hostname or ""
        self.model = model
        self.at = time.time()
        self.receipt_id = None
        self.usage = {}
        self.expected = choices
        self.finished = set()

    def observe(self, chunk, *, streaming=False):
        if not isinstance(chunk, dict):
            return
        if self.receipt_id is None and isinstance(chunk.get("id"), str):
            self.receipt_id = chunk["id"]
        choices = chunk.get("choices") or []
        if streaming:
            self.finished.update(c.get("index", 0) for c in choices if c.get("finish_reason"))
            if choices and len(self.finished) < self.expected:
                return  # SiliconFlow can return cumulative, unfinished counts on every delta.
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage = usage

    def save(self):
        prompt, hit, miss, output = counters(self.usage)
        rates = PRICES.get((self.host, self.model))
        cost = None
        if rates and None not in (hit, miss, output):
            cost = hit * rates[0] + miss * rates[1] + output * rates[2]
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                # The audit directory's setgid group is the authenticated portal reader.
                self.path.chmod(0o640)
                db.execute("""CREATE TABLE IF NOT EXISTS calls(
                    host TEXT, model TEXT, receipt TEXT, at REAL, kind TEXT,
                    prompt INTEGER, hit INTEGER, miss INTEGER, output INTEGER, nanoyuan INTEGER,
                    PRIMARY KEY(host, model, receipt))""")
                db.execute("CREATE INDEX IF NOT EXISTS calls_at ON calls(at)")
                db.execute("""INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(host,model,receipt) DO UPDATE SET
                    prompt=excluded.prompt,hit=excluded.hit,miss=excluded.miss,
                    output=excluded.output,nanoyuan=excluded.nanoyuan
                    WHERE calls.nanoyuan IS NULL AND
                      (excluded.nanoyuan IS NOT NULL OR (calls.output IS NULL AND excluded.output IS NOT NULL))""",
                    (self.host, self.model, self.receipt_id or uuid.uuid4().hex, self.at,
                     self.kind, prompt, hit, miss, output, cost))
        except (OSError, sqlite3.Error):
            logging.getLogger("uvicorn.error").warning("Could not save model usage receipt", exc_info=True)


def summary(path, start, end):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("""SELECT kind,COUNT(*),COUNT(nanoyuan),SUM(nanoyuan),
            SUM(hit) FILTER (WHERE miss IS NOT NULL),SUM(miss) FILTER (WHERE hit IS NOT NULL),
            SUM(output),COUNT(hit) FILTER (WHERE miss IS NOT NULL),COUNT(output)
            FROM calls WHERE at>=? AND at<? GROUP BY kind""", (start, end)).fetchall()
