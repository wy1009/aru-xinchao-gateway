"""Recover unambiguous V3.2 parameter tags inside Aru's structured MCP dispatcher."""
import json
import re


_WRAPPER = 'invokeMCPTool'
_ALIAS = re.compile(r'mcp__(list_materials|openmemorysource|search_library)__[a-f0-9]{32}', re.I)
_TAG = re.compile(r'<([｜|])DSML\1parameter name="(tool|query|id)" string="true">([^<>]+)(?:</\1DSML\1parameter>)?')
_MAX_BUFFER = 128 * 1024


def advertised(body):
    for tool in body.get('tools') or ():
        fn = tool.get('function') if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get('name') == _WRAPPER:
            return True
    return any(isinstance(fn, dict) and fn.get('name') == _WRAPPER for fn in body.get('functions') or ())


def _unique(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError('Ambiguous arguments')
    return values


def corrected_arguments(raw):
    """Return replacement wire JSON only when every recovered value is explicit."""
    if not isinstance(raw, str) or len(raw) > _MAX_BUFFER or 'DSML' not in raw:
        return None
    try:
        obj = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.keys() != {'tool', 'arguments'}:
        return None
    alias, inner = obj['tool'], obj['arguments']
    if not isinstance(alias, str) or not isinstance(inner, str):
        return None
    target, tag = _ALIAS.fullmatch(alias), _TAG.fullmatch(inner)
    if not target or not tag:
        return None
    name, field, value = target[1].lower(), tag[2], tag[3]
    if name == 'list_materials' and field == 'tool' and value == alias:
        obj['arguments'] = {}
    elif (name, field) in {('search_library', 'query'), ('openmemorysource', 'id')} and value.strip():
        obj['arguments'] = {field: value}
    else:
        return None
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def _functions(choice, field):
    message = choice.get(field)
    if not isinstance(message, dict):
        return
    if isinstance(message.get('function_call'), dict):
        yield -1, message['function_call']
    for pos, call in enumerate(message.get('tool_calls') or []):
        if isinstance(call, dict) and isinstance(call.get('function'), dict):
            index = call.get('index', pos)
            if type(index) is int and index >= 0:
                yield index, call['function']


def repair(chunks, field):
    """Patch argument fragments in place, keeping parallel choices and calls distinct."""
    parts = {}
    finished = {}
    changed = set()
    for pos, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        if 'error' in chunk:
            return changed
        for choice in chunk.get('choices') or []:
            index = choice.get('index', 0)
            if choice.get('finish_reason'):
                finished[index] = choice['finish_reason']
            for call_index, fn in _functions(choice, field):
                parts.setdefault((index, call_index), []).append((pos, fn))
    for (index, _), fragments in parts.items():
        if finished.get(index) not in ('stop', 'tool_calls', 'function_call'):
            continue
        names = [fn['name'] for _, fn in fragments if fn.get('name') is not None]
        if not all(isinstance(name, str) for name in names) or ''.join(names) != _WRAPPER:
            continue
        args = [(pos, fn) for pos, fn in fragments if fn.get('arguments') is not None]
        if not args or not all(isinstance(fn['arguments'], str) for _, fn in args):
            continue
        value = corrected_arguments(''.join(fn['arguments'] for _, fn in args))
        if value is not None:
            for pos, fn in args:
                changed.add(pos)
                fn['arguments'] = ''
            args[-1][1]['arguments'] = value
    return changed


def _flush(frames, chunks):
    changed = repair(chunks, 'delta')
    for pos, frame in enumerate(frames):
        if pos not in changed:
            yield from frame
            continue
        emitted = False
        for line in frame:
            if not line.startswith('data:'):
                yield line
            elif not emitted:
                yield 'data: ' + json.dumps(chunks[pos], ensure_ascii=False, separators=(',', ':'))
                emitted = True


async def stream_lines(lines, choices=1):
    """Keep ordinary SSE live. Stage tool frames only until a complete call can be checked."""
    frames, chunks, frame = [], [], []
    size = 0
    finished = set()
    names = {}
    async for line in lines:
        frame.append(line)
        if line:
            continue
        event = '\n'.join(row[5:].lstrip(' ') for row in frame if row.startswith('data:'))
        try:
            chunk = json.loads(event) if event and event != '[DONE]' else None
        except (ValueError, RecursionError):
            chunk = None
        items = (chunk.get('choices') or []) if isinstance(chunk, dict) else []
        possible = False
        for choice in items:
            index = choice.get('index', 0)
            if choice.get('finish_reason'):
                finished.add(index)
            for call, fn in _functions(choice, 'delta'):
                key = (index, call)
                fragment = fn.get('name') or ''
                if isinstance(fragment, str):
                    names[key] = names.get(key, '') + fragment
                    possible |= _WRAPPER.startswith(names[key])
        if frames or possible:
            frames.append(frame)
            chunks.append(chunk)
            size += sum(len(row) + 1 for row in frame)
            if size > _MAX_BUFFER or (event and chunk is None and event != '[DONE]'):
                for original in frames:
                    for row in original:
                        yield row
                async for row in lines:
                    yield row
                return
            if len(finished) >= choices or event == '[DONE]':
                for row in _flush(frames, chunks):
                    yield row
                frames, chunks, size = [], [], 0
        else:
            for row in frame:
                yield row
        frame = []
    for row in _flush(frames, chunks):
        yield row
    for row in frame:
        yield row
