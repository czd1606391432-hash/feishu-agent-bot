"""飞书 API 封装：回复消息、更新卡片。

只依赖飞书官方 SDK lark-oapi。所有网络调用都是同步的（SDK 本身如此），
调用方在线程池里执行，不会阻塞事件循环。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .events import build_card

logger = logging.getLogger(__name__)


def build_client(app_id: str, app_secret: str, domain: str) -> lark.Client:
    builder = lark.Client.builder().app_id(app_id).app_secret(app_secret)
    # 私有化部署传自定义域名；公有云传官方地址
    try:
        builder = builder.domain(domain)
    except Exception as exc:  # 极老版本 SDK 没有 domain
        logger.warning("SDK 不支持自定义 domain，将使用默认地址：%s", exc)
    return builder.log_level(lark.LogLevel.INFO).build()


def build_ws_client(app_id: str, app_secret: str, domain: str, event_handler) -> Any:
    """构造长连接客户端。不同 SDK 版本对 domain 参数的支持不一致，做一次探测。"""
    kwargs: dict[str, Any] = {
        "event_handler": event_handler,
        "log_level": lark.LogLevel.INFO,
    }
    try:
        params = inspect.signature(lark.ws.Client.__init__).parameters
        if "domain" in params:
            kwargs["domain"] = domain
        else:
            logger.warning(
                "当前 lark-oapi 版本的长连接客户端不接受 domain 参数。"
                "若你是私有化部署，请升级 SDK，或改用 Webhook 接入方式。"
            )
    except (TypeError, ValueError):
        pass
    return lark.ws.Client(app_id, app_secret, **kwargs)


def _with_retry(fn, *args, attempts: int = 3, base_delay: float = 0.6, **kwargs):
    """飞书接口的瞬时故障（SSL EOF、连接重置、超时）重试几次再放弃。

    企业网络里 TLS 握手偶发中断很常见，重试一次通常就好。
    """
    delay = base_delay
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if i == attempts - 1:
                raise
            logger.warning("飞书接口调用失败（第 %d/%d 次），%.1f 秒后重试：%s",
                           i + 1, attempts, delay, exc)
            time.sleep(delay)
            delay *= 2


class FeishuMessenger:
    def __init__(self, client: lark.Client):
        self._client = client

    def reply_card(self, message_id: str, markdown: str, footer: str | None = None,
                   buttons: list[dict[str, Any]] | None = None) -> str | None:
        """以卡片形式回复某条消息，返回新消息的 message_id（用于后续更新）。"""
        card = build_card(markdown, footer, buttons)
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .msg_type("interactive")
                .build()
            )
            .build()
        )
        resp = _with_retry(self._client.im.v1.message.reply, req)
        if not resp.success():
            logger.error("回复消息失败 code=%s msg=%s", resp.code, resp.msg)
            return None
        return getattr(resp.data, "message_id", None)

    def update_card(self, message_id: str, markdown: str, footer: str | None = None,
                    buttons: list[dict[str, Any]] | None = None) -> bool:
        card = build_card(markdown, footer, buttons)
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = _with_retry(self._client.im.v1.message.patch, req)
        if not resp.success():
            logger.error("更新卡片失败 code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    def send_text(self, chat_id: str, text: str) -> bool:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = _with_retry(self._client.im.v1.message.create, req)
        if not resp.success():
            logger.error("发送消息失败 code=%s msg=%s", resp.code, resp.msg)
            return False
        return True
