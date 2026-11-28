"""Compare the configured MCP tool catalogs with the gateway's translation table."""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

CATALOG = Path("/etc/nian-aru-native-tools/catalog.json")
PREFIX = {"Serein": "serein_", "Leto": "leto_", "xhs-mcp": "xhs_", "Aru 北京 VPS Tools": "aru_vps_"}
ARU_OPERATOR = Path("/var/lib/aru-selfhost/credentials/local-operator.credential")


def connection(name):
    if name == "Serein":
        return "http://127.0.0.1:18217/serein/mcp", Path("/etc/gushou-serein/hook-key").read_text().strip()
    if name == "Leto":
        if os.environ["LETO_BASE_URL"].rstrip("/") != "https://leto.sarl":
            raise ValueError("Leto endpoint changed")
        return "https://leto.sarl/mcp", os.environ["LETO_MACHINE_TOKEN"]
    if name == "xhs-mcp":
        return "http://127.0.0.1:18060/mcp", os.environ["AUTH_TOKEN"]
    if name == "Aru 北京 VPS Tools":
        return "http://127.0.0.1:8787/aru/v1/mcp", ARU_OPERATOR.read_text().strip()
    raise ValueError("Unknown service")


def fetch_tools(name):
    url, token = connection(name)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
    with httpx.Client(headers=headers, timeout=15, trust_env=False, follow_redirects=False) as client:
        sequence = 0

        def rpc(method, params, *, notification=False):
            nonlocal sequence
            sequence += 1
            request = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notification:
                request["id"] = sequence
            with client.stream("POST", url, json=request) as response:
                response.raise_for_status()
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    client.headers["Mcp-Session-Id"] = session
                if notification:
                    return
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            message = json.loads(line[5:])
                            if message.get("id") == sequence:
                                break
                    else:
                        raise ValueError("Incomplete directory response")
                else:
                    message = json.loads(response.read())
                if message.get("id") != sequence or "result" not in message:
                    raise ValueError("Invalid directory response")
                return message["result"]

        try:
            hello = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "aru-catalog-check", "version": "1"}})
            client.headers["MCP-Protocol-Version"] = hello["protocolVersion"]
            rpc("notifications/initialized", {}, notification=True)
            tools = []
            params = {}
            seen = set()
            while True:
                page = rpc("tools/list", params)
                tools.extend(page["tools"])
                cursor = page.get("nextCursor")
                if not cursor:
                    return tools
                if cursor in seen:
                    raise ValueError("Repeated directory cursor")
                seen.add(cursor)
                params = {"cursor": cursor}
        finally:
            if "Mcp-Session-Id" in client.headers:
                client.delete(url)


def installed_aru_tools():
    state = json.loads(Path("/var/lib/aru-selfhost/data/state.json").read_text())
    if state.get("plugins") != []:
        raise ValueError("Dynamic plugins require the paired or local operator directory")
    helper = Path(__file__).with_name("aru_installed_tool_list.mjs")
    result = subprocess.run(["/usr/local/bin/node", "--input-type=module", "-"], input=helper.read_bytes(),
                            cwd="/", user="nobody", group="nobody",
                            extra_groups=[], env={"PATH": os.defpath}, capture_output=True,
                            timeout=15, check=True)
    return json.loads(result.stdout)


def compare(server, tools):
    prefix = PREFIX[server["server"]]
    local = {item["function"]["name"].removeprefix(prefix): item["function"] for item in server["tools"]}
    remote = {item["name"]: item for item in tools}
    if len(local) != len(server["tools"]) or len(remote) != len(tools):
        raise ValueError("Duplicate tool names")
    common = local.keys() & remote.keys()
    return {
        "registered": len(local), "available": len(remote),
        "service_only": sorted(remote.keys() - local.keys()),
        "missing_on_service": sorted(local.keys() - remote.keys()),
        "changed_input_schema": sorted(k for k in common if local[k]["parameters"] != remote[k]["inputSchema"]),
        "changed_description": sorted(k for k in common if local[k].get("description", "") != remote[k].get("description", "")),
    }


def main():
    failed = False
    for server in json.loads(CATALOG.read_text()):
        name = server["server"]
        source = "tools/list"
        try:
            if name == "Aru 北京 VPS Tools" and not ARU_OPERATOR.exists():
                source = "installed Aru definitions"
                tools = installed_aru_tools()
            else:
                tools = fetch_tools(name)
            result = compare(server, tools)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
            failed = True
            result = {"error": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError):
                result["http_status"] = exc.response.status_code
        print(json.dumps({"server": name, "source": source, **result}, ensure_ascii=False), flush=True)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
