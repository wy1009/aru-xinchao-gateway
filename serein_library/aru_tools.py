"""Aru-shaped MCP operations for authored Serein materials."""
import json
import re
from typing import Annotated, Any

from pydantic import Field

from . import store

# The read ID stays stable. Only the edit handle carries the revision of the text just read.
HANDLE = re.compile(r'((card|ref)-[0-9a-f]{16})@([1-9][0-9]{0,17})')
ARU_UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
PAGE_CHARS = 4000


def resolve_read_id(path, identifier):
    handle = HANDLE.fullmatch(identifier)
    if handle:
        return handle[1]
    prefix, _, value = identifier.partition(':')
    source_prefix = {'memory_event': 'aru-card-', 'memory_reference_doc': 'aru-reference-'}.get(prefix)
    if source_prefix and value:
        # UUID spelling varies across Aru exports. Newer card IDs are opaque.
        sql = 'SELECT id FROM companion_material WHERE source_id=?'
        if re.fullmatch(ARU_UUID, value, re.I):
            sql += ' COLLATE NOCASE'
        params = (source_prefix + value,)
    elif prefix == 'card' and re.fullmatch(r'[0-9a-f]{8}', value, re.I):
        sql = 'SELECT id FROM companion_material WHERE substr(source_id,1,?)=? COLLATE NOCASE'
        short = 'aru-card-' + value
        params = (len(short), short)
    else:
        return identifier
    with store.connect(path) as db:
        rows = db.execute(sql, params).fetchall()
    if len(rows) != 1:
        raise ValueError('这个 Aru 来源标识未唯一对应已迁入的资料，请用 Serein 目录里的 ID')
    return rows[0]['id']


def edit_target(handle, expected_kind):
    match = HANDLE.fullmatch(handle)
    if not match or match[2] != expected_kind:
        field = 'cardId' if expected_kind == 'card' else 'reference'
        raise ValueError(f'请先用 Serein 的 openMemorySource(id) 读全文，然后原样传回 {field}')
    return match[1], int(match[3])


def read_history(path, identifier):
    from serein.compat.originals import raw_id
    from serein.core.reader import Reader

    kind, _, key = identifier.partition(':')
    unavailable = '这条来源已撤下或不可读，请重新搜索。'
    if kind in ('raw', 'aru'):
        with store.connect(path) as db:
            if kind == 'raw':
                rows = store.original_rows(db, number=raw_id(identifier))
            else:
                session, sep, mid = key.partition(':')
                if not sep or not session or not mid:
                    raise store.SourceUnavailable(unavailable)
                rows = store.original_rows(db, message=(session, mid))
            row = next(rows, None)
            if row is None:
                raise store.SourceUnavailable(unavailable)
            return store.original_document(row)

    # Resolve the native revision, bindings, visibility and source text in one snapshot.
    with Reader(path) as reader, reader.store.transaction():
        result = reader.read(identifier, kind=kind, with_evidence=True)
        doc = result.get('document')
        if (not result['readable'] or not result.get('surface_state', {}).get('can_surface')
                or doc['metadata'].get('aru_import')):
            raise store.SourceUnavailable(unavailable)
        originals = {}
        for binding in result['evidence']:
            try:
                coords = json.loads(binding['source_key'])
            except (TypeError, ValueError):
                continue
            if not isinstance(coords, list) or len(coords) != 3 or coords[0] != 'aru':
                continue  # Only verified chat originals belong in the evidence list.
            row = next(store.original_rows(reader.store.conn, message=coords[1:]), None)
            if row is None or row['text'] != binding['content']:
                raise store.SourceUnavailable(unavailable)
            originals[(row['stamp'], row['session'], row['seq'], row['id'])] = store.original_document(row)
        return {'id': identifier, 'kind': '事件' if kind == 'event' else '场景',
                'title': doc['title'], 'content': doc['body_md'], 'revision': doc['revision'],
                'evidence': [originals[k] for k in sorted(originals)]}


def open_source(path, identifier, *, page=None, query=None):
    revision = None
    if identifier.startswith(('raw:', 'aru:', 'event:', 'scene:')):
        doc = read_history(path, identifier)
    else:
        doc = store.read(path, resolve_read_id(path, identifier))
        doc.pop('imported_summary', None)
        revision = doc.pop('revision')
    body = doc['content']
    total = max(1, (len(body) + PAGE_CHARS - 1) // PAGE_CHARS)
    if query:
        pages = set()
        pos = body.find(query)
        while pos >= 0:
            pages.add(pos // PAGE_CHARS + 1)
            pos = body.find(query, pos + 1)
        doc['matchedPages'] = sorted(pages)
    if page is not None:
        if page > total:
            raise ValueError(f'这份资料共 {total} 页，请使用有效页码')
        doc.update(page=page, totalPages=total, content=body[(page - 1) * PAGE_CHARS : page * PAGE_CHARS])
        if total > 1:
            doc['nextPage'] = page + 1 if page < total else None
            if revision is not None:
                doc['editHint'] = '要更新这份资料，请仅传 id 读回全文和更新标识。'
            return doc
    if revision is not None:
        handle_field = 'cardId' if doc['kind'] == '信息卡' else 'reference'
        doc[handle_field] = f"{doc['id']}@{revision}"
    return doc


def register_aru_tools(server, settings):
    from mcp.types import ToolAnnotations
    path = settings.database

    def openMemorySource(id: str, page: Annotated[int, Field(ge=1)] | None = None,
                         query: str | None = None) -> dict[str, Any]:
        """打开任一 search_library 结果或资料目录项。直接传返回的 id 即可。事件或场景同时返回正文和当前绑定的备份原话 evidence；聊天原文带说话人和日期。信息卡和长期资料读全文后可沿用 cardId 或 reference 更新。page 从 1 开始；query 按原文字面定位页码。"""
        return open_source(path, id, page=page, query=query)

    def writeInformationCard(subject: str, content: str, cardId: str | None = None) -> dict[str, Any]:
        """在 Serein 保存信息卡。新建传 subject（主题）和 content（完整正文）。补写旧卡先读全文，将原有内容和改动合在 content 里，并原样带回 cardId。"""
        if cardId is None:
            identifier = store.create(path, '信息卡', subject, content)
            return {'id': identifier}
        identifier, revision = edit_target(cardId, 'card')
        saved = store.update(path, identifier, revision, title=subject, content=content)
        return {'id': saved['id']}

    def writeLongTermReference(title: str, content: str, summary: str | None = None) -> dict[str, Any]:
        """在 Serein 新建长期资料，传标题、完整正文，可附摘要。更新已有资料请先 openMemorySource，再用 updateLongTermReference。"""
        identifier = store.create(path, '长期资料', title, content, summary=summary)
        return {'id': identifier}

    def updateLongTermReference(reference: str, content: str, title: str | None = None,
                                summary: str | None = None) -> dict[str, Any]:
        """更新 Serein 长期资料。先读全文，原样带回 reference 和改好的完整正文 content。按需传新标题或摘要；省略就保留原值。"""
        identifier, revision = edit_target(reference, 'ref')
        saved = store.update(path, identifier, revision, content=content, title=title, summary=summary)
        return {'id': saved['id']}

    for fn in (openMemorySource, writeInformationCard, writeLongTermReference, updateLongTermReference):
        server.add_tool(fn, annotations=ToolAnnotations(
            readOnlyHint=fn is openMemorySource, destructiveHint=False, idempotentHint=True, openWorldHint=False,
        ))
        # Misspelling cardId on an edit must never turn it into a new-card request.
        tool = server._tool_manager.get_tool(fn.__name__)
        model = tool.fn_metadata.arg_model
        model.model_config['extra'] = 'forbid'
        model.model_rebuild(force=True)
        tool.parameters = model.model_json_schema(by_alias=True)
