"""Present selected Aru MCP methods as ordinary OpenAI functions. Aru still executes them."""
import json
import re
from pathlib import Path

import aru_mcp_arguments as dsml

DISPATCH = "invokeMCPTool"
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
HANDLE = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Fa-f0-9]{32}")


def wire(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def parse(value):
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def load(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text())
    seen = set()
    for service in data:
        for tool in service["tools"]:
            name, alias = tool["function"]["name"], tool["alias"]
            if (not NAME.fullmatch(name) or not HANDLE.fullmatch(alias) or alias in seen
                    or name in seen or tool["function"]["parameters"].get("type") != "object"):
                raise ValueError("Invalid MCP projection catalog")
            seen.update((alias, name))
    return data


def directory(value):
    if (isinstance(value, dict) and value.get("kind") == "mcp_tool_directory"
            and value.get("scope") == "server" and value.get("complete") is True
            and isinstance(value.get("server"), dict) and isinstance(value.get("items"), list)):
        return value["server"].get("name"), {
            item["tool"] for item in value["items"] if isinstance(item, dict) and isinstance(item.get("tool"), str)
        }
    return None


def project(body, catalog, is_environment, as_text):
    tools = body.get("tools")
    if (not catalog or not isinstance(tools, list) or body.get("tool_choice") == "none"
            or isinstance(body.get("tool_choice"), dict)):
        return body, {}
    originals = {t["function"].get("name"): t["function"] for t in tools
                 if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    if DISPATCH not in originals:
        return body, {}
    params = originals[DISPATCH].get("parameters") or {}
    if set(params.get("required") or ()) - {"tool", "arguments"}:
        return body, {}
    selector = (params.get("properties") or {}).get("tool") or {}
    allowed = selector.get("enum")
    if "const" in selector:
        allowed = [selector["const"]]
    messages = body["messages"]
    env = next((m for m in reversed(messages) if is_environment(m)), None)
    if not env:
        return body, {}
    roster = next((line for line in as_text(env.get("content")).splitlines()
                   if line.startswith("mcp.tools:")), "")
    # Only the roster segment gives the active server names and counts.
    counts = re.search(r"([0-9]+) 个服务：([^。]+)", roster)
    if not counts:
        return body, {}
    active = {s.strip(): int(n) for s, n in re.findall(r"([^、,，]+?)\(([0-9]+)\)", counts[2])}
    if not active or len(active) != int(counts[1]):
        return body, {}

    # A newer directory receipt takes precedence over the installed alias snapshot.
    pending, available = {}, {}
    selected, wanted = set(), set()
    for msg in messages:
        if msg.get("role") == "user" and not is_environment(msg):
            selected, wanted = set(), set()
        elif msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or ():
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and fn.get("name") in ("listMCPTools", "readMCPTool") and call.get("id"):
                    pending[call["id"]] = fn
        elif msg.get("role") == "tool":
            fn = pending.pop(msg.get("tool_call_id"), None)
            if fn is None:
                continue
            receipt = parse(as_text(msg.get("content")))
            if fn["name"] == "listMCPTools":
                if isinstance(receipt, dict) and receipt.get("kind") == "mcp_tool_directory":
                    wanted.update(i["tool"] for i in receipt.get("items") or ()
                                  if isinstance(i, dict) and isinstance(i.get("tool"), str))
                info = directory(receipt)
                if info and info[0] in active:
                    available[info[0]] = info[1]
                    selected.add(info[0])
            elif isinstance(receipt, dict) and not receipt.get("error"):
                args = parse(fn.get("arguments"))
                if isinstance(args, dict) and isinstance(args.get("tool"), str):
                    wanted.add(args["tool"])

    projected = {}
    covered = set()
    for service in catalog:
        name = service["server"]
        if name not in active:
            continue
        handles = available.get(name)
        if (len(handles) if handles is not None else service["count"]) != active[name]:
            continue
        included = 0
        for tool in service["tools"]:
            fn = tool["function"]
            if (handles is not None and tool["alias"] not in handles
                    or allowed is not None and tool["alias"] not in allowed
                    or fn["name"] in originals):
                continue
            if name in selected or tool["alias"] in wanted or "listMCPTools" not in originals:
                projected[fn["name"]] = tool
            included += 1
        if included == active[name]:
            covered.add(name)
    all_covered = covered == active.keys()
    if not projected and not all_covered:
        return body, {}
    visible = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if all_covered and name == DISPATCH:
            continue
        if name == DISPATCH:
            fn = {**fn, "description": "如本轮已有对应的独立函数，请直接调用该函数。其余 MCP 工具可用此入口。 " + (fn.get("description") or "")}
            tool = {**tool, "function": fn}
        visible.append(tool)
    visible.extend({"type": "function", "function": t["function"]} for t in projected.values())
    history = provider_history(messages, projected)
    # Align the app-generated instructions with the provider-facing tool definitions.
    if all_covered:
        idx = next(i for i in range(len(messages) - 1, -1, -1) if messages[i] is env)
        content = env["content"]
        if "listMCPTools" in originals:
            note = roster.split("。", 1)[0] + "。用 listMCPTools 查看所需服务的目录；已展开的方法按 tools 中的独立函数名和参数直接调用。"
            if "readMCPTool" in originals:
                note += "需要详情时可用 readMCPTool。"
        else:
            note = roster.split("。", 1)[0] + "。按 tools 中的独立函数名和参数直接调用。"
        if isinstance(content, str):
            content = content.replace(roster, note, 1)
        else:
            content = [{**part, "text": part["text"].replace(roster, note, 1)} for part in content]
        history[idx] = {**history[idx], "content": content}
    return {**body, "tools": visible, "messages": history}, projected


def exceeds_provider_limit(raw):
    text = raw[:16384].decode("utf-8", errors="replace").lower()
    return any(term in text for term in ("context length", "context_length", "context window",
        "too many tools", "maximum number of tools", "too many functions", "at most 128",
        "maximum context", "context size", "context_length_exceeded", "工具数量超过"))


def provider_history(messages, projected):
    by_alias = {t["alias"]: name for name, t in projected.items()}
    renamed, directories, history = {}, set(), []
    for msg in messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list):
            calls = []
            for call in msg["tool_calls"]:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and fn.get("name") == "listMCPTools" and call.get("id"):
                    directories.add(call["id"])
                if isinstance(fn, dict) and fn.get("name") == DISPATCH:
                    outer = parse(fn.get("arguments"))
                    if (isinstance(outer, dict) and isinstance(outer.get("tool"), str)
                            and outer["tool"] in by_alias and isinstance(outer.get("arguments"), dict)):
                        name = by_alias[outer["tool"]]
                        call = {**call, "function": {**fn, "name": name, "arguments": wire(outer["arguments"])}}
                        if call.get("id"):
                            renamed[call["id"]] = name
                calls.append(call)
            msg = {**msg, "tool_calls": calls}
        elif msg.get("role") == "tool" and msg.get("tool_call_id") in directories:
            directories.remove(msg["tool_call_id"])
            content = msg.get("content")
            if isinstance(content, str):
                receipt = parse(content)
                if (isinstance(receipt, dict) and receipt.get("kind") == "mcp_tool_directory"
                        and isinstance(receipt.get("items"), list)):
                    for item in receipt["items"]:
                        if isinstance(item, dict) and isinstance(item.get("tool"), str) and item["tool"] in by_alias:
                            item["call_as"] = by_alias[item["tool"]]
                    if any(isinstance(i, dict) and "call_as" in i for i in receipt["items"]):
                        receipt["usage"] = "已展开的方法直接调用 call_as 所列函数，只传其业务参数。"
                        msg = {**msg, "content": json.dumps(receipt, ensure_ascii=False)}
        elif msg.get("role") == "tool" and msg.get("name") == DISPATCH and msg.get("tool_call_id") in renamed:
            msg = {**msg, "name": renamed[msg["tool_call_id"]]}
        history.append(msg)
    return history


def wrapped(tool, raw, complete=True):
    args = parse(raw)
    if complete and not isinstance(args, dict):
        recovered = dsml.recover_parameters(args if isinstance(args, str) else raw,
                                            tool["alias"], tool["function"]["parameters"])
        if recovered is not None:
            args = recovered
            dsml.log.info("MCP DSML parameters restored: projected function")
    if complete and (raw is None or isinstance(raw, str) and not raw.strip()) and not tool["function"]["parameters"].get("required"):
        args = {}
    # Malformed or truncated calls must fail the client's object validation.
    return wire({"tool": tool["alias"], "arguments": args if complete and isinstance(args, dict) else None})


def restore(chunk, projected):
    changed = False
    for choice in chunk.get("choices") or ():
        for call in (choice.get("message") or {}).get("tool_calls") or ():
            fn = call.get("function") or {}
            tool = projected.get(fn.get("name"))
            if tool:
                fn["name"] = DISPATCH
                fn["arguments"] = wrapped(tool, fn.get("arguments"), choice.get("finish_reason") in ("stop", "tool_calls"))
                changed = True
    return changed


class StreamCalls:
    """Assemble MCP arguments per choice and call; let ordinary text and other tools stream."""
    def __init__(self, projected, dispatcher_schemas=None):
        self.tools = projected
        self.dispatcher_schemas = dispatcher_schemas
        self.names = set(projected)
        if dispatcher_schemas is not None:
            self.names.add(DISPATCH)
        self.calls = {}

    def apply(self, chunk):
        changed = False
        for choice in chunk.get("choices") or ():
            idx = choice.get("index", 0)
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reason = choice.get("finish_reason")
            current = {}
            for n, fn in dsml.functions_in_choice(choice, "delta"):
                current[n] = fn
                state = self.calls.setdefault((idx, n), {"name": "", "args": [], "mode": "pending"})
                if state["mode"] == "passthrough":
                    continue
                part = fn.get("name") or ""
                args = fn.get("arguments")
                old_name = state["name"]
                state["name"] += part
                if args is not None:
                    state["args"].append(args)
                if state["mode"] == "pending":
                    name = state["name"]
                    if name in self.names and (args or reason or not any(s != name and s.startswith(name) for s in self.names)):
                        state["mode"] = "projected"
                        fn["name"] = DISPATCH
                    elif not any(s.startswith(name) for s in self.names):
                        state["mode"] = "passthrough"
                        if old_name or len(state["args"]) > (args is not None):
                            fn.update(name=name, arguments="".join(state["args"]))
                            changed = True
                        state["args"].clear()
                        continue
                    else:
                        fn.pop("name", None)
                else:
                    fn.pop("name", None)
                fn.pop("arguments", None)
                changed = True
            if reason:
                for key in [key for key in self.calls if key[0] == idx]:
                    _, n = key
                    state = self.calls.pop(key)
                    if state["mode"] == "passthrough":
                        continue
                    fn = current.get(n)
                    if fn is None:
                        fn = {}
                        if n == -1:
                            delta["function_call"] = fn
                        else:
                            delta.setdefault("tool_calls", []).append({"index": n, "function": fn})
                    name = state["name"]
                    args = "".join(state["args"])
                    if name in self.names:
                        if state["mode"] == "pending":
                            fn["name"] = DISPATCH
                        complete = reason in ("stop", "tool_calls", "function_call")
                        if name == DISPATCH:
                            repaired = dsml.corrected_arguments(args, self.dispatcher_schemas) if complete else None
                            fn["arguments"] = repaired if repaired is not None else args
                        else:
                            fn["arguments"] = wrapped(self.tools[name], args, complete)
                    else:
                        fn.update(name=name, arguments=args)
                    changed = True
        return changed


async def stream_lines(lines, projected, dispatcher_schemas=None):
    adapter = StreamCalls(projected, dispatcher_schemas)
    frame = []
    async for line in lines:
        if line:
            frame.append(line)
            continue
        data = "\n".join(row[5:].lstrip(" ") for row in frame if row.startswith("data:"))
        chunk = parse(data)
        if isinstance(chunk, dict) and adapter.apply(chunk):
            # Send completed arguments before the finish marker, as native Aru tools do.
            before_finish = []
            for choice in chunk.get("choices") or ():
                delta = choice.get("delta") or {}
                if choice.get("finish_reason") and (delta.get("tool_calls") or delta.get("function_call")):
                    before_finish.append({**choice, "finish_reason": None})
                    choice["delta"] = {}
            if before_finish:
                yield "data: " + wire({**chunk, "choices": before_finish, "usage": None})
                yield ""
            frame = [row for row in frame if not row.startswith("data:")] + ["data: " + wire(chunk)]
        for row in frame:
            yield row
        yield ""
        frame = []
    for row in frame:
        yield row
