"""Bind Serein evidence to the current version of each Aru original."""
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from serein.core.store import encode, now

BEIJING = ZoneInfo("Asia/Shanghai")
SOURCE = "aru"
LIVE = "aru-live"
SCHEMA = """
CREATE TABLE IF NOT EXISTS aru_original(
 source TEXT, session TEXT, mid TEXT, role TEXT, text TEXT, stamp INTEGER, seq INTEGER,
 received INTEGER, PRIMARY KEY(source,session,mid));
CREATE INDEX IF NOT EXISTS aru_original_text ON aru_original(role,text);
CREATE TABLE IF NOT EXISTS aru_anchor(hash TEXT,session TEXT, PRIMARY KEY(hash,session));
CREATE TABLE IF NOT EXISTS aru_live_hold(turn TEXT PRIMARY KEY,received INTEGER,payload TEXT);
CREATE TABLE IF NOT EXISTS aru_scope(session TEXT PRIMARY KEY, allowed INTEGER);
CREATE TABLE IF NOT EXISTS aru_snapshot(owner TEXT, stamp INTEGER, archive TEXT);
CREATE TABLE IF NOT EXISTS aru_live_alias(session TEXT,client TEXT,provisional TEXT,PRIMARY KEY(session,client));
CREATE TABLE IF NOT EXISTS aru_source_revision(source TEXT,session TEXT,mid TEXT,revision INTEGER NOT NULL,
 PRIMARY KEY(source,session,mid));
CREATE TABLE IF NOT EXISTS aru_live_seen(
 session TEXT,source TEXT,mid TEXT,observed INTEGER NOT NULL,PRIMARY KEY(session,source,mid));
CREATE TABLE IF NOT EXISTS aru_live_request(
 turn TEXT PRIMARY KEY,session TEXT,source TEXT,qmid TEXT,requested INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS aru_live_generation(
 session TEXT,source TEXT,qmid TEXT,requested INTEGER NOT NULL,turn TEXT NOT NULL,
 PRIMARY KEY(session,source,qmid));
CREATE TABLE IF NOT EXISTS aru_live_deleted(session TEXT,mid TEXT,received INTEGER NOT NULL,PRIMARY KEY(session,mid));
"""

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")

def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

def invalidate_material(store, source, session, mid, raw_id):
    """Invalidate derived events and frozen plans in the same transaction as a source change."""
    from serein.compat.events import project_events
    from serein.core.store import digest
    conn = store.conn
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.execute("""INSERT INTO aru_source_revision VALUES (?,?,?,1) ON CONFLICT(source,session,mid)
        DO UPDATE SET revision=revision+1""",(source,session,mid))
    pending = {raw_id}
    if 'fact_event_sources' in tables:
        affected = conn.execute("""SELECT e.item_id FROM fact_event_sources s JOIN fact_events e USING(item_id)
            WHERE s.source_system=? AND s.session_id=? AND s.message_id=?
              AND e.item_type='event' AND e.status='active'""", (source, session, mid)).fetchall()
        for (event_id,) in affected:
            pending.update(r[0] for r in conn.execute("""SELECT r.id FROM fact_event_sources s
                JOIN raw_events r ON (r.source,r.session_id,r.source_event_id)=(s.source_system,s.session_id,s.message_id)
                WHERE s.item_id=?""", (event_id,)))
            conn.execute("UPDATE fact_events SET status='tombstoned',updated_at=? WHERE item_id=?", (now(), event_id))
        if affected:
            project_events(conn)
    if 'pipeline_batches' not in tables:
        return
    # Frozen work contains verbatim source/context. Rebuild open work in this scope
    # from the current archive, including completed batches with deferred material.
    scope = digest(encode([source, session]))[:20]
    if 'raw_processing' in tables:
        pending.update(r[0] for r in conn.execute("""SELECT r.id FROM raw_events r WHERE source=? AND session_id=?
            AND NOT EXISTS (SELECT 1 FROM raw_processing p WHERE p.raw_id=r.id)""",(source,session)))
    if 'pipeline_pending_materials' in tables:
        pending.update(r[0] for r in conn.execute("""SELECT j.value FROM pipeline_pending_materials pm,
            json_each(pm.source_ids_json) j WHERE pm.scope=? AND pm.status IN ('open','running')""",(scope,)))
        conn.execute("""UPDATE pipeline_pending_materials SET status='resolved',updated_at=?
            WHERE scope=? AND status IN ('open','running')""", (now(), scope))
    # A Writer may have started settling in another process. Its final status write
    # is in the same transaction as the Events, so a late commit rolls back in full.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS aru_revised_batch_guard
        BEFORE UPDATE OF status ON pipeline_batches
        WHEN OLD.status='superseded_original' AND NEW.status<>OLD.status
        BEGIN SELECT RAISE(ABORT,'Source changed; claim a fresh batch'); END""")
    conn.execute("""UPDATE pipeline_batches SET status='superseded_original',result_json=?
        WHERE scope=? AND status NOT IN ('done','superseded_original') AND status NOT GLOB 'superseded_*'""",
        (encode({'status':'superseded_original','reason':'source_changed'}), scope))
    for table in ('raw_processing', 'pipeline_routes', 'pipeline_route_provenance'):
        if table in tables:
            conn.executemany(f'DELETE FROM {table} WHERE raw_id=?', ((i,) for i in pending))


def remove_original(store, source, session, mid):
    conn = store.conn
    key = encode([source, session, mid])
    # Never deliver a derivative of a removed source, including while it is being re-indexed.
    conn.execute("""UPDATE documents SET manual_surface=0 WHERE id IN
        (SELECT b.document_id FROM evidence_bindings b JOIN sources s ON s.id=b.source_id
         WHERE s.source_key=? AND b.active=1)""", (key,))
    row = conn.execute("SELECT * FROM raw_events WHERE source=? AND session_id=? AND source_event_id=?",
                       (source, session, mid)).fetchone()
    if row:
        invalidate_material(store, source, session, mid, row["id"])
        conn.execute("""INSERT INTO raw_events_fts(raw_events_fts,rowid,text,source,conversation_id,session_id)
                        VALUES ('delete',?,?,?,?,?)""",
                     (row["id"], row["text"], source, session, session))
        conn.execute("DELETE FROM raw_events WHERE id=?", (row["id"],))
    conn.execute("DELETE FROM aru_original WHERE source=? AND session=? AND mid=?", (source, session, mid))

def put_original(store, archive, source, session, mid, role, text, stamp, seq, received, *, confirmed_edit=False):
    previous = store.conn.execute("SELECT role,text FROM aru_original WHERE source=? AND session=? AND mid=?",
                                  (source, session, mid)).fetchone()
    if previous and tuple(previous) == (role, text):
        store.conn.execute("""UPDATE aru_original SET received=? WHERE source=? AND session=? AND mid=?
            AND received<?""",(received,source,session,mid,received))
        return
    if previous:
        # Keep the original dialogue order when an old message is corrected.
        old = store.conn.execute("SELECT id,text FROM raw_events WHERE source=? AND session_id=? AND source_event_id=?",
                                 (source,session,mid)).fetchone()
        if old:
            obj, _ = archive._normalize_event(dict(role=role,text=text,source_event_id=mid,session_id=session,
                conversation_id=session,created_at=iso(stamp)),default_source=source,ingested_at=now())
            if obj is None:
                remove_original(store, source, session, mid)
                return
            invalidate_material(store, source, session, mid, old["id"])
            store.conn.execute("""INSERT INTO raw_events_fts(raw_events_fts,rowid,text,source,conversation_id,session_id)
                VALUES ('delete',?,?,?,?,?)""",(old["id"],old["text"],source,session,session))
            store.conn.execute("""UPDATE raw_events SET text=?,event_hash=?,role=?,created_at=?,
                metadata_json=json_set(metadata_json,'$.aru_edit_review',1) WHERE id=?""",
                               (obj["text"],obj["event_hash"],role,obj["created_at"],old["id"]))
            store.conn.execute("INSERT INTO raw_events_fts(rowid,text,source,conversation_id,session_id) VALUES (?,?,?,?,?)",
                               (old["id"],obj["text"],source,session,session))
            store.conn.execute("""UPDATE aru_original SET text=?,role=?,stamp=?,seq=?,received=?
                WHERE source=? AND session=? AND mid=?""",(text,role,stamp,seq,received,source,session,mid))
            return
        store.conn.execute("DELETE FROM aru_original WHERE source=? AND session=? AND mid=?",(source,session,mid))
    store.conn.execute("INSERT INTO aru_original VALUES(?,?,?,?,?,?,?,?)",
                       (source,session,mid,role,text,stamp,seq,received))
    append_native_original(store, archive, source, session, mid, role, text, stamp,
                           confirmed_edit=confirmed_edit or previous is not None)

def append_native_original(store, archive, source, session, mid, role, text, stamp, *, confirmed_edit=False):
    obj, _ = archive._normalize_event(dict(role=role,text=text,source_event_id=mid,session_id=session,
        conversation_id=session,created_at=iso(stamp),metadata={"aru_edit_review":1} if confirmed_edit else {}),
        default_source=source,ingested_at=now())
    if obj:
        archive._insert_event_on_connection(store.conn, obj)
