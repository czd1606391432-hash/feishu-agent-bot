"""示例钩子：有副作用的操作，必须由用户本人确认后才真正执行。

只在提示词里写「先确认再执行」是不够的——模型可能自己替用户「确认」，
也可能在确认之后悄悄改了参数。这里用代码把三件事定死：

  1. 第一次调用受保护的工具时一律不执行，只把内容记下来，让模型先给用户看。
  2. 只有用户的下一条消息本身是确认（含确认词、且不含修改词）才放行；
     模型在同一轮里自己说「已确认」没有用。
  3. 放行时执行的是用户看过的那一份参数，不是模型这次新给的。

启用：在 .env 里写  HOOKS_MODULES=examples.confirm_gate
哪些工具受保护：环境变量 CONFIRM_TOOLS（逗号分隔，默认 create_ticket）。
"""

from __future__ import annotations

import os

from app.hooks import Block

CONFIRM_WORDS = ("确认", "可以", "没问题", "提交吧", "就这样", "ok", "OK", "好的")
CHANGE_WORDS = ("改", "不对", "换成", "先别", "等等", "等一下", "取消", "不要", "不用", "算了")

BLOCK_MSG = (
    "这次没有执行：还没有得到用户确认。"
    "请把将要提交的内容原样列给用户，并请他回复「确认」；"
    "用户想修改就按他的意思重新发起。不要替用户确认。"
)


def is_confirm(text: str) -> bool:
    text = (text or "").strip()
    return any(w in text for w in CONFIRM_WORDS) and not any(w in text for w in CHANGE_WORDS)


def guarded_tools() -> set:
    return {x.strip() for x in os.getenv("CONFIRM_TOOLS", "create_ticket").split(",") if x.strip()}


def setup(hooks) -> None:
    hooks.prompt(
        "【提交类操作的规则】创建、提交、发送这类操作第一次调用时不会真的执行，"
        "系统会要求你先把内容给用户确认。用户确认后，再调用一次同一个工具即可。"
    )

    @hooks.on_message
    def drop_stale(msg):
        # 待确认的内容只对「紧接着的下一条消息」有效：用户说了别的，就作废，免得很久以后一句「好的」误触发
        if msg.addressed and msg.state.get("pending") and not is_confirm(msg.text):
            msg.state.pop("pending", None)
        return None

    @hooks.before_tool
    def gate(ctx, name, args):
        if name not in guarded_tools():
            return None
        pending = ctx.state.get("pending")
        if (pending and pending["tool"] == name and pending["turn"] < ctx.turn_id
                and is_confirm(ctx.user_text)):
            ctx.state.pop("pending", None)
            return pending["args"]                 # 放行，并且用的是用户看过的那份参数
        ctx.state["pending"] = {"tool": name, "args": args, "turn": ctx.turn_id}
        ctx.turn["need_confirm"] = True
        return Block(BLOCK_MSG)

    @hooks.card_buttons
    def buttons(ctx, answer):
        if ctx.turn.get("need_confirm"):
            return [{"text": "确认", "say": "确认", "type": "primary"}, {"text": "取消", "say": "取消"}]
        return None
