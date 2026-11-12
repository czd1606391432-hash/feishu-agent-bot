"""入口。

启动顺序：
  1. 校验配置
  2. 在后台线程里起一个 asyncio 事件循环
  3. 在该循环里连接 MCP（拉起 lark-openapi-mcp 子进程，可关闭）
  4. 加载自定义工具和钩子，组装 Agent 与消息处理器
  5. 在主线程启动飞书长连接（阻塞）

运行：python main.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from app import ownership
from app.agent import Agent
from app.feishu_client import FeishuMessenger, build_client, build_ws_client
from app.handler import MessageHandler
from app.hooks import load_hooks
from app.llm import build_adapter
from app.session import SessionStore
from app.tools import CustomToolRegistry, ToolRouter
from config import config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # SDK 自身日志比较吵
    for noisy in ("lark_oapi", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger("main")


def start_background_loop() -> tuple:
    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=runner, name="agent-loop", daemon=True)
    thread.start()
    return loop, thread


def main() -> int:
    setup_logging(config.log_level)

    problems = config.validate()
    if problems:
        for p in problems:
            logger.error("配置缺失：%s", p)
        logger.error("请复制 .env.example 为 .env 并填写后重试。")
        return 1

    logger.info("飞书开放平台地址：%s", config.feishu.domain)

    loop, _thread = start_background_loop()

    # ---- MCP（飞书官方工具集，可选） ----
    hub = None
    if config.mcp.enabled:
        from app.mcp_hub import MCPHub

        command, args = config.mcp.command(config.feishu)
        hub = MCPHub(command, args)
        try:
            asyncio.run_coroutine_threadsafe(hub.start(), loop).result(timeout=120)
        except Exception as exc:
            logger.error("MCP 启动失败：%s", exc)
            logger.error("请确认已安装 Node.js，且能执行：npx -y @larksuiteoapi/lark-mcp --help")
            logger.warning("将不带飞书工具继续运行。")
            hub = None
    else:
        logger.info("MCP 已关闭（MCP_ENABLED=false），不加载飞书官方工具。")

    # ---- 自定义 HTTP 工具 ----
    custom = CustomToolRegistry(config.agent.custom_tools_file)
    custom.load()

    # ---- 钩子（代码层面的业务规则） ----
    hooks = load_hooks(config.agent.hooks_modules)
    if config.agent.auto_transfer_owner:
        ownership.install(hooks)

    router = ToolRouter(mcp_hub=hub, custom=custom)
    total = len(router.openai_tools()) + len(hooks.tool_schemas())
    if total:
        logger.info("工具总数 %d 个（飞书 %d + 自定义 %d + 本地 %d）",
                    total,
                    len(hub.openai_tools()) if hub else 0,
                    len(custom.openai_tools()),
                    len(hooks.tool_schemas()))
    else:
        logger.warning("当前没有任何可用工具，机器人只能纯聊天。")

    # ---- 模型与 Agent ----
    llm = build_adapter(config.llm)
    agent = Agent(
        llm=llm,
        tools=router,
        system_prompt=config.agent.system_prompt,
        max_tool_rounds=config.agent.max_tool_rounds,
        hooks=hooks,
    )

    # ---- 飞书 ----
    api_client = build_client(config.feishu.app_id, config.feishu.app_secret, config.feishu.domain)
    messenger = FeishuMessenger(api_client)
    sessions = SessionStore(
        max_messages=config.agent.session_max_messages,
        ttl_seconds=config.agent.session_ttl_seconds,
    )
    handler = MessageHandler(
        agent=agent,
        messenger=messenger,
        sessions=sessions,
        loop=loop,
        require_mention_in_group=config.feishu.require_mention_in_group,
        workers=config.agent.workers,
        timeout_seconds=config.agent.timeout_seconds,
        bot_open_id=config.feishu.bot_open_id,
    )

    def on_message(data: P2ImMessageReceiveV1) -> None:
        handler.on_message(data)

    # 长连接模式下 encrypt_key / verification_token 传空字符串即可
    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(handler.on_card_action)   # 卡片按钮点击
        .build()
    )

    ws_client = build_ws_client(
        config.feishu.app_id, config.feishu.app_secret, config.feishu.domain, dispatcher
    )

    stopping = threading.Event()

    def shutdown(signum, frame) -> None:  # noqa: ARG001
        if stopping.is_set():
            return
        stopping.set()
        logger.info("正在退出…")
        handler.shutdown()
        if hub is not None:
            try:
                asyncio.run_coroutine_threadsafe(hub.stop(), loop).result(timeout=15)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("机器人已就绪，等待飞书消息…（Ctrl+C 退出）")
    ws_client.start()   # 阻塞
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
