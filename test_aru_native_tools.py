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


def directory_fixture():
    search, listing = flat("search", ["query"]), flat("list")
    catalog = [{"server": "Serein", "count": 2, "tools": [search, listing]}]
    body = {"tools": [builtin(n) for n in ("local", "listMCPTools", "readMCPTool", native.DISPATCH)],
                 "messages": [{"role":"user","content":"列目录"}, {"role":"assistant",
                     "tool_calls":[call("listMCPTools", "{}", "dir")]}, {"role":"tool","tool_call_id":"dir",
                     "content":json.dumps({"kind":"mcp_tool_directory","scope":"server","complete":True,
                        "server":{"name":"Serein"},"items":[{"tool":t["alias"]} for t in (search,listing)]})},
                     env()]}
    return search, listing, catalog, body


class NativeTools(unittest.TestCase):
    def setUp(self):
        self.search, self.listing, self.catalog, self.body = directory_fixture()

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
        with self.assertRaises(native.UnavailableToolChoice):
            self.project()
        del self.body["tool_choice"]
        self.body["tools"].append(builtin("serein_search"))
        out, mapping = self.project()
        self.assertEqual(set(mapping), {"serein_list"})
        self.assertNotIn(native.DISPATCH, [t["function"]["name"] for t in out["tools"]])
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
        out, mapping = self.project()
        self.assertFalse(mapping)
        self.assertNotIn(native.DISPATCH, json.dumps(out))

    def test_all_prompt_copies_and_discovery_only_retry(self):
        earlier = env("Unknown(4)")
        earlier["content"] = [{"type": "text", "text": earlier["content"]}]
        self.body["messages"].insert(0, earlier)
        self.body["tools"][1]["function"]["description"] = "Call invokeMCPTool after listing."
        for catalog in ([], self.catalog):
            for choice in ("auto", "none", {"type":"function", "function":{"name":"local"}}):
                self.body["tool_choice"] = choice
                out, _ = native.project(self.body, catalog, is_aru_environment, as_text)
                self.assertNotIn(native.DISPATCH, json.dumps(out))
        del self.body["tool_choice"]
        def compact():
            return native.project(self.body, self.catalog, is_aru_environment, as_text, single_tool=True)
        self.assertEqual(compact()[1], {})
        self.body["messages"] += [
            {"role":"assistant","tool_calls":[call("readMCPTool",json.dumps({"tool":self.search["alias"]}),"read")]},
            {"role":"tool","tool_call_id":"read","content":json.dumps({"kind":"mcp_tool_contract","tool":self.search["alias"]})}]
        out, mapping = compact()
        self.assertEqual(set(mapping), {"serein_search"})
        self.assertNotIn(native.DISPATCH, json.dumps(out))
        self.body["messages"].append({"role":"user","content":"再查一次"})
        self.assertEqual(self.project()[1], {})
        self.assertEqual(compact()[1], {})

    def test_single_discovery_after_new_turn(self):
        self.body["messages"] += [
            {"role":"user","content":"看看详情"},
            {"role":"assistant","tool_calls":[call("readMCPTool",json.dumps({"tool":self.search["alias"]}),"r")]},
            {"role":"tool","tool_call_id":"r","content":json.dumps({"kind":"mcp_tool_contract","tool":self.search["alias"]})}]
        out, mapping = self.project()
        self.assertEqual(set(mapping), {"serein_search"})
        self.assertNotIn(native.DISPATCH, json.dumps(out))

    def test_wrapper_only_missing_roster_and_required_choices(self):
        body = {"messages":[{"role":"user","content":"invokeMCPTool 是什么？"}],
                "tools":[builtin(native.DISPATCH)],"tool_choice":"auto"}
        out, mapping = native.project(body, [], is_aru_environment, as_text)
        self.assertEqual(out, {"messages":body["messages"]})
        self.assertFalse(mapping)
        body["tool_choice"] = "required"
        with self.assertRaises(native.UnavailableToolChoice):
            native.project(body, [], is_aru_environment, as_text)

    def test_restricted_tool_selection_preserves_local_choice(self):
        allowed = {"type":"allowed_tools", "allowed_tools":{"mode":"required",
                   "tools":[{"type":"function","function":{"name":name}} for name in (native.DISPATCH, "local")]}}
        self.body["tool_choice"] = allowed
        out, _ = self.project()
        self.assertEqual(out["tool_choice"]["allowed_tools"], {"mode":"required","tools":[allowed["allowed_tools"]["tools"][1]]})
        self.assertNotIn(native.DISPATCH, json.dumps(out))

    def test_excluded_history_and_rejected_attempts_preserve_other_calls(self):
        error = json.dumps({"error": {"code": "invokeMCPTool.invalid_arguments"}, "status": "error"})
        # Empty args with an excluded receipt and malformed args with a confirmed
        # rejection are the two shapes Aru sent in the affected conversation.
        block = [{"role": "assistant", "content": "我先查一下。", "tool_calls": [
            call(native.DISPATCH, "{}", "excluded"), call(native.DISPATCH, '{"function":"guess"}', "bad"),
            call("local", "{}", "good")]},
            {"role": "tool", "tool_call_id": "good", "content": "ok"},
            {"role": "tool", "tool_call_id": "excluded", "content": native.EXCLUDED_RESULT},
            {"role": "tool", "tool_call_id": "bad", "content": error}]
        authored = {"role":"user","content":"刚才聊过 invokeMCPTool"}
        self.body["messages"] = block + [authored, env()]
        before = copy.deepcopy(self.body)
        out, _ = self.project()
        self.assertEqual(self.body, before)
        history = out["messages"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["tool_calls"], [block[0]["tool_calls"][2]])
        self.assertIn("我先查一下。", history[0]["content"])
        self.assertIn("未执行", history[0]["content"])
        self.assertEqual(history[1:3], [block[1], authored])
        self.assertNotIn(native.DISPATCH, json.dumps([history[0],history[1],history[3]], ensure_ascii=False))

    def test_only_exactly_paired_exclusions_are_omitted(self):
        old = {"role":"assistant","tool_calls":[call(native.DISPATCH, "{}", "x")]}
        pair = [old, {"role":"tool","tool_call_id":"x","content":native.EXCLUDED_RESULT}]
        self.assertEqual(native.compact_dispatch_history(pair), [])
        for receipt in ({"role":"tool","tool_call_id":"x","content":"a real result"},
                        {"role":"tool","tool_call_id":"y","content":native.EXCLUDED_RESULT}):
            self.assertEqual(native.compact_dispatch_history([old, receipt]), [old, receipt])

    def test_service_directory_and_contract_give_the_next_real_call(self):
        server_list = {"kind":"mcp_server_directory","items":[{"name":"Serein","handle":"serein"}]}
        contract = {"kind":"mcp_tool_contract","tool":self.search["alias"]}
        self.body["messages"] = [
            {"role":"assistant","tool_calls":[call("listMCPTools", "{}", "d")]},
            {"role":"tool","tool_call_id":"d","content":json.dumps(server_list)}, env()]
        out, tools = self.project()
        self.assertFalse(tools)
        suggested = json.loads(out["messages"][1]["content"])["items"][0]["next_call"]
        self.assertEqual(suggested, {"name":"listMCPTools","arguments":{"server":"serein"}})
        self.assertIn("已展开 0 个", out["messages"][2]["content"])
        self.body["messages"] += [
            {"role":"assistant","tool_calls":[call("readMCPTool", json.dumps({"tool":self.search["alias"]}), "r")]},
            {"role":"tool","tool_call_id":"r","content":json.dumps(contract)}]
        out, tools = self.project()
        self.assertEqual(set(tools), {"serein_search"})
        self.assertEqual(json.loads(out["messages"][-1]["content"])["call_as"], "serein_search")
        self.assertIn("已展开 1 个", out["messages"][2]["content"])

    def test_known_call_without_sending_schemas(self):
        opening = flat("openMemorySource", ["id"])
        opening["alias"] = "mcp__openmemorysource__" + "a"*32
        catalog = [{"server":"Serein","count":1,"tools":[opening]}]
        self.body["messages"] = [env("Serein(1)")]
        allowed = {}
        out, exposed = native.project(self.body,catalog,is_aru_environment,as_text,response_tools=allowed)
        self.assertFalse(exposed)
        self.assertNotIn("serein_openMemorySource", json.dumps(out))
        real_name = opening["function"]["name"]
        old = json.dumps({"function":"openMemorySource","sourceId":"card-example"})
        for name, raw in [(native.DISPATCH,old), (real_name,'{"id":"card-example"}'),
                          (opening["alias"],'{"id":"card-example"}'),
                          ("openMemorySource",'{"id":"card-example"}')]:
            tc = call(name, raw, "id1")
            native.restore({"choices":[{"message":{"tool_calls":[tc]},"finish_reason":"tool_calls"}]}, exposed, allowed)
            self.assertEqual(tc["id"],"id1")
            self.assertEqual(tc["function"]["name"],native.DISPATCH)
            expected = {"tool":opening["alias"],"arguments":{"id":"card-example"}}
            self.assertEqual(json.loads(tc["function"]["arguments"]), expected)
            stream = native.StreamCalls(exposed, {}, allowed)
            for n, text in enumerate([raw[:5],raw[5:]]):
                fn = {"arguments":text}
                if not n: fn["name"] = name
                stream.apply({"choices":[{"delta":{"tool_calls":[{"index":0,"function":fn}]}}]})
                self.assertNotIn("arguments",fn)
            delta = {}
            stream.apply({"choices":[{"delta":delta,"finish_reason":"tool_calls"}]})
            self.assertEqual(json.loads(delta["tool_calls"][0]["function"]["arguments"]),expected)
        for raw in ('{"function":"openMemorySource","id":"a","sourceId":"b"}',
                    '{"function":"openMemorySource","sourceId":42}',
                    '{"function":"openMemorySource","sourceId":"a","sourceId":"b"}',
                    '{"function":"openMemorySource","server":"other","sourceId":"a"}'):
            self.assertIsNone(native.known_call(native.DISPATCH,raw,allowed))
        self.assertIsNone(native.known_call(native.DISPATCH,old,{}))
        other = {**opening, "alias":"mcp__openmemorysource__" + "b"*32,
                 "function":{**opening["function"], "name":"other_openMemorySource"}}
        other_service = {"server":"Other","count":1,"tools":[other]}
        both = {**self.body,"messages":[env("Serein(1)、Other(1)")]}
        both["messages"][0]["content"] = both["messages"][0]["content"].replace("1 个服务", "2 个服务")
        ambiguous = {}
        native.project(both,[*catalog, other_service],is_aru_environment,as_text,response_tools=ambiguous)
        self.assertIn("other_openMemorySource",ambiguous)
        self.assertIn(real_name,ambiguous)
        self.assertIsNone(native.known_call(native.DISPATCH,old,ambiguous))
        self.body["tools"].insert(0, builtin("openMemorySource"))
        local_collision = {}
        native.project(self.body,catalog,is_aru_environment,as_text,response_tools=local_collision)
        self.assertNotIn("openMemorySource",local_collision)
        self.assertIsNone(native.known_call(native.DISPATCH,old,local_collision))
        self.body["tools"][-1]["function"]["parameters"]["properties"] = {"tool":{"enum":["something-else"]}}
        restricted = {}
        native.project(self.body,catalog,is_aru_environment,as_text,response_tools=restricted)
        self.assertEqual(restricted, {})

    def test_unexpanded_calls_follow_the_catalog_schema_and_leave_client_tools_intact(self):
        self.body["messages"] = [env()]
        permitted = {}
        _, projected = native.project(self.body, self.catalog, is_aru_environment, as_text, response_tools=permitted)
        self.assertEqual(projected, {})
        tagged = '<｜DSML｜parameter name="query" string="true">小窝'
        cases = [("serein_search", tagged, self.search, {"query":"小窝"}),
                 ("serein_list", "", self.listing, {}),
                 ("serein_search", '{"query":"a","extra":1}', self.search, {"query":"a","extra":1})]
        for name, raw, method, args in cases:
            self.assertEqual(json.loads(native.known_call(name, raw, permitted)),
                             {"tool":method["alias"],"arguments":args})
        local = call("local",'{"id":"example"}',"internal")
        original = copy.deepcopy(local)
        part = {"choices":[{"message":{"tool_calls":[local]},"finish_reason":"tool_calls"}]}
        self.assertFalse(native.restore(part, projected, permitted))
        self.assertEqual(local, original)

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
