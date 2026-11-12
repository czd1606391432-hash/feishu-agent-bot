"""Agent 主循环：模型 ↔ 工具的多轮往返。

流程：
    用户输入 → 模型 → (要调工具?) → 钩子检查 → 执行工具 → 结果回填 → 再问模型 → …
                        ↓ 否
                      产出答复

工具结果是外部数据，不是指令。system prompt 里已经写明这一点，
这里额外做长度截断，避免超长返回撑爆上下文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .hooks import Hooks, ToolContext
from .llm import AssistantTurn, LLMAdapter
from .tools import format_arguments

logger = logging.getLogger(__name__)

ProgressHook = Callable[[str], Awaitable[None]]

MAX_TOOL_RESULT_CHARS = 12000


@dataclass
class AgentResult:
    answer: str
    tool_trace: list[str] = field(default_factory=list)
    rounds: int = 0
    buttons: list[dict[str, Any]] = field(default_factory=list)

    @property
    def footer(self) -> str | None:
        if not self.tool_trace:
            return None
        shown = self.tool_trace[:6]
        more = "" if len(self.tool_trace) <= 6 else f" 等 {len(self.tool_trace)} 项"
        return "调用工具：" + "、".join(shown) + more


class Agent:
    def __init__(self, llm: LLMAdapter, tools: Any | None, system_prompt: str,
                 max_tool_rounds: int = 8, hooks: Hooks | None = None):
        self._llm = llm
        self._tools = tools              # ToolRouter 或任何有 openai_tools/has_tools/call 的对象
        self._system_prompt = system_prompt
        self._max_rounds = max_tool_rounds
        self._hooks = hooks or Hooks()

    @property
    def hooks(self) -> Hooks:
        return self._hooks

    async def run(
        self,
        user_text: str,
        history: list[dict[str, Any]],
        on_progress: ProgressHook | None = None,
        requester_open_id: str | None = None,
        session_key: str = "",
    ) -> tuple[AgentResult, list[dict[str, Any]]]:
        """返回 (结果, 本轮新增的消息)。新增消息交给调用方写回会话历史。"""
        hooks = self._hooks
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt + hooks.system_suffix()}]
        messages.extend(history)
        user_msg = {"role": "user", "content": user_text}
        messages.append(user_msg)

        new_messages: list[dict[str, Any]] = [user_msg]
        tools: list[dict[str, Any]] = []
        if self._tools is not None and self._tools.has_tools():
            tools += self._tools.openai_tools()
        tools += hooks.tool_schemas()                  # 本地工具（钩子注册的 Python 函数）
        ctx = ToolContext(
            session_key=session_key, user_text=user_text,
            requester_open_id=requester_open_id or "",
            turn_id=hooks.state.next_turn(session_key),
            state=hooks.state.get(session_key),
        )
        trace: list[str] = []

        def done(answer: str, rounds: int) -> tuple[AgentResult, list[dict[str, Any]]]:
            return AgentResult(answer=answer, tool_trace=trace, rounds=rounds,
                               buttons=hooks.buttons_for(ctx, answer)), new_messages

        for round_index in range(self._max_rounds + 1):
            last_round = round_index == self._max_rounds
            # 最后一轮不再给工具，强制模型给出文字结论
            turn: AssistantTurn = await self._llm.complete(
                messages, None if (last_round or not tools) else tools
            )

            messages.append(turn.raw_message)
            new_messages.append(turn.raw_message)

            if not turn.wants_tools:
                return done(turn.content.strip() or "（模型没有返回内容）", round_index)

            if on_progress is not None:
                names = "、".join(c.name for c in turn.tool_calls)
                await on_progress(f"正在调用工具：{names}…")

            for call in turn.tool_calls:
                trace.append(call.name)
                args = format_arguments(call.arguments)
                logger.info("调用工具 %s args=%s", call.name, str(args)[:300])
                output = await self._run_tool(ctx, call.name, args)
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": output,
                }
                messages.append(tool_msg)
                new_messages.append(tool_msg)

        # 理论上到不了这里（最后一轮已禁用工具）
        return done("处理超出最大轮数，请把问题拆细一些再试。", self._max_rounds)

    async def _run_tool(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        hooks = self._hooks
        try:
            args, blocked = await hooks.run_before(ctx, name, args)
        except Exception as exc:      # 检查本身出错时宁可不执行，也不要绕过检查
            logger.exception("before_tool 钩子出错，工具 %s 未执行", name)
            return f"工具 {name} 未执行：前置检查出错（{exc}）。"
        if blocked is not None:
            logger.info("已拦截工具调用 %s session=%s", name, ctx.session_key)
            return blocked
        try:
            if hooks.owns(name):
                output = await hooks.call_local(ctx, name, args)
            elif self._tools is None:
                output = f"未知工具：{name}"
            else:
                output = await self._tools.call(name, args)
        except Exception as exc:      # 工具报错如实告诉模型，由它向用户说明，不让整轮对话崩掉
            logger.exception("工具 %s 执行出错", name)
            return f"工具 {name} 执行出错：{exc}"
        output = await hooks.run_after(ctx, name, args, output or "")
        if len(output) > MAX_TOOL_RESULT_CHARS:
            output = output[:MAX_TOOL_RESULT_CHARS] + "\n…（结果过长已截断）"
        return output or "（工具返回空结果）"
