import copy
import json
import unittest

import aru_native_tools as native

from gateway import as_text, is_aru_environment


def flat(name, required=()):
    alias = "mcp__" + name + "__" + "a" * 32
    schema = {"type": "object", "properties": {key: {"type": "string"} for key in required}}
    if required:
        schema["required"] = list(required)
    return {"alias": alias, "function": {"name": "serein_" + name, "parameters": schema}}


def builtin(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def call(name, args, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": args}}


def env(roster="Serein(2)"):
    text = "<environment>\naru.feature_slice: 1\nmcp.tools: 本轮 MCP 目录包含 1 个服务：" + roster + "。本轮可调用 invokeMCPTool。\n</environment>"
    return {"role": "user", "content": text}


class NativeTools(unittest.TestCase):
    def setUp(self):
        self.search, self.listing = flat("search", ["query"]), flat("list")
        self.catalog = [{"server": "Serein", "count": 2, "tools": [self.search, self.listing]}]
        self.body = {"tools": [builtin(n) for n in ("local", "listMCPTools", "readMCPTool", native.DISPATCH)],
                     "messages": [{"role":"user","content":"列目录"}, {"role":"assistant",
                         "tool_calls":[call("listMCPTools", "{}", "dir")]}, {"role":"tool","tool_call_id":"dir",
                         "content":json.dumps({"kind":"mcp_tool_directory","scope":"server","complete":True,
                            "server":{"name":"Serein"},"items":[{"tool":t["alias"]} for t in (self.search,self.listing)]})},
                         env()]}

    def project(self):
        return native.project(self.body, self.catalog, is_aru_environment, as_text)

    def test_real_messages_both_ways_and_original_unchanged(self):
        fn = self.search["function"]["name"]
        original = call(native.DISPATCH, native.wrapped(self.search, '{"query":"小窝"}'), "A")
        self.body["messages"][:0] = [{"role": "assistant", "tool_calls": [original], "reasoning_content": "ok"},
                                     {"role": "tool", "name": native.DISPATCH, "tool_call_id": "A", "content": "ok"}]
        before = copy.deepcopy(self.body)
        out, mapping = self.project()
        self.assertEqual(set(mapping), {"serein_search", "serein_list"})
        self.assertEqual([t["function"]["name"] for t in out["tools"]], ["local", "listMCPTools", "readMCPTool", "serein_search", "serein_list"])
        self.assertEqual(out["messages"][0]["tool_calls"][0]["function"]["name"], fn)
        self.assertEqual(out["messages"][1]["name"], fn)
        self.assertNotIn(native.DISPATCH, out["messages"][-1]["content"])
        self.assertEqual(self.body, before)
        result = {"choices": [{"message": {"tool_calls": [call(fn, '{"query":"小窝"}', "A")]},
                               "finish_reason": "tool_calls"}]}
        self.assertTrue(native.restore(result, mapping))
        self.assertEqual(result["choices"][0]["message"]["tool_calls"], [original])

    def test_respects_roster_choices_collisions_and_directory(self):
        self.body["messages"].append(env("Disabled(2)"))
        self.assertEqual(self.project()[1], {})
        self.body["messages"].pop()
        self.body["tool_choice"] = {"type": "function", "function": {"name": native.DISPATCH}}
        self.assertEqual(self.project()[1], {})
        del self.body["tool_choice"]
        self.body["tools"].append(builtin("serein_search"))
        out, mapping = self.project()
        self.assertEqual(set(mapping), {"serein_list"})
        self.assertIn(native.DISPATCH, [t["function"]["name"] for t in out["tools"]])
        del self.body["tools"][-1]
        receipt = {"kind":"mcp_tool_directory", "scope":"server", "complete":True, "server":{"name":"Serein"},
                   "items":[{"tool":self.listing["alias"]},{"tool":"changed"}]}
        self.body["messages"][-1:-1] = [
            {"role":"assistant","tool_calls":[call("listMCPTools", "{}", "list-1")]},
            {"role":"tool","tool_call_id":"list-1","content":json.dumps(receipt)}]
        self.assertEqual(set(self.project()[1]), {"serein_list"})
        self.body["messages"][-2]["tool_call_id"]="wrong"
        self.assertEqual(len(self.project()[1]),2)
        self.body["messages"] = [env("Serein(3)")]
        self.assertEqual(self.project()[1], {})

    def test_first_round_known_and_new_server(self):
        self.body["messages"]=[env()]
        out,mapping=self.project()
        self.assertFalse(mapping)
        self.assertEqual([t["function"]["name"] for t in out["tools"]], ["local","listMCPTools","readMCPTool"])
        self.assertIn("Serein(2)", out["messages"][0]["content"])
        self.body["messages"]=[env("new(7)")]
        self.assertIs(self.project()[0], self.body)

    def test_malformed_never_redirects_no_args_and_interruption(self):
        bad = '{},"tool":"changed","arguments":{}'
        for raw,tool,expected in [(bad,self.search,None),("",self.search,None),("",self.listing,{}),
                                  ('"text"',self.listing,None),('{"query":"abc"}',self.search,{"query":"abc"})]:
            self.assertEqual(json.loads(native.wrapped(tool,raw)),{"tool":tool["alias"], "arguments":expected})
        self.assertIsNone(json.loads(native.wrapped(self.listing, "{}", complete=False))["arguments"])

    def test_completed_tagged_arguments_are_restored_before_forwarding(self):
        projected = {self.search["function"]["name"]: self.search}
        tag = '<｜DSML｜parameter name="query" string="true">天气'
        expected = {"tool": self.search["alias"], "arguments": {"query": "天气"}}
        for raw in (tag, json.dumps(tag), json.dumps({"query": tag})):
            tc = call("serein_search", raw, "A")
            result = {"choices": [{"message": {"tool_calls": [tc]}, "finish_reason": "tool_calls"}]}
            native.restore(result, projected)
            recovered = json.loads(tc["function"]["arguments"])
            self.assertEqual(recovered, expected if raw != json.dumps({"query":tag})
                             else {**expected,"arguments":{"query":tag}})
            self.assertEqual(tc["id"], "A")

        stream = native.StreamCalls(projected, {self.search["alias"]: self.search["function"]["parameters"]})
        raw_wrapper = native.wire({"tool":self.search["alias"], "arguments":tag})
        def feed(delta, finish=None):
            chunk = {"choices":[{"delta":delta, "finish_reason":finish}]}
            stream.apply(chunk)
            return delta.get("tool_calls", [])
        original = feed({"tool_calls":[{"id":"A","index":0,"function":{"name":"serein_search","arguments":tag[:4]}},
                                        {"id":"B","index":1,"function":{"name":native.DISPATCH,"arguments":raw_wrapper[:8]}}]})
        self.assertEqual([i["id"] for i in original], ["A","B"])
        feed({"tool_calls":[{"index":0,"function":{"arguments":tag[4:]}},
                            {"index":1,"function":{"arguments":raw_wrapper[8:]}}]})
        completed = feed({}, "tool_calls")
        self.assertEqual([json.loads(i["function"]["arguments"]) for i in completed], [expected, expected])

    def test_stream_fragmented_parallel_mixed_and_long_arguments(self):
        _, m = self.project()
        stream=native.StreamCalls(m)
        parts={}
        def feed(items, reason=None, choice=0):
            chunk={"choices":[{"index":choice, "delta":{"tool_calls":items} if items else {}, "finish_reason":reason}]}
            stream.apply(chunk)
            for tc in chunk["choices"][0]["delta"].get("tool_calls",[]):
                f=tc["function"]
                name,args=parts.get((choice, tc["index"]), ("",""))
                parts[choice,tc["index"]] = name + f.get("name",""), args + f.get("arguments","")
        def frag(i, **fields):
            return {"index":i,"function":fields}
        value="字" * 140000
        feed([frag(0,name="sere"),frag(1,name="serein_list",arguments=""),frag(2,name="local",arguments="{}")])
        feed([frag(0,name="in_search",arguments='{"query":"'),frag(1,arguments="{}")])
        feed([frag(0,arguments=value),frag(0,arguments='"}')])
        feed([frag(0,name="serein_search",arguments="{}")],choice=1)
        feed([],reason="length",choice=1)
        feed([],reason="tool_calls")
        self.assertEqual(parts[0,2],("local","{}"))
        self.assertEqual(parts[0,0][0],native.DISPATCH)
        self.assertEqual(json.loads(parts[0,0][1])["arguments"],{"query":value})
        self.assertEqual(json.loads(parts[0,1][1])["arguments"],{})
        self.assertIsNone(json.loads(parts[1,0][1])["arguments"])
