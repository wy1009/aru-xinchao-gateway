"""Release a one-time hold after the new Aru transcript snapshot has committed."""
import json
import os
import re
import sqlite3
import subprocess
import tomllib
from pathlib import Path

STATE = Path("/var/lib/gushou-serein")
HOLD = STATE / ".awaiting-transcript-upload"
CONFIG = Path("/etc/gushou-serein/config.toml")


def resume():
    if not HOLD.exists():
        return
    baseline = json.loads(HOLD.read_text())
    config = tomllib.loads(CONFIG.read_text())
    database = Path(config["storage"]["database"])
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT owner,stamp FROM aru_snapshot").fetchone()
        if not row or row[0] != baseline["owner"] or row[1] <= baseline["stamp"]:
            return
    # The standalone model worker can run even when the extension is disabled.
    # Restore both native execution mechanisms only after committing the new snapshot.
    subprocess.run(["runuser", "-u", "gushou-serein", "--", "/opt/gushou-serein/venv/bin/python",
                    "-c", "from serein.deployment import save_settings; from pathlib import Path; "
                    "import sys,json; resume=json.loads(Path(sys.argv[2]).read_text()); "
                    "save_settings(Path(sys.argv[1]),{'assignments':resume['assignments'],"
                    "'pipeline':{'execution_mode':resume['mode']}})", str(database), str(HOLD)], check=True)
    if not config["extensions"]["event_pipeline"]["enabled"]:
        text, matched = re.subn(r"(\[extensions\.event_pipeline\]\s*\nenabled\s*=\s*)false",
                                r"\g<1>true", CONFIG.read_text())
        if matched != 1:
            raise ValueError("Unexpected event pipeline configuration")
        staged = CONFIG.with_suffix(".resume.tmp")
        staged.write_text(text)
        staged.chmod(CONFIG.stat().st_mode & 0o777)
        os.replace(staged, CONFIG)
    subprocess.run(["systemctl", "restart", "gushou-serein.service"], check=True)
    # Use the native worker for the backlog, including uploads received before 03:00.
    subprocess.run(["runuser", "-u", "gushou-serein", "--", "/opt/gushou-serein/venv/bin/python",
                    "-c", "from serein.work_tasks import enqueue; from pathlib import Path; "
                    "import sys; enqueue(Path(sys.argv[1]),'pipeline',{'include_recent':True},"
                    "followup_if_running=True)", str(database)], check=True)
    HOLD.unlink()
    print("Serein resumed after applying the new transcript snapshot", flush=True)


if __name__ == "__main__":
    resume()
