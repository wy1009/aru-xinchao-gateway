"""Import the four verified Polaris v19 collections. Default operation is a preview."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import aru_source_time as source_time

OWNER = os.environ.get("ARU_COLLABORATOR_ID", "").strip()
LABEL = os.environ.get("ARU_COLLABORATOR_NAME", "小禾")
TABLES = {
    "结构化记忆": ("structured_memory_entry", "structured_memory_entries"),
    "长期资料": ("memory_reference_doc", "memory_reference_docs"),
    "信息卡": ("memory_event", "memory_events"),
    "条目记忆": ("memory_entry", "memory_entries"),
}
SUBTYPES = {"relational_profile": "关系画像", "recent_topic": "近期话题"}
SECTIONS = {
    "observedFactsJSON": "观察到的事实",
    "userInterpretationsJSON": "用户的解读",
    "assistantInferencesJSON": "AI 的推断",
    "unresolvedQuestionsJSON": "待解问题",
    "entitiesJSON": "涉及对象",
    "placesJSON": "地点",
    "timeHintsJSON": "时间线索",
}
COLUMNS = {
    "结构化记忆": "id,title,content,kind,state,expiresAt,deletedAt",
    "长期资料": "id,title,content,summary,charCount,contentLoaded,deletedAt",
    "信息卡": "id,title,synopsis,aggregationStatus,metadataJSON," + ",".join(SECTIONS),
    "条目记忆": "id,content,tagsJSON,deletedAt",
}
ENDPOINT = "http://127.0.0.1:18001/internal/aru-backup/sync"


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def array(raw):
    values = json.loads(raw or "[]")
    if not isinstance(values, list):
        raise ValueError("Unexpected Aru list field")
    return values


def parts(text):
    """Keep short documents intact; split large ones on paragraph/line boundaries."""
    while len(text.encode()) > 30000:
        head = text.encode()[:30000].decode(errors="ignore")
        cut = head.rfind("\n\n", len(head) // 2)
        if cut < 0:
            cut = head.rfind("\n", len(head) // 2)
        cut = cut + 1 if cut >= 0 else len(head)
        yield text[:cut]
        text = text[cut:]
    if text:
        yield text


def render(kind, row, at):
    tags = ["Aru", LABEL, kind]
    if kind == "信息卡":
        if row["aggregationStatus"] != "ready":
            raise ValueError("信息卡尚未整理完成")
        title = row["title"]
        paragraphs = [row["synopsis"] or ""]
        for field, heading in SECTIONS.items():
            values = array(row[field])
            if values:
                lines = [
                    v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for v in values
                ]
                paragraphs.append(
                    "### " + heading + "\n" + "\n".join("- " + v for v in lines)
                )
        meta = json.loads(row["metadataJSON"] or "{}")
        tags.extend(
            v for v in (meta.get("recallTriggerNames") or []) if isinstance(v, str)
        )
        text = "\n\n".join(p for p in paragraphs if p)
    else:
        if row["deletedAt"] is not None:
            return []
        text = row["content"] or ""
        if kind == "条目记忆":
            if not text.strip():
                raise ValueError("条目记忆缺少正文")
            title = text.strip().splitlines()[0].lstrip("# ").strip()[:70] or "条目记忆"
            tags.extend(v for v in array(row["tagsJSON"]) if isinstance(v, str))
        else:
            title = row["title"]
        if kind == "结构化记忆":
            if row["state"] == "deleted" or (
                row["expiresAt"] is not None and row["expiresAt"] <= at
            ):
                return []
            if row["state"] != "ready":
                raise ValueError("结构化记忆状态未就绪")
            tags.append(SUBTYPES.get(row["kind"], row["kind"]))
        elif kind == "长期资料":
            if not row["contentLoaded"] or (row["charCount"] and not text.strip()):
                raise ValueError("长期资料正文未完整导出")
            if row["summary"] and row["summary"].strip() != text.strip():
                text = "摘要\n" + row["summary"] + "\n\n" + text
    if not isinstance(title, str) or not title.strip() or not row["id"]:
        raise ValueError("Missing Aru title or source ID")
    tags = list(dict.fromkeys(t.strip()[:40] for t in tags if t and t.strip()))[:64]
    blocks = list(parts(text)) or [""]
    return [
        {
            "id": row["id"],
            "part": i,
            "name": (
                title if len(blocks) == 1 else f"{title}（{i + 1}/{len(blocks)}）"
            ),
            "tags": tags,
            "content": "# " + title + "\n\n" + block,
        }
        for i, block in enumerate(blocks)
    ]


def inspect_table(db, table):
    schema = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
    ).fetchone()
    if (
        not schema
        or schema[0] != "table"
        or not schema[1].upper().lstrip().startswith("CREATE TABLE")
    ):
        raise ValueError("Missing ordinary table: " + table)


def snapshot(archive, work):
    if not OWNER:
        raise ValueError("Configure ARU_COLLABORATOR_ID before importing a backup")
    with zipfile.ZipFile(archive) as z:
        for name in ("manifest.json", "database.sqlite"):
            if z.namelist().count(name) != 1:
                raise ValueError("Missing or duplicate " + name)
        if z.getinfo("manifest.json").file_size > 2**20:
            raise ValueError("Unexpectedly large manifest")
        manifest = json.loads(z.read("manifest.json"))
        if (
            manifest.get("format"),
            manifest.get("version"),
            manifest.get("databasePath"),
        ) != (
            "polaris-native-archive",
            19,
            "database.sqlite",
        ) or "sqlite-snapshot" not in manifest.get("featureFlags", []):
            raise ValueError("Unsupported Polaris archive schema")
        at = manifest["createdAt"]
        if type(at) is not int or at <= 0:
            raise ValueError("Invalid backup timestamp")
        info = z.getinfo("database.sqlite")
        if info.file_size > shutil.disk_usage(work).free - 64 * 1024**2:
            raise ValueError("Insufficient temporary disk space")
        path = work / "database.sqlite"
        with z.open(info) as src, path.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024**2)
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        inspect_table(db, "collaborator")
        if not db.execute(
            "SELECT 1 FROM collaborator WHERE id=? AND deletedAt IS NULL", (OWNER,)
        ).fetchone():
            raise ValueError("This backup does not contain the configured collaborator")
        collections = {}
        skipped = {}
        for kind, (table, count_key) in TABLES.items():
            count = manifest["databaseObjectCounts"].get(count_key)
            if count is None:
                skipped[kind] = "范围未声明"
                continue
            inspect_table(db, table)
            if db.execute("SELECT count(*) FROM " + table).fetchone()[0] != count:
                raise ValueError("Inventory mismatch: " + kind)
            if any(
                w.get("objectType") in (table, count_key)
                for w in manifest.get("warnings", [])
            ):
                skipped[kind] = "该类别有导出警告"
                continue
            items = []
            for row in db.execute(
                "SELECT "
                + COLUMNS[kind]
                + " FROM "
                + table
                + " WHERE collaboratorId=? ORDER BY id",
                (OWNER,),
            ):
                items.extend(render(kind, row, at))
            collections[kind] = items
        if not collections:
            raise ValueError("No complete supported collection found")
        result = {"owner": OWNER, "at": at, "collections": collections}
        if source_time.KIND in collections:
            eligible = {item["id"] for item in collections[source_time.KIND]}
            result["source_times"] = {sid: bounds for sid, bounds in source_time.read(db, OWNER).items()
                                      if sid in eligible}
        return result, skipped
    finally:
        db.close()


def synchronize(archive, apply, credential):
    with tempfile.TemporaryDirectory(prefix="aru-read-", dir=archive.resolve().parent) as work:
        data, skipped = snapshot(archive, Path(work))
    data["apply"] = apply
    req = urllib.request.Request(
        ENDPOINT,
        data=encoded(data),
        headers={
            "Authorization": "Bearer " + credential.read_text().strip(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=900) as resp:
        return {"owner": LABEL, "skipped": skipped, **json.load(resp)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the verified snapshot to the local OB instance",
    )
    p.add_argument(
        "--consume",
        action="store_true",
        help="Remove this private staging copy after processing",
    )
    args = p.parse_args()
    os.umask(0o077)
    try:
        result = synchronize(args.archive, args.apply, Path("/etc/xinchao-nian/gateway-internal"))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if args.consume:
            args.archive.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
