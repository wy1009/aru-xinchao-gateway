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
    async def test_dream_window_and_rotation(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from aru_backup_ob import dream_material_page, identifiers, open_state, recent_dream_candidates
        from aru_source_time import KIND

        db = open_state(":memory:")
        self.addCleanup(db.close)
        hour = 3_600_000
        buckets = {}
        ids = []
        # The archive can be years old; one record is exactly on the inclusive boundary.
        for i, end in enumerate([400 * hour, 200 * hour, 199 * hour, 198 * hour, 197 * hour,
                                 196 * hour, 195 * hour, 152 * hour, 152 * hour - 1]):
            sid = str(i)
            bid, marker = identifiers(KIND, {"id": sid, "part": 0})
            db.execute("INSERT INTO links VALUES (?,?,0,?,'hash',1)", (KIND, sid, bid))
            db.execute("INSERT INTO source_time VALUES (?,'hash',?,?)", (bid, end - hour, end))
            buckets[bid] = {"metadata": {"grow_batch_id": marker, "dont_surface": i == 0},
                            "content": f"素材{i}"}
            ids.append(bid)
        manager = SimpleNamespace(get=AsyncMock(side_effect=buckets.get))
        pool = await recent_dream_candidates(manager, db)
        self.assertEqual([m["id"] for m in pool], ids[1:8])
        after, seen = None, []
        for _ in range(4):
            page = dream_material_page(pool, after)
            self.assertEqual((page["candidateCount"], page["windowEndMs"]), (7, 200 * hour))
            seen.extend(m["id"] for m in page["memories"])
            after = seen[-1]
        self.assertEqual(seen, ids[1:8] + ids[1:2])

        # A newer eligible import moves the window past the previous cursor.
        buckets[ids[0]]["metadata"]["dont_surface"] = False
        only = await recent_dream_candidates(manager, db)
        self.assertEqual([m["id"] for m in dream_material_page(only, after)["memories"]], ids[:1])
        self.assertEqual(dream_material_page([], after)["memories"], [])

    def test_dream_bucket_eligibility(self):
        from aru_backup_ob import eligible_dream_bucket

        for meta in ({"dont_surface": True}, {"tags": ["Dream"]}, {"tags": ["梦境"]},
                     {"source": "xinchao-dream"}, {"type": "archived"}, {"deleted_at": "today"}):
            self.assertFalse(eligible_dream_bucket({"metadata": meta, "content": "旧素材"}))
        legacy = {"metadata": {}, "content": "梦境：旧的梦\n梦境余韵：旧余韵"}
        self.assertFalse(eligible_dream_bucket(legacy))
        self.assertTrue(eligible_dream_bucket({"metadata": {}, "content": "我们吃饭时聊到了梦。"}))

    async def test_dream_candidates_follow_source_conversations_across_imports(self):
        import tempfile
        from aru_backup import OWNER, LABEL
        from aru_backup_ob import open_state, recent_dream_candidates, sync
        from bucket_manager import BucketManager

        with tempfile.TemporaryDirectory() as temp:
            db = open_state(":memory:")
            self.addCleanup(db.close)
            manager = BucketManager({"buckets_dir": temp})
            kind = "结构化记忆"
            def item(sid):
                return {"id": sid, "part": 0, "name": sid, "content": "# " + sid + "\n\n在[[北京]]见面。",
                        "tags": ["Aru", LABEL, kind]}

            rows = [item(s) for s in ("a", "b", "c", "unknown", "hidden")]
            # Entirely old conversations; b started first and ended most recently.
            times = {"a": [5000, 5200], "b": [1000, 9000], "c": [6000, 6500], "hidden": [8000, 9500]}
            payload = {"owner": OWNER, "at": 100, "apply": True, "collections": {kind: rows}, "source_times": times}
            await sync(manager, db, payload)
            ids = dict(db.execute("SELECT source_id,bucket FROM links"))
            await manager.update(ids["hidden"], dont_surface=True)
            candidates = await recent_dream_candidates(manager, db)
            self.assertEqual([m["id"] for m in candidates], [ids["b"], ids["c"], ids["a"]])
            self.assertIn("北京", candidates[0]["text"])
            self.assertNotIn("[[", candidates[0]["text"])
            self.assertNotIn("来源对话", candidates[0]["text"])
            self.assertIn("1970-01-01", candidates[0]["period"])

            rows[0]["content"] = "编辑后的摘要"
            await sync(manager, db, {**payload, "at": 101, "collections": {kind: rows[:1]},
                                     "source_times": {"a": times["a"]}})
            latest = await recent_dream_candidates(manager, db)
            self.assertEqual([(m["id"], m["text"]) for m in latest], [(ids["a"], "编辑后的摘要")])

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
