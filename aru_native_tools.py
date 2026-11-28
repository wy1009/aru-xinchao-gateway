"""Present selected Aru MCP methods as ordinary OpenAI functions. Aru still executes them."""
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

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


class UnavailableToolChoice(ValueError):
    pass


def project(body, catalog, is_environment, as_text, *, single_tool=False, response_tools=None):
    tools = body.get("tools") or []
    if not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools):
        raise UnavailableToolChoice("Expected a tool definitions array.")
    originals = {t["function"].get("name"): t["function"] for t in tools
                 if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    messages = body["messages"]
    environments = [i for i, msg in enumerate(messages) if is_environment(msg)]
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and (choice.get("function") or {}).get("name") == DISPATCH:
        raise UnavailableToolChoice("Select an individual MCP function or use automatic tool choice.")
    if DISPATCH not in originals and not environments and not isinstance(choice, dict):
        return body, {}

    # Only the latest client environment can authorize projection. Older prompts
    # still need their provider-facing instructions updated after a turn reset.
    roster = ""
    if environments:
        content = as_text(messages[environments[-1]].get("content"))
        roster = next((line for line in content.splitlines() if line.startswith("mcp.tools:")), "")
    counts = re.search(r"([0-9]+) 个服务：([^。]+)", roster)
    active = {s.strip(): int(n) for s, n in re.findall(r"([^、,，]+?)\(([0-9]+)\)", counts[2])} if counts else {}
    params = originals.get(DISPATCH, {}).get("parameters") or {}
    if (DISPATCH not in originals or choice == "none" or isinstance(choice, dict)
            or not counts or len(active) != int(counts[1])
            or set(params.get("required") or ()) - {"tool", "arguments"}):
        active = {}
    selector = (params.get("properties") or {}).get("tool") or {}
    allowed = [selector["const"]] if "const" in selector else selector.get("enum")

    # A newer directory receipt takes precedence over the installed alias snapshot.
    pending, available = {}, {}
    selected, wanted = set(), set()
    last_read = None
    for msg in messages:
        if msg.get("role") == "user" and not is_environment(msg):
            selected, wanted = set(), set()
            last_read = None
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
                if (isinstance(args, dict) and isinstance(args.get("tool"), str)
                        and receipt.get("kind") == "mcp_tool_contract" and receipt.get("tool") == args["tool"]):
                    last_read = args["tool"]
                    wanted.add(last_read)

    projected = {}
    for service in catalog:
        name = service["server"]
        if name not in active:
            continue
        handles = available.get(name)
        if (len(handles) if handles is not None else service["count"]) != active[name]:
            continue
        for tool in service["tools"]:
            fn = tool["function"]
            if (handles is not None and tool["alias"] not in handles
                    or allowed is not None and tool["alias"] not in allowed
                    or fn["name"] in originals):
                continue
            # Local reply validation only; these schemas do not enter the provider request.
            if response_tools is not None:
                response_tools[fn["name"]] = tool
            if single_tool:
                expose = tool["alias"] == last_read
            else:
                expose = name in selected or tool["alias"] in wanted
            if expose:
                projected[fn["name"]] = tool

    if response_tools is not None:
        # Aru lowercases the method portion of its handles. Recover the spelling
        # from the independent function only when its suffix matches that handle.
        short_names = {}
        for tool in list(response_tools.values()):
            short = tool["alias"].split("__")[1].lower()
            short_names[short] = tool if short not in short_names else None
            if tool["alias"] not in originals:
                response_tools[tool["alias"]] = tool
        for short, tool in short_names.items():
            if tool is None:
                continue
            name = tool["function"]["name"]
            spellings = {short}
            if name.lower().endswith("_" + short):
                spellings.add(name[-len(short):])
            for spelling in spellings:
                if spelling not in originals and spelling not in response_tools:
                    response_tools[spelling] = tool

    visible = []
    for tool in tools:
        fn = tool.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name == DISPATCH:
            continue
        if name in DISCOVERY_HELP:
            tool = {**tool, "function": {**fn, "description": DISCOVERY_HELP[name]}}
        visible.append(tool)
    visible.extend({"type": "function", "function": t["function"]} for t in projected.values())
    history = provider_history(messages, projected, catalog)
    for idx in environments:
        history[idx] = provider_environment(history[idx], {} if choice == "none" else originals, single_tool, len(projected))

    out = {**body, "messages": compact_dispatch_history(history)}
    if isinstance(choice, dict) and choice.get("type") == "allowed_tools":
        limit = choice.get("allowed_tools", choice)
        permitted = [tool for tool in limit.get("tools", [])
                     if (tool.get("function") or {}).get("name") != DISPATCH]
        if permitted:
            limit = {**limit, "tools": permitted}
            out["tool_choice"] = {**choice, "allowed_tools": limit} if "allowed_tools" in choice else limit
        elif limit.get("mode") == "required":
            raise UnavailableToolChoice("No individual function is permitted for the required tool call.")
        else:
            out["tool_choice"] = "none"
    if visible:
        out["tools"] = visible
    elif "tools" in body:
        out.pop("tools")
        if choice == "required":
            raise UnavailableToolChoice("No individual function is available for the required tool call.")
        if out.get("tool_choice") in ("auto", "none"):
            out.pop("tool_choice")
    return out, projected


DISCOVERY_HELP = {
    "listMCPTools": "不带参数时列出服务。选定服务后用 server 列出它的工具；回执里 call_as 是下一步可直接调用的函数名。",
    "readMCPTool": "把工具目录条目的 tool 原样传入，读取并展开该方法。下一步按回执的 call_as 调用。",
}


def provider_environment(message, originals, single_tool, expanded_count):
    steps = [f"本轮已展开 {expanded_count} 个外部方法。"]
    if "listMCPTools" in originals:
        steps.append("已知准确的独立函数名和业务参数时，可直接发起工具调用。需要确认名称或参数时，用 listMCPTools 的 server 参数打开目标服务。")
    if "readMCPTool" in originals:
        steps.append("已有目录条目的完整 tool 标识时，可直接用 readMCPTool 单独展开。标识从目录照抄。")
        if single_tool:
            steps.append("本次目录过大，请只展开所需的一项。")
        steps.append("遇到工具名或参数错误，请查目录后按回执调用。")
    steps.append("方法已在本轮 tools 时，直接用独立函数名填业务参数。")

    def update(match):
        header = match[0].split("。", 1)[0]
        if DISPATCH in header:
            header = "mcp.tools: MCP 工具目录"
        return header + "。" + "".join(steps)

    content = message["content"]
    # is_environment accepts only a string or a single text part.
    part = content[0] if isinstance(content, list) else None
    text = part["text"] if part else content
    rewritten = re.sub(r"(?m)^mcp\.tools:[^\r\n]*", update, text)
    if rewritten == text:
        return message
    return {**message, "content": [{**part, "text": rewritten}] if part else rewritten}


def exceeds_provider_limit(raw):
    text = raw[:16384].decode("utf-8", errors="replace").lower()
    return any(term in text for term in ("context length", "context_length", "context window",
        "too many tools", "maximum number of tools", "too many functions", "at most 128",
        "maximum context", "context size", "context_length_exceeded", "工具数量超过"))


def provider_history(messages, projected, catalog):
    by_alias = {t["alias"]: t["function"]["name"] for service in catalog for t in service["tools"]}
    current = {t["alias"]: name for name, t in projected.items()}
    renamed, discovery, history = {}, {}, []
    for msg in messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list):
            calls = []
            for call in msg["tool_calls"]:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and fn.get("name") in DISCOVERY_HELP and call.get("id"):
                    discovery[call["id"]] = fn["name"]
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
        elif msg.get("role") == "tool" and msg.get("tool_call_id") in discovery:
            method = discovery.pop(msg["tool_call_id"])
            receipt = parse(msg.get("content"))
            if isinstance(receipt, dict):
                kind = receipt.get("kind")
                if method == "listMCPTools" and kind == "mcp_server_directory" and isinstance(receipt.get("items"), list):
                    for item in receipt["items"]:
                        if isinstance(item, dict) and isinstance(item.get("handle"), str):
                            item["next_call"] = {"name": "listMCPTools", "arguments": {"server": item["handle"]}}
                    receipt["usage"] = "这是服务目录。选所需服务，调用该行的 next_call，取得工具目录。"
                elif method == "listMCPTools" and kind == "mcp_tool_directory" and isinstance(receipt.get("items"), list):
                    for item in receipt["items"]:
                        if isinstance(item, dict) and isinstance(item.get("tool"), str) and item["tool"] in current:
                            item["call_as"] = current[item["tool"]]
                    receipt["usage"] = "直接调用已列入本轮 tools 的 call_as。需要单项参数定义时，将该行的 tool 原样交给 readMCPTool。"
                elif method == "readMCPTool" and kind == "mcp_tool_contract" and isinstance(receipt.get("tool"), str) and receipt["tool"] in current:
                    receipt["call_as"] = current[receipt["tool"]]
                    receipt["usage"] = "直接调用 call_as，只填写业务参数。"
                elif method == "readMCPTool" and isinstance(receipt.get("error"), dict) and receipt["error"].get("code") == "readMCPTool.not_found":
                    receipt["usage"] = "工具标识未找到。用 listMCPTools 的 server 参数列出目标服务，再照抄对应行的 tool 或调用已展开的 call_as。"
                else:
                    receipt = None
                if receipt is not None:
                    msg = {**msg, "content": json.dumps(receipt, ensure_ascii=False)}
        elif msg.get("role") == "tool" and msg.get("name") == DISPATCH and msg.get("tool_call_id") in renamed:
            msg = {**msg, "name": renamed[msg["tool_call_id"]]}
        history.append(msg)
    return history


EXCLUDED_RESULT = "[This tool result was excluded by the user. Its original content is not included.]"


def compact_dispatch_history(messages):
    """Omit matched redactions and summarize confirmed rejections in the provider copy only."""
    simplified = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        i += 1
        calls = msg.get("tool_calls")
        if msg.get("role") != "assistant" or not isinstance(calls, list):
            simplified.append(msg)
            continue
        start = i
        while i < len(messages) and messages[i].get("role") == "tool":
            i += 1
        replies = messages[start:i]
        by_id = {reply.get("tool_call_id"): reply for reply in replies}
        if len(by_id) != len(replies) or len({c.get("id") for c in calls if isinstance(c, dict)}) != len(calls):
            simplified.append(msg)
            simplified.extend(replies)
            continue
        omitted = set()
        rejected = False
        for call in calls:
            if not isinstance(call, dict) or (call.get("function") or {}).get("name") != DISPATCH:
                continue
            ident = call.get("id")
            if not ident or ident not in by_id:
                continue
            result = by_id[ident].get("content")
            if result == EXCLUDED_RESULT and parse(call["function"].get("arguments")) == {}:
                omitted.add(ident)
                continue
            receipt = parse(result)
            if (isinstance(receipt, dict) and set(receipt) <= {"error", "status"}
                    and isinstance(receipt.get("error"), dict)
                    and receipt["error"].get("code") == "invokeMCPTool.invalid_arguments"):
                rejected = True
                omitted.add(ident)
        if omitted:
            msg = {**msg}
            remaining = [c for c in calls if not isinstance(c, dict) or c.get("id") not in omitted]
            if remaining:
                msg["tool_calls"] = remaining
            else:
                del msg["tool_calls"]
            if rejected:
                note = "〔有一次 MCP 调用参数校验失败，该工具未执行。〕"
                content = msg.get("content")
                msg["content"] = ([*content, {"type": "text", "text": note}] if isinstance(content, list)
                                  else ((content or "") + "\n" + note).lstrip("\n"))
        if not omitted or msg.get("tool_calls") or msg.get("content") or msg.get("refusal") or msg.get("function_call"):
            simplified.append(msg)
        simplified.extend(reply for reply in replies if reply.get("tool_call_id") not in omitted)
    return simplified


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


def known_call(name, raw, permitted):
    """Resolve one completed structured call against the client's eligible methods."""
    if not permitted or not isinstance(name, str):
        return None
    tool = permitted.get(name)
    if name != DISPATCH and tool is None:
        return None
    if raw is not None and not isinstance(raw, str):
        return None
    if raw is None or not raw.strip():
        args = {}
    else:
        try:
            args = json.loads(raw, object_pairs_hook=dsml._unique, parse_constant=dsml._invalid_constant,
                              parse_float=dsml._finite_float)
        except (ValueError, RecursionError):
            args = raw
    if name == DISPATCH:
        # Some models send the short method and its business fields to the old
        # dispatcher. Resolve only an exact unique external method and never
        # reinterpret a normal Aru wrapper or an explicitly scoped request.
        if not isinstance(args, dict):
            return None
        method = args.pop("function", None)
        if not isinstance(method, str) or {"server", "tool"} & args.keys():
            return None
        tool = permitted.get(method)
        if set(args) == {"arguments"}:
            args = args["arguments"]
    if not tool:
        return None
    schema = tool["function"]["parameters"]
    if dsml.tagged(args):
        args = dsml.recover_parameters(args, tool["alias"], schema)
    if not isinstance(args, dict):
        return None
    if tool["function"]["name"] == "serein_openMemorySource" and "sourceId" in args:
        if "id" in args or "sourceId" in schema.get("properties", {}):
            return None
        args["id"] = args.pop("sourceId")
    try:
        if not Draft202012Validator(schema, registry=Registry()).is_valid(args):
            return None
    except (Unresolvable, ValueError, RecursionError):
        return None
    return wire({"tool": tool["alias"], "arguments": args})


def outbound_arguments(name, raw, complete, projected, permitted):
    tool = projected.get(name)
    # Projected functions keep their existing DSML handling. The one known
    # read-card spelling is accepted on both projected and unexpanded calls.
    if tool and name != "serein_openMemorySource":
        return wrapped(tool, raw, complete)
    if complete:
        result = known_call(name, raw, permitted)
        if result is not None:
            return result
    if tool:
        return wrapped(tool, raw, complete)
    if name in permitted:
        return wire({"tool": permitted[name]["alias"], "arguments": None})
    return None


def restore(chunk, projected, permitted=None):
    changed = False
    permitted = permitted or {}
    for choice in chunk.get("choices") or ():
        complete = choice.get("finish_reason") in ("stop", "tool_calls", "function_call")
        for _, fn in dsml.functions_in_choice(choice, "message"):
            name = fn.get("name")
            if name != DISPATCH and name not in projected and name not in permitted:
                continue
            args = outbound_arguments(name, fn.get("arguments"), complete, projected, permitted)
            if args is not None:
                fn.update(name=DISPATCH, arguments=args)
                changed = True
    return changed


class StreamCalls:
    """Assemble MCP arguments per choice and call; let ordinary text and other tools stream."""
    def __init__(self, projected, dispatcher_schemas=None, permitted=None):
        self.tools = projected
        self.permitted = permitted or {}
        self.dispatcher_schemas = dispatcher_schemas
        self.names = set(projected) | self.permitted.keys()
        if dispatcher_schemas is not None or self.permitted:
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
                        result = outbound_arguments(name, args, complete, self.tools, self.permitted)
                        if result is None and name == DISPATCH and complete:
                            result = dsml.corrected_arguments(args, self.dispatcher_schemas)
                        fn["arguments"] = result if result is not None else args
                    else:
                        fn.update(name=name, arguments=args)
                    changed = True
        return changed


async def stream_lines(lines, projected, dispatcher_schemas=None, permitted=None):
    adapter = StreamCalls(projected, dispatcher_schemas, permitted)
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
