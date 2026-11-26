import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient


class UploadTests(unittest.TestCase):
    def test_authenticated_resume_and_duplicate_finish(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("ARU_UPLOAD_TEST_TMP")) as state:
            env={"STATE_DIRECTORY":state, "INBOX_PATH":"/aru-backup","INBOX_DIGEST":hashlib.sha256(b"fixture").hexdigest(),
                 "ARU_OB_SYNC_ENABLED":"0"}
            with patch.dict(os.environ,env):
                import aru_portal, aru_archive_upload
                with patch.object(aru_archive_upload,"ROOT",Path(state)/"uploads"), TestClient(aru_portal.app) as c:
                    path="/aru-backup/archive/"
                    self.assertEqual(c.get(path).status_code,401)
                    c.auth=("aru","fixture")
                    self.assertEqual(c.get(path).status_code,200)
                    raw=io.BytesIO()
                    with zipfile.ZipFile(raw,"w") as z:
                        z.writestr("manifest.json",json.dumps({"databasePath":"database.sqlite"}))
                        z.writestr("database.sqlite",b"z"*(aru_archive_upload.CHUNK+125))
                    content=raw.getvalue()
                    chunks=[content[i:i+aru_archive_upload.CHUNK] for i in range(0,len(content),aru_archive_upload.CHUNK)]
                    manifest={"size":len(content),"hashes":[hashlib.sha256(b).hexdigest() for b in chunks]}
                    h={"X-Aru-Upload":"1"}
                    def start():
                        return c.post(path+"start",json=manifest,headers=h).json()
                    first=start()
                    def write(n, block):
                        return c.post(path+"chunk/"+first["id"]+"/"+str(n),content=block,headers=h)
                    self.assertEqual(write(0,b"y"*len(chunks[0])).status_code,422)
                    self.assertEqual(write(0,chunks[0]).json()["offset"],len(chunks[0]))
                    self.assertEqual(start()["offset"],len(chunks[0]))
                    self.assertEqual(write(1,chunks[1]).json()["state"],"received")
                    self.assertEqual(start()["state"],"received")
                    self.assertEqual(write(1,chunks[1]).json()["state"],"received")
                    saved=next((Path(state)/"uploads").glob("*/archive.polarisarchive"))
                    self.assertEqual(saved.read_bytes(),content)
                    saved.rename(saved.with_name("upload.part"))
                    self.assertEqual(start()["offset"],len(chunks[0]))
                    self.assertEqual(write(1,chunks[1]).json()["state"],"received")
