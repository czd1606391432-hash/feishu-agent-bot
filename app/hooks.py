"""钩子：把「必须由代码保证」的业务规则挂到 Agent 上，而不是只写在提示词里。

提示词只能「要求」模型怎么做，模型可能不照做。真正不能出错的规则
（没确认不许提交、某一步没做完不许进下一步、参数必须带上某个值……）
应该写成代码，在工具调用前后强制执行。这里提供这些挂载点：

    hooks.prompt(text)                 往系统提示词后面追加规则说明
    @hooks.tool(name, desc, params)    注册一个本地工具（进程内的 Python 函数）
    @hooks.before_tool                 工具执行前：放行 / 拦截 / 改参数
    @hooks.after_tool                  工具执行后：改写结果 / 记录状态
    @hooks.on_message                  消息进模型之前：可以直接回复，不走模型
    @hooks.card_buttons                给答复卡片加按钮
    @hooks.card_action                 把按钮点击翻译成一句用户消息

写法见 examples/confirm_gate.py，说明见 docs/hooks.md。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Block:
    """before_tool 返回它：这次工具调用不执行，message 当作工具结果交给模型。"""

    def __init__(self, message: str):
        self.message = message


@dataclass
class ToolContext:
    """一次工具调用的上下文。"""
    session_key: str = ""               # 会话标识（对话 + 用户）
    user_text: str = ""                 # 用户这一轮发的原话（模型改不了，可以放心拿来判断）
    requester_open_id: str = ""         # 发消息的人
    turn_id: int = 0                    # 第几轮用户消息（同一会话内递增）
    state: dict[str, Any] = field(default_factory=dict)   # 会话级状态，跨轮保留
    turn: dict[str, Any] = field(default_factory=dict)    # 本轮状态，这一轮结束就丢


@dataclass
class MessageContext:
    """一条刚收到的消息（还没交给模型）。"""
    session_key: str
    text: str
    sender_open_id: str
    chat_id: str
    is_group: bool
    addressed: bool                     # 是不是在对机器人说话（单聊，或群里 @ 了机器人）
    state: dict[str, Any] = field(default_factory=dict)


class StateStore:
    """会话级状态：每个会话一个 dict，放在内存里，长时间不用自动清掉。"""

    def __init__(self, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}
        self._touched: dict[str, float] = {}
        self._turns: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            last = self._touched.get(key)
            if last is not None and now - last > self._ttl:
                self._data.pop(key, None)
                self._turns.pop(key, None)
            self._touched[key] = now
            return self._data.setdefault(key, {})

    def next_turn(self, key: str) -> int:
        self.get(key)
        with self._lock:
            self._turns[key] = self._turns.get(key, 0) + 1
            return self._turns[key]

    def clear(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._touched.pop(key, None)
            self._turns.pop(key, None)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class Hooks:
    def __init__(self, state_ttl_seconds: int = 3600):
        self.prompt_rules: list[str] = []
        self.state = StateStore(state_ttl_seconds)
        self._tools: dict[str, tuple[dict[str, Any], Callable[..., Any]]] = {}
        self._before: list[Callable[..., Any]] = []
        self._after: list[Callable[..., Any]] = []
        self._on_message: list[Callable[..., Any]] = []
        self._card_buttons: list[Callable[..., Any]] = []
        self._card_action: list[Callable[..., Any]] = []

    # ---------- 注册 ----------

    def prompt(self, text: str) -> None:
        if text and text.strip():
            self.prompt_rules.append(text.strip())

    def tool(self, name: str, description: str, parameters: dict[str, Any] | None = None):
        """注册本地工具。函数签名 fn(ctx, args) -> str，可以是普通函数也可以是 async。"""
        def deco(fn):
            self._tools[name] = ({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description[:1024],
                    "parameters": parameters or {"type": "object", "properties": {}},
                },
            }, fn)
            return fn
        return deco

    def before_tool(self, fn):
        """fn(ctx, name, args)：返回 None 放行；返回 Block(...) 拦截；返回 dict 则用它替换参数。"""
        self._before.append(fn)
        return fn

    def after_tool(self, fn):
        """fn(ctx, name, args, output)：返回 None 不改；返回字符串则替换工具结果。"""
        self._after.append(fn)
        return fn

    def on_message(self, fn):
        """fn(msg: MessageContext)：返回字符串就直接回复用户、不再交给模型；返回 None 继续。"""
        self._on_message.append(fn)
        return fn

    def card_buttons(self, fn):
        """fn(ctx, answer) -> [{"text": "确认", "say": "确认", "type": "primary"}, ...] 或 None。"""
        self._card_buttons.append(fn)
        return fn

    def card_action(self, fn):
        """fn(value: dict) -> 一句话 或 None。默认规则：按钮的 value 里有 "say" 就用它。"""
        self._card_action.append(fn)
        return fn

    # ---------- 给 Agent / Handler 调用 ----------

    def system_suffix(self) -> str:
        return "".join("\n\n" + r for r in self.prompt_rules)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [schema for schema, _ in self._tools.values()]

    def owns(self, name: str) -> bool:
        return name in self._tools

    async def call_local(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        _, fn = self._tools[name]
        out = await _maybe_await(fn(ctx, args))
        return "" if out is None else str(out)

    async def run_before(self, ctx: ToolContext, name: str, args: dict[str, Any]):
        """返回 (参数, 拦截提示)。拦截提示不为 None 时不执行工具。"""
        for fn in self._before:
            got = await _maybe_await(fn(ctx, name, args))
            if isinstance(got, Block):
                return args, got.message
            if isinstance(got, dict):
                args = got
        return args, None

    async def run_after(self, ctx: ToolContext, name: str, args: dict[str, Any], output: str) -> str:
        for fn in self._after:
            try:
                got = await _maybe_await(fn(ctx, name, args, output))
            except Exception as exc:      # 事后处理失败不能影响主流程
                logger.warning("after_tool 钩子出错，已忽略：%s", exc)
                continue
            if isinstance(got, str):
                output = got
        return output

    def run_on_message(self, msg: MessageContext) -> str | None:
        for fn in self._on_message:
            got = fn(msg)
            if isinstance(got, str) and got:
                return got
        return None

    def buttons_for(self, ctx: ToolContext, answer: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for fn in self._card_buttons:
            try:
                out += list(fn(ctx, answer) or [])
            except Exception as exc:
                logger.warning("card_buttons 钩子出错，已忽略：%s", exc)
        return out

    def text_for_action(self, value: dict[str, Any]) -> str:
        for fn in self._card_action:
            got = fn(value)
            if isinstance(got, str) and got:
                return got
        say = value.get("say") if isinstance(value, dict) else None
        return str(say) if say else ""


def load_hooks(module_names: str, hooks: Hooks | None = None) -> Hooks:
    """按逗号分隔的模块名加载钩子。每个模块提供一个 setup(hooks) 函数。"""
    hooks = hooks or Hooks()
    for name in [x.strip() for x in (module_names or "").split(",") if x.strip()]:
        mod = importlib.import_module(name)
        if not hasattr(mod, "setup"):
            raise RuntimeError(f"钩子模块 {name} 里没有 setup(hooks) 函数")
        mod.setup(hooks)
        logger.info("已加载钩子模块 %s", name)
    return hooks
