"""Serein HTTP and MCP entry points sharing one canonical read/write implementation."""
from typing import Any, Literal
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from . import store
from .search import KINDS, recall, search

Kind = Literal['信息卡','长期资料']
SearchKind = Literal['信息卡','长期资料','事件','原文']


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Source(Payload):
    id: str


class New(Payload):
    kind: Kind
    title: str
    content: str
    request_id: str


class Change(Payload):
    expected_revision: int = Field(ge=1)
    title: str | None = None
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None


class Query(Payload):
    query: str = Field(min_length=1, max_length=16000)
    kind: SearchKind | None = None
    limit: int = Field(default=10, ge=1, le=30)


def new(settings, data):
    i = store.create(settings.database,data.kind,data.title,data.content,data.request_id)
    return {k:v for k,v in store.read(settings.database,i).items() if k != 'content'}


def change(settings, i, data):
    return store.update(settings.database,i,data.expected_revision,**data.model_dump(exclude={'expected_revision'}))


def find(settings, data):
    return search(settings,data.query,kinds=[data.kind] if data.kind else KINDS,limit=data.limit)


def routes(settings, auth):
    router = APIRouter(prefix='/v1/library', dependencies=auth)

    @router.get('/materials')
    def listing():
        return {'items':store.catalog(settings.database)}

    @router.post('/materials')
    def create(data: New):
        return new(settings,data)

    @router.get('/materials/{identifier}')
    def read(identifier: str):
        return store.read(settings.database,identifier)

    @router.patch('/materials/{identifier}')
    def update(identifier: str, data: Change):
        return change(settings,identifier,data)

    @router.post('/source')
    def source(data: Source):
        category = '原文' if data.id.startswith('raw:') else '事件'
        with store.connect(settings.database) as db:
            for identifier, kind, title, content, _ in store.sources(db, [category]):
                if identifier == data.id:
                    return {'id':identifier, 'kind':kind, 'title':title, 'content':content}
        raise ValueError("来源已撤下或不存在")

    @router.post('/search')
    def lookup(data: Query):
        return find(settings,data)

    @router.post('/recall')
    def automatic(data: Query):
        return recall(settings,data.query)

    return router


def register(server, settings):
    from mcp.types import ToolAnnotations
    def list_materials() -> dict[str,Any]:
        """列出所有信息卡和长期资料的标题、类型及 ID。已有 ID 时直接用 read_material。"""
        return {'items':store.catalog(settings.database)}

    def read_material(id: str) -> dict[str,Any]:
        """按目录或搜索给出的稳定 ID 直接读取信息卡或长期资料的完整原文。revision 是更新时要传的当前版本。"""
        return store.read(settings.database,id)

    def update_material(id: str, expected_revision: int, title: str | None=None, content: str | None=None,
                        old_text: str | None=None, new_text: str | None=None) -> dict[str,Any]:
        """先 read_material 读原文，然后沿用 id 和 revision 更新同一份资料。局部修改传正文中唯一的原段落 old_text 和替换段落 new_text；整体重写传 content。只改标题可只传 title。发生版本冲突时重新读取。"""
        return change(settings,id,Change(expected_revision=expected_revision,title=title,content=content,
                                          old_text=old_text,new_text=new_text))

    def create_material(kind: Kind, title: str, content: str, request_id: str) -> dict[str,Any]:
        """新建一份信息卡或长期资料，传标题和完整正文，重试沿用 request_id。补充已有资料请先读后更新同一个 ID。"""
        return new(settings,New(kind=kind,title=title,content=content,request_id=request_id))

    def search_library(query: str, kind: SearchKind | None=None, limit: int=10) -> dict[str,Any]:
        """查过去的事、原话或资料时用此入口；同时用关键词和语义近邻搜索事件、聊天原文、信息卡、长期资料。可省略 kind 查全部。信息卡和长期资料用 read_material(id)，事件用 read_memory(identifier)，原话用 source_message_read(ids) 读全文。"""
        return find(settings,Query(query=query,kind=kind,limit=limit))

    for fn in (list_materials,read_material,search_library,create_material,update_material):
        readonly = fn in (list_materials,read_material,search_library)
        server.add_tool(fn,annotations=ToolAnnotations(readOnlyHint=readonly,destructiveHint=False,
                                                       idempotentHint=fn!=update_material,openWorldHint=False))
