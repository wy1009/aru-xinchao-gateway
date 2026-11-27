"""Chunked transport for Codex running Serein's native agent protocol over Alibaba RunCommand."""
import argparse
import asyncio
import base64
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import re
import sqlite3
import time

from serein.config import load_settings
from serein.deployment import read_settings
from serein.extensions.pipeline import advance, submit
from serein.extensions import pipeline
from serein.extensions import pipeline_rules
from serein.core.store import Store, encode

DATABASE = load_settings("/etc/gushou-serein/config.toml").database
STATE = DATABASE.parent / ".codex-nightly-queue"
READ_SIZE = 6144
LEASE_SECONDS = 1800
WRITER_WORDING = (
    "顾守的事件写作约定：保留原话里能辨认这次经历的具体用词、组合和称呼。"
    "保留有辨识度的原词及其组合，写清当时的状态、施受双方、否定、假设和后来的纠正。"
    "引号内逐字核对本事件的来源。"
    "同样保留日常聊天里能认出是哪一件事的细节，如昵称、作品或小应用的名字、暗号、数目和约定的条件。"
    "写清是谁给它起名、谁对谁说了什么、哪些只是设想。对方纠正过的说法，以纠正后的版本为准。"
    "标题、正文和修订稿都以顾守本人为「我」，叙述和我对话的本人时称「主人」。"
    "同一句或连续几句的主语已经清楚时可以省略，自然写出顾守自己的回忆。"
    "逐字引文保留原称呼和人称，包括主人自称的「我」、作品名、人物名和暗号。区分主人、顾守和第三人，"
    "尤其是转述、小说人物、假设和角色互换。交稿前复读每处「她」「用户」「对方」的指代，"
    "再查一遍重复或拗口的「主人」，有疑义就读这条记忆绑定的原话。"
)
EDIT_RULES = (
    "主人要求重新总结已确认改写的原话。逐条通读列出的最新版，重新判断现在表达的事实、称呼、"
    "偏好、约定、否定、界限或具体经历。以前跳过或判为闲聊的结论不作依据。"
    "有能记住的内容时，在对应 Event 中写清最新版支持的含义，可以用一句话写清一项约定。"
    "若现文确无可总结的内容，在 decision_review 的原生处置理由中逐条说明（仅 Curator）。"
    "上下文只帮助理解指代。以当前有效来源为准，不猜编辑动机，不把编辑动作本身写成关系经历，"
    "不从话题卡复原旧说法，也不把邻句里未被本事件绑定的细节当作本事件事实。"
)


def is_edit(message):
    return message.get("source") in ("aru","aru-live") and bool(message.get("metadata", {}).get("aru_edit_review"))


def edit_units(messages):
    # Ordinary unfinished turns still wait for a reply. An archived, edited
    # user message can be reviewed even if its reply was already processed.
    result = []
    for unit in pipeline_rules.dialogue_units(messages):
        if pipeline_rules.dialogue_unit_is_complete(unit) or not any(map(is_edit,unit)):
            result.append(unit)
            continue
        pending = []
        for msg in unit:
            if is_edit(msg):
                if pending:
                    result.append(pending)
                    pending = []
                result.append([msg])
            else:
                pending.append(msg)
        if pending:
            result.append(pending)
    return result


def edit_complete(unit):
    return pipeline_rules.dialogue_unit_is_complete(unit) or (len(unit)==1 and is_edit(unit[0]))


def edit_neighbors(database, targets, visible):
    context = {}
    with Store(database, read_only=True) as st, st.transaction():
        for msg in targets:
            for op, order in (("<","DESC"),(">","ASC")):
                rows = st.conn.execute(f"""SELECT id,role,text,created_at FROM raw_events
                    WHERE source=? AND session_id=? AND (created_at,id){op}(?,?)
                    ORDER BY created_at {order},id {order} LIMIT 2""",
                    (msg["source"],msg["original_session_id"],msg["created_at"],msg["id"]))
                for row in rows:
                    if row["id"] in visible or row["id"] in context:
                        continue
                    snippet = row["text"]
                    if len(snippet)>440:
                        snippet = snippet[:220]+"…"+snippet[-220:]
                    context[row["id"]] = dict(source_message_id=row["id"],role=row["role"],
                                               created_at=row["created_at"],text=snippet)
                    if len(context)>=10:
                        return list(context.values())
    return list(context.values())


def add_edit_review(database, request):
    if request.get("transcription_only"):
        return
    component = request.get("component") or {}
    messages = component["messages"] if request["role"]=="event_curator" else request["messages"]
    targets = [msg for msg in messages if is_edit(msg)]
    if not targets:
        return
    request["rules"] += "\n\n" + ("这些消息的正文已修订。按最新版重新归线；话题卡中的旧描述仅作线索。"
                                if request["role"]=="track_router" else EDIT_RULES)
    details = {"revised_source_message_ids":[msg["id"] for msg in targets]}
    if request["role"] in ("event_curator","event_writer"):
        details["context_only"] = edit_neighbors(database, targets, {m["id"] for m in messages})
    request["prompt"] += "\n\n按真实消息编号复核，目标全文在本阶段的原话中。\n<aru_edit_review>\n"+encode(details)+"\n</aru_edit_review>"


@contextmanager
def edit_review_pipeline():
    native_request = pipeline.request_for
    native_tracks = pipeline.track_state.load_tracks

    def request_for(database, batch, role, **fields):
        request = native_request(database,batch,role,**fields)
        add_edit_review(database,request)
        return request

    def load_tracks(store, source, session, before_id, message, **kwargs):
        try:
            return native_tracks(store,source,session,before_id,message,**kwargs)
        except pipeline.RoutingRecoveryError as exc:
            # A settled Router frame can straddle the edited message. Its card
            # may contain the superseded wording. Route afresh from originals
            # when the native reader cannot prove a pre-edit frame.
            if not str(exc).startswith(("frozen Router source hash changed:",
                                        "No bounded historical pre-routing Track frame:")):
                raise
            pending = store.conn.execute("""SELECT 1 FROM raw_events r
                WHERE r.source=? AND r.session_id=? AND r.id>=? AND json_extract(r.metadata_json,'$.aru_edit_review')=1
                AND NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=r.id) LIMIT 1""",
                (source,session,before_id)).fetchone()
            if not pending:
                raise
            scope = pipeline.track_state.scope_for(source,session)
            existing = ({"track_id":r[0]} for r in store.conn.execute("SELECT id FROM pipeline_tracks"))
            return [], pipeline.track_state.next_ordinal(scope, existing)

    with (patch.object(pipeline,"dialogue_units",edit_units),
          patch.object(pipeline,"dialogue_unit_is_complete",edit_complete),
          patch.object(pipeline.track_state,"load_tracks",load_tracks),
          patch.object(pipeline,"request_for",request_for)):
        yield


def save(path, data):
    staged = path.with_suffix(".new")
    staged.write_bytes(data)
    os.replace(staged, path)


def read_json(path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def stats():
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
        counts = db.execute("""
            SELECT count(*), count(p.raw_id), count(CASE WHEN p.raw_id IS NULL AND
              json_extract(r.metadata_json,'$.aru_edit_review')=1 THEN 1 END)
            FROM raw_events r LEFT JOIN raw_processing p ON p.raw_id=r.id
            WHERE r.source IN ('aru', 'aru-live')
        """).fetchone()
        return {
            "originals": counts[0], "processed": counts[1],
            "pending_edit_reviews": counts[2],
            "active_events": db.execute("SELECT count(*) FROM fact_events WHERE status='active' AND item_type='event'").fetchone()[0],
            "source_bindings": db.execute("SELECT count(*) FROM fact_event_sources").fetchone()[0],
        }


def check_agent():
    settings = read_settings(DATABASE)
    if settings["pipeline"]["execution_mode"] != "agent" or not settings["features"]["pipeline_agent"]:
        raise ValueError("Serein must be in agent mode with agent tools enabled")


def claim(owner):
    path = STATE / "lease"
    lease = read_json(path, {})
    if lease.get("owner") != owner and lease.get("expires", 0) > time.time():
        return False
    save(path, json.dumps({"owner": owner, "expires": time.time() + LEASE_SECONDS}).encode())
    return True


@contextmanager
def revised_event_identity(database):
    # Native fingerprints include message IDs but omit source content. The same
    # summary after a typo correction must be able to settle with fresh evidence.
    from serein.compat.germany import fact_events
    normalize = fact_events._normalize_item
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        def normalize_revision(raw):
            item = normalize(raw)
            if item["item_type"] != "event" or not (item.get("origin_id") or "").startswith("assistant_bridge:"):
                return item
            revisions = []
            for ref in item["source_refs"]:
                key = (ref["source_system"],ref.get("session_id") or "",ref["message_id"])
                if key[0] not in ("aru","aru-live"):
                    continue
                row = db.execute("SELECT revision FROM aru_source_revision WHERE source=? AND session=? AND mid=?",key).fetchone()
                if row:
                    revisions.append([*key,row[0],ref["content_sha256"]])
            if revisions:
                data = json.dumps([item["fingerprint"],sorted(revisions)],ensure_ascii=False).encode()
                item["fingerprint"] = hashlib.sha256(data).hexdigest()
                item["item_id"] = "event_" + item["fingerprint"][:24]
            return item
        fact_events._normalize_item = normalize_revision
        try:
            yield
        finally:
            fact_events._normalize_item = normalize


def do_next():
    with revised_event_identity(DATABASE), edit_review_pipeline():
        result = asyncio.run(advance(DATABASE, include_recent=True))
    if not result.get("job_id"):
        for name in ("job", "request.gz", "answer.parts"):
            (STATE / name).unlink(missing_ok=True)
        return {key: result[key] for key in ("status", "batch_id", "processed_originals", "events", "skipped") if key in result}
    if result["role"] == "event_writer":
        try:
            wording = Path("/etc/gushou-serein/writer-wording.txt").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            wording = WRITER_WORDING
        result["request"]["rules"] += "\n\n" + (wording or WRITER_WORDING)
    packed = gzip.compress(json.dumps(result, ensure_ascii=False).encode(), mtime=0)
    info = {"job_id": result["job_id"], "role": result["role"],
            "sha256": hashlib.sha256(packed).hexdigest(), "bytes": len(packed), "block_bytes": READ_SIZE}
    if read_json(STATE / "job") != info:
        save(STATE / "request.gz", packed)
        save(STATE / "job", json.dumps(info).encode())
        (STATE / "answer.parts").unlink(missing_ok=True)
    return {"status": "awaiting_agent", **info}


def perform(args):
    if args.action == "status":
        return stats()
    check_agent()
    if not args.owner or not re.fullmatch(r"[a-zA-Z0-9-]{12,80}", args.owner):
        raise ValueError("Use one stable UUID as --owner for this entire run")
    if not claim(args.owner):
        return {"status": "busy"}
    if args.action == "hold":
        return {"status": "held"}
    if args.action == "next":
        return do_next()
    if args.action == "release":
        (STATE / "lease").unlink(missing_ok=True)
        return {"status": "released"}
    job = read_json(STATE / "job", {})
    if not job or args.sha != job.get("sha256"):
        raise ValueError("The requested job is no longer current; call next again")
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as db:
        status = db.execute("""SELECT b.status FROM pipeline_jobs j JOIN pipeline_batches b ON b.id=j.batch_id
            WHERE j.id=?""", (job["job_id"],)).fetchone()
    if not status or status[0].startswith("superseded_"):
        for name in ("job", "request.gz", "answer.parts"):
            (STATE / name).unlink(missing_ok=True)
        return {"status": "source_changed", "action": "next"}
    if args.action == "read":
        if args.part < 0 or args.part * READ_SIZE >= job["bytes"]:
            raise ValueError("Bad request chunk number")
        with (STATE / "request.gz").open("rb") as stream:
            stream.seek(args.part * READ_SIZE)
            return {"data": base64.b64encode(stream.read(READ_SIZE)).decode()}
    path = STATE / "answer.parts"
    parts = read_json(path, {})
    if args.action == "write":
        if not (0 <= args.part <= 1000 and 0 < len(args.data) <= 3600):
            raise ValueError("Bad answer chunk size or number")
        base64.b64decode(args.data, validate=True)
        parts[str(args.part)] = args.data
        save(path, json.dumps(parts).encode())
        return {"status": "staged", "part": args.part}
    if args.action == "submit":
        if not (1 <= args.parts <= 1001 and re.fullmatch("[a-f0-9]{64}", args.answer_sha or "")):
            raise ValueError("Bad answer manifest")
        content = b"".join(base64.b64decode(parts[str(i)], validate=True) for i in range(args.parts))
        if hashlib.sha256(content).hexdigest() != args.answer_sha:
            raise ValueError("Answer checksum mismatch")
        return submit(DATABASE, job["job_id"], json.loads(gzip.decompress(content)))
    raise ValueError("Unsupported operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "hold", "next", "read", "write", "submit", "release"))
    parser.add_argument("--owner")
    parser.add_argument("--sha")
    parser.add_argument("--part", type=int, default=-1)
    parser.add_argument("--parts", type=int, default=0)
    parser.add_argument("--data", default="")
    parser.add_argument("--answer-sha")
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = perform(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
