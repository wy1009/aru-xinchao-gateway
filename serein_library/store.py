import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

CATEGORIES = ("信息卡", "长期资料")
SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_material(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('信息卡','长期资料')),
 title TEXT NOT NULL, content TEXT NOT NULL, revision INTEGER NOT NULL,
 create_key TEXT UNIQUE, initial_hash TEXT, source_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS companion_material_origin(
 id TEXT PRIMARY KEY, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companion_material_source(
 id TEXT NOT NULL, raw_id INTEGER NOT NULL, event_hash TEXT NOT NULL, PRIMARY KEY(id,raw_id));
CREATE TABLE IF NOT EXISTS companion_search_vector(
 id TEXT NOT NULL, stamp TEXT NOT NULL, profile TEXT NOT NULL, part INTEGER NOT NULL,
 embedding BLOB NOT NULL, PRIMARY KEY(id,stamp,profile,part));
"""

@contextmanager
def connect(path, *, write=False):
    mode = 'rw' if write else 'ro'
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=' + mode, uri=True, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def setup(path):
    with connect(path, write=True) as db:
        db.executescript(SCHEMA)


def validate(kind, title, content):
    if kind not in CATEGORIES or not title.strip() or len(title) > 240 or '\n' in title or '\r' in title:
        raise ValueError("请选择资料类型，并填写不超过 240 字的单行标题")
    if not content.strip():
        raise ValueError("资料正文不能为空")


def catalog(path):
    with connect(path) as db:
        return [dict(r) for r in db.execute(
            'SELECT id,kind,title,revision FROM companion_material ORDER BY kind,title,id')]


def read(path, identifier):
    with connect(path) as db:
        row = db.execute('SELECT id,kind,title,content,revision FROM companion_material WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError("资料 ID 不存在，请重新查看目录")
        result = dict(row)
        origin = db.execute('SELECT summary FROM companion_material_origin WHERE id=?',(identifier,)).fetchone()
        if origin:
            result['imported_summary'] = origin[0]
            result['imported_source_ids'] = [r[0] for r in db.execute("""
                SELECT 'raw:'||r.id FROM companion_material_source b JOIN raw_events r
                ON r.id=b.raw_id AND r.event_hash=b.event_hash AND r.source='aru'
                JOIN aru_scope a ON a.session=r.session_id AND a.allowed=1 WHERE b.id=? ORDER BY r.id""",(identifier,))]
        return result


def create(path, kind, title, content, key, *, source_id=None):
    validate(kind, title, content)
    if not key or not key.strip() or len(key) > 150:
        raise ValueError("新建资料需要一个稳定的请求标识，重试时请沿用")
    initial = hashlib.sha256(repr((kind,title,content)).encode()).hexdigest()
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT id,initial_hash FROM companion_material WHERE create_key=?', (key,)).fetchone()
        if prior and prior['initial_hash'] != initial:
            raise ValueError("这个请求标识已经用于另一份内容")
        if prior:
            identifier = prior['id']
        else:
            identifier = ('card-' if kind == CATEGORIES[0] else 'ref-') + uuid.uuid4().hex[:16]
            db.execute('INSERT INTO companion_material VALUES (?,?,?,?,1,?,?,?)',
                       (identifier, kind, title, content, key, initial, source_id))
    return identifier


def update(path, identifier, revision, *, title=None, content=None, old_text=None, new_text=None):
    if (old_text is None) != (new_text is None) or (old_text is not None and content is not None):
        raise ValueError("请给完整正文，或给一对 old_text、new_text")
    if title is None and content is None and old_text is None:
        raise ValueError("请提供需要修改的内容")
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM companion_material WHERE id=?', (identifier,)).fetchone()
        if not row or row['revision'] != revision:
            raise ValueError("资料已变更或不存在。请按 ID 重读最新正文，再决定如何修改")
        if old_text is not None:
            if not old_text or row['content'].count(old_text) != 1:
                raise ValueError("原段落必须在当前正文中恰好出现一次，请重读并扩大原段落")
            content = row['content'].replace(old_text, new_text, 1)
        title = row['title'] if title is None else title
        content = row['content'] if content is None else content
        validate(row['kind'], title, content)
        if (title, content) != (row['title'], row['content']):
            db.execute('UPDATE companion_material SET title=?,content=?,revision=revision+1 WHERE id=?',
                       (title, content, identifier))
            db.execute('DELETE FROM companion_search_vector WHERE id=?', (identifier,))
            revision += 1
        return {'id': identifier, 'kind': row['kind'], 'title': title, 'revision': revision}


def passages(body, size=1000):
    # Contiguous slices preserve every character. Prefer paragraph, then sentence boundaries.
    start = 0
    while start < len(body):
        end = min(start + size, len(body))
        if end < len(body):
            for boundary in ('\n\n', '\n', '。', '！', '？', '. '):
                cut = body.rfind(boundary, start + size // 3, end)
                if cut >= 0:
                    end = cut + len(boundary)
                    break
        yield body[start:end]
        start = end


def sources(db, kinds):
    if set(kinds) & set(CATEGORIES):
        for row in db.execute('SELECT id,kind,title,content,revision FROM companion_material ORDER BY id'):
            if row['kind'] in kinds:
                yield (row['id'], row['kind'], row['title'], row['content'], str(row['revision']))
    if '原文' in kinds:
        from serein.compat.originals import READABLE
        from serein.compat.germany.raw_events import strip_raw_client_context
        rows = db.execute("""SELECT r.id,r.role,r.text,r.event_hash,r.created_at,o.text AS exported FROM
            (SELECT * FROM raw_events WHERE source='aru' AND """ + READABLE + """) r
            JOIN aru_scope a ON a.session=r.session_id AND a.allowed=1
            JOIN aru_original o ON o.source=r.source AND o.session=r.session_id AND o.mid=r.source_event_id
            AND o.role=r.role ORDER BY r.id""")
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for r in rows:
            if r['text'] != r['exported'] and r['text'] != strip_raw_client_context(r['exported']):
                continue
            speaker = '主人' if r['role'] == 'user' else '顾守'
            stamp = datetime.fromisoformat(r['created_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai'))
            yield ('raw:'+str(r['id']), '原文', f'{speaker} · {stamp:%Y-%m-%d %H:%M}', r['text'], r['event_hash'])
    if '事件' in kinds:
        from serein.core.store import Store
        # Delegate visibility (e.g. covered or promoted Events) to Serein's canonical rules.
        rows = db.execute("""SELECT d.id,d.kind,d.revision,r.title,r.body_md FROM documents d JOIN revisions r
            ON r.document_id=d.id AND r.number=d.revision WHERE d.kind IN ('event','scene')
            AND d.lifecycle='active' AND d.manual_surface=1 AND NOT coalesce(json_extract(r.metadata_json,'$.aru_import'),0)""")
        with Store(db.execute('PRAGMA database_list').fetchone()['file'], read_only=True) as store:
            for r in rows:
                if store.surface_state(r['id'])['can_surface']:
                    yield (r['kind']+':'+r['id'], '事件', r['title'], r['body_md'], str(r['revision']))
