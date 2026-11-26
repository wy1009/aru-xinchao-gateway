"""Publish the selected collaborator's Polaris v19 backup to the private PaiMem service."""
import json
import os
import sqlite3
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import aru_backup
from aru_source_time import footer

KINDS = {"结构化记忆": "structured", "长期资料": "reference", "信息卡": "events", "条目记忆": "entries"}
URL = os.environ.get("PAIMEM_URL", "http://127.0.0.1:18118")
OWNER = os.environ["PAIMEM_OWNER"]


def post(path, payload, token):
    req = urllib.request.Request(URL + path, data=aru_backup.encoded(payload),
                                 headers={"X-Api-Key": token, "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=120) as resp:
        return json.load(resp)


def transcripts(path, manifest):
    # Import only a declared and complete inventory of owner-scoped, authored text rows.
    counts = manifest.get("databaseObjectCounts", {})
    if type(counts.get("messages")) is not int:
        return None, "备份未声明聊天原文"
    if any(w.get("objectType") in ("message", "messages", "conversation", "conversations")
           for w in manifest.get("warnings", [])):
        return None, "聊天原文有导出警告"
    db = sqlite3.connect(path.as_uri() + "?immutable=1&mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "message" not in tables:
            return None, "聊天原文表未导出"
        aru_backup.inspect_table(db, "message")
        cols = {r[1] for r in db.execute("PRAGMA table_info(message)")}
        text_col = next((name for name in ("content", "text", "body") if name in cols), None)
        if not text_col or not {"id","conversationId","role","createdAt"} <= cols:
            return None, "当前备份的消息正文格式待适配"
        if db.execute("SELECT count(*) FROM message").fetchone()[0] != counts["messages"]:
            return None, "聊天原文数量与清单不符"
        join = ""
        condition = "m.collaboratorId=?"
        if "conversation" in tables:
            aru_backup.inspect_table(db, "conversation")
            conv = {r[1] for r in db.execute("PRAGMA table_info(conversation)")}
            if {"id", "collaboratorId"} <= conv:
                join = "JOIN conversation c ON c.id=m.conversationId"
                condition = "c.collaboratorId=?"
                if "collaboratorId" in cols:
                    condition += " AND (m.collaboratorId IS NULL OR m.collaboratorId=c.collaboratorId)"
                if "deletedAt" in conv:
                    condition += " AND c.deletedAt IS NULL"
        if not join and "collaboratorId" not in cols:
            return None, "无法核实聊天原文归属"
        if "deletedAt" in cols:
            condition += " AND m.deletedAt IS NULL"
        roles = {r[0] for r in db.execute(
            f"SELECT DISTINCT m.role FROM message m {join} WHERE {condition}", (aru_backup.OWNER,))}
        if roles - {"user", "assistant", "tool", "system", "developer", "function"}:
            return None, "当前备份使用未识别的消息角色"
        rows = db.execute(f"SELECT m.id,m.conversationId,m.role,m.{text_col},m.createdAt FROM message m "
                          f"{join} WHERE {condition} AND m.role IN ('user','assistant') "
                          "ORDER BY m.conversationId,m.createdAt,m.id", (aru_backup.OWNER,))
        batches = []
        current = []
        session = None
        for source_id, cid, role, value, stamp in rows:
            if not isinstance(value, str) or not value.strip():
                continue
            if value.lstrip().startswith(("{", "[")):
                try:
                    obj = json.loads(value)
                except ValueError:
                    pass
                else:
                    if isinstance(obj, list):
                        value = "\n".join(p["text"] for p in obj if isinstance(p, dict)
                            and p.get("type") in ("text", "input_text", "output_text") and isinstance(p.get("text"), str))
                    elif isinstance(obj, dict):
                        value = obj.get("text") if isinstance(obj.get("text"), str) else ""
            if not value or value.startswith(("<environment>","External event data, not an authored user message")):
                continue
            if current and (session != cid or len(current) >= 16):
                batches.append((session,current))
                current = []
            session = cid
            msg = {"role":role,"content":value}
            if type(stamp) is int and stamp > 0:
                msg["timestamp"] = stamp
            current.append((source_id,msg))
        if current:
            batches.append((session,current))
        return batches, None
    finally:
        db.close()


def synchronize(archive, token):
    with tempfile.TemporaryDirectory(prefix="aru-read-", dir=archive.parent) as work:
        path = Path(work)
        with zipfile.ZipFile(archive) as z:
            if z.namelist().count("manifest.json") != 1 or z.getinfo("manifest.json").file_size > 2**20:
                raise ValueError("Invalid manifest")
            manifest = json.loads(z.read("manifest.json"))
        try:
            data, skipped = aru_backup.snapshot(archive, path)
        except ValueError as exc:
            if str(exc) != "No complete supported collection found":
                raise
            data = {"at":manifest["createdAt"], "collections":{}, "source_times":{}}
            skipped = {}
        chats, reason = transcripts(path / "database.sqlite", manifest)
        if reason:
            skipped["聊天原文"] = reason
    groups = {}

    def stage(kind, source, session, messages):
        rid = "aru:" + kind + ":" + aru_backup.digest([OWNER,source,session,messages])
        result = post("/add", {"user_id":OWNER+":backup-stage","request_id":rid,
                              "session_id":session,"messages":messages}, token)
        if result.get("success") is not True:
            raise RuntimeError("Unconfirmed backup write")
        groups[kind].append(rid)

    for label, items in data["collections"].items():
        kind = KINDS[label]
        groups[kind] = []
        for item in items:
            bounds = data.get("source_times", {}).get(item["id"]) if kind == "structured" else None
            message = {"role":"Aru备份资料", "content":f"【Aru 备份·{label}】\n" + item["content"] + footer(bounds)}
            if bounds:
                message["timestamp"] = bounds[0]
            stage(kind,[item["id"],item["part"]],"aru:"+kind+":"+item["id"],[message])
    if chats is not None:
        groups["transcript"] = []
        for conversation, rows in chats:
            stage("transcript",[i for i,m in rows],"aru:chat:"+conversation,[m for i,m in rows])
    if not groups:
        raise ValueError("No supported inventory")
    changes = post("/aru/publish", {"at":data["at"],"groups":groups},token)
    return {"ok":True, "changes":changes, "skipped":[k+"（"+v+"）" for k,v in skipped.items()],
            "source":"PaiMem"}


def process():
    os.umask(0o077)
    directory = Path(os.environ["STATE_DIRECTORY"])
    archive = directory / "archive.polarisarchive"
    if not archive.is_file():
        return
    token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "paimem-key").read_text().strip()
    try:
        result = synchronize(archive, token)
    except Exception as exc:
        print("PaiMem backup import failed:", type(exc).__name__, flush=True)
        result = {"ok":False,"source":"PaiMem","message":"本次备份未发布。请核对备份格式，或稍后重新上传。"}
    result["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = directory / "paimem-receipt.tmp"
    with tmp.open("w") as f:
        json.dump(result, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, directory / "paimem-receipt.json")
    # The uploaded file is a transient copy. Existing gateway policy consumes it after a receipt.
    archive.unlink()


if __name__ == "__main__":
    process()
