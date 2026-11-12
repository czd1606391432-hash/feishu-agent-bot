"""离线测试：飞书和模型都用替身，不联网、不需要凭证，也不需要安装飞书/模型的 SDK。

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import ownership                                                  # noqa: E402
from app.agent import Agent, MAX_TOOL_RESULT_CHARS                         # noqa: E402
from app.events import build_card, parse_message_event                     # noqa: E402
from app.handler import MessageHandler                                     # noqa: E402
from app.hooks import Block, Hooks, StateStore, load_hooks                 # noqa: E402
from app.llm import AnthropicAdapter, OpenAICompatAdapter                  # noqa: E402
from app.mcp_hub import MCPHub, _as_schema, _render_result, _sanitize      # noqa: E402
from app.session import Deduplicator, SessionStore                         # noqa: E402
from app.tools import (CustomToolRegistry, HttpTimeout, ToolRouter, _fill_params,   # noqa: E402
                       _shape_response, format_arguments)
from examples import confirm_gate, demo_api_server                         # noqa: E402
from fakes import FakeLLM, FakeMessenger, FakeTools, card_event, message_event, say, use   # noqa: E402


logging.disable(logging.CRITICAL)      # 测试里故意触发了很多报错，日志不用刷屏


def run(coro):
    return asyncio.run(coro)


# ====================================================================== Agent 循环
class AgentTest(unittest.TestCase):
    def test_plain_answer(self):
        llm = FakeLLM(say("你好"))
        agent = Agent(llm, None, "系统提示")
        res, new = run(agent.run("在吗", [{"role": "user", "content": "上一句"}]))
        self.assertEqual((res.answer, res.rounds, res.footer), ("你好", 0, None))
        msgs, tools = llm.calls[0]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "user"])
        self.assertEqual(msgs[0]["content"], "系统提示")
        self.assertIsNone(tools)                                  # 没有工具时不传 tools
        self.assertEqual([m["role"] for m in new], ["user", "assistant"])      # 只返回本轮新增的消息

    def test_multi_round_tool_calls(self):
        tools = FakeTools(lookup=lambda a: "结果:" + a["q"], add=lambda a: str(a["x"] + 1))
        llm = FakeLLM(use(("lookup", {"q": "甲"})), use(("add", {"x": 1}), ("lookup", {"q": "乙"})), say("做完了"))
        notes = []

        async def progress(note):
            notes.append(note)

        res, new = run(Agent(llm, tools, "S").run("干活", [], progress))
        self.assertEqual((res.answer, res.rounds), ("做完了", 2))
        self.assertEqual(res.tool_trace, ["lookup", "add", "lookup"])
        self.assertEqual(res.footer, "调用工具：lookup、add、lookup")
        self.assertEqual(tools.calls, [("lookup", {"q": "甲"}), ("add", {"x": 1}), ("lookup", {"q": "乙"})])
        self.assertEqual(notes, ["正在调用工具：lookup…", "正在调用工具：add、lookup…"])
        self.assertEqual([m["role"] for m in new], ["user", "assistant", "tool", "assistant", "tool", "tool", "assistant"])
        # 工具结果按 tool_call_id 对上，并且第三次问模型时已经带着前面的结果
        self.assertEqual(new[2]["tool_call_id"], new[1]["tool_calls"][0]["id"])
        self.assertEqual([m["content"] for m in llm.calls[2][0] if m["role"] == "tool"], ["结果:甲", "2", "结果:乙"])

    def test_last_round_has_no_tools(self):
        tools = FakeTools(loop=lambda a: "再来")
        llm = FakeLLM(forever=lambda msgs, t: use(("loop", {})) if t else say("只能给结论了"))
        res, _ = run(Agent(llm, tools, "S", max_tool_rounds=3).run("x", []))
        self.assertEqual(res.answer, "只能给结论了")
        self.assertEqual(len(tools.calls), 3)
        given = [t for _, t in llm.calls]
        self.assertTrue(all(given[:3]))
        self.assertIsNone(given[3])                               # 最后一轮不给工具，逼模型出结论

    def test_model_ignores_and_still_calls_tools_on_last_round(self):
        llm = FakeLLM(forever=use(("loop", {})))                  # 不给工具也硬要调
        res, _ = run(Agent(llm, FakeTools(loop=lambda a: "x"), "S", max_tool_rounds=1).run("x", []))
        self.assertIn("超出最大轮数", res.answer)

    def test_tool_error_goes_back_to_model(self):
        def boom(a):
            raise RuntimeError("数据库连不上")

        llm = FakeLLM(use(("boom", {}), ("missing", {})), say("失败了，原因已说明"))
        res, new = run(Agent(llm, FakeTools(boom=boom), "S").run("x", []))
        self.assertEqual(res.answer, "失败了，原因已说明")
        self.assertIn("数据库连不上", new[2]["content"])            # 报错原样交给模型
        self.assertIn("未知工具", new[3]["content"])

    def test_bad_arguments_and_empty_output(self):
        tools = FakeTools(t=lambda a: "")
        llm = FakeLLM(use(("t", "{不是json")), say(""))
        res, new = run(Agent(llm, tools, "S").run("x", []))
        self.assertEqual(tools.calls, [("t", {})])
        self.assertEqual(new[2]["content"], "（工具返回空结果）")
        self.assertEqual(res.answer, "（模型没有返回内容）")
        self.assertEqual(format_arguments('[1]'), {"value": [1]})
        self.assertEqual(format_arguments({"a": 1}), {"a": 1})

    def test_long_output_truncated(self):
        llm = FakeLLM(use(("big", {})), say("ok"))
        _, new = run(Agent(llm, FakeTools(big=lambda a: "x" * 50000), "S").run("x", []))
        self.assertLess(len(new[2]["content"]), MAX_TOOL_RESULT_CHARS + 50)
        self.assertIn("已截断", new[2]["content"])

    def test_footer_abbreviates(self):
        llm = FakeLLM(*[use(("t", {})) for _ in range(8)], say("ok"))
        res, _ = run(Agent(llm, FakeTools(t=lambda a: "1"), "S", max_tool_rounds=8).run("x", []))
        self.assertEqual(res.footer, "调用工具：" + "、".join(["t"] * 6) + " 等 8 项")


# ====================================================================== 钩子
class HooksTest(unittest.TestCase):
    def test_prompt_rules_and_local_tool(self):
        hooks = Hooks()
        hooks.prompt("规则一")
        hooks.prompt("  ")
        seen = {}

        @hooks.tool("whoami", "看看是谁在问", {"type": "object", "properties": {}})
        def whoami(ctx, args):
            seen.update(key=ctx.session_key, text=ctx.user_text, who=ctx.requester_open_id, turn=ctx.turn_id)
            return "是 " + ctx.requester_open_id

        @hooks.tool("slow", "异步本地工具")
        async def slow(ctx, args):
            await asyncio.sleep(0)
            return "异步也行"

        llm = FakeLLM(use(("whoami", {}), ("slow", {})), say("好"))
        agent = Agent(llm, None, "基础", hooks=hooks)
        _, new = run(agent.run("我是谁", [], requester_open_id="ou_a", session_key="s1"))
        self.assertEqual(llm.calls[0][0][0]["content"], "基础\n\n规则一")
        self.assertEqual([t["function"]["name"] for t in llm.calls[0][1]], ["whoami", "slow"])
        self.assertEqual(seen, {"key": "s1", "text": "我是谁", "who": "ou_a", "turn": 1})
        self.assertEqual([new[2]["content"], new[3]["content"]], ["是 ou_a", "异步也行"])

    def test_before_tool_block_and_rewrite(self):
        hooks, tools = Hooks(), FakeTools(search=lambda a: json.dumps(a, ensure_ascii=False), delete=lambda a: "删了")

        @hooks.before_tool
        def no_delete(ctx, name, args):
            if name == "delete":
                return Block("不允许删除")

        @hooks.before_tool
        def force_scope(ctx, name, args):
            if name == "search":
                return dict(args, scope="本部门")              # 参数由代码补上，模型绕不过去

        llm = FakeLLM(use(("delete", {"id": 1}), ("search", {"q": "x", "scope": "全公司"})), say("好"))
        _, new = run(Agent(llm, tools, "S", hooks=hooks).run("x", []))
        self.assertEqual(new[2]["content"], "不允许删除")
        self.assertEqual(tools.calls, [("search", {"q": "x", "scope": "本部门"})])      # delete 根本没执行

    def test_before_hook_error_fails_closed(self):
        hooks, tools = Hooks(), FakeTools(pay=lambda a: "付了")

        @hooks.before_tool
        def broken(ctx, name, args):
            raise ValueError("检查代码有 bug")

        llm = FakeLLM(use(("pay", {})), say("好"))
        _, new = run(Agent(llm, tools, "S", hooks=hooks).run("x", []))
        self.assertEqual(tools.calls, [])                          # 检查出错时不执行，而不是放行
        self.assertIn("未执行", new[2]["content"])

    def test_after_tool_rewrite_state_and_turn(self):
        hooks, tools = Hooks(), FakeTools(fetch=lambda a: "正文\n---\n内部备注")

        @hooks.after_tool
        def trim(ctx, name, args, output):
            ctx.state["fetched"] = ctx.state.get("fetched", 0) + 1
            ctx.turn["n"] = ctx.turn.get("n", 0) + 1
            return output.split("\n---")[0]

        @hooks.after_tool
        def broken(ctx, name, args, output):
            raise RuntimeError("事后处理出错不影响结果")

        @hooks.tool("stats", "看状态")
        def stats(ctx, args):
            return "%s/%s/%s" % (ctx.state.get("fetched"), ctx.turn.get("n"), ctx.turn_id)

        agent = Agent(FakeLLM(use(("fetch", {})), say("a"), use(("fetch", {}), ("stats", {})), say("b")), tools, "S", hooks=hooks)
        _, new1 = run(agent.run("一", [], session_key="k"))
        _, new2 = run(agent.run("二", [], session_key="k"))
        self.assertEqual(new1[2]["content"], "正文")
        self.assertEqual(new2[3]["content"], "2/1/2")              # 会话状态跨轮累计到 2；本轮状态每轮从头计；这是第 2 轮

    def test_state_store_ttl_and_load(self):
        st = StateStore(ttl_seconds=10)
        st.get("k")["a"] = 1
        self.assertEqual(st.next_turn("k"), 1)
        with mock.patch("time.time", return_value=time.time() + 11):
            self.assertEqual(st.get("k"), {})
            self.assertEqual(st.next_turn("k"), 1)
        hooks = load_hooks("examples.confirm_gate")
        self.assertTrue(hooks.prompt_rules)
        with self.assertRaises(RuntimeError):
            load_hooks("json")                                     # 没有 setup(hooks)
        self.assertEqual(Hooks().text_for_action({"say": "确认"}), "确认")
        self.assertEqual(Hooks().text_for_action({"x": 1}), "")


# ====================================================================== 自定义 HTTP 工具
class CustomToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        demo_api_server.TICKETS.clear()
        cls.srv = demo_api_server.serve(0, token="demo-token")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["DEMO_API_BASE"] = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        os.environ["DEMO_API_TOKEN"] = "demo-token"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def registry(self, specs=None, sender=None):
        path = os.path.join(ROOT, "examples", "custom_tools.example.json")
        if specs is not None:
            path = os.path.join(tempfile.mkdtemp(), "tools.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(specs if isinstance(specs, str) else json.dumps(specs, ensure_ascii=False))
        reg = CustomToolRegistry(path, **({"sender": sender} if sender else {}))
        reg.load()
        return reg

    def test_example_tools_roundtrip(self):
        reg = self.registry()
        self.assertEqual([t["function"]["name"] for t in reg.openai_tools()], ["search_kb", "get_ticket", "create_ticket"])
        got = json.loads(run(reg.call("search_kb", {"q": "VPN"})))
        self.assertEqual(set(got), {"total", "items"})             # fields:只留这两个,took_ms/trace_id 被裁掉
        self.assertEqual(got["items"][0]["id"], "KB-101")
        self.assertEqual(json.loads(run(reg.call("search_kb", {"q": "", "limit": 2})))["total"], 4)   # 空参数不发,limit 保持数字
        md = run(reg.call("create_ticket", {"title": "VPN 连不上", "urgency": "紧急"}))
        self.assertTrue(md.startswith("**工单 T-0001 已创建**"))    # extract:取出一段 markdown,原样给模型
        self.assertIn("紧急", md)
        self.assertEqual(json.loads(run(reg.call("get_ticket", {"ticket_id": "T-0001"})))["status"], "待处理")
        self.assertIn("HTTP 404", run(reg.call("get_ticket", {"ticket_id": "../kb?q="})))   # 参数里的 / ? 被转义,改不了路径
        self.assertIn("HTTP 400", run(reg.call("create_ticket", {})))
        self.assertIn("缺少标题", run(reg.call("create_ticket", {})))

    def test_whole_chain_with_gate_over_real_http(self):
        """示例工具 + 示例后端(真实 HTTP) + 确认闸门，连起来走两轮对话。"""
        hooks = Hooks()
        confirm_gate.setup(hooks)
        before = len(demo_api_server.TICKETS)
        ticket = {"title": "打印机脱机", "detail": "三楼那台", "urgency": "普通"}
        llm = FakeLLM(use(("search_kb", {"q": "打印机"})), use(("create_ticket", ticket)), say("请确认"),
                      use(("create_ticket", dict(ticket, urgency="紧急"))), say("已提交"))
        agent = Agent(llm, ToolRouter(custom=self.registry()), "S", hooks=hooks)
        r1, new1 = run(agent.run("打印机坏了，帮我报修", [], session_key="k"))
        self.assertIn("KB-102", new1[2]["content"])                         # 第一步真的查到了知识库
        self.assertEqual(len(demo_api_server.TICKETS), before)              # 提交被拦下，后端没有新工单
        self.assertEqual([b["text"] for b in r1.buttons], ["确认", "取消"])
        r2, new2 = run(agent.run("确认", new1, session_key="k"))
        self.assertEqual(len(demo_api_server.TICKETS), before + 1)
        created = list(demo_api_server.TICKETS.values())[-1]
        self.assertEqual((created["title"], created["urgency"]), ("打印机脱机", "普通"))   # 按用户看过的那份提交
        self.assertTrue(new2[2]["content"].startswith("**工单 T-"))
        self.assertEqual((r2.answer, r2.buttons), ("已提交", []))

    def test_env_and_errors(self):
        reg = self.registry()
        with mock.patch.dict(os.environ, {"DEMO_API_TOKEN": "wrong"}):
            self.assertIn("HTTP 401", run(reg.call("search_kb", {"q": "x"})))
        saved = os.environ.pop("DEMO_API_TOKEN")
        try:
            self.assertIn("环境变量 DEMO_API_TOKEN 未设置", run(reg.call("search_kb", {"q": "x"})))
        finally:
            os.environ["DEMO_API_TOKEN"] = saved
        self.assertEqual(run(reg.call("nope", {})), "未知工具：nope")
        with mock.patch.dict(os.environ, {"DEMO_API_BASE": "http://127.0.0.1:9"}):
            self.assertIn("请求失败", run(reg.call("search_kb", {"q": "x"})))

    def test_param_cannot_smuggle_env_secret(self):
        sent = {}

        def sender(method, url, headers, query, body, timeout):
            sent.update(url=url, headers=headers, query=query, body=body)
            return 200, "application/json", "{}"

        reg = self.registry([{"name": "t", "description": "d", "request": {
            "method": "POST", "url": "http://x/{{p}}", "headers": {"X-Who": "{{p}}", "X-Key": "${BOT_TEST_SECRET}"},
            "query": {"p": "{{p}}"}, "json": {"p": "{{p}}", "n": "{{n}}", "tags": "{{tags}}", "note": "共{{n}}项"}}}], sender)
        with mock.patch.dict(os.environ, {"BOT_TEST_SECRET": "s3cret"}):
            run(reg.call("t", {"p": "${BOT_TEST_SECRET}", "n": 3, "tags": ["a", "b"]}))
        self.assertEqual(sent["headers"], {"X-Who": "${BOT_TEST_SECRET}", "X-Key": "s3cret"})   # 模型给的参数不会被当成环境变量展开
        self.assertNotIn("s3cret", sent["url"])
        self.assertEqual(sent["query"], {"p": "${BOT_TEST_SECRET}"})
        self.assertEqual(sent["body"], {"p": "${BOT_TEST_SECRET}", "n": 3, "tags": ["a", "b"], "note": "共3项"})   # 整段占位符保留原类型

    def test_response_shaping_and_limits(self):
        payload = {"data": {"items": [1, 2], "summary": "一段话"}, "debug": "x" * 100, "total": 2}
        self.assertEqual(_shape_response(payload, {"extract": "data.summary"}), "一段话")
        self.assertEqual(_shape_response(payload, {"extract": "data.nope"}), payload)       # 取不到就原样返回
        self.assertEqual(_shape_response(payload, {"fields": ["total", "nope"]}), {"total": 2})
        self.assertEqual(_shape_response("纯文本", {"fields": ["a"]}), "纯文本")
        self.assertEqual(_fill_params("{{ a }}", {"a": True}), True)
        self.assertIsNone(_fill_params("{{a}}", {}))

        def sender(method, url, headers, query, body, timeout):
            if "slow" in url:
                raise HttpTimeout("timed out")
            if "plain" in url:
                return 200, "text/plain", "就是一段文字"
            if "empty" in url:
                return 200, "text/plain", ""
            return 200, "application/json", json.dumps({"text": "字" * 500})

        spec = lambda name, url, **resp: {"name": name, "description": "d", "request": {"url": url, "timeout": 7}, "response": resp}
        reg = self.registry([spec("big", "http://x/big", max_chars=100), spec("slow", "http://x/slow"),
                             spec("plain", "http://x/plain"), spec("empty", "http://x/empty")], sender)
        out = run(reg.call("big", {}))
        self.assertTrue(out.endswith("…（结果过长已截断）") and len(out) < 130)
        self.assertIn("请求超时（7 秒）", run(reg.call("slow", {})))
        self.assertEqual(run(reg.call("plain", {})), "就是一段文字")
        self.assertEqual(run(reg.call("empty", {})), "（接口返回空内容）")

    def test_load_validation(self):
        reg = self.registry([{"name": "ok", "description": "d", "request": {"url": "http://x"}},
                             {"name": "有中文", "description": "d", "request": {"url": "http://x"}},
                             {"name": "no_url", "description": "d", "request": {}},
                             {"description": "没名字", "request": {"url": "http://x"}}, "不是对象"])
        self.assertEqual([t["function"]["name"] for t in reg.openai_tools()], ["ok"])
        self.assertFalse(self.registry("{坏的").has_tools())
        self.assertFalse(self.registry({"不是": "数组"}).has_tools())
        missing = CustomToolRegistry("/nonexistent/tools.json")
        missing.load()
        self.assertFalse(missing.has_tools())

    def test_router_merges_and_dispatches(self):
        custom = self.registry([{"name": "same", "description": "自定义的", "request": {"url": "http://x"}}],
                               lambda *a: (200, "text/plain", "来自自定义"))
        hub = FakeTools(same=lambda a: "来自 MCP", only_mcp=lambda a: "MCP 独有")
        router = ToolRouter(mcp_hub=hub, custom=custom)
        self.assertEqual([t["function"]["name"] for t in router.openai_tools()], ["same", "only_mcp"])
        self.assertEqual(router.openai_tools()[0]["function"]["description"], "自定义的")    # 重名时自定义工具优先
        self.assertEqual(run(router.call("same", {})), "来自自定义")
        self.assertEqual(run(router.call("only_mcp", {})), "MCP 独有")
        self.assertEqual(run(ToolRouter().call("x", {})), "未知工具：x")
        self.assertFalse(ToolRouter().has_tools())


# ====================================================================== 会话与去重
class SessionTest(unittest.TestCase):
    def test_trim_keeps_history_well_formed(self):
        st = SessionStore(max_messages=3)
        k = SessionStore.key("oc_1", "ou_1")
        self.assertEqual(k, "oc_1::ou_1")
        st.extend(k, [{"role": "user", "content": "1"}, {"role": "assistant", "content": "", "tool_calls": [{}]},
                      {"role": "tool", "content": "r1"}, {"role": "tool", "content": "r2"}, {"role": "assistant", "content": "a"}])
        # 留最后 3 条会以两条 tool 结果开头(对应的 assistant 调用已被裁掉) → 这两条也要丢,否则模型接口会报错
        self.assertEqual(st.history(k), [{"role": "assistant", "content": "a"}])
        st.extend(k, [{"role": "user", "content": str(i)} for i in range(10)])
        self.assertEqual([m["content"] for m in st.history(k)], ["7", "8", "9"])
        self.assertEqual(st.history("别人"), [])                   # 不同会话互不可见
        h = st.history(k)
        h.append("x")
        self.assertEqual(len(st.history(k)), 3)                    # 返回的是副本

    def test_ttl_and_clear(self):
        st = SessionStore(ttl_seconds=100)
        st.extend("k", [{"role": "user", "content": "hi"}])
        now = time.time()
        with mock.patch("time.time", return_value=now + 99):
            self.assertEqual(len(st.history("k")), 1)
        with mock.patch("time.time", return_value=now + 101):
            self.assertEqual(st.history("k"), [])                  # 超时自动清空
        st.extend("k", [{"role": "user", "content": "again"}])
        st.clear("k")
        self.assertEqual(st.history("k"), [])

    def test_dedup(self):
        d = Deduplicator(capacity=8)
        self.assertFalse(d.seen("m1"))
        self.assertTrue(d.seen("m1"))
        self.assertFalse(d.seen(""))                               # 没有 id 的不去重
        self.assertFalse(d.seen(""))
        for i in range(20):
            d.seen("x%d" % i)
        self.assertLessEqual(len(d._seen), 8)                      # 超过容量会淘汰最早的
        self.assertTrue(d.seen("x19"))


# ====================================================================== 事件解析与卡片
class EventsTest(unittest.TestCase):
    def test_parse_text_post_and_mentions(self):
        m = parse_message_event(message_event("@_user_1 帮我查一下 ", chat_type="group", mentions=["ou_bot"]))
        self.assertEqual((m.text, m.is_group, m.mentioned_bot, m.sender_open_id), ("帮我查一下", True, True, "ou_user"))
        post = json.dumps({"title": "", "content": [[{"tag": "at", "user_id": "x"}, {"tag": "text", "text": "第一段"}],
                                                    [{"tag": "text", "text": "第二段"}]]}, ensure_ascii=False)
        self.assertEqual(parse_message_event(message_event(msg_type="post", content=post)).text, "第一段 第二段")
        self.assertEqual(parse_message_event(message_event(msg_type="image", content='{"image_key":"k"}')).text, "")
        self.assertEqual(parse_message_event(message_event(content="{坏的")).text, "")
        self.assertIsNone(parse_message_event(NS()))
        self.assertFalse(parse_message_event(message_event(chat_type="group")).mentioned_bot)
        # 填了机器人自己的 open_id 后，@ 别人不算
        e = message_event(chat_type="group", mentions=["ou_someone"])
        self.assertTrue(parse_message_event(e).mentioned_bot)
        self.assertFalse(parse_message_event(e, bot_open_id="ou_bot").mentioned_bot)
        self.assertTrue(parse_message_event(message_event(mentions=["ou_x", "ou_bot"]), bot_open_id="ou_bot").mentioned_bot)

    def test_build_card(self):
        self.assertEqual(build_card(""), {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": " "}]})
        card = build_card("正文", "调用工具：a", [{"text": "确认", "say": "确认", "type": "primary"},
                                              {"text": "打开", "url": "https://example.com"},
                                              {"text": "自定义", "value": {"op": "x"}}, {"text": "没动作"}, {"say": "没文字"}])
        self.assertEqual([e["tag"] for e in card["elements"]], ["markdown", "action", "hr", "note"])
        acts = card["elements"][1]["actions"]
        self.assertEqual(len(acts), 3)
        self.assertEqual((acts[0]["value"], acts[0]["type"]), ({"say": "确认"}, "primary"))
        self.assertEqual(acts[1]["url"], "https://example.com")
        self.assertEqual(acts[2]["value"], {"op": "x"})
        json.dumps(card)


# ====================================================================== 消息处理
class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.messenger = FakeMessenger()
        self.sessions = SessionStore()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def handler(self, llm, tools=None, hooks=None, **kw):
        agent = Agent(llm, tools, "S", hooks=hooks)
        h = MessageHandler(agent, self.messenger, self.sessions, self.loop, card_response=lambda p: p, **kw)
        self.addCleanup(h.shutdown)
        return h

    def wait(self, n, timeout=5.0):
        end = time.time() + timeout
        while len(self.messenger.sent) < n and time.time() < end:
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.messenger.sent), n, self.messenger.sent)
        time.sleep(0.05)                                           # 确认没有多余的消息紧跟着发出来
        return self.messenger.sent

    def test_placeholder_then_answer(self):
        llm = FakeLLM(use(("t", {})), say("查到了"))
        h = self.handler(llm, FakeTools(t=lambda a: "数据"))
        h.on_message(message_event("查一下", message_id="om_1"))
        sent = self.wait(3)
        self.assertEqual(sent[0][:3], ("reply", "om_1", "正在处理…"))          # 先回占位卡片
        self.assertEqual(sent[1][:3], ("update", "card_1", "正在调用工具：t…"))  # 进度
        self.assertEqual(sent[2][:4], ("update", "card_1", "查到了", "调用工具：t"))   # 原地更新成结果
        self.assertEqual(len(sent), 3)
        hist = self.sessions.history(SessionStore.key("oc_chat", "ou_user"))
        self.assertEqual([m["role"] for m in hist], ["user", "assistant", "tool", "assistant"])
        # 第二条消息带着上一轮的历史
        llm.script.append(say("第二次"))
        h.on_message(message_event("再问"))
        self.wait(5)
        self.assertEqual([m["content"] for m in llm.calls[-1][0] if m["role"] == "user"], ["查一下", "再问"])

    def test_duplicate_event_ignored(self):
        h = self.handler(FakeLLM(say("一次")))
        ev = message_event("hi", message_id="om_dup")
        h.on_message(ev)
        h.on_message(ev)                                           # 飞书重投同一条
        self.assertEqual(len(self.wait(2)), 2)                     # 占位 + 结果，只处理了一次

    def test_group_requires_mention(self):
        h = self.handler(FakeLLM(say("在")))
        h.on_message(message_event("大家好", chat_type="group"))
        time.sleep(0.15)
        self.assertEqual(self.messenger.sent, [])
        h.on_message(message_event("@_user_1 在吗", chat_type="group", mentions=["ou_bot"]))
        self.assertEqual(self.wait(2)[-1][2], "在")

    def test_group_without_mention_when_not_required(self):
        h = self.handler(FakeLLM(say("在")), require_mention_in_group=False)
        h.on_message(message_event("大家好", chat_type="group"))
        self.assertEqual(self.wait(2)[-1][2], "在")

    def test_commands_and_non_text(self):
        hooks = Hooks()
        h = self.handler(FakeLLM(say("答")), hooks=hooks)
        key = SessionStore.key("oc_chat", "ou_user")
        self.sessions.extend(key, [{"role": "user", "content": "旧"}])
        hooks.state.get(key)["x"] = 1
        h.on_message(message_event("/reset"))
        self.assertIn("已清空", self.wait(1)[0][2])
        self.assertEqual((self.sessions.history(key), hooks.state.get(key)), ([], {}))
        h.on_message(message_event("帮助"))
        self.assertIn("怎么用", self.wait(2)[1][2])
        h.on_message(message_event(msg_type="image", content="{}"))
        self.assertIn("只能处理文字", self.wait(3)[2][2])

    def test_agent_failure_and_timeout(self):
        h = self.handler(FakeLLM(RuntimeError("模型接口 500")))
        h.on_message(message_event("x"))
        self.assertEqual(self.wait(2)[1][2], "处理失败：模型接口 500")
        self.assertEqual(self.sessions.history(SessionStore.key("oc_chat", "ou_user")), [])    # 失败的一轮不进历史
        self.messenger.sent.clear()
        h2 = self.handler(FakeLLM(say("太慢了"), delay=2), timeout_seconds=0.2)
        h2.on_message(message_event("x"))
        self.assertEqual(self.wait(2)[1][2], "处理失败：超过 0 秒没有完成")

    def test_fallbacks_when_card_ops_fail(self):
        self.messenger.fail_update = True
        h = self.handler(FakeLLM(say("答复")))
        h.on_message(message_event("x", message_id="om_9"))
        sent = self.wait(2)
        self.assertEqual([s[:3] for s in sent], [("reply", "om_9", "正在处理…"), ("reply", "om_9", "答复")])   # 更新不了就新发一条
        self.messenger.sent.clear()
        self.messenger.fail_update, self.messenger.fail_reply_id = False, True
        h.on_message(message_event("y", message_id="om_10"))      # 占位卡片没发出去 → 直接回复结果
        h._agent._llm.script.append(say("答复2"))
        self.assertEqual(self.wait(2)[-1][:3], ("reply", "om_10", "答复2"))

    def test_on_message_hook_short_circuits(self):
        hooks, seen = Hooks(), []

        @hooks.on_message
        def collect(msg):
            seen.append((msg.text, msg.addressed, msg.is_group))
            if msg.text.startswith("工号"):
                msg.state["emp"] = msg.text[2:].strip()
                return "已记下工号"                                # 直接回复，这条消息不进模型

        llm = FakeLLM(say("模型回答"))
        h = self.handler(llm, hooks=hooks)
        h.on_message(message_event("工号 A123", chat_type="group"))   # 群里没 @ 也能截到
        self.assertEqual(self.wait(1)[0][2], "已记下工号")
        self.assertEqual(llm.calls, [])
        self.assertEqual(hooks.state.get(SessionStore.key("oc_chat", "ou_user")), {"emp": "A123"})
        h.on_message(message_event("你好"))
        self.assertEqual(self.wait(3)[-1][2], "模型回答")
        self.assertEqual(seen, [("工号 A123", False, True), ("你好", True, False)])

    def test_card_action_becomes_user_message(self):
        llm = FakeLLM(say("收到确认"))
        h = self.handler(llm)
        resp = h.on_card_action(card_event({"say": "确认"}, operator="ou_clicker", message_id="om_card", event_id="ev_1"))
        self.assertEqual(resp, {"toast": {"type": "info", "content": "收到：确认"}})
        sent = self.wait(2)
        self.assertEqual(sent[0][:3], ("reply", "om_card", "正在处理…"))
        self.assertEqual(llm.calls[0][0][-1], {"role": "user", "content": "确认"})     # 和用户自己打字走同一条路
        self.assertTrue(self.sessions.history(SessionStore.key("oc_chat", "ou_clicker")))   # 记在点击人的会话里
        again = h.on_card_action(card_event({"say": "确认"}, event_id="ev_1"))          # 同一次点击重投
        self.assertEqual(again["toast"]["content"], "正在处理…")
        self.assertEqual(h.on_card_action(card_event({"unknown": 1}))["toast"]["type"], "error")
        self.assertEqual(h.on_card_action(NS())["toast"]["type"], "error")
        time.sleep(0.1)
        self.assertEqual(len(self.messenger.sent), 2)

    def test_custom_card_action_mapping(self):
        hooks = Hooks()

        @hooks.card_action
        def pick(value):
            if value.get("op") == "pick":
                return "选第%d个" % int(value["rank"])

        llm = FakeLLM(say("好"))
        h = self.handler(llm, hooks=hooks)
        self.assertEqual(h.on_card_action(card_event({"op": "pick", "rank": 2}))["toast"]["content"], "收到：选第2个")
        self.wait(2)
        self.assertEqual(llm.calls[0][0][-1]["content"], "选第2个")


# ====================================================================== 示例钩子：确认闸门（端到端）
class ConfirmGateTest(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.tools = FakeTools(create_ticket=lambda a: (self.created.append(a), "工单已创建")[1],
                               search_kb=lambda a: "知识库结果")
        self.hooks = Hooks()
        confirm_gate.setup(self.hooks)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.messenger = FakeMessenger()
        self.sessions = SessionStore()
        self.ticket = {"title": "VPN 连不上", "urgency": "普通"}

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def turn(self, llm_script, text, via_card=False):
        """发一条用户消息，等到最终答复，返回 (正文, 按钮)。"""
        agent = Agent(FakeLLM(*llm_script), self.tools, "S", hooks=self.hooks)
        h = MessageHandler(agent, self.messenger, self.sessions, self.loop, card_response=lambda p: p)
        self.addCleanup(h.shutdown)
        self.messenger.sent.clear()
        if via_card:
            h.on_card_action(card_event({"say": text}))
        else:
            h.on_message(message_event(text))
        end = time.time() + 5
        while time.time() < end and not any(s[0] == "update" and not s[2].startswith("正在") for s in self.messenger.sent):
            time.sleep(0.01)
        last = self.messenger.sent[-1]
        return last[2], last[4]

    def test_blocked_then_confirmed_by_button(self):
        # 第 1 轮：模型直接提交，还「自己确认」再提交一次 → 两次都被拦下
        body, buttons = self.turn([use(("create_ticket", self.ticket)), use(("create_ticket", self.ticket)), say("请确认以上内容")], "帮我报修 VPN")
        self.assertEqual(self.created, [])
        self.assertEqual(body, "请确认以上内容")
        self.assertEqual([b["text"] for b in buttons], ["确认", "取消"])       # 卡片上带确认按钮
        # 第 2 轮：用户点「确认」按钮；模型这次还偷偷把紧急程度改了
        changed = dict(self.ticket, urgency="紧急")
        body, buttons = self.turn([use(("create_ticket", changed)), say("已提交")], "确认", via_card=True)
        self.assertEqual(self.created, [self.ticket])                       # 执行的是用户看过的那份，不是模型新给的
        self.assertEqual((body, buttons), ("已提交", []))
        # 第 3 轮：没有新的待确认内容，一句「好的」不会再触发提交
        self.turn([use(("create_ticket", self.ticket)), say("需要你先确认")], "好的")
        self.assertEqual(len(self.created), 1)

    def test_change_request_voids_pending(self):
        self.turn([use(("create_ticket", self.ticket)), say("请确认")], "帮我报修")
        new = {"title": "VPN 和邮箱都连不上", "urgency": "紧急"}
        body, buttons = self.turn([use(("create_ticket", new)), say("改好了，请再确认")], "标题改一下，加上邮箱，确认前先给我看")
        self.assertEqual(self.created, [])                                  # 话里有「确认」两个字但带了修改意见 → 不算确认
        self.assertEqual(len(buttons), 2)
        self.turn([use(("create_ticket", new)), say("已提交")], "可以")
        self.assertEqual(self.created, [new])

    def test_unrelated_message_voids_pending(self):
        self.turn([use(("create_ticket", self.ticket)), say("请确认")], "帮我报修")
        self.turn([use(("search_kb", {"q": "打印机"})), say("打印机这样修")], "对了打印机怎么修")     # 别的工具不受影响
        self.assertEqual(self.tools.calls[-1][0], "search_kb")
        self.turn([use(("create_ticket", self.ticket)), say("请确认")], "确认")   # 隔了一条别的消息，之前的待确认已作废
        self.assertEqual(self.created, [])
        self.turn([use(("create_ticket", self.ticket)), say("已提交")], "确认")
        self.assertEqual(self.created, [self.ticket])

    def test_is_confirm(self):
        for t in ("确认", "可以，提交吧", "OK", "没问题"):
            self.assertTrue(confirm_gate.is_confirm(t), t)
        for t in ("先别提交", "确认一下，标题改成别的", "取消", "", "再等等"):
            self.assertFalse(confirm_gate.is_confirm(t), t)


# ====================================================================== 建文档后自动转所有者
class OwnershipTest(unittest.TestCase):
    OUT = '{"app": {"app_token": "Tok123", "name": "新表"}}'

    def poster(self, *codes):
        calls, results = [], list(codes)

        def post(path, payload):
            calls.append((path, payload))
            code = results.pop(0)
            return {"code": code, "msg": "ok" if code == 0 else "no permission %s" % code}
        return post, calls

    def test_transfer_success(self):
        post, calls = self.poster(0)
        note = ownership.after_tool_call("bitable_v1_app_create", self.OUT, "ou_asker", post)
        self.assertIn("所有者转给你", note)
        path, payload = calls[0]
        self.assertIn("/permissions/Tok123/members/transfer_owner?type=bitable", path)
        self.assertIn("old_owner_perm=full_access", path)                  # 应用自己保留管理权限，之后还能写
        self.assertEqual(payload, {"member_type": "openid", "member_id": "ou_asker"})

    def test_fallback_to_collaborator_then_give_up(self):
        post, calls = self.poster(1063002, 0)
        self.assertIn("可管理", ownership.after_tool_call("docx_v1_document_create", '{"document_id":"Doc9"}', "ou_a", post))
        self.assertIn("/permissions/Doc9/members?type=docx", calls[1][0])
        self.assertEqual(calls[1][1]["perm"], "full_access")
        post, _ = self.poster(1063002, 1063003)
        self.assertIn("没能给你开权限（no permission 1063003）", ownership.after_tool_call(
            "sheets_v3_spreadsheet_create", '{"spreadsheet_token": "Sh1"}', "ou_a", post))

    def test_not_applicable(self):
        post, calls = self.poster()
        self.assertIsNone(ownership.after_tool_call("im_v1_message_create", self.OUT, "ou_a", post))   # 不是建文档的工具
        self.assertIsNone(ownership.after_tool_call("bitable_v1_app_create", self.OUT, None, post))    # 不知道发起人
        self.assertIsNone(ownership.after_tool_call("bitable_v1_app_create", "创建失败", "ou_a", post))  # 返回里没有 token
        self.assertEqual(calls, [])

        def broken(path, payload):
            raise OSError("网络断了")
        self.assertIsNone(ownership.after_tool_call("bitable_v1_app_create", self.OUT, "ou_a", broken))   # 出错不影响主流程

    def test_installed_as_hook(self):
        post, calls = self.poster(0)
        hooks = Hooks()
        ownership.install(hooks, post)
        tools = FakeTools(bitable_v1_app_create=lambda a: self.OUT, other=lambda a: "x")
        llm = FakeLLM(use(("bitable_v1_app_create", {}), ("other", {})), say("建好了"))
        _, new = run(Agent(llm, tools, "S", hooks=hooks).run("建个表", [], requester_open_id="ou_asker"))
        self.assertTrue(new[2]["content"].startswith(self.OUT + "\n\n[系统] 已自动把这份文档的所有者转给你"))
        self.assertEqual(new[3]["content"], "x")
        self.assertEqual(len(calls), 1)


# ====================================================================== 模型适配层
class LLMAdapterTest(unittest.TestCase):
    def test_openai_compat(self):
        seen = {}

        async def create(**kw):
            seen.update(kw)
            calls = [NS(id="c1", function=NS(name="t", arguments=None))] if kw.get("tools") else None
            return NS(choices=[NS(message=NS(content=None if calls else "你好", tool_calls=calls))])

        client = NS(chat=NS(completions=NS(create=create)))
        ad = OpenAICompatAdapter("http://x", "k", "m1", temperature=0.1, max_tokens=99, client=client)
        turn = run(ad.complete([{"role": "user", "content": "hi"}], None))
        self.assertEqual((turn.content, turn.wants_tools, turn.raw_message), ("你好", False, {"role": "assistant", "content": "你好"}))
        self.assertNotIn("tools", seen)
        self.assertEqual((seen["model"], seen["max_tokens"]), ("m1", 99))
        turn = run(ad.complete([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "t"}}]))
        self.assertEqual(seen["tool_choice"], "auto")
        self.assertEqual((turn.tool_calls[0].name, turn.tool_calls[0].arguments), ("t", "{}"))
        self.assertEqual(turn.raw_message["tool_calls"][0], {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}})

    def test_anthropic_translation(self):
        system, out = AnthropicAdapter._to_anthropic([
            {"role": "system", "content": "S"}, {"role": "user", "content": "问"},
            {"role": "assistant", "content": "想一下", "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": '{"a": 1}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"}, {"role": "assistant", "content": "答"}])
        self.assertEqual(system, "S")
        self.assertEqual(out[1]["content"], [{"type": "text", "text": "想一下"}, {"type": "tool_use", "id": "c1", "name": "t", "input": {"a": 1}}])
        self.assertEqual(out[2], {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "结果"}]})

        seen = {}

        async def create(**kw):
            seen.update(kw)
            return NS(content=[NS(type="text", text="好"), NS(type="tool_use", id="u1", name="t", input={"q": "中"})])

        ad = AnthropicAdapter("k", "http://x", "claude", client=NS(messages=NS(create=create)))
        turn = run(ad.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "x"}],
                               [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}]))
        self.assertEqual(seen["tools"], [{"name": "t", "description": "d", "input_schema": {"type": "object"}}])
        self.assertEqual((turn.content, turn.tool_calls[0].arguments), ("好", '{"q": "中"}'))


# ====================================================================== MCP 桥接（不启动真实的 MCP 服务）
class MCPHubTest(unittest.TestCase):
    def hub(self, session, timeout=60.0):
        hub = MCPHub("npx", [], call_timeout=timeout)
        hub._session = session
        run(hub._load_tools())
        return hub

    def test_names_schemas_and_calls(self):
        calls = []

        class Session:
            async def list_tools(self):
                return NS(tools=[NS(name="im.v1.message.create", description="发消息", inputSchema={"type": "object", "properties": {"a": {}}}),
                                 NS(name="im_v1_message_create", description=None, input_schema={"type": "object"}),
                                 NS(name="", description="没名字")])

            async def call_tool(self, name, args):
                calls.append((name, args))
                if name == "im_v1_message_create":
                    return NS(content=[NS(text="出错了")], isError=True)
                return NS(content=[NS(text="第一段"), NS(text=None, data={"k": "v"}), NS(text=None, data=None, type="image")], isError=False)

        hub = self.hub(Session())
        names = [t["function"]["name"] for t in hub.openai_tools()]
        self.assertEqual(names, ["im_v1_message_create", "im_v1_message_create_2"])    # 点号换成下划线，撞名加后缀
        self.assertEqual(hub.openai_tools()[0]["function"]["parameters"]["properties"], {"a": {}})
        self.assertEqual(hub.tool_names(), ["im.v1.message.create", "im_v1_message_create"])
        self.assertEqual(run(hub.call("im_v1_message_create", {"x": 1})), '第一段\n{"k": "v"}\n[image]')
        self.assertEqual(calls[0], ("im.v1.message.create", {"x": 1}))                 # 调用时换回原名
        self.assertEqual(run(hub.call("im_v1_message_create_2", {})), "工具返回错误：出错了")
        self.assertEqual(run(hub.call("nope", {})), "未知工具：nope")
        self.assertIn("未建立", run(MCPHub("npx", []).call("x", {})))

    def test_timeout_error_and_truncation(self):
        class Session:
            async def list_tools(self):
                return NS(tools=[NS(name="slow"), NS(name="boom"), NS(name="big"), NS(name="structured")])

            async def call_tool(self, name, args):
                if name == "slow":
                    await asyncio.sleep(1)
                if name == "boom":
                    raise RuntimeError("子进程挂了")
                if name == "structured":
                    return NS(content=[], structuredContent={"n": 1})
                return NS(content=[NS(text="字" * 20000)])

        hub = self.hub(Session(), timeout=0.05)
        self.assertIn("调用超时", run(hub.call("slow", {})))
        self.assertIn("调用失败：子进程挂了", run(hub.call("boom", {})))
        self.assertTrue(run(hub.call("big", {})).endswith("…（结果过长已截断）"))
        self.assertEqual(run(hub.call("structured", {})), '{"n": 1}')

    def test_helpers(self):
        self.assertEqual(_sanitize("a.b/c d"), "a_b_c_d")
        self.assertEqual(len(_sanitize("x" * 100)), 64)
        self.assertEqual(_sanitize("..."), "tool")
        self.assertEqual(_as_schema(None), {"type": "object", "properties": {}})
        self.assertEqual(_as_schema('{"type": "object"}'), {"type": "object"})
        self.assertEqual(_render_result(NS(content=None)), "（工具返回空结果）")


if __name__ == "__main__":
    unittest.main()
