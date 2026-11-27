"""Local authenticated adapter for evidence checking and live reconciliation."""
import hmac
import json
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from serein.compat.raw_archive import raw_archive
from serein.config import load_settings
from serein.recall.germany.gateway import GatewayService
from serein.recall.rendering import render
from serein.core.store import Store

from aru_serein_turns import select_exchange, visible_turns
from aru_serein_alignment import align, chronology, split_original
from aru_serein_store import BEIJING, SOURCE, LIVE, fingerprint, put_original, remove_original

BACKUP_ONLY = Path("/etc/gushou-serein/.backup-only")
settings=load_settings("/etc/gushou-serein/config.toml")
archive=raw_archive(settings)
token=(Path(os.environ["CREDENTIALS_DIRECTORY"])/"hook-key").read_text().strip()

def authenticate(req:Request):
    if not hmac.compare_digest(req.headers.get("authorization","").encode(),("Bearer "+token).encode()):
        raise HTTPException(401)
app=FastAPI(dependencies=[Depends(authenticate)],docs_url=None,redoc_url=None,openapi_url=None)

def scopes(c, turns, session, turn):
    explicit = session if session and session != "personal" else None
    if explicit:
        row = c.execute("SELECT session,allowed FROM aru_scope WHERE session=? COLLATE NOCASE", (explicit,)).fetchone()
        if row:
            return row[0] if row[1] else False

    excluded = {r[0] for r in c.execute("SELECT session FROM aru_scope WHERE allowed=0")}
    candidates = None
    observed = set()
    for pos, message in enumerate(turns):
        role, text = message["role"], message["text"]
        found = set()
        if message.get("mid"):
            found.update(r[0] for r in c.execute(
                "SELECT session FROM aru_original WHERE mid=? AND role=? UNION SELECT a.session FROM aru_live_alias a "
                "JOIN aru_original o ON o.source=? AND o.session=a.session AND o.mid=a.provisional "
                "WHERE a.client=? AND o.role=?", (message["mid"], role, LIVE, message["mid"], role)))
        if not found:
            for spelling in {text, split_original(text)[0]}:
                found.update(r[0] for r in c.execute("SELECT session FROM aru_anchor WHERE hash=?",
                                                     (fingerprint([role, spelling]),)))
                found.update(r[0] for r in c.execute(
                    "SELECT session FROM aru_original WHERE role=? AND text=?", (role, spelling)))
            # A new question alone can repeat one in an unrelated conversation.
            # Use older dialogue to link windows, but retain exclusion evidence.
            if pos == len(turns) - 1 and not found.intersection(excluded):
                continue
        if found:
            observed.update(found)
            candidates = found if candidates is None else candidates.intersection(found)
    if candidates and len(candidates) == 1:
        result, = candidates
        return False if result in excluded else (explicit or result)
    if (candidates or observed).intersection(excluded):
        return None
    # First turn of a headerless chat, or a history shared by several allowed
    # chats. Keep its own identity; subsequent turns can link to these originals.
    return explicit or ("gateway-" + fingerprint(turn))

def original(c, session, mid, role=None):
    return c.execute("""SELECT o.* FROM aru_original o WHERE o.session=? AND (? IS NULL OR o.role=?)
        AND (o.mid=? OR (o.source=? AND o.mid=(SELECT provisional FROM aru_live_alias
        WHERE session=? AND client=?))) ORDER BY (o.mid=?) DESC,o.source LIMIT 1""",
        (session, role, role, mid, LIVE, session, mid, mid)).fetchone()


def is_deleted(c, session, mid):
    return c.execute("SELECT 1 FROM aru_live_deleted WHERE session=? AND mid=?", (session, mid)).fetchone()


def apply_changes(st, session, stamp, turns, changes):
    c = st.conn
    for mid in changes.get("deleted", []):
        old = original(c, session, mid)
        if old and old["received"] > stamp:
            continue
        ids = {mid}
        if old:
            ids.add(old["mid"])
            remove_original(st, old["source"], session, old["mid"])
        c.executemany("""INSERT INTO aru_live_deleted VALUES (?,?,?) ON CONFLICT(session,mid)
            DO UPDATE SET received=max(received,excluded.received)""", ((session, key, stamp) for key in ids))
    for m in changes.get("identified", turns[:-1]):
        mid = m.get("mid")
        if not mid or is_deleted(c, session, mid):
            continue
        old = original(c, session, mid, m["role"])
        if not old and turns and mid == turns[-1].get("mid") and m["role"] == "user":
            # A fresh question can repeat an older, unidentified question verbatim.
            continue
        if not old:
            rows = c.execute("""SELECT * FROM aru_original o WHERE source=? AND session=? AND role=? AND text=?
                AND (mid LIKE 'a-%' OR mid LIKE 'q-%') AND NOT EXISTS (SELECT 1 FROM aru_live_alias a
                WHERE a.session=o.session AND a.provisional=o.mid) LIMIT 2""",
                (LIVE, session, m["role"], m["text"])).fetchall()
            if len(rows) == 1 and stamp >= rows[0]["received"]:
                c.execute("INSERT OR IGNORE INTO aru_live_alias VALUES (?,?,?)", (session, mid, rows[0]["mid"]))
            old = original(c, session, mid, m["role"])
        if old and stamp >= old["received"] and (old["text"] != m["text"] or stamp > old["received"]):
            put_original(st, archive, old["source"], session, old["mid"], m["role"], m["text"],
                         old["stamp"], old["seq"], stamp)


def wait_for_archive(c, key, p):
    c.execute("""INSERT INTO aru_live_hold VALUES(?,?,?) ON CONFLICT(turn) DO UPDATE SET
        payload=excluded.payload,received=excluded.received WHERE excluded.received>received OR
        (excluded.received=received AND json_extract(excluded.payload,'$.answer') IS NOT NULL)""",
        (key, p["stamp"], json.dumps(p, ensure_ascii=False)))
    return {"status":"awaiting_archive"}


def uncertain_key(session, transcript, index):
    preceding = [split_original(m["text"])[0] for m in transcript[max(0,index-2):index]]
    return "uncertain-"+fingerprint([session,split_original(transcript[index]["text"])[1],preceding])


def update_text(st, row, message, stamp):
    # Preserve the stored representation when the only difference is Aru's
    # transport timestamp. It is absent from backups and generated answers.
    body = split_original(message)[0]
    old_body, old_clock = split_original(row["text"])
    identity = (row["session"],row["source"],row["mid"])
    seen = st.conn.execute("SELECT observed FROM aru_live_seen WHERE session=? AND source=? AND mid=?",identity).fetchone()
    if stamp < row["received"] or (seen and stamp < seen[0]):
        return
    st.conn.execute("""INSERT INTO aru_live_seen VALUES(?,?,?,?) ON CONFLICT(session,source,mid)
        DO UPDATE SET observed=excluded.observed WHERE observed<excluded.observed""", (*identity,stamp))
    if body != old_body:
        text = body if not old_clock else body + row["text"][len(old_body):]
        put_original(st, archive, row["source"], row["session"], row["mid"], row["role"],
                     text, row["stamp"], row["seq"], stamp)


def store_dialogue(st, p, session, prior):
    c = st.conn
    stamp, turn = int(p["stamp"]), p["turn"]
    transcript = p.get("source_window") or p["turns"]
    q = transcript[-1]
    # Archive sequence is authoritative. Live question and answer have the
    # same request timestamp, so consistently put the question first.
    rows = chronology(c, session)
    qmid = q.get("mid") or "q-"+turn
    candidate = original(c, session, qmid, "user")
    if prior and prior["session"] == session:
        candidate = original(c, session, prior["qmid"], "user") or candidate
    matches, pending = align(transcript, rows)
    position = matches.get(len(transcript)-1)
    if candidate is None and position is not None:
        candidate = rows[position]
    if candidate is not None:
        qmid, source = candidate["mid"], candidate["source"]
        if position is None or (rows[position]["source"], rows[position]["mid"]) != (source,qmid):
            position = next(i for i,r in enumerate(rows) if (r["source"],r["mid"]) == (source,qmid))
    else:
        source = LIVE
    previous = c.execute("SELECT requested,turn FROM aru_live_generation WHERE session=? AND source=? AND qmid=?",
                         (session,source,qmid)).fetchone()
    if previous and (stamp < previous["requested"] or
                     (stamp == previous["requested"] and turn != previous["turn"])):
        return {"status":"stored"}
    # Re-delivery of an old request must never replay an old visible history.
    replay = prior and prior["requested"] == stamp
    if not replay:
        apply_changes(st, session, stamp, p["turns"], p.get("source_changes") or {})
        for index, location in matches.items():
            if index < len(transcript)-1 and not transcript[index].get("mid"):
                update_text(st, rows[location], transcript[index]["text"], stamp)
    if is_deleted(c,session,qmid) or (candidate and candidate["received"] > stamp and
                                      split_original(candidate["text"])[0] != split_original(q["text"])[0]):
        return {"status":"stored"}
    if candidate is None and pending:
        return wait_for_archive(c,uncertain_key(session,transcript,len(transcript)-1),p)
    if candidate:
        update_text(st, candidate, q["text"], stamp)
    else:
        put_original(st, archive, source, session, qmid, "user", q["text"], stamp, 0, stamp)
    c.execute("INSERT OR REPLACE INTO aru_live_request VALUES(?,?,?,?,?)",(turn,session,source,qmid,stamp))
    c.execute("INSERT OR REPLACE INTO aru_live_generation VALUES(?,?,?,?,?)",(session,source,qmid,stamp,turn))
    if p.get("answer"):
        amid = p.get("answer_id") or ("a-"+qmid[2:] if qmid.startswith("q-") else "a-"+fingerprint(qmid))
        answer = original(c, session, amid, "assistant")
        if not answer and not p.get("answer_id") and position is not None and position+1 < len(rows):
            following = rows[position+1]
            if following["role"] == "assistant":
                answer, amid = following, following["mid"]
        if not is_deleted(c,session,amid):
            if answer:
                update_text(st, answer, p["answer"], stamp)
            else:
                put_original(st, archive, LIVE, session, amid, "assistant", p["answer"], stamp, 0, stamp)
    if c.execute("SELECT 1 FROM aru_live_hold LIMIT 1").fetchone():
        keys = {turn, *(uncertain_key(session,transcript,index) for index in matches)}
        c.executemany("DELETE FROM aru_live_hold WHERE turn=? AND received<=?",((key,stamp) for key in keys))
    return {"status":"stored"}


@app.post("/turn")
def record(p:dict):
    if BACKUP_ONLY.is_file():
        return {"status": "archive_only"}
    turns=p["turns"];stamp=int(p["stamp"]);turn=p["turn"]
    with Store(settings.database) as st,st.transaction(immediate=True):
        c=st.conn
        last=c.execute("SELECT stamp FROM aru_snapshot").fetchone()
        if last and stamp<=last[0]:return {"status":"covered_by_archive"}
        changes=p.get("source_changes") or {}
        evidence=list(turns)
        historical=[{"role":m["role"],"mid":m["mid"],"text":m["text"]}
                    for m in changes.get("identified",[]) if m not in turns]
        for mid in changes.get("deleted",[]):
            historical.extend(dict(r) for r in c.execute("""SELECT role, ? AS mid, text FROM aru_original
                WHERE mid=? OR (source=? AND EXISTS (SELECT 1 FROM aru_live_alias a
                WHERE a.provisional=aru_original.mid AND a.session=aru_original.session AND a.client=?))""",
                (mid,mid,LIVE,mid)))
        prior=c.execute("SELECT * FROM aru_live_request WHERE turn=?",(turn,)).fetchone()
        if prior and prior["requested"] > stamp:return {"status":"stored"}
        session=scopes(c,[*historical,*evidence],p["session"],turn)
        if session is False:return {"status":"excluded"}
        if session is None:return wait_for_archive(c,turn,p)
        return store_dialogue(st,p,session,prior)


def grams(text):
    parts=re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+",text.lower())
    return {p[i:i+n] for p in parts for n in (2,3,4) for i in range(len(p)-n+1)}

def short_quote(text,focus,context,size=160):
    # Choose one continuous, traceable passage, including when a long reply mentions the subject at its end.
    if len(text)<=size:return text
    starts=range(0,len(text),size//2)
    def score(i):
        tokens=grams(text[i:i+size])
        return 3*len(tokens & focus)+len(tokens & context)
    i=max(starts,key=score)
    return ("…" if i else "")+text[i:i+size]+("…" if i+size<len(text) else "")

def hook_card_is_current(card, doc, limit):
    """Recheck native text projection, including passage offsets, before forwarding its context."""
    hit = {"id": doc["id"], "kind": doc["kind"], "object": {"document": doc}}
    if spans := card.get("source_spans"):
        text = doc["body_md"]
        hit["body_excerpt"] = {
            "text": "".join(text[s["start_offset"]:s["end_offset"]] for s in spans),
            "source_spans": spans, "input_tokens": card.get("reranker_input_tokens"),
        }
    try:
        current = render([hit], body_char_limit=limit)["cards"]
    except ValueError:
        return False
    if not current:
        return False
    fields = ("id", "source_kind", "title", "text", "date", "end_date", "started_at", "ended_at",
              "date_basis", "recorded_at", "local_start", "local_end", "timezone")
    return all(current[0].get(name) == card.get(name) for name in fields)


def evidence_card(row):
    speaker = "主人" if row["role"] == "user" else "顾守"
    when = datetime.fromtimestamp(row["stamp"]/1000, BEIJING).strftime("%Y-%m-%d %H:%M")
    return {"id": f"{row['source']}:{row['session']}:{row['mid']}", "source_kind": "raw",
            "title": f"{speaker} · {when}（北京时间）", "date": when[:10], "text": row["text"]}


@app.post("/evidence")
def evidence(p:dict):
    out=[]
    native_cards = p["cards"]
    hook_context = p.get("hook_context", "")
    delivered_docs = []
    supplements = []
    focus=grams(p["latest"]);context=grams(p["query"])
    visible = visible_turns(p.get("visible", []))
    emitted = set()
    before = p.get("before", 2**62)
    with Store(settings.database,read_only=True) as st:
        c=st.conn
        with st.transaction():
            for card in native_cards[:3]:
                kind,sep,key=card["id"].partition(":")
                doc=st.read(key)
                if not sep or not doc or doc["kind"]!=kind or not st.surface_state(key)["can_surface"]:continue
                # Rehydrate displayed text from the active revision, eliminating in-flight stale card contents.
                if doc["metadata"].get("aru_import") or kind not in ("event","scene"):continue
                rows=c.execute("""SELECT s.source_key,s.content FROM sources s JOIN evidence_bindings b
                                  ON b.source_id=s.id WHERE b.document_id=? AND b.active=1""",(key,)).fetchall()
                refs=[]; valid=True
                for row in rows:
                    try:src,session,mid=json.loads(row["source_key"])
                    except (TypeError,ValueError):valid=False;break
                    cur=c.execute("""SELECT o.*,r.text AS native_text FROM aru_original o JOIN raw_events r
                        ON r.source=o.source AND r.session_id=o.session AND r.source_event_id=o.mid
                        JOIN aru_scope a ON a.session=o.session AND a.allowed=1
                        WHERE o.source=? AND o.session=? AND o.mid=? AND o.role=r.role""",(src,session,mid)).fetchone()
                    if not cur or cur["stamp"]>=before or cur["native_text"]!=row["content"]:
                        valid=False;break
                    refs.append(cur)
                if not valid or not rows:continue
                def score(r):
                    g=grams(r["text"]);return (4*len(g & focus)+len(g & context))/(1+len(g)/1200)
                ranked = sorted(refs, key=lambda r: (-score(r), r["session"], r["seq"], r["mid"]))
                day=(doc["metadata"].get("date") or doc["metadata"].get("local_date") or
                     datetime.fromtimestamp(ranked[0]["stamp"]/1000,BEIJING).date().isoformat())
                if card.get("revision") and card["revision"]!=doc["revision"]:continue
                if native_cards and hook_context and not hook_card_is_current(card, doc, p["body_char_limit"]):
                    continue
                excerpt=card.get("text","").strip().strip("…")
                summary=(excerpt if excerpt and excerpt in doc["body_md"] else doc["body_md"])
                item={"date":day or "日期未明","title":doc["title"][:90],
                      "summary":short_quote(summary,focus,context,380)}
                pair = select_exchange(c, ranked, before, visible)
                if pair:
                    raw_cards = [evidence_card(row) for row in pair]
                    pair_ids = tuple(raw["id"] for raw in raw_cards)
                    item["original_ids"] = pair_ids
                    if pair_ids not in emitted:
                        supplements.extend(raw_cards)
                        emitted.add(pair_ids)
                out.append(item)
                delivered_docs.append({"id": card["id"], "source_kind": kind, "title": item["title"],
                                       "date": item["date"], "text": item["summary"]})
    # Preserve the complete Hook response, including preferences or Arc menus.
    # When a returned card is stale, only render the independently revalidated subset.
    use_hook = bool(hook_context) and len(delivered_docs) == len(native_cards)
    compact_cards = supplements if use_hook else [*delivered_docs, *supplements]
    compact = GatewayService._render_hook_recall_additional_context(compact_cards)
    context = "\n\n".join(part for part in (hook_context if use_hook else "", compact) if part)
    return {"items": out, "context": context}
