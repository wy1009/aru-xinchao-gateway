"""Run incremental OB metadata enrichment independently of archive retention."""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def queue(directory):
    (directory / "enrich.request").touch(mode=0o600, exist_ok=True)


def receipt(directory, state, counts):
    result = {"state": state, **counts, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = directory / "enrich.tmp"
    tmp.write_text(json.dumps(result))
    os.replace(tmp, directory / "enrich.json")


def process(directory, credential):
    requested, running = directory / "enrich.request", directory / "enrich.running"
    if requested.exists():
        requested.replace(running)
    elif not running.exists():
        return
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def invoke(mode):
        req = urllib.request.Request("http://127.0.0.1:18001/internal/aru-backup/enrich",
                                     data=json.dumps({"mode": mode}).encode(),
                                     headers={"Authorization": "Bearer " + credential.read_text().strip(),
                                              "Content-Type": "application/json"})
        with client.open(req, timeout=540) as response:
            return json.load(response)
    def retry(mode, attempts):
        for attempt in range(attempts):
            try:
                return invoke(mode)
            except (OSError, ValueError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(2 ** (attempt + 1))

    counts = {}
    try:
        counts = retry("status", 5)
        receipt(directory, "running", counts)
        while counts["done"] < counts["total"]:
            # Retry a transient model failure. Already committed groups are skipped server-side.
            counts = retry("batch", 3)
            receipt(directory, "running", counts)
        receipt(directory, "done", counts)
    except Exception as exc:
        print("Aru AI worker:", type(exc).__name__, flush=True)
        receipt(directory, "failed", counts)
    running.unlink(missing_ok=True)


if __name__ == "__main__":
    process(Path(os.environ["STATE_DIRECTORY"]), Path(os.environ["CREDENTIALS_DIRECTORY"]) / "gateway-internal")
