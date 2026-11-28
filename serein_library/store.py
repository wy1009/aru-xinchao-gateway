import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

CATEGORIES = ("信息卡", "长期资料")
HISTORY_LIMIT = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_material(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('信息卡','长期资料')),
 title TEXT NOT NULL, content TEXT NOT NULL, revision INTEGER NOT NULL,
 create_key TEXT UNIQUE, initial_hash TEXT, source_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS companion_material_summary(
 id TEXT PRIMARY KEY, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companion_material_origin(
 id TEXT PRIMARY KEY, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companion_material_source(
 id TEXT NOT NULL, raw_id INTEGER NOT NULL, event_hash TEXT NOT NULL, PRIMARY KEY(id,raw_id));
CREATE TABLE IF NOT EXISTS companion_material_history(
 id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
 summary TEXT, replaced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 PRIMARY KEY(id, revision));
CREATE TABLE IF NOT EXISTS companion_removed_import(source_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS companion_search_vector(
 id TEXT NOT NULL, stamp TEXT NOT NULL, profile TEXT NOT NULL, part INTEGER NOT NULL,
 embedding BLOB NOT NULL, PRIMARY KEY(id,stamp,profile,part));
CREATE TRIGGER IF NOT EXISTS companion_vector_requires_current_material
BEFORE INSERT ON companion_search_vector
WHEN (NEW.id GLOB 'card-*' OR NEW.id GLOB 'ref-*')
 AND NOT EXISTS (SELECT 1 FROM companion_material WHERE id=NEW.id AND revision=NEW.stamp)
BEGIN SELECT RAISE(IGNORE); END;
"""

class SourceUnavailable(ValueError):
    """The selected source is missing, withdrawn or no longer readable."""

class RemovedImport(ValueError):
    """A previously imported material was explicitly deleted."""

class RevisionConflict(ValueError):
    """The selected material has changed since it was opened."""


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
        row = db.execute("""SELECT m.id,m.kind,m.title,m.content,m.revision,
                coalesce(s.summary,o.summary,'') AS summary,o.summary AS imported_summary
            FROM companion_material m LEFT JOIN companion_material_summary s ON s.id=m.id
            LEFT JOIN companion_material_origin o ON o.id=m.id WHERE m.id=?""",(identifier,)).fetchone()
        if row is None:
            raise SourceUnavailable("资料 ID 不存在，请重新查看目录")
        result = dict(row)
        if result['kind'] != '长期资料':
            result.pop('summary')
        if result['imported_summary'] is None:
            result.pop('imported_summary')
        else:
            result['imported_source_ids'] = [r[0] for r in db.execute("""
                SELECT 'raw:'||r.id FROM companion_material_source b JOIN raw_events r
                ON r.id=b.raw_id AND r.event_hash=b.event_hash AND r.source='aru'
                JOIN aru_scope a ON a.session=r.session_id AND a.allowed=1 WHERE b.id=? ORDER BY r.id""",(identifier,))]
        return result


def create(path, kind, title, content, key=None, *, source_id=None, summary=None):
    validate(kind, title, content)
    initial_fields = (kind, title, content)
    if summary is not None:
        if kind != '长期资料':
            raise ValueError('只有长期资料可以填写摘要')
        initial_fields += (summary,)
    initial = hashlib.sha256(repr(initial_fields).encode()).hexdigest()
    if key is None:
        key = 'aru-mcp:' + initial
    if not key.strip() or len(key) > 150:
        raise ValueError("新建资料需要一个稳定的请求标识，重试时请沿用")
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if source_id and db.execute('SELECT 1 FROM companion_removed_import WHERE source_id=?', (source_id,)).fetchone():
            raise RemovedImport('这份迁入资料已删除')
        prior = db.execute('SELECT id,initial_hash FROM companion_material WHERE create_key=?', (key,)).fetchone()
        if prior and prior['initial_hash'] != initial:
            raise ValueError("这个请求标识已经用于另一份内容")
        if prior:
            identifier = prior['id']
        else:
            identifier = ('card-' if kind == CATEGORIES[0] else 'ref-') + uuid.uuid4().hex[:16]
            db.execute('INSERT INTO companion_material VALUES (?,?,?,?,1,?,?,?)',
                       (identifier, kind, title, content, key, initial, source_id))
            if summary is not None:
                db.execute('INSERT INTO companion_material_summary VALUES (?,?)',(identifier,summary))
    return identifier


def _current(db, identifier):
    row = db.execute("""SELECT m.id,m.kind,m.revision,m.title,m.content,
        CASE WHEN m.kind='长期资料' THEN coalesce(s.summary,o.summary,'') END AS summary
        FROM companion_material m LEFT JOIN companion_material_summary s ON s.id=m.id
        LEFT JOIN companion_material_origin o ON o.id=m.id WHERE m.id=?""", (identifier,)).fetchone()
    if row is None:
        raise SourceUnavailable('资料已不存在，请重新查看目录')
    return row


def _publish(db, row, title, content, summary):
    """Archive the outgoing state and publish the next one in the same write transaction."""
    identifier, revision = row['id'], row['revision']
    if (title, content, summary) != (row['title'], row['content'], row['summary']):
        db.execute("""INSERT INTO companion_material_history(id,revision,title,content,summary)
            VALUES (?,?,?,?,?)""", (identifier, revision, row['title'], row['content'], row['summary']))
        db.execute("""DELETE FROM companion_material_history WHERE id=? AND revision NOT IN
            (SELECT revision FROM companion_material_history WHERE id=? ORDER BY revision DESC LIMIT ?)""",
            (identifier, identifier, HISTORY_LIMIT))
        if summary != row['summary']:
            db.execute('INSERT OR REPLACE INTO companion_material_summary VALUES (?,?)', (identifier, summary))
        revision += 1
        db.execute('UPDATE companion_material SET title=?,content=?,revision=? WHERE id=?',
                   (title, content, revision, identifier))
        db.execute('DELETE FROM companion_search_vector WHERE id=?', (identifier,))
    return {'id': identifier, 'kind': row['kind'], 'title': title, 'revision': revision}


def _snapshot(db, identifier, revision):
    row = db.execute('SELECT * FROM companion_material_history WHERE id=? AND revision=?',
                     (identifier, revision)).fetchone()
    if row is None:
        raise SourceUnavailable('这个历史版本已不在保留范围内，请重新打开历史版本')
    return row


def history(path, identifier, selected=None):
    with connect(path) as db:
        db.execute('BEGIN')
        current = _current(db, identifier)
        versions = [dict(row) for row in db.execute("""SELECT revision,replaced_at FROM companion_material_history
            WHERE id=? ORDER BY revision DESC""", (identifier,))]
        if selected is None and versions:
            selected = versions[0]['revision']
        old = _snapshot(db, identifier, selected) if selected is not None else None

    changes = []
    if old is not None:
        for key in ('title', 'summary', 'content'):
            if old[key] == current[key]:
                continue
            before = old[key].splitlines(keepends=True)
            after = current[key].splitlines(keepends=True)
            for op, a, b, c, d in SequenceMatcher(None, before, after).get_opcodes():
                if op != 'equal':
                    changes.append({'field': key, 'old_line': a+1, 'new_line': c+1,
                                    'before': ''.join(before[a:b]), 'after': ''.join(after[c:d])})
    return {'current_revision': current['revision'], 'current_title': current['title'], 'versions': versions,
            'selected_revision': selected, 'changes': changes}


def restore(path, identifier, revision, expected_revision):
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        current = _current(db, identifier)
        if current['revision'] != expected_revision:
            raise RevisionConflict('资料刚刚更新了，请重新打开历史版本，核对差异后再回退。')
        old = _snapshot(db, identifier, revision)
        return _publish(db, current, old['title'], old['content'], old['summary'])


def update(path, identifier, revision, *, title=None, content=None, old_text=None, new_text=None, summary=None):
    if (old_text is None) != (new_text is None) or (old_text is not None and content is not None):
        raise ValueError("请给完整正文，或给一对 old_text、new_text")
    if title is None and content is None and old_text is None and summary is None:
        raise ValueError("请提供需要修改的内容")
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = _current(db, identifier)
        if row['revision'] != revision:
            raise ValueError("资料已变更或不存在。请按 ID 重读最新正文，再决定如何修改")
        if old_text is not None:
            if not old_text or row['content'].count(old_text) != 1:
                raise ValueError("原段落必须在当前正文中恰好出现一次，请重读并扩大原段落")
            content = row['content'].replace(old_text, new_text, 1)
        title = row['title'] if title is None else title
        content = row['content'] if content is None else content
        validate(row['kind'], title, content)
        if summary is not None and row['kind'] != '长期资料':
            raise ValueError('只有长期资料可以修改摘要')
        return _publish(db, row, title, content, row['summary'] if summary is None else summary)


def delete(path, identifier, revision):
    with connect(path, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision,source_id FROM companion_material WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return {'id': identifier, 'deleted': False}
        if row['revision'] != revision:
            raise RevisionConflict('这份资料已更新。请重新点开标题，核对最新内容后再删除。')
        if row['source_id']:
            db.execute('INSERT INTO companion_removed_import VALUES (?)', (row['source_id'],))
        for table in ('companion_material_source', 'companion_material_origin', 'companion_material_summary',
                      'companion_search_vector', 'companion_material_history', 'companion_material'):
            db.execute(f'DELETE FROM {table} WHERE id=?', (identifier,))
        return {'id': identifier, 'deleted': True}


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


def original_rows(db, *, number=None, message=None):
    """Current backup originals shared by search, direct reads and bound evidence."""
    from serein.compat.originals import READABLE
    from serein.compat.germany.raw_events import strip_raw_client_context

    sql = """SELECT r.id,r.role,r.text,r.event_hash,r.created_at,o.text AS exported,
                    o.stamp,o.session,o.seq FROM
        (SELECT * FROM raw_events WHERE source='aru' AND """ + READABLE + """) r
        JOIN aru_scope a ON a.session=r.session_id AND a.allowed=1
        JOIN aru_original o ON o.source=r.source AND o.session=r.session_id AND o.mid=r.source_event_id
            AND o.role=r.role"""
    params = ()
    if number is not None:
        sql += ' WHERE r.id=?'
        params = (number,)
    elif message is not None:
        sql += ' WHERE r.session_id=? AND r.source_event_id=?'
        params = message
    for row in db.execute(sql + ' ORDER BY r.id', params):
        if row['text'] == row['exported'] or row['text'] == strip_raw_client_context(row['exported']):
            yield row


def original_document(row):
    speaker = '主人' if row['role'] == 'user' else '顾守'
    # This database stores Aru timestamps with an explicit offset.
    stamp = datetime.fromisoformat(row['created_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai'))
    return {'id': 'raw:' + str(row['id']), 'kind': '原文', 'speaker': speaker,
            'date': stamp.isoformat(), 'title': f'{speaker} · {stamp:%Y-%m-%d %H:%M}', 'content': row['text']}


def sources(db, kinds):
    if set(kinds) & set(CATEGORIES):
        for row in db.execute('SELECT id,kind,title,content,revision FROM companion_material ORDER BY id'):
            if row['kind'] in kinds:
                yield (row['id'], row['kind'], row['title'], row['content'], str(row['revision']))
    if '原文' in kinds:
        for row in original_rows(db):
            doc = original_document(row)
            yield (doc['id'], '原文', doc['title'], doc['content'], row['event_hash'])
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
