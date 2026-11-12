"""事件解析与卡片拼装。这里不依赖飞书 SDK，只按属性名读事件对象，方便单独测试。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 文本消息里 @ 某人会留下 @_user_1 这样的占位符
_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+")


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str          # "p2p" 单聊 / "group" 群聊 / "card" 卡片按钮点击
    message_type: str       # "text" / "post" / ...
    text: str               # 已剥离 @ 占位符的纯文本
    sender_open_id: str
    mentioned_bot: bool

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


def parse_message_event(event: Any, bot_open_id: str = "") -> IncomingMessage | None:
    """把「接收消息」事件转成内部结构。无法处理时返回 None。

    bot_open_id 填了的话，只有 @ 的是机器人本人才算被 @；
    不填则消息里 @ 了任何人都算（群里 @ 别人时机器人也会响应）。
    """
    try:
        message = event.event.message
        sender = event.event.sender
    except AttributeError:
        logger.warning("事件结构异常，已跳过")
        return None

    msg_type = getattr(message, "message_type", "") or ""
    raw_content = getattr(message, "content", "") or "{}"

    text = ""
    if msg_type == "text":
        try:
            text = json.loads(raw_content).get("text", "")
        except json.JSONDecodeError:
            text = ""
    elif msg_type == "post":
        text = _flatten_post(raw_content)
    else:
        # 图片、文件等暂不处理，交由上层给出提示
        text = ""

    mentions = getattr(message, "mentions", None) or []
    mentioned_ids = [getattr(getattr(m, "id", None), "open_id", None) for m in mentions]
    if bot_open_id:
        mentioned_bot = bot_open_id in mentioned_ids
    else:
        mentioned_bot = any(mentioned_ids)

    sender_open_id = ""
    sender_id = getattr(sender, "sender_id", None)
    if sender_id is not None:
        sender_open_id = getattr(sender_id, "open_id", "") or ""

    return IncomingMessage(
        message_id=getattr(message, "message_id", "") or "",
        chat_id=getattr(message, "chat_id", "") or "",
        chat_type=getattr(message, "chat_type", "") or "",
        message_type=msg_type,
        text=_MENTION_PLACEHOLDER.sub("", text).strip(),
        sender_open_id=sender_open_id,
        mentioned_bot=mentioned_bot,
    )


def _flatten_post(raw_content: str) -> str:
    """富文本消息取其中的文本片段拼起来。"""
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError:
        return ""
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "text" and node.get("text"):
                chunks.append(str(node["text"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return " ".join(chunks)


def _button(spec: dict[str, Any]) -> dict[str, Any] | None:
    """按钮写法：{"text": "确认", "say": "确认"} 点了等于用户发「确认」；
    {"text": "打开", "url": "https://…"} 跳转链接；{"text": "…", "value": {...}} 自定义回调值。"""
    label = str(spec.get("text") or "").strip()
    if not label:
        return None
    b: dict[str, Any] = {"tag": "button", "text": {"tag": "plain_text", "content": label},
                         "type": spec.get("type") or "default"}
    if spec.get("url"):
        b["url"] = str(spec["url"])
    elif isinstance(spec.get("value"), dict):
        b["value"] = spec["value"]
    elif spec.get("say"):
        b["value"] = {"say": str(spec["say"])}
    else:
        return None
    return b


def build_card(markdown: str, footer: str | None = None,
               buttons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": markdown or " "}]
    actions = [b for b in (_button(x) for x in (buttons or [])) if b]
    if actions:
        elements.append({"tag": "action", "actions": actions})
    if footer:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": footer}],
        })
    return {"config": {"wide_screen_mode": True}, "elements": elements}
