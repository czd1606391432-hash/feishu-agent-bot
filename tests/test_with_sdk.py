"""需要真实 SDK 的测试：装了 lark-oapi / mcp 才跑，没装自动跳过。仍然不联网、不需要凭证。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HAS_LARK = importlib.util.find_spec("lark_oapi") is not None
HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_LARK, "没装 lark-oapi")
class LarkSdkTest(unittest.TestCase):
    def test_real_event_objects_parse(self):
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

        from app.events import parse_message_event

        ev = P2ImMessageReceiveV1({"schema": "2.0", "header": {"event_id": "e1"}, "event": {
            "sender": {"sender_id": {"open_id": "ou_x"}},
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group", "message_type": "text",
                        "content": json.dumps({"text": "@_user_1 你好"}),
                        "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "bot"}]}}})
        m = parse_message_event(ev, bot_open_id="ou_bot")
        self.assertEqual((m.message_id, m.chat_id, m.text, m.sender_open_id, m.mentioned_bot, m.is_group),
                         ("om_1", "oc_1", "你好", "ou_x", True, True))
        ce = P2CardActionTrigger({"schema": "2.0", "header": {"event_id": "c1"}, "event": {
            "operator": {"open_id": "ou_x"}, "action": {"value": {"say": "确认"}, "tag": "button"},
            "context": {"open_message_id": "om_2", "open_chat_id": "oc_1"}}})
        self.assertEqual((ce.event.action.value, ce.event.operator.open_id, ce.event.context.open_chat_id,
                          ce.event.context.open_message_id, ce.header.event_id),
                         ({"say": "确认"}, "ou_x", "oc_1", "om_2", "c1"))

    def test_sdk_entry_points_exist(self):
        import inspect

        import lark_oapi as lark

        from app.feishu_client import FeishuMessenger, build_client, build_ws_client
        from app.handler import _sdk_card_response

        builder = lark.EventDispatcherHandler.builder("", "")
        dispatcher = (builder.register_p2_im_message_receive_v1(lambda d: None)
                      .register_p2_card_action_trigger(lambda d: None).build())
        self.assertIn("domain", inspect.signature(lark.ws.Client.__init__).parameters)
        build_ws_client("cli_x", "secret", "https://open.feishu.cn", dispatcher)      # 只构造，不连接
        FeishuMessenger(build_client("cli_x", "secret", "https://open.feishu.cn"))
        resp = _sdk_card_response({"toast": {"type": "info", "content": "收到"}})
        self.assertEqual(resp.toast.content, "收到")


@unittest.skipUnless(HAS_MCP, "没装 mcp")
class McpSdkTest(unittest.TestCase):
    def test_hub_against_real_stdio_server(self):
        from app.mcp_hub import MCPHub

        async def go():
            hub = MCPHub(sys.executable, [os.path.join(ROOT, "tests", "mcp_demo_server.py")], call_timeout=20)
            await hub.start()
            try:
                names = sorted(t["function"]["name"] for t in hub.openai_tools())
                schema = next(t for t in hub.openai_tools() if t["function"]["name"] == "math_add")["function"]["parameters"]
                return (names, sorted(schema.get("properties", {})), await hub.call("math_add", {"a": 2, "b": 3}),
                        await hub.call("text_repeat", {"text": "好", "times": 3}), await hub.call("math_add", {"a": "x"}))
            finally:
                await hub.stop()

        names, props, total, repeated, bad = asyncio.run(go())
        self.assertEqual(names, ["math_add", "text_repeat"])       # 带点号的工具名被净化
        self.assertEqual(props, ["a", "b"])
        self.assertEqual((total, repeated), ("5", "好好好"))
        self.assertTrue(bad.startswith("工具返回错误"), bad)        # 参数不对 → 错误如实返回


if __name__ == "__main__":
    unittest.main()
