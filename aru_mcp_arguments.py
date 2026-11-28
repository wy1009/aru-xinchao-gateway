"""Decode DSML only where a structured MCP tool call expects an argument object."""
import json
import logging
import math
import re


_WRAPPER = 'invokeMCPTool'
_ALIAS = re.compile(r'mcp__[A-Za-z0-9_-]+__[a-f0-9]{32}', re.I)
_PREFIXES = ('<｜DSML｜parameter', '<|DSML|parameter')
_OPEN = re.compile(r'<([｜|])DSML\1parameter\s+name="([^"<>\s]+)"\s+string="(true|false)"\s*>')
_MARKER = re.compile(r'</?[｜|]DSML[｜|]')
_MAX_BUFFER = 128 * 1024
log = logging.getLogger("uvicorn.error")


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


def tagged(text):
    return isinstance(text, str) and text.lstrip().startswith(_PREFIXES)


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _parameters(text):
    """Read explicit fields in order. A finished provider call may omit its final closing tag."""
    values = {}
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        opening = _OPEN.match(text, pos)
        if not opening:
            raise ValueError("Malformed DSML parameter")
        bar, field, literal = opening.groups()
        if field in values:
            raise ValueError("Duplicate DSML parameter")
        marker = _MARKER.search(text, opening.end())
        end = marker.start() if marker else len(text)
        value = text[opening.end():end]
        if marker:
            closing = f'</{bar}DSML{bar}parameter>'
            if not text.startswith(closing, end):
                raise ValueError("Ambiguous DSML parameter boundary")
            pos = end + len(closing)
        else:
            # Reject a clipped closing delimiter, instead of appending it to the value.
            if re.search(r'</?[｜|][^<>]*$', value):
                raise ValueError("Incomplete DSML delimiter")
            pos = end
        values[field] = value if literal == 'true' else json.loads(
            value, object_pairs_hook=_unique, parse_constant=_invalid_constant, parse_float=_finite_float)
    return values


def recover_parameters(text, alias, schema=None):
    if not tagged(text):
        return None
    try:
        values = _parameters(text)
    except (ValueError, RecursionError):
        return None
    # Some providers echo the dispatcher handle as the only argument of a no-input method.
    method = alias.split('__')[1].lower() if _ALIAS.fullmatch(alias) else ''
    if schema is None and method == 'list_materials':
        schema = {'properties': {}, 'additionalProperties': False}
    properties = (schema or {}).get('properties', {})
    closed = schema is not None and schema.get('additionalProperties') is False and not schema.get('patternProperties')
    if values == {'tool': alias} and not properties and (method == 'list_materials' or closed):
        values = {}
    elif closed and set(values) == {'arguments'} and 'arguments' not in properties and isinstance(values['arguments'], dict):
        values = values['arguments']
    if schema is not None and not set(schema.get('required', ())).issubset(values):
        return None
    if closed and values.keys() - properties.keys():
        return None
    field = {'search_library': 'query', 'openmemorysource': 'id'}.get(method)
    if field and (not isinstance(values.get(field), str) or not values[field].strip()):
        return None
    return values


def corrected_arguments(raw, schemas=None):
    """Restore the inner object before forwarding the completed call to Aru."""
    if not isinstance(raw, str):
        return None
    outer_tagged = tagged(raw)
    try:
        obj = _parameters(raw) if outer_tagged else json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.keys() != {'tool', 'arguments'}:
        return None
    alias, inner = obj['tool'], obj['arguments']
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
        return None
    if tagged(inner):
        inner = recover_parameters(inner, alias, (schemas or {}).get(alias))
        if inner is None:
            return None
    elif not (outer_tagged and isinstance(inner, dict)):
        return None
    obj['arguments'] = inner
    log.info('MCP DSML parameters restored: dispatcher')
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def functions_in_choice(choice, field):
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


def repair(chunks, field, schemas=None):
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
            for call_index, fn in functions_in_choice(choice, field):
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
        value = corrected_arguments(''.join(fn['arguments'] for _, fn in args), schemas)
        if value is not None:
            for pos, fn in args:
                changed.add(pos)
                fn['arguments'] = ''
            args[-1][1]['arguments'] = value
    return changed


def _flush(frames, chunks, schemas=None):
    changed = repair(chunks, 'delta', schemas)
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


async def stream_lines(lines, choices=1, schemas=None):
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
            for call, fn in functions_in_choice(choice, 'delta'):
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
                for row in _flush(frames, chunks, schemas):
                    yield row
                frames, chunks, size = [], [], 0
        else:
            for row in frame:
                yield row
        frame = []
    for row in _flush(frames, chunks, schemas):
        yield row
    for row in frame:
        yield row
