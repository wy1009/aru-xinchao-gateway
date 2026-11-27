"""Resolve full historical exchanges from the active Aru backup."""
from collections import defaultdict

from aru_serein_alignment import split_original


SCHEMA = """
CREATE TABLE IF NOT EXISTS aru_reply_link(
    session TEXT NOT NULL, answer_mid TEXT NOT NULL, question_mid TEXT NOT NULL,
    PRIMARY KEY(session,answer_mid));
CREATE INDEX IF NOT EXISTS aru_replies_by_question ON aru_reply_link(session,question_mid);
"""
SNAPSHOT_KEY = "aru_reply_links_snapshot_v1"


def replace_reply_links(conn, messages, snapshot):
    questions = defaultdict(list)
    for row in messages.values():
        if row["role"] == "user" and row["generationId"]:
            questions[row["session"], row["generationId"]].append(row)
    links = []
    for answer in messages.values():
        if answer["role"] != "assistant":
            continue
        parents = questions.get((answer["session"], answer["reply_target"]), ())
        if len(parents) == 1 and parents[0]["seq"] < answer["seq"]:
            links.append((answer["session"], answer["mid"], parents[0]["mid"]))
    conn.execute("DELETE FROM aru_reply_link")
    conn.executemany("INSERT INTO aru_reply_link VALUES (?,?,?)", links)
    conn.execute("INSERT OR REPLACE INTO background_state(name,value_json) VALUES (?,?)",
                 (SNAPSHOT_KEY, snapshot))


def visible_turns(visible):
    bodies = [split_original(text.strip())[0].strip() for text in visible]
    return set(zip(bodies, bodies[1:]))


def select_exchange(conn, ranked, before, visible):
    """Choose the strongest event source with a verifiable, same-session reply."""
    bound = {(r["session"], r["mid"]) for r in ranked if r["source"] == "aru"}
    for ref in ranked:
        if ref["source"] != "aru" or ref["role"] not in ("user", "assistant"):
            continue
        is_question = ref["role"] == "user"
        anchor, other = ("question_mid", "answer_mid") if is_question else ("answer_mid", "question_mid")
        partners = conn.execute(f"""
            SELECT o.* FROM aru_reply_link l
            JOIN aru_scope s ON s.session=l.session AND s.allowed=1
            JOIN aru_original o ON o.source='aru' AND o.session=l.session AND o.mid=l.{other}
            JOIN raw_events r ON (r.source,r.session_id,r.source_event_id)=(o.source,o.session,o.mid)
            WHERE l.session=? AND l.{anchor}=? AND o.role=? AND r.role=o.role AND o.stamp<?
            ORDER BY o.seq,o.mid""",
            (ref["session"], ref["mid"], "assistant" if is_question else "user", before)).fetchall()
        if len(partners) > 1:
            # A question can have several replies. Only the event's own sources can disambiguate.
            partners = [p for p in partners if (p["session"], p["mid"]) in bound]
        if len(partners) != 1:
            continue
        pair = (ref, partners[0]) if is_question else (partners[0], ref)
        texts = tuple(split_original(r["text"].strip())[0].strip() for r in pair)
        if texts not in visible:
            return pair
    return ()
