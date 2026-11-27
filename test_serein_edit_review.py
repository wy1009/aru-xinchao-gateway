"""Regression for revisiting a skipped message in a previously settled Router frame."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("serein"), "requires the Serein worker environment")
class EditReviewTest(unittest.TestCase):
    def test_skipped_historical_user_message_reaches_curator_again(self):
        from aru_serein_store import SCHEMA, SOURCE, put_original
        from serein.bootstrap import initialize
        from serein.compat.raw_archive import raw_archive
        from serein.config import Settings
        from serein.core.store import Store
        from serein.deployment import save_settings
        from serein.extensions import pipeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Settings(root / "memory.db", root / "index.db", writable=True)
            initialize(cfg)
            save_settings(cfg.database, {"pipeline": {"execution_mode": "agent", "round_gate_enabled": False}})
            path = Path(__file__).parent / "scripts" / "serein-agent-queue.py"
            spec = importlib.util.spec_from_file_location("edit_review_queue", path)
            queue = importlib.util.module_from_spec(spec)
            with patch("serein.config.load_settings", return_value=cfg):
                spec.loader.exec_module(queue)

            archive = raw_archive(cfg)
            with Store(cfg.database) as store:
                store.conn.executescript(SCHEMA)
                with store.transaction(immediate=True):
                    for index, (role, text) in enumerate((
                        ("user", "在吗"), ("assistant", "在"), ("user", "周五"), ("assistant", "收到")
                    ), 1):
                        put_original(store, archive, SOURCE, "session", str(index), role, text,
                                     index * 1000, index, 5000)
            seen = []

            async def skip_fixture(role, request):
                seen.append(request)
                if role == "track_router":
                    return {"message_assignments": [
                        {"source_message_id": m["id"], "primary_track_ref": "new:1",
                         "context_track_refs": [], "routing_role": "primary_activity"} for m in request["messages"]
                    ], "track_updates": [{"track_ref": "new:1", "subject": "问候",
                                         "throughline": "应答", "status": "active"}]}
                self.assertEqual(role, "event_curator")
                ids = [m["id"] for m in request["component"]["messages"]]
                return {"events": [], "skip_unit_roots": ids, "defer_unit_roots": [],
                        "decision_review": {"events": [], "boundaries": [], "dispositions": [
                            {"disposition": "skip", "unit_roots": ids, "reason": "寒暄", "parked_source_message_ids": []}
                        ]}}

            with queue.edit_review_pipeline():
                first = asyncio.run(pipeline.advance(cfg.database, include_recent=True, runner=skip_fixture))
                self.assertEqual(first["skipped"], 4)
                self.assertTrue(all("<aru_edit_review>" not in r["prompt"] for r in seen))
                seen.clear()
                with Store(cfg.database) as store, store.transaction(immediate=True):
                    put_original(store, archive, SOURCE, "session", "3", "user", "周六才用这个称呼",
                                 3000, 3, 6000)
                    target = store.conn.execute("SELECT id FROM raw_events WHERE source_event_id='3'").fetchone()[0]
                second = asyncio.run(pipeline.advance(cfg.database, include_recent=True, runner=skip_fixture))
                self.assertEqual(second["processed_originals"], 1)
                self.assertEqual([r["role"] for r in seen], ["track_router", "event_curator"])
                focus = seen[-1]["prompt"].split("<aru_edit_review>\n")[1].split("\n</aru_edit_review>")[0]
                self.assertEqual(json.loads(focus)["revised_source_message_ids"], [target])
                self.assertIn("周六才用这个称呼", seen[-1]["prompt"])
                self.assertNotIn("周五", seen[-1]["prompt"])
                self.assertIn("收到", focus)
