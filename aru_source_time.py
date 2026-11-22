"""Original chat message times for Aru structured memories (Polaris v19, epoch ms)."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
KIND = "结构化记忆"
SCHEMA = """CREATE TABLE IF NOT EXISTS source_time (
    bucket TEXT PRIMARY KEY, source_hash TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL
)"""


def valid(bounds):
    return (isinstance(bounds, (list, tuple)) and len(bounds) == 2
            and all(type(v) is int for v in bounds)
            and 0 < bounds[0] <= bounds[1] < 253402200000000)


def read(db, owner):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message','structured_memory_entry_source')"
    )}
    if tables != {"message", "structured_memory_entry_source"}:
        return {}
    # message.createdAt is the original logical message time. An edit/regeneration has its own timestamps.
    query = """
        SELECT e.id, min(m.createdAt), max(m.createdAt)
        FROM structured_memory_entry e
        JOIN structured_memory_entry_source s ON s.entryId=e.id
        LEFT JOIN message m ON m.id=s.messageId AND m.conversationId=s.conversationId
        WHERE e.collaboratorId=? AND e.state='ready' AND e.deletedAt IS NULL
        GROUP BY e.id
        HAVING count(*)=count(m.createdAt) AND min(typeof(m.createdAt)='integer')=1
    """
    return {sid: [lo, hi] for sid, lo, hi in db.execute(query, (owner,)) if valid([lo, hi])}


def created_at(start_ms):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (epoch + timedelta(milliseconds=start_ms)).astimezone(BEIJING).isoformat(timespec="milliseconds")


def footer(bounds):
    if not bounds:
        return ""
    start, end = (datetime.fromtimestamp(v // 1000, BEIJING) for v in bounds)
    label = start.strftime("%Y-%m-%d %H:%M:%S")
    if start != end:
        label += " 至 " + end.strftime("%H:%M:%S" if start.date() == end.date() else "%Y-%m-%d %H:%M:%S")
    return "\n\n> 来源对话（北京时间）：" + label


def stored(db, bucket, source_hash):
    row = db.execute("SELECT start_ms,end_ms FROM source_time WHERE bucket=? AND source_hash=?",
                     (bucket, source_hash)).fetchone()
    return list(row) if row else None
