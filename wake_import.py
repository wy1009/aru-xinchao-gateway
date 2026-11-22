"""Authenticated sender-bundle intake mounted inside the existing Aru upload portal."""
import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import HTMLResponse, PlainTextResponse

ROOT = Path(os.environ["STATE_DIRECTORY"])
FILE = ROOT / "aru-wake.incoming"
DONE = ROOT / "aru-wake.configured"
HEAD = {"Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Frame-Options":"DENY",
        "Content-Security-Policy":"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'"}
HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>小屋通知</title><style>body{font:17px system-ui;max-width:32rem;margin:8vh auto;padding:0 1em}input,textarea,button{font:inherit;margin:.6em 0;width:100%}textarea{min-height:7em}button{padding:.7em}</style><h1>小屋通知</h1><p>选择 Aru 导出的协作者会话发送方 JSON，或粘贴 JSON。</p><input type="file" id="bundle"><textarea id="raw" autocapitalize="off" spellcheck="false" placeholder="粘贴发送方配置"></textarea><button id="send">绑定到协作者的会话</button><p role="status" id="result"></p><script>
send.onclick=async()=>{send.disabled=true;try{const data=bundle.files[0]?await bundle.files[0].text():raw.value;const r=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json','X-Aru-Upload':'1'},body:data});result.textContent=await r.text();if(r.ok){raw.value='';bundle.value=''}else{send.disabled=false}}catch{result.textContent='连接失败，请重试';send.disabled=false}}
</script></html>"""
def checked(raw):
    obj=json.loads(raw)
    if not isinstance(obj,dict) or obj.get("schema")!="aru.wake-bridge.sender-bundle.v2":
        raise ValueError()
    data={k:obj[k] for k in ("schema","submitURL","submitToken","triggerId","encryptionKey")}
    if not all(isinstance(v,str) for v in data.values()): raise ValueError()
    u=urlsplit(data["submitURL"])
    if u.scheme!="https" or u.username is not None or u.password is not None or "https://" + u.netloc != os.environ.get("ARU_PUBLIC_ORIGIN") or u.query or u.fragment or not re.fullmatch(r"/aru/v1/wake-bridge/endpoints/[A-Za-z0-9_.:-]{1,128}/events",u.path):
        raise ValueError()
    if not 32<=len(data["submitToken"])<=256 or not 1<=len(data["triggerId"])<=180 or len(base64.b64decode(data["encryptionKey"],validate=True))!=32:
        raise ValueError()
    return data
async def wake_import(request):
    def reply(s,code): return PlainTextResponse(s,status_code=code,headers=HEAD)
    if DONE.exists(): return reply("这台 VPS 已绑定协作者的发送方配置。", 200 if request.method=="GET" else 409)
    if request.method=="GET": return HTMLResponse(HTML,headers=HEAD)
    if request.headers.get("x-aru-upload")!="1" or request.headers.get("content-type")!="application/json":
        return reply("请从此页面提交。",415)
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>32768: return reply("文件过大，请选择发送方 JSON。",413)
        body.extend(chunk)
    try: value=checked(body)
    except (ValueError,KeyError,TypeError,binascii.Error): return reply("格式不匹配，请选择 此 Aru Host 的发送方 JSON。",400)
    with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".wake-in-",delete=False) as tmp:
        p=Path(tmp.name)
        try:
            tmp.write(json.dumps(value).encode()); tmp.flush(); os.fsync(tmp.fileno())
            try: os.link(p,FILE)
            except FileExistsError: return reply("已有一份配置等待核验。",409)
        finally: p.unlink(missing_ok=True)
    return reply("已收到协作者的配置，可以关闭此页。",201)
