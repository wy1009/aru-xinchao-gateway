import hashlib
import hmac
import os
import shutil
from pathlib import Path
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

path = os.environ["INBOX_PATH"]
dest = Path(os.environ["STATE_DIRECTORY"])
pending = dest / "upload.part"
saved = dest / "archive.polarisarchive"
page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Aru 备份上传</title><style>body{font:17px system-ui,sans-serif;margin:10vh auto;max-width:32rem;padding:0 1.5rem;color:#142b35}button,input{display:block;font:inherit;margin:1.2rem 0}button{padding:.65rem 1.3rem;background:#174f5b;color:white;border:none;border-radius:.5rem}button:disabled{opacity:.65}</style><h1>上传 Aru 备份</h1><p>选择原始 .polarisarchive 文件，上传完成前请保持此页打开。</p><input type="file" id="f"><button id="b">开始上传</button><p id="statusText" aria-live="polite"></p><script>
const key=location.hash.slice(1), b=document.querySelector('#b'), statusText=document.querySelector('#statusText'), f=document.querySelector('#f');
b.onclick=()=>{const file=f.files[0];if(!key||!file){statusText.textContent='请使用完整链接，并选择备份。';return}
const x=new XMLHttpRequest();x.open('POST',location.pathname+'/upload');x.setRequestHeader('Authorization','Bearer '+key);x.setRequestHeader('Content-Type','application/octet-stream');b.disabled=true;
x.upload.onprogress=e=>{if(e.lengthComputable)statusText.textContent='已上传 '+Math.floor(100*e.loaded/e.total)+'%'};
x.onload=()=>{statusText.textContent=x.responseText;b.disabled=x.status===201};x.onerror=x.onabort=()=>{statusText.textContent='传输中断，请重试。';b.disabled=false};x.send(file)};
</script></html>"""


async def form(request):
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        },
    )


async def upload(request):
    head = request.headers.get("authorization", "")
    if not head.startswith("Bearer ") or not hmac.compare_digest(
        hashlib.sha256(head[7:].encode()).hexdigest(), os.environ["INBOX_DIGEST"]
    ):
        return PlainTextResponse("此链接无效。", status_code=403)
    return await receive(request)


async def receive(request, *, busy="已收到备份，可返回聊天。", received="上传完成，可以返回聊天。"):
    if saved.exists():
        return PlainTextResponse(busy, status_code=409)
    budget = shutil.disk_usage(dest).free - (1024**3)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > budget):
        return PlainTextResponse("备份超过服务器可用空间。", status_code=413)
    try:
        stream = pending.open("xb")
    except FileExistsError:
        return PlainTextResponse("已有上传正在进行。", status_code=409)
    try:
        with stream:
            size = 0
            async for block in request.stream():
                size += len(block)
                if size > budget:
                    return PlainTextResponse(
                        "备份超过服务器可用空间。", status_code=413
                    )
                stream.write(block)
            if not size or (length is not None and size != int(length)):
                return PlainTextResponse("文件不完整，请重试。", status_code=400)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, saved)
    except ClientDisconnect:
        return PlainTextResponse("传输中断。", status_code=400)
    except FileExistsError:
        return PlainTextResponse(busy, status_code=409)
    finally:
        pending.unlink(missing_ok=True)
    return PlainTextResponse(received, status_code=201)


app = Starlette(
    routes=[Route(path, form), Route(path + "/upload", upload, methods=["POST"])]
)
