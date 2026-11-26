"""Private, resumable full-archive intake mounted inside the existing authenticated portal."""
import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import zipfile
from contextlib import contextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

CHUNK = 4 * 1024**2
RESERVE = 1024**3
ROOT = Path(os.environ["STATE_DIRECTORY"]) / "archive-uploads"
PAGE = Path(__file__).with_name("aru_archive_upload.html").read_text()
HEAD = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY", "Content-Security-Policy":
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"}


def reply(message, code=200, **fields):
    return JSONResponse({"message": message, **fields}, status_code=code, headers=HEAD)


async def index(request):
    return HTMLResponse(PAGE, headers=HEAD)


@contextmanager
def lock(directory):
    with (directory / "lock").open("ab") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def store(path, data):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def state(directory, size):
    if (directory / "archive.polarisarchive").is_file():
        return reply("文件已完整接收，等待核对。", state="received", offset=size)
    part = directory / "upload.part"
    length = part.stat().st_size if part.exists() else 0
    # A restart after the last fsync but before rename must replay final validation.
    length = min(length, size - 1)
    length -= length % CHUNK
    return reply("可以继续上传。", state="uploading", offset=length)


def begin(data):
    size, hashes = data["size"], data["hashes"]
    if (type(size) is not int or size <= 0 or not isinstance(hashes, list)
            or len(hashes) != (size+CHUNK-1)//CHUNK
            or not all(isinstance(h,str) and re.fullmatch("[0-9a-f]{64}",h) for h in hashes)):
        return reply("文件清单无效。", 400)
    key = hashlib.sha256(f"{size}:".encode() + "".join(hashes).encode()).hexdigest()
    ROOT.mkdir(mode=0o700, exist_ok=True)
    d = ROOT / key
    if not d.exists() and size + RESERVE > shutil.disk_usage(ROOT).free:
        return reply("服务器的可用空间不足。", 413)
    d.mkdir(mode=0o700, exist_ok=True)
    with lock(d):
        manifest = d / "upload.json"
        if not manifest.exists():
            store(manifest, {"size":size,"hashes":hashes})
        status = state(d, size)
        return JSONResponse({**json.loads(status.body), "id":key}, headers=HEAD)


async def bounded_body(request, limit):
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > limit:
            raise ValueError
        data.extend(block)
    return data


async def start(request):
    if request.headers.get("x-aru-upload") != "1":
        return reply("请从备份页选择文件。", 415)
    try:
        data = json.loads(await bounded_body(request, 1024**2))
        return await asyncio.to_thread(begin, data)
    except (KeyError, TypeError, ValueError, ClientDisconnect):
        return reply("未收到有效的文件清单。", 400)


def append(d, n, payload):
    with lock(d):
        manifest = json.loads((d/"upload.json").read_text())
        size, hashes = manifest["size"], manifest["hashes"]
        target = d/"archive.polarisarchive"
        if target.exists():
            return state(d, size)
        offset = n*CHUNK
        if not 0 <= n < len(hashes) or len(payload)!=min(CHUNK,size-offset):
            return reply("分块大小不符。", 400)
        if hashlib.sha256(payload).hexdigest()!=hashes[n]:
            return reply("分块校验失败，请重试。", 422)
        part = d/"upload.part"
        length = part.stat().st_size if part.exists() else 0
        if offset > length:
            return state(d,size)
        if offset+len(payload)>length:
            if shutil.disk_usage(d).free < len(payload)+RESERVE:
                return reply("服务器的可用空间不足。", 413)
            with part.open("r+b" if part.exists() else "xb") as f:
                f.seek(offset)
                f.write(payload)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
        if part.stat().st_size == size:
            try:
                with zipfile.ZipFile(part) as z:
                    for name in ["manifest.json","database.sqlite"]:
                        if z.namelist().count(name)!=1: raise ValueError
                    if z.getinfo("manifest.json").file_size>2**20: raise ValueError
                    meta=json.loads(z.read("manifest.json"))
                    if not isinstance(meta,dict) or meta.get("databasePath")!="database.sqlite": raise ValueError
            except (zipfile.BadZipFile, ValueError, KeyError, RuntimeError, NotImplementedError, OSError):
                part.unlink(missing_ok=True)
                return reply("文件校验失败，请重新选择含数据库的 Aru 原始备份。", 422)
            os.replace(part,target)
            fd = os.open(d, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return state(d,size)


async def chunk(request):
    name = request.path_params["key"]
    if request.headers.get("x-aru-upload") != "1":
        return reply("请从备份页选择文件。", 415)
    if not re.fullmatch('[0-9a-f]{64}', name) or not (ROOT/name/"upload.json").is_file():
        return reply("请重新选择同一份备份。",404)
    try:
        payload=await bounded_body(request,CHUNK)
    except (ValueError,ClientDisconnect):
        return reply("分块传输未完成。",400)
    return await asyncio.to_thread(append,ROOT/name,request.path_params["n"],payload)


app = Starlette(routes=[Route("/", index), Route("/start",start,methods=["POST"]),
                        Route("/chunk/{key}/{n:int}",chunk,methods=["POST"])])
