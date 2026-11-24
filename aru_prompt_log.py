"""Authenticated, read-only phone view of Xinchao requests and Aru receipts."""
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

from starlette.responses import HTMLResponse
from prompt_audit import PAGE_SIZE, page_records
from usage_ledger import KINDS, summary

BEIJING = timezone(timedelta(hours=8))
FEEDS = {"gateway": ("发给模型", "/run/nian-audit-gateway"), "bridge": ("手机已收件", "/run/nian-audit-bridge"),
         "emotion": ("情绪分类", "/run/nian-audit-gateway")}
STYLE = """*{box-sizing:border-box}body{margin:auto;max-width:54rem;padding:1.25rem;color:#202821;background:#f7f7f3;font:16px/1.6 system-ui,sans-serif}h1{font-size:1.8rem;line-height:1.25}h2{font-size:1.25rem}p{margin:.45rem 0 1rem}article{border:1px solid #d6ddd6;background:white;border-radius:.65rem;padding:.7rem .9rem;margin:.55rem 0}a{color:#225b47}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f4ef;padding:.65rem;font:inherit}nav{display:flex;gap:1rem;flex-wrap:wrap}"""
HEADERS = {"Cache-Control":"private, no-store", "Referrer-Policy":"no-referrer",
           "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'", "X-Frame-Options":"DENY"}


def stamp(at):
    return datetime.fromtimestamp(at, BEIJING).strftime("%m-%d %H:%M:%S")


NAV = '<nav><a href="?feed=gateway">发给模型</a><a href="?feed=bridge">手机已收件</a><a href="?feed=emotion">情绪分类</a><a href="?feed=usage">模型用量</a></nav>'


def page_body(chunks):
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>心潮消息日志</title><style>'
                        + STYLE + '</style><main><h1>心潮消息日志</h1>' + "".join(chunks) + '</main></html>', headers=HEADERS)


def model_usage(period):
    now = datetime.now(BEIJING)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    periods = {"today": ("今天", today.timestamp()), "week": ("近七天", (today - timedelta(days=6)).timestamp()),
               "month": ("本月", today.replace(day=1).timestamp()), "all": ("累计", 0)}
    if period not in periods:
        return HTMLResponse("未知统计时段", status_code=400, headers=HEADERS)
    label, start = periods[period]
    chunks = [NAV, f"<h2>{label}的模型用量</h2>", "<nav>"]
    for key, (name, _) in periods.items():
        chunks.append(f'<a href="?feed=usage&amp;period={key}">{name}</a>' if key != period else f"<b>{name}</b>")
    chunks.append("</nav>")
    try:
        rows = summary(Path(FEEDS["gateway"][1]) / "usage.sqlite", start, now.timestamp())
    except (OSError, sqlite3.Error):
        return HTMLResponse("用量暂时无法读取", status_code=503, headers=HEADERS)
    if not rows:
        chunks.append("<p>这个时段还没有调用记录。</p>")
    else:
        priced = sum(row[2] for row in rows)
        if priced:
            total = sum(row[3] or 0 for row in rows) / 1_000_000_000
            chunks.append(f"<p>已估价 {priced:,} 次，合计 <b>¥{total:,.6f}</b>。</p>")
        for kind, count, priced, cost, hit, miss, output, split_count, output_count in rows:
            amount = f"¥{cost / 1_000_000_000:,.6f}" if priced else "待估价"
            chunks.append(f"<article><b>{escape(KINDS.get(kind, kind))} · {amount}</b><br>调用 {count:,} 次")
            if count != priced:
                chunks.append(f" · 缺完整用量或价格 {count - priced:,} 次")
            if split_count:
                ratio = f"（{hit / (hit + miss):.1%}）" if hit + miss else ""
                chunks.append(f"<br>缓存命中 {hit:,}{ratio} · 未命中 {miss:,} token")
            if output_count:
                chunks.append(f"<br>输出 {output:,} token")
            chunks.append("</article>")
    chunks.append("<p>北京时间。按硅基流动返回的用量和调用时的模型标价估算，实际扣款以平台账单为准。"
                  "只统计经过网关且上游已接收的调用，主聊天含工具续接。中断时若缺少最终用量，金额留空。</p>")
    return page_body(chunks)


async def prompt_log(request):
    feed = request.query_params.get("feed", "gateway")
    if feed == "usage":
        return model_usage(request.query_params.get("period", "today"))
    if feed not in FEEDS:
        return HTMLResponse("未知日志", status_code=404, headers=HEADERS)
    heading, directory = FEEDS[feed]
    chunks = [NAV]
    record_id = request.query_params.get("id")
    try:
        page = min(1000000, max(1, int(request.query_params.get("page", "1"))))
    except ValueError:
        page = 1
    try:
        rows = page_records(directory, record_id, offset=(page - 1) * PAGE_SIZE,
                            limit=PAGE_SIZE + 1, emotion=feed == "emotion")
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
    return page_body(chunks)
