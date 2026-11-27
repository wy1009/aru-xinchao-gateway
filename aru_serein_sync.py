"""Apply only complete, newer Polaris snapshots, atomically. Open input before dropping privileges."""
import json
from collections import defaultdict
from datetime import datetime
import os
import pwd
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from serein.compat.raw_archive import raw_archive
from serein.config import load_settings
from serein.core.store import Store

import aru_backup
import aru_serein_turns as turns
from aru_serein_alignment import align, chronology, split_original
from aru_serein_store import BEIJING, LIVE, SCHEMA, SOURCE, append_native_original, fingerprint, put_original, remove_original

BACKUP_ONLY = Path("/etc/gushou-serein/.backup-only")
STATE=Path("/var/lib/gushou-serein")
UPLOADS=Path("/var/lib/aru-backup-inbox/archive-uploads")
TECH=("External event data,", "<environment>", "【外部唤醒】", "[external_wake]")
LIVE_SENT_AT = re.compile(r"\n\n<sent_at \d{4}-\d{2}-\d{2} \d{2}:\d{2}>\s*\Z")


def bind_snapshot_slots(conn, messages):
    """Resolve provisional originals against the full export before applying its cutoff."""
    sessions = {r[0] for r in conn.execute("SELECT DISTINCT session FROM aru_original WHERE source=?", (LIVE,))}
    per_session = defaultdict(list)
    for message in messages.values():
        if message["session"] in sessions:
            per_session[message["session"]].append(message)
    for session, group in per_session.items():
        incoming = sorted(group, key=lambda m:(m["seq"],m["mid"]))
        rows = chronology(conn, session)
        existing = {r["mid"] for r in rows}
        visible = []
        for m in incoming:
            clock = datetime.fromtimestamp(m["stamp"]/1000, BEIJING).strftime("%Y-%m-%d %H:%M")
            visible.append({"role":m["role"], "text":split_original(m["text"])[0]+f"\n\n<sent_at {clock}>",
                            "mid":m["mid"] if m["mid"] in existing else None})
        # Every real archive entry is historical, including its last answer.
        matches, _ = align([*visible, {"role":"end","text":""}], rows)
        for i, msg in enumerate(incoming):
            j = matches.get(i)
            if j is None and i and msg["role"] == "assistant" and incoming[i-1]["role"] == "user":
                parent = matches.get(i-1)
                if parent is not None and parent+1 < len(rows) and rows[parent+1]["role"] == "assistant":
                    j = parent+1
            if j is None or rows[j]["source"] != LIVE or rows[j]["role"] != msg["role"]:
                continue
            conn.execute("""INSERT OR IGNORE INTO aru_live_alias(session,client,provisional)
                SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM aru_live_alias
                    WHERE session=? AND provisional=? AND client<>?)""",
                (session,msg["mid"],rows[j]["mid"],session,rows[j]["mid"],msg["mid"]))


def snapshot_live_edits(conn, messages, cutoff):
    """Carry a confirmed live edit across the switch to canonical Polaris IDs."""
    if not conn.execute("SELECT 1 FROM aru_original WHERE source=? LIMIT 1",(LIVE,)).fetchone():
        return set()
    edited = set()
    rows = conn.execute("""SELECT o.session,o.mid,o.role,o.text,a.client,
        json_extract(r.metadata_json,'$.aru_edit_review') AS was_edited
        FROM aru_original o JOIN raw_events r ON
        (r.source,r.session_id,r.source_event_id)=(o.source,o.session,o.mid)
        LEFT JOIN aru_live_alias a ON a.session=o.session AND a.provisional=o.mid
        WHERE o.source=? AND o.received<=?""", (LIVE, cutoff))
    for row in rows:
        item = messages.get(row["client"]) or messages.get(row["mid"])
        if item is None or item["session"] != row["session"]:
            continue
        if (row["was_edited"] or row["role"] != item["role"] or
                LIVE_SENT_AT.sub("", row["text"]) != LIVE_SENT_AT.sub("", item["text"])):
            edited.add(item["mid"])
    return edited

def read_manifest(z):
    for name in ("manifest.json","database.sqlite"):
        if z.namelist().count(name)!=1:
            raise ValueError("Incomplete or ambiguous archive")
    if z.getinfo("manifest.json").file_size>1024**2:
        raise ValueError("Invalid manifest size")
    m=json.loads(z.read("manifest.json"))
    if ((m.get("format"),m.get("version"),m.get("databasePath"))!=("polaris-native-archive",19,"database.sqlite")
            or "sqlite-snapshot" not in m.get("featureFlags",[]) or type(m.get("createdAt")) is not int):
        raise ValueError("Unsupported Polaris manifest")
    return m

def read_data(db,m):
    counts=m["databaseObjectCounts"]
    tables={"message":"messages","conversation":"conversations","collaborator":"collaborators"}
    warns={w.get("objectType") for w in m.get("warnings",[]) if w.get("code")!="backup_credentials_not_password_protected"}
    if warns & (set(tables)|set(tables.values())|{"archive"}):
        raise ValueError("Incomplete export warning")
    for table,field in tables.items():
        aru_backup.inspect_table(db,table)
        if db.execute("SELECT count(*) FROM "+table).fetchone()[0]!=counts.get(field):
            raise ValueError("Incomplete collection: "+table)
    aru_backup.inspect_table(db,"memory_source_exclusion")
    owner,=db.execute("SELECT id FROM collaborator WHERE name=? AND deletedAt IS NULL",("顾守",)).fetchall()
    owner=owner[0]
    scope={r[0]:bool(r[1]) for r in db.execute("""SELECT v.id,v.deletedAt IS NULL AND NOT EXISTS
        (SELECT 1 FROM memory_source_exclusion e WHERE e.collaboratorId=v.collaboratorId AND
        e.scopeKind='conversation' AND e.scopeId=v.id) FROM conversation v WHERE collaboratorId=?""",(owner,))}
    anchors={(fingerprint([r[0],r[1]]),r[2]) for r in db.execute("""SELECT m.role,m.content,v.id
        FROM message m JOIN conversation v ON v.id=m.conversationId WHERE v.collaboratorId=? AND
        m.deletedAt IS NULL AND m.status='complete' AND m.role IN ('user','assistant')
        AND length(trim(m.content))>0""",(owner,))}
    messages={}
    history = "0"
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='message_revision'").fetchone():
        aru_backup.inspect_table(db,"message_revision")
        history = """EXISTS (SELECT 1 FROM message_revision rev WHERE rev.messageId=message.id
            AND rev.role=message.role AND rev.status='complete' AND length(trim(rev.content))>0
            AND rev.reason IN ('edit','edit-only','edit-rerun','regenerate','regenerate-truncate-following')
            AND rev.content<>message.content)"""
    columns = {r[1] for r in db.execute("PRAGMA table_info(message)")}
    reply_target = "replyToUserGenerationId" if "replyToUserGenerationId" in columns else "NULL"
    for session,allowed in scope.items():
        if not allowed:continue
        for r in db.execute(f"""SELECT id mid,conversationId session,role,content text,createdAt stamp,
               seq,generationId,{reply_target} AS reply_target,updatedAt,{history} AS has_content_revision
               FROM message WHERE conversationId=? AND deletedAt IS NULL
               AND status='complete' AND role IN ('user','assistant') ORDER BY seq,id""",(session,)):
            if (r["text"] or "").strip() and not r["text"].lstrip().startswith(TECH):
                messages[r["mid"]]=r
    if not any(scope.values()): # An explicitly excluded final branch is a valid empty snapshot.
        if not scope: raise ValueError("Missing collaborator conversations")
    return owner,scope,messages,anchors


def run():
    backup_only = BACKUP_ONLY.is_file()
    candidates=[]
    for path in UPLOADS.glob("*/archive.polarisarchive"):
        with zipfile.ZipFile(path) as z:
            candidates.append((read_manifest(z)["createdAt"],path))
    if not candidates:return
    stamp,path=max(candidates)
    f=path.open("rb")
    uid=pwd.getpwnam("gushou-serein")
    if os.getuid()==0:
        os.setgroups([]);os.setgid(uid.pw_gid);os.setuid(uid.pw_uid)
    os.umask(0o077)
    settings=load_settings("/etc/gushou-serein/config.toml")
    archive=raw_archive(settings)
    with f, Store(settings.database) as st:
        c=st.conn;c.executescript(SCHEMA + turns.SCHEMA)
        prior=c.execute("SELECT * FROM aru_snapshot").fetchone()
        snapshot = json.dumps([stamp, path.parent.name])
        links_only = False
        if prior and stamp < prior["stamp"]:
            return
        if prior and stamp == prior["stamp"]:
            if path.parent.name != prior["archive"]:
                raise ValueError("Conflicting snapshots")
            applied = c.execute("SELECT 1 FROM background_state WHERE name='aru_backup_only'").fetchone()
            if (not backup_only or applied) and c.execute("SELECT 1 FROM aru_anchor LIMIT 1").fetchone():
                marker = c.execute("SELECT value_json FROM background_state WHERE name=?", (turns.SNAPSHOT_KEY,)).fetchone()
                if marker and marker[0] == snapshot:
                    return
                links_only = True
        with tempfile.TemporaryDirectory(dir=STATE) as work,zipfile.ZipFile(f) as z:
            m=read_manifest(z);info=z.getinfo("database.sqlite")
            if info.file_size>shutil.disk_usage(work).free-256*1024**2:raise ValueError("Insufficient disk")
            p=Path(work)/"db"
            with z.open(info) as src,p.open("xb") as dst:shutil.copyfileobj(src,dst,1024**2)
            db=sqlite3.connect(p.as_uri()+"?mode=ro&immutable=1",uri=True)
            db.row_factory=sqlite3.Row
            try:
                db.execute("PRAGMA trusted_schema=OFF");owner,scope,msgs,anchors=read_data(db,m)
            finally:db.close()
        if prior and owner!=prior["owner"]:raise ValueError("Collaborator identity changed")
        holdfile=STATE/".awaiting-transcript-upload"
        hold=json.loads(holdfile.read_text()) if holdfile.exists() else {}
        first_replay=bool(prior and hold.get("owner")==owner and hold.get("stamp")==prior["stamp"] and stamp>prior["stamp"])
        with st.transaction(immediate=True):
            current = c.execute("SELECT * FROM aru_snapshot").fetchone()
            if current != prior:
                # A concurrent import committed while we read the zip. Recheck on the next tick.
                return
            turns.replace_reply_links(c, msgs, snapshot)
            if links_only:
                return
            live_edits = set()
            if backup_only:
                for table in ("aru_live_seen","aru_live_hold","aru_live_request","aru_live_generation","aru_live_deleted","aru_live_alias"):
                    c.execute("DELETE FROM "+table)
            else:
                bind_snapshot_slots(c,msgs)
                live_edits = snapshot_live_edits(c,msgs,stamp)
            c.execute("DELETE FROM aru_anchor")
            c.executemany("INSERT INTO aru_anchor VALUES (?,?)",anchors)
            c.execute("DELETE FROM aru_live_hold WHERE received<=?",(stamp,))
            c.execute("DELETE FROM aru_live_seen WHERE observed<=?",(stamp,))
            c.execute("DELETE FROM aru_live_request WHERE requested<=?",(stamp,))
            c.execute("DELETE FROM aru_live_generation WHERE requested<=?",(stamp,))
            c.execute("DELETE FROM aru_live_deleted WHERE received<=?",(stamp,))
            c.execute("DELETE FROM aru_scope")
            c.executemany("INSERT INTO aru_scope VALUES (?,?)",scope.items())
            for r in c.execute("SELECT source,session,mid,received FROM aru_original").fetchall():
                if ((r["source"]==SOURCE and r["mid"] not in msgs and (backup_only or r["received"]<=stamp)) or
                    (r["source"]==LIVE and (backup_only or r["received"]<=stamp or scope.get(r["session"]) is False))):
                    remove_original(st,r["source"],r["session"],r["mid"])
            for r in sorted(msgs.values(),key=lambda r:(r["session"],r["seq"])):
                if c.execute("SELECT 1 FROM aru_live_deleted WHERE session=? AND mid=?",
                             (r["session"],r["mid"])).fetchone():continue
                newer=c.execute("""SELECT max(received) FROM aru_original o WHERE o.session=? AND
                    (o.mid=? OR (o.source=? AND o.mid=(SELECT a.provisional FROM aru_live_alias a
                    WHERE a.session=o.session AND a.client=?))) HAVING count(*)>0""",
                    (r["session"],r["mid"],LIVE,r["mid"])).fetchone()
                if not backup_only and newer and newer[0]>stamp:continue
                put_original(st,archive,SOURCE,r["session"],r["mid"],r["role"],r["text"],r["stamp"],r["seq"],stamp,
                             confirmed_edit=bool(r["has_content_revision"]) or r["mid"] in live_edits)
            if first_replay:
                # Serein reads each conversation in raw row-id order. A historical edit
                # gets a new row ID during reconciliation. Replay the first clean snapshot
                # in export order before its worker can freeze any batches.
                for table in ("fact_events","raw_processing","pipeline_batches"):
                    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
                    if exists and c.execute("SELECT 1 FROM "+table+" LIMIT 1").fetchone():
                        raise ValueError("Initial replay requires an unprocessed archive")
                edited_keys=set(map(tuple,c.execute("""SELECT source,session_id,source_event_id FROM raw_events
                    WHERE source IN (?,?) AND json_extract(metadata_json,'$.aru_edit_review')=1""",(SOURCE,LIVE))))
                c.execute("DELETE FROM raw_events WHERE source IN (?,?)",(SOURCE,LIVE))
                c.execute("INSERT INTO raw_events_fts(raw_events_fts) VALUES ('rebuild')")
                rows=c.execute("""SELECT source,session,mid,role,text,stamp FROM aru_original
                    WHERE source IN (?,?) ORDER BY session,(source=? AND seq=0),seq,stamp,mid""",(SOURCE,LIVE,LIVE))
                for row in rows:
                    append_native_original(st,archive,*row,confirmed_edit=tuple(row[:3]) in edited_keys)
            if backup_only:
                c.execute("INSERT OR REPLACE INTO background_state(name,value_json) VALUES ('aru_backup_only','true')")
            c.execute("DELETE FROM aru_snapshot")
            c.execute("INSERT INTO aru_snapshot VALUES (?,?,?)",(owner,stamp,path.parent.name))
        print("committed conversations",sum(scope.values()),"excluded",len(scope)-sum(scope.values()),
              "originals",len(msgs),"unique",len({(r['role'],r['stamp'],r['text']) for r in msgs.values()}),flush=True)
if __name__=="__main__":run()
