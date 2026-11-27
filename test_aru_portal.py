import hashlib
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from aru_portal_worker import process


class PortalTest(unittest.TestCase):
    def test_compact_log_separates_injection_and_limits_closed_messages(self):
        from aru_prompt_log import compact_log
        from serein_gateway import inject
        from prompt_audit import request_messages

        messages = [{"role": "system", "content": "private setup"}]
        messages.extend({"role": "user", "content": f"old-{i}!"} for i in range(13))
        messages.append({"role": "user", "content": [{"type": "text", "text": "question"}]})
        index = len(messages) - 1
        entries = request_messages(inject(messages, "note <tag>", index), index, None)
        entries.append([None, "assistant", "本轮模型回复 · stop", "answer"])
        rows = [(str(i), 10 + i, "200") for i in range(10)]
        with patch("aru_prompt_log.page_records", side_effect=[rows, (10, "200", entries)]) as records:
            response = compact_log(None)
            self.assertEqual(records.call_args_list[0].kwargs["limit"], 10)
        html = response.body.decode()
        self.assertEqual(html.count("<details>"), 10)
        self.assertNotIn("<details open", html)
        self.assertNotIn("private setup", html)
        self.assertNotIn("old-4!", html)
        self.assertIn("old-5!", html)
        self.assertEqual(html.count("note &lt;tag&gt;"), 1)
        self.assertIn("question", html)
        self.assertIn("answer", html)
        with patch("aru_prompt_log.page_records", return_value=rows):
            self.assertEqual(compact_log("too-old").status_code, 404)

    def test_ob_pause_blocks_writes_and_preserves_shared_routes(self):
        with tempfile.TemporaryDirectory() as work, patch.dict(os.environ, {
            "INBOX_PATH": "/aru-backup", "STATE_DIRECTORY": work, "ARU_OB_SYNC_ENABLED": "0",
            "INBOX_DIGEST": hashlib.sha256(b"test-password").hexdigest(),
        }):
            import aru_inbox
            import aru_portal
            importlib.reload(aru_inbox)
            importlib.reload(aru_portal)
            auth = ("aru", "test-password")
            path = "/aru-backup"
            headers = {"Content-Type": "application/octet-stream", "X-Aru-Upload": "1"}
            with TestClient(aru_portal.app) as client:
                for suffix in ("", "/", "/status"):
                    self.assertEqual(client.get(path + suffix, auth=auth).status_code, 503)
                for suffix in ("/upload", "/enrich"):
                    self.assertEqual(client.post(path + suffix, headers=headers).status_code, 401)
                    self.assertEqual(client.post(path + suffix, auth=auth, headers=headers,
                                                 content=b"backup").status_code, 503)
                self.assertEqual(client.get(path + "/xinchao", auth=auth).status_code, 200)
            self.assertEqual(list(Path(work).iterdir()), [])

    def test_repeated_uploads_and_restart(self):
        with tempfile.TemporaryDirectory() as work:
            folder = Path(work)
            env = {
                "INBOX_PATH": "/aru-backup",
                "STATE_DIRECTORY": work,
                "INBOX_DIGEST": hashlib.sha256(b"test-password").hexdigest(),
            }
            with patch.dict(os.environ, env):
                import aru_inbox
                import aru_portal
                importlib.reload(aru_inbox)
                importlib.reload(aru_portal)
            incomplete = folder / "upload.part"
            incomplete.write_bytes(b"interrupted")
            path = "/aru-backup"
            final = folder / "archive.polarisarchive"
            headers = {"Content-Type": "application/octet-stream", "X-Aru-Upload": "1"}
            auth = ("aru", "test-password")
            with TestClient(aru_portal.app) as client:
                self.assertFalse(incomplete.exists())
                self.assertEqual(client.post(path + "/enrich", headers=headers).status_code, 401)
                self.assertEqual(client.post(path + "/enrich", auth=auth).status_code, 415)
                self.assertFalse((folder / "enrich.request").exists())
                self.assertEqual(client.post(path + "/enrich", auth=auth, headers=headers).status_code, 202)
                self.assertEqual(client.get(path + "/status", auth=auth).json()["ai"]["state"], "queued")
                (folder / "enrich.request").unlink()
                for suffix in ("", "/", "/status"):
                    self.assertEqual(client.get(path + suffix).status_code, 401)
                self.assertEqual(client.post(path + "/upload", headers=headers, content=b"x").status_code, 401)
                self.assertEqual(client.get(path, auth=("aru", "wrong")).status_code, 401)
                self.assertEqual(client.get(path + "/", auth=auth).status_code, 200)
                self.assertEqual(client.post(path + "/upload", auth=auth, content=b"x").status_code, 415)
                self.assertFalse(final.exists())
                def upload(body):
                    return client.post(path + "/upload", auth=auth, headers=headers, content=body)
                self.assertEqual(upload(b"").status_code, 400)
                self.assertFalse(incomplete.exists())
                self.assertEqual(upload(b"first").status_code, 201)
                self.assertEqual(upload(b"second").status_code, 409)
            with TestClient(aru_portal.app) as client:
                self.assertEqual(final.read_bytes(), b"first")
                self.assertEqual(client.get(path + "/status", auth=auth).json()["state"], "syncing")
                scratch = folder / "aru-read-test"
                scratch.mkdir()
                with patch("aru_portal_worker.synchronize", return_value={
                    "applied": True, "changes": {"信息卡": {"create": 1, "unchanged": 2}}, "skipped": {"长期资料": ""}
                }):
                    process(folder, folder / "credential")
                receipt = client.get(path + "/status", auth=auth).json()
                self.assertEqual(receipt["state"], "ready")
                self.assertTrue(receipt["last"]["ok"])
                self.assertEqual(receipt["last"]["changes"]["create"], 1)
                self.assertEqual(receipt["last"]["skipped"], ["长期资料"])
                self.assertEqual(upload(b"invalid file").status_code, 201)
                process(folder, folder / "credential")
                self.assertFalse(client.get(path + "/status", auth=auth).json()["last"]["ok"])
                self.assertEqual({p.name for p in folder.iterdir()}, {"receipt.json", "enrich.request"})
