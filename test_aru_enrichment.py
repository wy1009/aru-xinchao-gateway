import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aru_backup import LABEL, OWNER
import aru_wikilinks as wiki
from aru_backup_ob import open_state, sync
from aru_enrichment import enrich_batch, parse_results, progress


class EnrichmentTest(unittest.IsolatedAsyncioTestCase):
    async def test_enrichment_lifecycle(self):
        from bucket_manager import BucketManager
        with tempfile.TemporaryDirectory() as tmp:
            db = open_state(":memory:")
            self.addCleanup(db.close)
            manager = BucketManager({"buckets_dir": tmp})
            lock = asyncio.Lock()
            kind = "条目记忆"
            tags = ["Aru", LABEL, kind, "原标签"]
            def payload(at, text):
                group = [] if not text else [{"id": "source", "part": 0, "name": "阅读",
                                               "content": text, "tags": tags}]
                return {"owner": OWNER, "apply": True, "at": at, "collections": {kind: group}}
            first = payload(1, "小禾的读书记录，又提到小禾。")
            await sync(manager, db, first)
            extra = await manager.create(content="independent")
            calls = []
            during = None
            async def chat(prompt, user, **kwargs):
                records = json.loads(user)["records"]
                calls.extend(x["content"] for x in records)
                if during:
                    await sync(manager, db, during)
                return json.dumps({"records": [{"id": x["id"], "tags": ["阅读"], "importance": 9,
                                   "domain": ["回忆"], "valence": .8, "arousal": .4,
                                   "wikilinks": [{"text": "小禾", "kind": "人名"}]} for x in records]})
            shared = SimpleNamespace(import_engine=SimpleNamespace(dehydrator=SimpleNamespace(_chat=chat)), bucket_mgr=manager)
            async def run():
                return await enrich_batch(shared, db, lock)
            self.assertEqual(await run(), {"done": 1, "total": 1})
            bid = db.execute("select bucket from links").fetchone()[0]
            bucket = await manager.get(bid)
            self.assertEqual(set(bucket["metadata"]["tags"]), {*tags, "阅读"})
            self.assertEqual(bucket["metadata"]["importance"], 9)
            self.assertEqual(bucket["metadata"]["domain"], ["Aru·小禾", "回忆"])
            self.assertEqual(bucket["content"].strip(), "[[小禾]]的读书记录，又提到小禾。")
            self.assertEqual((await manager.get(extra))["metadata"]["importance"], 5)
            self.assertEqual((await sync(manager, db, first))[kind], {"unchanged": 1})
            await run()
            self.assertEqual(len(calls), 1)
            db.execute("update annotations set applied=0")
            await run()
            self.assertEqual(len(calls), 1)
            await sync(manager, db, payload(2, "改变后的内容"))
            self.assertEqual(progress(db)["done"], 0)
            self.assertNotIn("阅读", (await manager.get(bid))["metadata"]["tags"])
            during = payload(3, "进一步变更")
            self.assertEqual((await run())["done"], 0)
            during = None
            await run()
            self.assertEqual(progress(db)["done"], 1)
            await sync(manager, db, payload(4, ""))
            self.assertEqual(await run(), {"done": 0, "total": 0})
            await sync(manager, db, payload(5, "进一步变更"))
            await run()
            self.assertEqual(len(calls), 3)
            self.assertEqual((await manager.get(bid))["metadata"]["importance"], 9)

    def test_source_only_wikilinks(self):
        source = "# 小禾\n\n主人、小禾与小禾在 New York。MyAru 不是 Aru。已有 [[小屋]] 和 `小禾`。"
        terms = wiki.names([{"text": t, "kind": k} for t,k in [
            ("小禾","人名"),("主人","人名"),("小屋","专名"),("New York","地名"),
            ("Aru","专名"),("纽约","地名"),("👻","类别错误")]], source)
        marked = wiki.render(source, terms)
        self.assertEqual(marked, "# 小禾\n\n主人、[[小禾]]与小禾在 [[New York]]。MyAru 不是 [[Aru]]。已有 [[小屋]] 和 `小禾`。")
        self.assertEqual(wiki.render(marked, terms), marked)

    async def test_existing_analysis_only_adds_names(self):
        from bucket_manager import BucketManager
        with tempfile.TemporaryDirectory() as tmp:
            db = open_state(":memory:")
            self.addCleanup(db.close)
            mgr = BucketManager({"buckets_dir":tmp})
            item = {"id":"old", "part":0, "name":"会面", "content":"小禾在北京。", "tags":["Aru",LABEL,"信息卡"]}
            payload = {"owner":OWNER, "apply":True, "at":1, "collections":{"信息卡":[item]}}
            await sync(mgr, db, payload)
            row = db.execute("select * from links").fetchone()
            old = {"tags":["相遇"],"domain":["回忆"],"importance":9,"valence":.7,"arousal":.4}
            db.execute("insert into annotations values(?,?,?,?,1)",(row["bucket"],row["content_hash"],json.dumps(item["tags"]),json.dumps(old)))
            async def chat(prompt, data, **kwargs):
                self.assertNotIn("importance", prompt)
                return json.dumps({"records":[{"id":1,"wikilinks":[{"text":"北京","kind":"地名"}]}]})
            shared=SimpleNamespace(import_engine=SimpleNamespace(dehydrator=SimpleNamespace(_chat=chat)), bucket_mgr=mgr)
            self.assertEqual(progress(db)["done"], 0)
            self.assertEqual((await enrich_batch(shared,db,asyncio.Lock()))["done"],1)
            current=await mgr.get(row["bucket"])
            self.assertEqual(current["metadata"]["importance"],9)
            self.assertEqual(current["content"].strip(),"小禾在[[北京]]。")
            self.assertEqual((await sync(mgr,db,payload))["信息卡"],{"unchanged":1})

    def test_reject_unusable_responses(self):
        for raw in ('{}', '{"records":[]}', '{"records":[{"id":2}]}'):
            with self.assertRaises((KeyError, ValueError)):
                parse_results(raw, 1)
