"""Consume one atomically published upload and persist only its summary."""
import json
import os
import shutil
import sqlite3
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from aru_backup import TABLES, synchronize
from aru_enrich_job import queue


def process(directory, credential):
    saved = directory / "archive.polarisarchive"
    if not saved.is_file():
        return
    # A machine or worker restart can leave an unfinished SQLite extraction.
    for work in directory.glob("aru-read-*"):
        if work.is_dir():
            shutil.rmtree(work)
    try:
        result = synchronize(saved, True, credential)
        if result.get("applied") is not True:
            raise RuntimeError("OB did not confirm the import")
        keys = ("create", "update", "archive", "restore", "unchanged")
        changes = {k: sum(int(group.get(k, 0)) for group in result["changes"].values()) for k in keys}
        receipt = {"ok": True, "changes": changes, "skipped": [k for k in TABLES if k in result["skipped"]]}
    except urllib.error.HTTPError as exc:
        message = "OB 已拒绝这份快照，请使用最新导出的完整备份。" if exc.code == 409 else "OB 暂时无法完成同步，请稍后重新上传。"
        receipt = {"ok": False, "message": message}
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile, sqlite3.DatabaseError):
        receipt = {"ok": False, "message": "请检查文件是否为包含协作者记忆的完整 Aru 备份，再重新上传。"}
    except Exception as exc:
        print("Sync failed:", type(exc).__name__, flush=True)
        receipt = {"ok": False, "message": "同步未完成，请稍后重新上传。"}
    receipt["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = directory / "receipt.tmp"
    with tmp.open("w") as stream:
        json.dump(receipt, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, directory / "receipt.json")
    if receipt["ok"]:
        queue(directory)
    saved.unlink()


if __name__ == "__main__":
    process(Path(os.environ["STATE_DIRECTORY"]), Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gateway-internal")
