"""Persistent phone upload on the VPS. The uploader has no OB API credentials."""
import base64
import binascii
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from aru_archive_upload import app as full_archive_upload
from aru_prompt_log import prompt_log
from aru_enrich_job import queue
from aru_inbox import dest, path, pending, receive, saved

NO_CACHE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
PAGE = Path(__file__).with_name("aru_portal.html").read_text()
PASSWORD_DIGEST = os.environ["INBOX_DIGEST"]
OB_SYNC_ENABLED = os.environ.get("ARU_OB_SYNC_ENABLED", "1") != "0"
OB_SYNC_PATHS = {path, path + "/status", path + "/upload", path + "/enrich"}


def authorized(header):
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, sep, password = base64.b64decode(value, validate=True).partition(b":")
    except (ValueError, binascii.Error):
        return False
    return user == b"aru" and bool(sep) and hmac.compare_digest(
        hashlib.sha256(password).hexdigest(), PASSWORD_DIGEST
    )


async def index(request):
    return HTMLResponse(PAGE, headers={
        **NO_CACHE,
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    })


async def status(request):
    try:
        last = json.loads((dest / "receipt.json").read_text())
    except FileNotFoundError:
        last = None
    state = "syncing" if saved.exists() else "uploading" if pending.exists() else "ready"
    try:
        ai = json.loads((dest / "enrich.json").read_text())
    except FileNotFoundError:
        ai = {"state": "idle"}
    if (dest / "enrich.request").exists() and not (dest / "enrich.running").exists():
        ai["state"] = "queued"
    return JSONResponse({"state": state, "last": last, "ai": ai}, headers=NO_CACHE)


async def enrich(request):
    if request.headers.get("x-aru-upload") != "1":
        return PlainTextResponse("请从上传页开始补全。", status_code=415)
    queue(dest)
    return JSONResponse({"queued": True}, status_code=202, headers=NO_CACHE)


async def upload(request):
    # Browsers preflight cross-origin requests with this header. No CORS permissions are granted.
    if request.headers.get("x-aru-upload") != "1" or request.headers.get("content-type") != "application/octet-stream":
        return PlainTextResponse("请在上传页选择备份。", status_code=415)
    return await receive(request, busy="上一份备份正在同步，请稍后再试。", received="上传完成。")


@asynccontextmanager
async def lifespan(app):
    pending.unlink(missing_ok=True)  # Only this single-worker service writes partial uploads.
    yield


from wake_import import wake_import

routes = Starlette(lifespan=lifespan, routes=[
    Mount(path + "/archive", app=full_archive_upload),
    Route(path + "/xinchao-log", prompt_log),
    Route(path + "/xinchao", wake_import, methods=["GET","POST"]),
    Route(path, index), Route(path + "/", index), Route(path + "/status", status),
    Route(path + "/enrich", enrich, methods=["POST"]),
    Route(path + "/upload", upload, methods=["POST"]),
])


async def app(scope, receive_body, send):
    if scope["type"] == "http":
        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin1")
        if not authorized(header):
            response = PlainTextResponse("请使用上传专用账号登录。", status_code=401, headers={
                **NO_CACHE, "WWW-Authenticate": 'Basic realm="Aru Backup", charset="UTF-8"',
            })
            return await response(scope, receive_body, send)
        if not OB_SYNC_ENABLED and scope["path"].rstrip("/") in OB_SYNC_PATHS:
            response = PlainTextResponse("OB 已停用，备份同步和记忆补全已暂停。",
                                         status_code=503, headers=NO_CACHE)
            return await response(scope, receive_body, send)
    return await routes(scope, receive_body, send)
