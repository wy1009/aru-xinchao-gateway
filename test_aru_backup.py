from datetime import datetime
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from aru_backup import LABEL, OWNER, snapshot
from aru_backup_ob import identifiers, open_state, sync


class BackupSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_sync_lifecycle(self):
        from bucket_manager import BucketManager

        with tempfile.TemporaryDirectory() as work:
            manager = BucketManager({"buckets_dir": work})
            db = open_state(":memory:")
            self.addCleanup(db.close)
            kind = "信息卡"
            doc = "长期资料"

            def item(kind, name):
                return {
                    "id": "stable",
                    "part": 0,
                    "tags": ["Aru", LABEL, kind],
                    "name": name,
                    "content": "# " + name + "\n\nexample",
                }

            def data(stamp, groups, apply=True):
                return {
                    "owner": OWNER,
                    "at": stamp,
                    "collections": groups,
                    "apply": apply,
                }

            dated_title = "2024-01-02 10-30-00 观察笔记"
            first = data(1, {kind: [item(kind, "长标题" * 40)], doc: [item(doc, dated_title)]})
            dry = await sync(manager, db, {**first, "apply": False})
            self.assertEqual(dry[kind], {"create": 1})
            self.assertEqual(await manager.list_all(), [])
            other = await manager.create(name="独立记忆", content="independent record")
            await sync(manager, db, first)
            dated = await manager.get(identifiers(doc, first["collections"][doc][0])[0])
            stamp = datetime.fromisoformat(dated["metadata"]["created"]).strftime("%Y-%m-%d %H-%M-%S")
            self.assertEqual(dated["metadata"]["name"], f"{stamp} {dated_title}")
            self.assertRegex((await manager.get(other))["metadata"]["name"], r"^\d{4}-\d{2}-\d{2} .* 独立记忆$")
            self.assertEqual((await sync(manager, db, first))[doc], {"unchanged": 1})
            payload = data(2, {kind: [item(kind, "改名")]})
            self.assertEqual((await sync(manager, db, payload))[kind], {"update": 1})
            bucket = identifiers(kind, item(kind, "a"))[0]
            renamed = await manager.get(bucket)
            original_stamp = datetime.fromisoformat(renamed["metadata"]["created"]).strftime("%Y-%m-%d %H-%M-%S")
            self.assertEqual(renamed["metadata"]["name"], f"{original_stamp} 改名")
            self.assertIn("改名", renamed["content"])
            await sync(manager, db, data(3, {kind: []}))
            self.assertIsNone(await manager.get(bucket))
            self.assertIsNotNone(await manager.get(other))
            self.assertIsNotNone(await manager.get(identifiers(doc, item(doc, "b"))[0]))
            self.assertEqual(
                (await sync(manager, db, data(4, {kind: [item(kind, "还原")]})))[kind],
                {"restore": 1},
            )
            self.assertEqual((await manager.get(bucket))["metadata"]["name"], f"{original_stamp} 还原")
            with self.assertRaises(ValueError):
                await sync(manager, db, first)

    def test_backup_coverage_and_owner(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source = root / "source"
            source.mkdir()
            db = sqlite3.connect(root / "fixture.sqlite")
            db.executescript(
                "create table collaborator(id,deletedAt); create table memory_entry(id,collaboratorId,content,tagsJSON,deletedAt);"
            )
            db.execute("insert into collaborator values(?,NULL)", (OWNER,))
            for args in [
                (OWNER, "keep", None),
                (OWNER, "deleted", 1),
                ("other", "private", None),
            ]:
                db.execute(
                    "insert into memory_entry values(?,?,?,'[]',?)", (args[1], *args)
                )
            db.commit()
            db.close()
            m = {
                "format": "polaris-native-archive",
                "version": 19,
                "createdAt": 100,
                "databasePath": "database.sqlite",
                "featureFlags": ["sqlite-snapshot"],
                "databaseObjectCounts": {"memory_entries": 3},
            }
            archive = root / "test.polarisarchive"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("manifest.json", json.dumps(m))
                z.write(root / "fixture.sqlite", "database.sqlite")
                z.writestr("secrets/test", "must not be extracted")
            s, skipped = snapshot(archive, source)
            self.assertEqual(len(skipped), 3)
            self.assertEqual([r["id"] for r in s["collections"]["条目记忆"]], ["keep"])
            self.assertEqual(list(source.iterdir()), [source / "database.sqlite"])
