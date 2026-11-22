"""Authenticated, read-only phone view of Xinchao requests and Aru receipts."""
from datetime import datetime, timedelta, timezone
from html import escape
import sqlite3
from urllib.parse import urlencode

from starlette.responses import HTMLResponse
from prompt_audit import PAGE_SIZE, page_records

BEIJING = timezone(timedelta(hours=8))
FEEDS = {"gateway": ("发给模型", "/run/nian-audit-gateway"), "bridge": ("手机已收件", "/run/nian-audit-bridge")}
STYLE = """*{box-sizing:border-box}body{margin:auto;max-width:54rem;padding:1.25rem;color:#202821;background:#f7f7f3;font:16px/1.6 system-ui,sans-serif}h1{font-size:1.8rem;line-height:1.25}h2{font-size:1.25rem}p{margin:.45rem 0 1rem}article{border:1px solid #d6ddd6;background:white;border-radius:.65rem;padding:.7rem .9rem;margin:.55rem 0}a{color:#225b47}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f4ef;padding:.65rem;font:inherit}nav{display:flex;gap:1rem;flex-wrap:wrap}"""
HEADERS = {"Cache-Control":"private, no-store", "Referrer-Policy":"no-referrer",
           "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'", "X-Frame-Options":"DENY"}


def stamp(at):
    return datetime.fromtimestamp(at, BEIJING).strftime("%m-%d %H:%M:%S")


async def prompt_log(request):
    feed = request.query_params.get("feed", "gateway")
    if feed not in FEEDS:
        return HTMLResponse("未知日志", status_code=404, headers=HEADERS)
    heading, directory = FEEDS[feed]
    chunks = ['<nav><a href="?feed=gateway">发给模型</a><a href="?feed=bridge">手机已收件</a></nav>']
    record_id = request.query_params.get("id")
    try:
        page = min(1000000, max(1, int(request.query_params.get("page", "1"))))
    except ValueError:
        page = 1
    try:
        rows = page_records(directory, record_id, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE + 1)
    except (sqlite3.Error, ValueError, OSError):
        return HTMLResponse("日志暂时无法读取", status_code=503, headers=HEADERS)
    if record_id is None:
        chunks.append(f"<h2>{heading}</h2>")
        chunks.append("<p>北京时间。滚动保留最近 7 天，点时间看原文。</p>")
        if feed == "gateway":
            chunks.append("<p>展开可看本轮请求的消息顺序，以及模型返回的可见回复和工具调用。媒体附件只记类型。旧版记录仅含非助手消息。</p>")
        if not rows:
            chunks.append("<p>还没有记录。</p>")
        for key, at, status in rows[:PAGE_SIZE]:
            query = escape(urlencode({"feed":feed,"id":key}), quote=True)
            chunks.append(f'<article><a href="?{query}">{stamp(at)}</a> · {escape(status)}</article>')
        if page > 1:
            chunks.append(f'<a href="?feed={feed}&amp;page={page - 1}">上一页</a> ')
        if len(rows) > PAGE_SIZE:
            chunks.append(f'<a href="?feed={feed}&amp;page={page + 1}">下一页</a>')
    elif rows is None:
        return HTMLResponse("这条记录已过期或不存在。", status_code=404, headers=HEADERS)
    else:
        at, status, entries = rows
        chunks.append(f"<h2>{stamp(at)} · {escape(status)}</h2>")
        for pos, role, kind, text in entries:
            label = (f"第 {pos} 条 · {role} · " if pos else "") + kind
            chunks.append("<article><b>" + escape(label) + "</b><pre>" + escape(text) + "</pre></article>")
        if not entries:
            chunks.append("<p>这条记录没有可展示的消息。</p>")
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>心潮消息日志</title><style>'
                        + STYLE + '</style><main><h1>心潮消息日志</h1>' + "".join(chunks) + '</main></html>', headers=HEADERS)
