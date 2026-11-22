"""Register a private OB import sink for memory items already extracted from a turn."""

import asyncio
import hashlib
import hmac
import json
import os
import re

from starlette.responses import JSONResponse
from tools._common import merge_or_create


def register_gateway_import(mcp, shared):
    from aru_backup_ob import register as register_aru_backup

    register_aru_backup(mcp, shared)
    lock = asyncio.Lock()

    @mcp.custom_route("/internal/gateway/memories", methods=["POST"])
    async def import_items(request):
        expected = os.environ.get("NIAN_INTERNAL_TOKEN", "")
        if not expected or not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + expected).encode(),
        ):
            return JSONResponse({}, status_code=401)
        body = await request.json()
        turn, raw = body.get("turn_id"), body.get("memories")
        if not isinstance(turn, str) or not re.fullmatch(r"[a-f0-9]{64}", turn):
            return JSONResponse({"error": "invalid turn id"}, status_code=400)
        if not isinstance(raw, list) or len(raw) > 5:
            return JSONResponse({"error": "invalid memories"}, status_code=400)
        items = shared.import_engine._parse_extraction(
            json.dumps(raw, ensure_ascii=False)
        )
        if len(items) != len(raw):
            return JSONResponse({"error": "invalid memory item"}, status_code=422)
        async with lock:
            if items:
                existing = await shared.bucket_mgr.list_all(include_archive=True)
                receipts = set()
                for bucket in existing:
                    metadata = bucket["metadata"]
                    receipts.add(metadata.get("grow_batch_id"))
                    receipts.update(metadata.get("gateway_receipts") or [])
                for index, item in enumerate(items):
                    receipt = hashlib.sha256(f"{turn}-{index}".encode()).hexdigest()
                    if receipt in receipts:
                        continue
                    fields = {
                        k: item[k]
                        for k in (
                            "content",
                            "tags",
                            "importance",
                            "domain",
                            "valence",
                            "arousal",
                            "name",
                        )
                    }
                    if item["preserve_raw"]:
                        await shared.bucket_mgr.create(
                            **fields,
                            source_tool="gateway-import",
                            grow_batch_id=receipt,
                        )
                    else:
                        await merge_or_create(
                            **fields,
                            raw_merge=True,
                            source_tool="gateway-import",
                            grow_batch_id=receipt,
                        )
        return JSONResponse({"stored_or_previously_delivered": len(items)})
