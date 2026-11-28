import json
import unittest
from unittest.mock import patch

import aru_mcp_arguments as adapter


def alias(name):
    return 'mcp__' + name + '__' + 'f2' * 16


def dsml(field, value):
    return f'<｜DSML｜parameter name="{field}" string="true">{value}'


def args(tool, inner):
    return json.dumps({'arguments': inner, 'tool': tool}, ensure_ascii=False)


def call(name, arguments, **extra):
    return {'function': {'name': name, 'arguments': arguments}, **extra}


async def lines(rows):
    for row in rows:
        yield row


def sse(chunk):
    return ['data: ' + json.dumps(chunk, ensure_ascii=False), '']


class MCPArguments(unittest.IsolatedAsyncioTestCase):
    def test_repair_complete_nonstreaming_calls(self):
        for name, field, value, expected in [
            ('list_materials', 'tool', alias('list_materials'), {}),
            ('search_library', 'query', '原词 "小狗"\n旧称呼', {'query':'原词 "小狗"\n旧称呼'}),
            ('openmemorysource', 'id', 'raw:81', {'id':'raw:81'}),
        ]:
            with self.subTest(name=name):
                tool = alias(name)
                fn = call('invokeMCPTool', args(tool, dsml(field, value)), id='call-one')
                choice = {'message': {'tool_calls':[fn], 'content':None}, 'finish_reason':'tool_calls'}
                self.assertEqual(adapter.repair([{'choices':[choice]}], 'message'), {0})
                self.assertEqual(json.loads(fn['function']['arguments']), {'tool':tool, 'arguments':expected})
                self.assertEqual(fn['id'], 'call-one')

    def test_ambiguous_incomplete_and_write_calls_pass_through(self):
        tool = alias('list_materials')
        bad = args(tool, dsml('tool', tool))
        for raw in (args(tool, {}), args(tool, dsml('tool', alias('other'))),
                    args(tool, dsml('tool',tool) + dsml('id','extra')), args(tool, dsml('id', 'wrong')),
                    args(alias('writeInformationCard'), dsml('subject', 'title')),
                    args(alias('search_library'), dsml('query','') ),
                    bad.replace('"arguments":', '"arguments":{}, "arguments":'),
                    bad.replace('"arguments":', '"extra":1,"arguments":')):
            self.assertIsNone(adapter.corrected_arguments(raw, {alias('writeInformationCard'): {'required': ['subject', 'content']}}))
        for name, finish in [('invokeMCPTool', 'length'), ('other', 'tool_calls')]:
            fn = call(name, bad)
            self.assertFalse(adapter.repair([{'choices':[{'message':{'tool_calls':[fn]},
                                                         'finish_reason':finish}]}], 'message'))
            self.assertEqual(fn['function']['arguments'], bad)

    def test_proactive_prefix_on_other_tools_and_typed_fields(self):
        target = alias('calendar_lookup')
        inner = ('  <｜DSML｜parameter name="query" string="true">a < b & c</｜DSML｜parameter>\n'
                 '<｜DSML｜parameter name="limit" string="false">12</｜DSML｜parameter>'
                 '<｜DSML｜parameter name="opts" string="false">{"more":false}')
        expected = {'query': 'a < b & c', 'limit': 12, 'opts': {'more': False}}
        for text in (inner, inner.replace('｜', '|')):
            with self.subTest(text=text):
                wire = adapter.corrected_arguments(args(target, text))
                self.assertEqual(json.loads(wire), {'tool': target, 'arguments': expected})
        outer = (dsml('tool', target) + '</｜DSML｜parameter>' +
                 '<｜DSML｜parameter name="arguments" string="false">{"limit":2}')
        self.assertEqual(json.loads(adapter.corrected_arguments(outer))['arguments'], {'limit': 2})

    def test_recognizes_only_the_object_slot_and_preserves_literal_text(self):
        target = alias('calendar_lookup')
        example = dsml('query', 'abc')
        for value in ({'query': example}, 'example: ' + example, example + '</｜DSML｜par',
                      example + '</｜DSML｜parameter>' + dsml('query', 'again'),
                      '<｜DSML｜parameter name="limit" string="false">true or false'):
            self.assertIsNone(adapter.corrected_arguments(args(target, value)))

    async def test_fragmented_interleaved_sse_keeps_call_ids_usage_and_choices(self):
        tool = alias('search_library')
        wire = args(tool, dsml('query','日常回忆'))
        foreign = call('other_tool',wire,index=1,id='foreign')
        sequence = [
            {'choices':[{'index':0,'delta':{'content':'工具前的文字'}}]},
            {'choices':[{'index':0,'delta':{'tool_calls':[call('invokeM',wire[:11],index=0,id='first'),foreign]}}]},
            {'choices':[{'index':1,'delta':{'tool_calls':[call('ordinary',wire,index=0)]},'finish_reason':'tool_calls'}]},
            {'choices':[{'index':0,'delta':{'tool_calls':[call('CPTool',wire[11:],index=0)]}}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]},
            {'choices':[],'usage':{'total_tokens':31}},
        ]
        rows = [': heartbeat', '', *sse(sequence[0]), 'id: frame-two']
        for chunk in sequence[1:]:
            rows.extend(sse(chunk))
        rows.extend(['data: [DONE]', ''])
        received = [line async for line in adapter.stream_lines(lines(rows), choices=2)]
        self.assertEqual(received[:5], rows[:5])
        self.assertEqual(received[-4:], rows[-4:])
        chunks = [json.loads(line[6:]) for line in received if line.startswith('data: {')]
        first = chunks[1]['choices'][0]['delta']['tool_calls']
        rest = chunks[3]['choices'][0]['delta']['tool_calls'][0]
        self.assertEqual(first[0]['id'],'first')
        self.assertEqual(first[1],foreign)
        self.assertEqual(chunks[2], sequence[2])
        self.assertEqual(json.loads(first[0]['function']['arguments'] + rest['function']['arguments']),
                         {'tool':tool,'arguments':{'query':'日常回忆'}})

    async def test_unfinished_stream_and_buffer_limit_preserve_original_frames(self):
        tool = alias('list_materials')
        fn = call('invokeMCPTool', args(tool, dsml('tool', tool)))
        rows = sse({'choices':[{'delta':{'tool_calls':[fn]}}]})
        self.assertEqual([x async for x in adapter.stream_lines(lines(rows))], rows)
        with patch.object(adapter, '_MAX_BUFFER', 10):
            full = rows + sse({'choices':[{'delta':{},'finish_reason':'tool_calls'}]}) + ['data: [DONE]', '']
            self.assertEqual([x async for x in adapter.stream_lines(lines(full))], full)
