import sqlite3
import unittest

from aru_serein_turns import SCHEMA, replace_reply_links, select_exchange, visible_turns


class ReplyLinks(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA + """
            CREATE TABLE background_state(name PRIMARY KEY,value_json);
            CREATE TABLE aru_original(source,session,mid,role,text,stamp,seq);
            CREATE TABLE raw_events(source,session_id,source_event_id,role);
            CREATE TABLE aru_scope(session,allowed);
            INSERT INTO aru_scope VALUES ('s',1),('t',1);""")
        self.records = {}
        self.add("u", "user", 1)
        self.add("other", "user", 2, session="t")
        self.add("intervening", "user", 3)
        self.add("a", "assistant", 5, "gen-u")
        self.sync()

    def add(self, mid, role, seq, target=None, session="s"):
        row = dict(mid=mid, role=role, seq=seq, session=session,
                   generationId="gen-"+mid, reply_target=target)
        self.records[mid] = row
        self.db.execute("INSERT INTO aru_original VALUES ('aru',?,?,?,?,?,?)",
                        (session, mid, role, mid*400, seq, seq))
        self.db.execute("INSERT INTO raw_events VALUES ('aru',?,?,?)", (session, mid, role))

    def sync(self):
        replace_reply_links(self.db, self.records, '"sample"')

    def select(self, *mids, cutoff=100, visible=()):
        refs = [self.db.execute("SELECT * FROM aru_original WHERE mid=?", (mid,)).fetchone() for mid in mids]
        return [r["text"] for r in select_exchange(self.db, refs, cutoff, visible_turns(visible))]

    def test_both_anchors_get_the_complete_actual_reply(self):
        pair = ["u"*400, "a"*400]
        self.assertEqual(self.select("a"), pair)
        self.assertEqual(self.select("u", visible=[pair[0]]), pair)
        self.assertEqual(self.select("a", visible=[pair[0]+"\n\n<sent_at 2026-11-27 10:00>", pair[1]]), [])
        self.assertEqual(self.select("intervening"), [])
        self.assertEqual(self.select("u", cutoff=5), [])

    def test_rerolls_edits_deletion_scope_and_new_snapshot(self):
        self.add("reroll", "assistant", 6, "gen-u")
        self.sync()
        self.assertEqual(self.select("u"), [])
        self.assertEqual(self.select("u", "reroll"), ["u"*400, "reroll"*400])
        self.db.execute("UPDATE aru_original SET text='corrected' WHERE mid='u'")
        self.assertEqual(self.select("a"), ["corrected", "a"*400])
        self.db.execute("UPDATE aru_scope SET allowed=0 WHERE session='s'")
        self.assertEqual(self.select("a"), [])
        self.db.execute("UPDATE aru_scope SET allowed=1 WHERE session='s'")
        self.db.execute("DELETE FROM raw_events WHERE source_event_id='u'")
        self.assertEqual(self.select("a"), [])
        del self.records["u"]
        self.sync()
        self.assertEqual(self.db.execute("SELECT count(*) FROM aru_reply_link").fetchone()[0], 0)

    def test_no_cross_session_or_ambiguous_generation(self):
        self.records["u"]["generationId"] = "gen-other"
        self.sync()
        self.assertEqual(self.select("a"), [])
        self.records["a"]["reply_target"] = "gen-other"
        self.records["intervening"]["generationId"] = "gen-other"
        self.sync()
        self.assertEqual(self.select("a"), [])
