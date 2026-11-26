"""SiliconFlow score adapter and atomic Aru snapshot publication for upstream PaiMem."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

key = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "paimem-key").read_text().strip()
os.environ.update(MEMORY_API_KEY=key, MEMORY_RERANKER_API_KEY=key,
                  EMBEDDING_API_KEY=os.environ["XINCHAO_CHAT_API_KEY"])
from paimem.api import app, require_api_key

OWNER = os.environ["PAIMEM_OWNER"]
STAGING = OWNER + ":backup-stage"


class ScoreRequest(BaseModel):
    queries: list[str]
    documents: list[str]
    model: str


@app.post("/score", dependencies=[Depends(require_api_key)])
async def score(request: ScoreRequest):
    if (not request.documents or len(request.documents) != len(request.queries)
            or len(set(request.queries)) != 1 or request.model != os.environ["MEMORY_RERANKER_MODEL"]):
        raise HTTPException(400, "Invalid score batch")
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        response = await client.post(os.environ["EMBEDDING_BASE_URL"] + "/rerank",
            headers={"Authorization": "Bearer " + os.environ["EMBEDDING_API_KEY"]},
            json={"model": request.model, "query": request.queries[0],
                  "documents": request.documents, "return_documents": False})
    if response.status_code != 200:
        raise HTTPException(502, f"Reranker HTTP {response.status_code}")
    return {"data": [{"index": item["index"], "score": item["relevance_score"]}
                     for item in response.json()["results"]]}


class Snapshot(BaseModel):
    at: int = Field(gt=0)
    groups: dict[str, list[str]]


@app.post("/aru/publish", dependencies=[Depends(require_api_key)])
def publish(snapshot: Snapshot):
    # /add is idempotent under STAGING. Only publish completed collections, in one transaction.
    # The pinned PaiMem version reads memories by user ID on each search. Role and NEAR caches are off.
    allowed = {"structured", "reference", "events", "entries", "transcript"}
    if not snapshot.groups or not snapshot.groups.keys() <= allowed:
        raise HTTPException(400, "Unknown collection")
    with closing(sqlite3.connect(os.environ["MEMORY_DB_PATH"], timeout=45)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS aru_publication (kind TEXT PRIMARY KEY, at INTEGER NOT NULL)")
        db.execute("CREATE TEMP TABLE wanted (id TEXT PRIMARY KEY)")
        added = removed = unchanged = 0
        for kind, ids in snapshot.groups.items():
            old = db.execute("SELECT at FROM aru_publication WHERE kind=?", (kind,)).fetchone()
            if old and old[0] > snapshot.at:
                raise HTTPException(409, "A newer snapshot has already been published")
            prefix = "aru:" + kind + ":"
            if any(not x.startswith(prefix) for x in ids) or len(ids) != len(set(ids)):
                raise HTTPException(400, "Invalid backup IDs")
            db.execute("DELETE FROM wanted")
            db.executemany("INSERT INTO wanted VALUES (?)", ((x,) for x in ids))
            count = db.execute("SELECT count(*) FROM wanted w JOIN add_requests a ON w.id=a.request_id WHERE a.user_id=?", (STAGING,)).fetchone()[0]
            if count != len(ids):
                raise HTTPException(409, "Incomplete backup staging")
            old_ids = {row[0] for row in db.execute(
                "SELECT DISTINCT request_id FROM memories WHERE user_id=? AND request_id GLOB ?", (OWNER,prefix+"*"))}
            wanted = set(ids)
            added += len(wanted - old_ids)
            removed += len(old_ids - wanted)
            unchanged += len(old_ids & wanted)
            db.execute("UPDATE memories SET user_id=? WHERE user_id=? AND request_id IN (SELECT id FROM wanted)", (OWNER,STAGING))
            db.executemany("DELETE FROM memories WHERE user_id=? AND request_id=?", ((OWNER,i) for i in old_ids-wanted))
            db.executemany("DELETE FROM add_requests WHERE user_id=? AND request_id=?", ((STAGING,i) for i in old_ids-wanted))
            db.execute("INSERT OR REPLACE INTO aru_publication VALUES (?,?)", (kind,snapshot.at))
    return {"create":added,"archive":removed,"unchanged":unchanged,"restore":0,"update":0}
