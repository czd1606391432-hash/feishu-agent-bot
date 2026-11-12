"""事件处理：飞书消息 → Agent → 回写卡片。

线程模型：
  - 飞书长连接客户端在自己的线程里回调，回调必须尽快返回，否则会拖慢事件消费；
    所以这里把每条消息丢进线程池，回调立即返回。
  - Agent 是异步的，跑在一个专用的 asyncio 事件循环线程里；
    工作线程用 run_coroutine_threadsafe 提交任务并等待结果。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .agent import Agent
from .events import IncomingMessage, parse_message_event
from .hooks import MessageContext
from .session import Deduplicator, SessionStore

logger = logging.getLogger(__name__)

RESET_COMMANDS = {"/reset", "/clear", "清空上下文", "重置对话"}
HELP_COMMANDS = {"/help", "帮助", "怎么用"}

HELP_TEXT = (
    "**怎么用**\n"
    "- 单聊直接发消息\n"
    "- 群里 @我 再说需求\n"
    "- 发送 `/reset` 清空当前对话上下文\n\n"
    "涉及发消息、改数据这类有副作用的操作，我会先跟你确认再执行。"
)

BUTTON_UNAVAILABLE = "按钮暂时不可用，请直接回复文字"


def _sdk_card_response(payload: dict[str, Any]) -> Any:
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    return P2CardActionTriggerResponse(payload)


class MessageHandler:
    def __init__(
        self,
        agent: Agent,
        messenger: Any,
        sessions: SessionStore,
        loop: asyncio.AbstractEventLoop,
        require_mention_in_group: bool = True,
        workers: int = 8,
        timeout_seconds: float = 300,
        bot_open_id: str = "",
        help_text: str = HELP_TEXT,
        card_response: Callable[[dict[str, Any]], Any] = _sdk_card_response,
    ):
        self._agent = agent
        self._messenger = messenger
        self._sessions = sessions
        self._loop = loop
        self._require_mention = require_mention_in_group
        self._timeout = timeout_seconds
        self._bot_open_id = bot_open_id
        self._help_text = help_text
        self._card_response = card_response
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="msg")
        self._dedup = Deduplicator()

    # ---------- 入口：收到消息（由飞书 SDK 回调） ----------

    def on_message(self, event: Any) -> None:
        try:
            incoming = parse_message_event(event, self._bot_open_id)
        except Exception:
            logger.exception("解析事件失败")
            return
        if incoming is None:
            return
        if self._dedup.seen(incoming.message_id):
            logger.debug("重复事件已忽略 message_id=%s", incoming.message_id)
            return
        self._pool.submit(self._safe_handle, incoming)

    # ---------- 入口：卡片按钮点击（长连接回调） ----------

    def on_card_action(self, data: Any) -> Any:
        """卡片按钮 = 点击人在这个对话里发了一句话。
        之后走和文字消息完全相同的流程，所有代码层面的检查照样生效。
        必须尽快返回，否则飞书会提示点击失败。"""
        try:
            ev = data.event
            value = (ev.action.value if ev and ev.action else None) or {}
            text = self._agent.hooks.text_for_action(value)
            open_id = getattr(ev.operator, "open_id", "") or ""
            chat_id = getattr(ev.context, "open_chat_id", "") or ""
            message_id = getattr(ev.context, "open_message_id", "") or ""
            event_id = getattr(getattr(data, "header", None), "event_id", "") or ""
        except Exception:
            logger.exception("解析卡片点击失败")
            return self._card_response({"toast": {"type": "error", "content": BUTTON_UNAVAILABLE}})
        if not (text and open_id and chat_id and message_id):
            return self._card_response({"toast": {"type": "error", "content": BUTTON_UNAVAILABLE}})
        if event_id and self._dedup.seen("card:" + event_id):
            return self._card_response({"toast": {"type": "info", "content": "正在处理…"}})
        logger.info("卡片点击 sender=%s", open_id)
        msg = IncomingMessage(message_id=message_id, chat_id=chat_id, chat_type="card", message_type="text",
                              text=text, sender_open_id=open_id, mentioned_bot=True)
        self._pool.submit(self._safe_handle, msg)
        return self._card_response({"toast": {"type": "info", "content": f"收到：{text}"}})

    # ---------- 工作线程 ----------

    def _safe_handle(self, msg: IncomingMessage) -> None:
        try:
            self._handle(msg)
        except Exception:
            logger.exception("处理消息时出错 message_id=%s", msg.message_id)
            try:
                self._messenger.reply_card(msg.message_id, "抱歉，处理时出错了，请稍后再试。")
            except Exception:
                logger.exception("兜底回复也失败了")

    def _handle(self, msg: IncomingMessage) -> None:
        hooks = self._agent.hooks
        key = SessionStore.key(msg.chat_id, msg.sender_open_id)
        addressed = (not msg.is_group) or msg.mentioned_bot or not self._require_mention

        # 钩子可以在消息进模型之前截下来直接回复（比如收集一段不该进模型的信息）
        if msg.text:
            reply = hooks.run_on_message(MessageContext(
                session_key=key, text=msg.text.strip(), sender_open_id=msg.sender_open_id,
                chat_id=msg.chat_id, is_group=msg.is_group, addressed=addressed,
                state=hooks.state.get(key)))
            if reply:
                self._messenger.reply_card(msg.message_id, reply)
                return

        # 群聊里默认要求被 @ 才响应，避免刷屏
        if not addressed:
            return

        if not msg.text:
            self._messenger.reply_card(
                msg.message_id,
                "我目前只能处理文字消息。图片、文件等类型还不支持。",
            )
            return

        text = msg.text.strip()
        logger.info("收到消息 chat_type=%s sender=%s", msg.chat_type, msg.sender_open_id)

        if text in RESET_COMMANDS:
            self._sessions.clear(key)
            hooks.state.clear(key)
            self._messenger.reply_card(msg.message_id, "已清空上下文，我们重新开始。")
            return

        if text in HELP_COMMANDS:
            self._messenger.reply_card(msg.message_id, self._help_text)
            return

        # 先回一张占位卡片，让用户立刻看到反馈
        card_id = self._messenger.reply_card(msg.message_id, "正在处理…")

        async def on_progress(note: str) -> None:
            # 进度提示纯属锦上添花，失败绝不能影响主流程
            if not card_id:
                return
            try:
                await self._loop.run_in_executor(
                    None, self._messenger.update_card, card_id, note
                )
            except Exception as exc:
                logger.warning("更新进度卡片失败，已忽略：%s", exc)

        history = self._sessions.history(key)
        future = asyncio.run_coroutine_threadsafe(
            self._agent.run(text, history, on_progress if card_id else None,
                            requester_open_id=msg.sender_open_id, session_key=key),
            self._loop,
        )

        try:
            result, new_messages = future.result(timeout=self._timeout)
        except Exception as exc:
            future.cancel()
            logger.exception("Agent 执行失败")
            timed_out = isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError))
            reason = f"超过 {self._timeout:.0f} 秒没有完成" if timed_out else str(exc)
            self._deliver(msg, card_id, f"处理失败：{reason}")
            return

        self._sessions.extend(key, new_messages)

        self._deliver(msg, card_id, result.answer, result.footer, result.buttons)

    def _deliver(self, msg: IncomingMessage, card_id: str | None, body: str,
                 footer: str | None = None, buttons: list[dict[str, Any]] | None = None) -> None:
        """把答复送达用户：优先原地更新卡片，失败则退回发新消息。"""
        if card_id:
            try:
                if self._messenger.update_card(card_id, body, footer, buttons):
                    return
            except Exception as exc:
                logger.warning("更新结果卡片失败，改为新发一条：%s", exc)
        try:
            self._messenger.reply_card(msg.message_id, body, footer, buttons)
        except Exception:
            logger.exception("回复消息彻底失败 message_id=%s", msg.message_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
