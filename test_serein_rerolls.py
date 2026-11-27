"""Regression tests for anonymous Aru request windows against the native source store."""
import importlib.util
import os
import sqlite3
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from serein_gateway import source_window, window

try:
    from serein.bootstrap import initialize
    from serein.config import Settings
    from serein.core.store import Store
    from serein.extensions import pipeline
    from aru_serein_store import SCHEMA
except ModuleNotFoundError as exc:
    if exc.name != "serein":
        raise
    Settings = None


def msg(role, body, minute=None):
    if minute:
        body += "\n\n<sent_at 2026-11-27 " + minute + ">"
    return {"role": role, "content": body}


@unittest.skipUnless(Settings, "requires Serein's native Python environment")
class Rerolls(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root/"hook-key").write_text("test")
        self.cfg = Settings(root/"db", root/"index", writable=True)
        initialize(self.cfg)
        with Store(self.cfg.database) as store:
            store.conn.executescript(SCHEMA)
            store.conn.execute("insert into aru_scope values ('s',1)")
        pipeline.initialize(self.cfg.database)
        spec = importlib.util.spec_from_file_location("local_bridge", Path(__file__).with_name("aru_serein_bridge.py"))
        self.bridge = importlib.util.module_from_spec(spec)
        with patch("serein.config.load_settings", return_value=self.cfg), patch.dict(os.environ, CREDENTIALS_DIRECTORY=directory.name):
            spec.loader.exec_module(self.bridge)
        self.bridge.BACKUP_ONLY = root/"backup-only"
        self.head = [msg("user","first","09:01"),msg("assistant","first reply")]
        self.send("first",1,self.head[:1],"first reply")

    def send(self, key, stamp, messages, answer=None):
        last = len(messages)-1
        return self.bridge.record(dict(turn=key, session="personal", stamp=stamp, turns=window(messages,last,str),
                                       source_window=source_window(messages,last,str), answer=answer))["status"]

    def rows(self):
        with sqlite3.connect(self.cfg.database) as c:
            return c.execute("select id,text,json_extract(metadata_json,'$.aru_edit_review') from raw_events order by id").fetchall()

    def test_reroll_and_typo_keep_ids_while_stale_completion_cannot_win(self):
        question = msg("user","bluee","09:02")
        messages = self.head+[question]
        self.send("generation1",2,messages,"version A")
        initial = self.rows()
        self.send("generation2",3,messages)
        self.send("generation3",4,messages,"version C")
        self.send("generation2",3,messages,"out of order version B")
        self.assertEqual(self.rows()[-1],(initial[-1][0],"version C",1))
        fixed = self.head+[msg("user","blue","09:02")]
        self.send("typo",5,fixed,"entirely different reply")
        self.send("generation1",2,messages,"stale version A")
        rows = self.rows()
        self.assertEqual([r[0] for r in rows],[r[0] for r in initial])
        self.assertEqual(rows[-2][1:],(fixed[-1]["content"],1))
        self.assertEqual(rows[-1][1:],("entirely different reply",1))

    def test_historical_typo_beyond_recall_window_and_genuine_repeats(self):
        messages = self.head[:]
        for i in range(7):
            messages.append(msg("user","again" if i<2 else f"question {i}",f"09:{i+3:02}"))
            self.send(str(i),i+10,messages,f"reply {i}")
            messages.append(msg("assistant",f"reply {i}"))
        old = self.rows()
        messages[3] = msg("assistant","revised first response")
        self.send("next",22,messages+[msg("user","new","09:22")])
        new = self.rows()
        self.assertEqual(new[3],(old[3][0],"revised first response",1))
        self.assertEqual(len(new),len(old)+1)
        self.assertEqual(sum(r[1].startswith("again") for r in new),2)
        before = len(new)
        self.send("sliding",23,messages[-2:]+[msg("user","new","09:22")],"new answer")
        self.assertEqual(len(self.rows()),before+1)

    def test_ambiguous_unstamped_edit_waits_for_snapshot(self):
        self.send("old",5,self.head+[msg("user","same question")],"initial")
        rc=self.send("rewrite",6,self.head+[msg("user","changed question")],"alternate")
        self.assertEqual(rc,"awaiting_archive")
        self.assertEqual(len(self.rows()),4)

    def test_history_wins_over_older_pending_generation_and_omission_keeps_originals(self):
        q = msg("user","draw a square","09:02")
        self.send("original",2,self.head+[q],"square")
        self.send("reroll",3,self.head+[q])
        following = [q,msg("assistant","square"),msg("user","next","09:04")]
        self.send("following",4,following,"reply")
        self.send("reroll",3,self.head+[q],"late triangle")
        self.assertEqual(self.rows()[3][1],"square")
        self.assertEqual(len(self.rows()),6)
        self.assertEqual(self.rows()[0][1],self.head[0]["content"])

    def test_canonical_backup_does_not_restore_an_answer_replaced_after_export(self):
        import zipfile
        from datetime import datetime
        from aru_serein_store import BEIJING
        import aru_serein_sync as sync
        root = self.cfg.database.parent
        uploads = root/"uploads"
        uploads.mkdir()
        session = "s"
        # The initial fixture was headerless and therefore has its own scope.
        with sqlite3.connect(self.cfg.database) as db:
            session, = db.execute("select session from aru_original limit 1").fetchone()
        base = int(datetime(2026,11,27,9,0,tzinfo=BEIJING).timestamp()*1000)
        q = msg("user","another question","09:02")
        self.send("v1",base+120000,self.head+[q],"old answer")
        self.send("v2",base+160000,self.head+[q],"chosen answer")
        def snapshot(cutoff, answer):
            folder = uploads/str(cutoff)
            folder.mkdir()
            with zipfile.ZipFile(folder/"archive.polarisarchive","w") as z:
                z.writestr("database.sqlite",b"")
            data={}
            entries = (("user","first",60000),("assistant","first reply",61000),
                       ("user","another question",120000),("assistant",answer,121000))
            for n,(role,text,offset) in enumerate(entries):
                mid=f"canonical{n}"
                data[mid]=dict(mid=mid,session=session,role=role,text=text,stamp=base+offset,seq=n+1,
                               generationId=mid, reply_target=f"canonical{n-1}" if role=="assistant" else None,
                               has_content_revision=False)
            def manifest(z):
                return {"createdAt":int(Path(z.filename).parent.name)}
            with ExitStack() as stack:
                for name,value in {"STATE":root, "UPLOADS":uploads, "BACKUP_ONLY":self.bridge.BACKUP_ONLY, "read_manifest":manifest,
                                   "load_settings":lambda _:self.cfg,
                                   "read_data":lambda *_:("fixture-owner",{session:True},data,[])}.items():
                    stack.enter_context(patch.object(sync,name,value))
                stack.enter_context(patch.object(sync.pwd,"getpwnam"))
                stack.enter_context(patch.object(sync.os,"getuid",return_value=1000))
                sync.run()
        snapshot(base+135000,"old answer")
        self.assertEqual([r[1] for r in self.rows()].count("chosen answer"),1)
        self.assertNotIn("old answer",[r[1] for r in self.rows()])
        self.bridge.BACKUP_ONLY.touch()
        self.assertEqual(self.send("ignored",base+220000,[msg("user","ignore")],"ignore answer"),"archive_only")
        snapshot(base+200000,"final archive answer")
        self.assertEqual(len(self.rows()),4)
        self.assertIn("final archive answer",[r[1] for r in self.rows()])
        self.assertNotIn("chosen answer",[r[1] for r in self.rows()])

    def test_reply_edited_before_existing_terminal_question_has_two_anchors(self):
        q=msg("user","choose a surface","09:02")
        self.send("a",2,self.head+[q],"first surface")
        current=self.head+[q,msg("assistant","first surface"),msg("user","then?","09:03")]
        self.send("b",3,current,"follow-up")
        old=self.rows()
        current[-2]=msg("assistant","corrected surface")
        self.send("b-again",4,current,"new follow-up")
        rows=self.rows()
        self.assertEqual(rows[3],(old[3][0],"corrected surface",1))
        self.assertEqual(len(rows),len(old))

    def test_clock_can_change_while_two_neighbors_and_spelling_identify_typo(self):
        self.send("old",2,self.head+[msg("user","blue flwer","09:02")],"one")
        self.send("corrected",3,self.head+[msg("user","blue flower","09:08")],"two")
        self.assertEqual(len(self.rows()),4)
        self.assertTrue(self.rows()[-2][1].startswith("blue flower"))
        self.assertEqual(self.rows()[-1][1],"two")
