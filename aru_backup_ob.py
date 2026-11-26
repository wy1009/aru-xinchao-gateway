"""Local, authenticated writer for source-owned Aru backup records."""

import asyncio
import hmac
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from starlette.responses import JSONResponse

from aru_backup import OWNER, LABEL, TABLES, digest
import aru_enrichment as ai
import aru_source_time as source_time
from aru_wikilinks import render as render_wikilinks

SOURCE = "aru-backup"


def open_state(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS scopes(kind TEXT PRIMARY KEY,stamp INTEGER NOT NULL,snapshot TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS links(kind TEXT NOT NULL,source_id TEXT NOT NULL,part INTEGER NOT NULL,
      bucket TEXT NOT NULL UNIQUE, content_hash TEXT, active INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(kind,source_id,part));
    """)
    db.execute(ai.SCHEMA)
    db.execute(source_time.SCHEMA)
    return db


def identifiers(kind, item):
    key = digest([OWNER, kind, item["id"], item["part"]])
    return "aru" + key[:24], "aru:" + key[:56]


def validate(payload):
    if (
        not OWNER
        or payload.get("owner") != OWNER
        or type(payload.get("at")) is not int
        or payload["at"] <= 0
        or type(payload.get("apply")) is not bool
    ):
        raise ValueError("Invalid owner, timestamp or operation")
    groups = payload.get("collections")
    if not isinstance(groups, dict) or not groups or not groups.keys() <= TABLES.keys():
        raise ValueError("Invalid collections")
    times = payload.get("source_times", {})
    if not isinstance(times, dict) or any(not isinstance(key, str) or not source_time.valid(value)
                                          for key, value in times.items()):
        raise ValueError("Invalid source message times")
    for kind, rows in groups.items():
        if not isinstance(rows, list):
            raise ValueError("Invalid collection")
        seen = set()
        for r in rows:
            if (
                not isinstance(r.get("id"), str)
                or not r["id"]
                or type(r.get("part")) is not int
                or r["part"] < 0
            ):
                raise ValueError("Invalid source key")
            if not all(
                isinstance(r.get(k), str) and r[k].strip() for k in ("name", "content")
            ):
                raise ValueError("Missing source text")
            if (
                not isinstance(r.get("tags"), list)
                or not all(isinstance(t, str) for t in r["tags"])
                or not {"Aru", LABEL, kind} <= set(r["tags"])
            ):
                raise ValueError("Invalid source tags")
            key = (r["id"], r["part"])
            if key in seen:
                raise ValueError("Duplicate source identity")
            seen.add(key)


async def sync(manager, db, payload):
    validate(payload)
    apply = payload["apply"]
    stamp = payload["at"]
    groups = payload["collections"]
    signatures = {kind: digest(rows) for kind, rows in groups.items()}
    # Set a watermark before external writes. Interrupted imports can converge by retry.
    with db:
        for kind, signature in signatures.items():
            old = db.execute(
                "SELECT stamp,snapshot FROM scopes WHERE kind=?", (kind,)
            ).fetchone()
            if old and (
                stamp < old["stamp"]
                or stamp == old["stamp"]
                and signature != old["snapshot"]
            ):
                raise ValueError("Older or conflicting backup: " + kind)
            if apply:
                db.execute(
                    "INSERT OR REPLACE INTO scopes VALUES (?,?,?)",
                    (kind, stamp, signature),
                )
    existing = {r["id"]: r for r in await manager.list_all(include_archive=True)}
    result = {}
    for kind, items in groups.items():
        counts = Counter()
        previous = {
            (r["source_id"], r["part"]): r
            for r in db.execute("SELECT * FROM links WHERE kind=?", (kind,))
        }
        for item in items:
            sid, part = item["id"], item["part"]
            saved = previous.pop((sid, part), None)
            bucket, marker = identifiers(kind, item)
            current = existing.get(bucket)
            meta = current["metadata"] if current else {}
            if (current and meta.get("grow_batch_id") != marker) or (
                saved and saved["bucket"] != bucket
            ):
                raise ValueError("Source identity collision")
            hashed = digest(item)
            annotation = db.execute("SELECT * FROM annotations WHERE bucket=?", (bucket,)).fetchone()
            derived = {}
            expected_content = item["content"]
            if annotation and annotation["source_hash"] == hashed:
                values = json.loads(annotation["metadata"])
                derived = ai.fields_for(item["tags"], values)
                expected_content = render_wikilinks(expected_content, values.get("wikilinks", []))
            elif annotation:
                # Drop obsolete analysis while the replacement is pending.
                derived = {"importance": 5, "valence": 0.5, "arousal": 0.3}
            expected_created = meta.get("created")
            if kind == source_time.KIND:
                bounds = payload.get("source_times", {}).get(sid)
                if bounds is not None and apply:
                    with db:
                        db.execute("INSERT OR REPLACE INTO source_time VALUES(?,?,?,?)", (bucket, hashed, *bounds))
                if bounds is None:
                    bounds = source_time.stored(db, bucket, hashed)
                expected_content += source_time.footer(bounds)
                derived["source_created_at"] = source_time.created_at(bounds[0]) if bounds else None
                expected_created = derived["source_created_at"] or meta.get("imported_at", expected_created)
            was_deleted = bool(meta.get("deleted_at"))
            active = current and not was_deleted and meta.get("type") != "archived"
            same = bool(
                active
                and saved
                and saved["content_hash"] == hashed
                and saved["active"]
                and meta.get("created") == expected_created
                and current["content"].strip()
                == manager._sanitize_text(expected_content).strip()
                and set(meta.get("tags") or []) == set(derived.get("tags", item["tags"]))
            )
            action = (
                "unchanged"
                if same
                else ("restore" if was_deleted else "update" if current else "create")
            )
            counts[action] += 1
            if not apply or same:
                continue
            # Leave room for OB's timestamp, stable ID and atomic-write suffix in the filename.
            source_name = item["name"].encode()[:144].decode(errors="ignore")
            fields = dict(
                content=expected_content,
                tags=item["tags"],
                domain=["Aru·" + LABEL],
            )
            fields.update(derived)
            # Reserve before create so a retry can reconcile even after process termination.
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO links(kind,source_id,part,bucket) VALUES(?,?,?,?)",
                    (kind, sid, part, bucket),
                )
            if not current:
                actual = await manager.create(
                    **fields,
                    name=source_name,
                    source_tool=SOURCE,
                    grow_batch_id=marker,
                    bucket_id_override=bucket,
                    bucket_type="permanent",
                )
                if actual != bucket:
                    raise RuntimeError("OB bucket identity changed during create")
            else:
                if was_deleted and not await manager.restore(bucket):
                    raise RuntimeError("OB restore failed")
                if not await manager.update(bucket, source_name=source_name, **fields):
                    raise RuntimeError("OB update failed")
            with db:
                db.execute(
                    "UPDATE links SET content_hash=?,active=1 WHERE bucket=?",
                    (hashed, bucket),
                )
            await asyncio.sleep(0)
        for (sid, part), saved in previous.items():
            bucket, marker = identifiers(kind, {"id": sid, "part": part})
            current = existing.get(bucket)
            if saved["bucket"] != bucket or (
                current and current["metadata"].get("grow_batch_id") != marker
            ):
                raise ValueError("Archive target does not belong to the source")
            if current and not current["metadata"].get("deleted_at"):
                counts["archive"] += 1
                if apply and not await manager.delete(
                    bucket, reason="Removed in Aru backup"
                ):
                    raise RuntimeError("OB archive failed")
            if apply and saved["active"]:
                with db:
                    db.execute("UPDATE links SET active=0 WHERE bucket=?", (bucket,))
        result[kind] = dict(counts)
    return result


def eligible_dream_bucket(bucket):
    if not bucket:
        return False
    meta = bucket["metadata"]
    if meta.get("dont_surface") or meta.get("deleted_at") or meta.get("type") == "archived":
        return False
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = tags.replace("，", ",").split(",")
    if meta.get("source") == "xinchao-dream" or any(
        str(tag).strip().casefold() in {"dream", "梦境"} for tag in tags
    ):
        return False
    # Older hold entries can lack origin metadata. Match the dream writer's format.
    content = (bucket.get("content") or "").lstrip()
    return not (content.startswith("梦境：") and "\n梦境余韵：" in content)


async def recent_dream_candidates(manager, db):
    from utils import strip_wikilinks

    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_time'").fetchone():
        return []
    rows = db.execute("""
        SELECT l.bucket, l.source_id, t.start_ms, t.end_ms
        FROM links l JOIN source_time t ON t.bucket=l.bucket AND t.source_hash=l.content_hash
        WHERE l.active=1 AND l.kind=? AND l.part=0
        ORDER BY t.end_ms DESC, t.start_ms DESC, l.bucket
    """, (source_time.KIND,))
    candidates = []
    cutoff = None
    for bid, sid, start, end in rows:
        expected, marker = identifiers(source_time.KIND, {"id": sid, "part": 0})
        if bid != expected or not source_time.valid([start, end]):
            continue
        if cutoff is not None and end < cutoff:
            break
        bucket = await manager.get(bid)
        meta = bucket["metadata"] if bucket else {}
        if (meta.get("grow_batch_id") != marker or not eligible_dream_bucket(bucket)
                or {"技术", "数字", "编程", "事务"}.intersection(meta.get("domain") or [])):
            continue
        footer = source_time.footer((start, end))
        text = strip_wikilinks(bucket["content"].strip().removesuffix(footer)).strip()
        if text:
            if cutoff is None:
                cutoff = end - 48 * 60 * 60 * 1000
            candidates.append({"id": bid, "endMs": end, "period": footer.strip().removeprefix("> "), "text": text[:900]})
    return candidates


def dream_material_page(candidates, after):
    count = len(candidates)
    if not count:
        return {"memories": [], "candidateCount": 0, "windowEndMs": None}
    start = next((i + 1 for i, item in enumerate(candidates) if item["id"] == after), 0)
    picked = [candidates[(start + i) % count] for i in range(min(2, count))]
    return {"memories": picked, "candidateCount": count, "windowEndMs": candidates[0]["endMs"]}


def register(mcp, shared):
    from auth_policy import build_mcp_token_validator

    sidecar_only = build_mcp_token_validator(lambda *_: False)
    lock = asyncio.Lock()

    analysis_lock = asyncio.Lock()

    def authenticated(req):
        expected = os.environ.get("NIAN_INTERNAL_TOKEN")
        return bool(expected) and hmac.compare_digest(
            req.headers.get("authorization", "").encode(),
            ("Bearer " + expected).encode(),
        )

    def connect():
        return open_state(Path(os.environ["OMBRE_CONFIG_PATH"]).parent / "aru-backup-sync.sqlite")

    @mcp.custom_route("/internal/aru-backup/dream-material", methods=["GET"])
    async def dream_material(req):
        auth = req.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not sidecar_only(auth[7:]):
            return JSONResponse({}, status_code=401)
        path = Path(os.environ["OMBRE_CONFIG_PATH"]).resolve().parent / "aru-backup-sync.sqlite"
        async with lock:
            candidates = []
            if path.is_file():
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    candidates = await recent_dream_candidates(shared.bucket_mgr, db)
        return JSONResponse(dream_material_page(candidates, req.query_params.get("after")),
                            headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/internal/aru-backup/enrich", methods=["POST"])
    async def enrich(req):
        if not authenticated(req):
            return JSONResponse({}, status_code=401)
        with closing(connect()) as db:
            if (await req.json()).get("mode") == "status":
                return JSONResponse(ai.progress(db))
            try:
                async with analysis_lock:
                    return JSONResponse(await ai.enrich_batch(shared, db, lock))
            except Exception as exc:
                # Report only the exception class. Model output may contain personal memories.
                print("Aru enrichment:", type(exc).__name__, flush=True)
                return JSONResponse({"error": "analysis unavailable"}, status_code=503)

    @mcp.custom_route("/internal/aru-backup/sync", methods=["POST"])
    async def endpoint(req):
        if not authenticated(req):
            return JSONResponse({}, status_code=401)
        try:
            body = await req.json()
            async with lock:
                db = connect()
                try:
                    result = await sync(shared.bucket_mgr, db, body)
                finally:
                    db.close()
            return JSONResponse({"applied": body["apply"], "changes": result})
        except (KeyError, ValueError, TypeError):
            return JSONResponse(
                {"error": "Invalid, older, or conflicting source snapshot"},
                status_code=409,
            )
