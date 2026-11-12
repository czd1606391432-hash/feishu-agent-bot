"""测试替身：假模型、假工具、假飞书。全部在内存里，不联网、不需要任何凭证和 SDK。"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from types import SimpleNamespace as NS
from typing import Any

from app.llm import AssistantTurn, ToolCall

_ids = itertools.count(1)


def say(text: str) -> AssistantTurn:
    return AssistantTurn(content=text, raw_message={"role": "assistant", "content": text})


def use(*calls: tuple) -> AssistantTurn:
    """use(("工具名", {参数}), ...) → 一次带工具调用的模型输出。参数也可以直接给字符串。"""
    tcs = [ToolCall(id="call_%d" % next(_ids), name=n, arguments=a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
           for n, a in calls]
    raw = {"role": "assistant", "content": "", "tool_calls": [
        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in tcs]}
    return AssistantTurn(content="", tool_calls=tcs, raw_message=raw)


class FakeLLM:
    """按剧本依次返回。剧本项可以是 AssistantTurn，也可以是函数 (messages, tools) -> AssistantTurn。"""

    def __init__(self, *script: Any, forever: Any = None, delay: float = 0):
        self.script, self.forever, self.delay = list(script), forever, delay
        self.calls: list[tuple[list, Any]] = []

    async def complete(self, messages, tools):
        import asyncio
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else self.forever
        if item is None:
            raise AssertionError("FakeLLM 的剧本用完了")
        if isinstance(item, Exception):
            raise item
        return item(messages, tools) if callable(item) else item


class FakeTools:
    """和 ToolRouter 同样接口的假工具集。impl: {工具名: 函数(args) -> str 或抛异常}"""

    def __init__(self, **impl: Any):
        self.impl, self.calls = impl, []

    def openai_tools(self):
        return [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}}
                for n in self.impl]

    def has_tools(self):
        return bool(self.impl)

    async def call(self, name, args):
        self.calls.append((name, args))
        if name not in self.impl:
            return "未知工具：%s" % name
        return self.impl[name](args)


class FakeMessenger:
    def __init__(self):
        self.sent: list[tuple] = []          # (动作, 消息 id, 正文, 页脚, 按钮)
        self.fail_update = False
        self.fail_reply_id = False
        self.changed = threading.Event()
        self._n = itertools.count(1)

    def reply_card(self, message_id, markdown, footer=None, buttons=None):
        self.sent.append(("reply", message_id, markdown, footer, buttons))
        self.changed.set()
        return None if self.fail_reply_id else "card_%d" % next(self._n)

    def update_card(self, message_id, markdown, footer=None, buttons=None):
        if self.fail_update:
            raise RuntimeError("patch 失败")
        self.sent.append(("update", message_id, markdown, footer, buttons))
        self.changed.set()
        return True

    def bodies(self):
        return [x[2] for x in self.sent]


def message_event(text="你好", chat_type="p2p", message_id=None, mentions=(), msg_type="text",
                  sender="ou_user", chat_id="oc_chat", content=None):
    """造一个「接收消息」事件，属性结构与飞书 SDK 的事件对象一致。"""
    if content is None:
        content = json.dumps({"text": text}, ensure_ascii=False)
    return NS(event=NS(
        message=NS(message_id=message_id or "om_%d" % next(_ids), chat_id=chat_id, chat_type=chat_type,
                   message_type=msg_type, content=content,
                   mentions=[NS(id=NS(open_id=m)) for m in mentions]),
        sender=NS(sender_id=NS(open_id=sender))))


def card_event(value, operator="ou_user", chat_id="oc_chat", message_id="om_card", event_id=None):
    return NS(header=NS(event_id=event_id or "ev_%d" % next(_ids)),
              event=NS(action=NS(value=value), operator=NS(open_id=operator),
                       context=NS(open_chat_id=chat_id, open_message_id=message_id)))
