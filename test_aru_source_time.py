import sqlite3
import unittest
from aru_source_time import footer, read


class SourceTimeTest(unittest.TestCase):
    def test_exact_message_provenance(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE structured_memory_entry(id,collaboratorId,state,deletedAt);
            CREATE TABLE message(id,conversationId,createdAt);
            CREATE TABLE structured_memory_entry_source(entryId,messageId,conversationId);
            INSERT INTO structured_memory_entry VALUES('ok','g','ready',NULL),('partial','g','ready',NULL),('other','w','ready',NULL);
            INSERT INTO message VALUES('m1','c',1704067200000),('m2','c',1704067218000);
            INSERT INTO structured_memory_entry_source VALUES('ok','m1','c'),('ok','m2','c'),('other','m2','c'),('partial','m1','c'),('partial','m2','wrong');
        """)
        self.assertEqual(read(db,'g'),{'ok':[1704067200000,1704067218000]})
        self.assertEqual(footer([]),"")
        self.assertIn("来源对话（北京时间）", footer(read(db,'g')['ok']))


class SourceTimeSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_backup_can_fill_times_and_retain_wikilinks(self):
        import json
        import tempfile
        from aru_backup import OWNER, LABEL
        from aru_backup_ob import open_state, sync
        from aru_enrichment import progress
        from bucket_manager import BucketManager

        with tempfile.TemporaryDirectory() as tmp:
            db = open_state(":memory:")
            self.addCleanup(db.close)
            manager = BucketManager({"buckets_dir":tmp})
            kind = "结构化记忆"
            item = {"id":"s","part":0,"name":"会面","content":"来到北京。","tags":["Aru",LABEL,kind]}
            data = {"owner":OWNER,"at":15,"apply":True,"collections":{kind:[item]}}
            await sync(manager, db, data)
            row = db.execute("select * from links").fetchone()
            original = (await manager.get(row["bucket"]))["metadata"]
            old = {"tags":["城市"],"domain":["回忆"],"importance":9,"valence":.7,"arousal":.4,"wikilinks":["北京"]}
            db.execute("insert into annotations values(?,?,?,?,1)",
                       (row["bucket"],row["content_hash"],json.dumps(item["tags"]),json.dumps(old)))
            bounds = [1704067200000,1704067218000]
            timed = {**data, "source_times":{"s":bounds}}
            self.assertEqual((await sync(manager,db,timed))[kind],{"update":1})
            self.assertEqual((await sync(manager,db,data))[kind],{"unchanged":1})
            self.assertEqual((await sync(manager,db,timed))[kind],{"unchanged":1})
            self.assertEqual(progress(db),{"total":1,"done":1})
            self.assertEqual((await manager.get(row["bucket"]))["content"].strip(),"来到[[北京]]。"+footer(bounds))
            meta = (await manager.get(row["bucket"]))["metadata"]
            self.assertEqual(meta["created"], "2024-01-01T08:00:00.000+08:00")
            self.assertEqual(meta["name"], "2024-01-01 08-00-00 会面")
            self.assertEqual(meta["imported_at"], original["created"])
            self.assertEqual(meta["last_active"], original["last_active"])

            # A changed source without verifiable messages falls back to the actual import.
            edited = {**item, "content": "更新后的记忆"}
            changed = {**data, "at": 16, "collections": {kind: [edited]}}
            await sync(manager, db, changed)
            fallback = (await manager.get(row["bucket"]))["metadata"]
            self.assertEqual(fallback["created"], original["created"])
            self.assertEqual(fallback["name"], original["name"])
            self.assertEqual((await sync(manager, db, changed))[kind], {"unchanged": 1})

            fresh = {**item, "id": "second"}
            extended = {**changed, "at": 17, "collections": {kind: [edited, fresh]}, "source_times": {"second": bounds}}
            self.assertEqual((await sync(manager, db, extended))[kind], {"unchanged": 1, "create": 1})
            fresh_id = db.execute("select bucket from links where source_id='second'").fetchone()[0]
            fresh_meta = (await manager.get(fresh_id))["metadata"]
            self.assertEqual(fresh_meta["created"], meta["created"])
            self.assertEqual(fresh_meta["name"], "2024-01-01 08-00-00 会面")
            self.assertEqual((await sync(manager, db, extended))[kind], {"unchanged": 2})
            self.assertEqual((await manager.get(fresh_id))["metadata"]["name"], fresh_meta["name"])
