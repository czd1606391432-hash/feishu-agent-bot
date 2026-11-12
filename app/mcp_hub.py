"""MCP 桥接层。

把一个 MCP 服务（默认是飞书官方的 lark-openapi-mcp）以 stdio 子进程方式拉起，本服务作为 MCP 客户端，
把它暴露的工具转成模型可用的 function calling schema。

为什么要转换：MCP 的工具名形如 `im.v1.message.create`，含点号；
而 OpenAI 兼容协议要求 function 名只能是 [a-zA-Z0-9_-]，所以需要
双向映射一张净化后的名字表。

兼容性说明：MCP Python SDK 在 1.x 和 2.x 之间改了若干字段名
（inputSchema → input_schema、isError → is_error 等），下面的读取
一律走兼容函数，两个版本都能跑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from typing import Any

logger = logging.getLogger(__name__)

_INVALID = re.compile(r"[^a-zA-Z0-9_-]")

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _sanitize(name: str) -> str:
    """把 MCP 工具名转成 OpenAI function 允许的形式，并截断到 64 字符。"""
    cleaned = _INVALID.sub("_", name).strip("_") or "tool"
    return cleaned[:64]


def _pick_attr(obj: Any, *names: str) -> Any:
    """按顺序取第一个存在且非空的属性，兼容 SDK 不同版本的命名。"""
    for name in names:
        value = getattr(obj, name, None)
        if value:
            return value
    # 退一步：从 pydantic 模型的字典表示里找
    for dump_kwargs in ({"by_alias": True}, {}):
        try:
            data = obj.model_dump(**dump_kwargs)  # type: ignore[attr-defined]
        except Exception:
            continue
        for name in names:
            if data.get(name):
                return data[name]
    return None


def _as_schema(raw: Any) -> dict[str, Any]:
    """把工具的入参 schema 规整成普通 dict。"""
    if raw is None:
        return dict(_EMPTY_SCHEMA)
    if isinstance(raw, dict):
        return raw
    try:
        return raw.model_dump(by_alias=True, exclude_none=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return dict(_EMPTY_SCHEMA)


class MCPHub:
    """维护一个长期存活的 MCP 会话，并提供工具列表与调用入口。"""

    def __init__(self, command: str, args: list[str], call_timeout: float = 60.0):
        self._command = command
        self._args = args
        self._call_timeout = call_timeout
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._tools: list[dict[str, Any]] = []          # OpenAI 格式
        self._name_map: dict[str, str] = {}             # 净化名 -> 原始名
        self._lock: asyncio.Lock | None = None          # 第一次调用时再建：锁要建在实际跑它的事件循环里

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        # MCP SDK 用到时才导入：不启用 MCP 的部署不必安装它
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=self._command, args=self._args, env=None)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._stack = stack
            self._session = session
            await self._load_tools()
            logger.info("MCP 已连接，可用工具 %d 个", len(self._tools))
        except Exception:
            await stack.aclose()
            raise

    async def stop(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # 关闭阶段的异常不应影响主流程
                logger.warning("关闭 MCP 会话时出错：%s", exc)
            finally:
                self._stack = None
                self._session = None

    # ---------- 工具 ----------

    async def _load_tools(self) -> None:
        assert self._session is not None
        listed = await self._session.list_tools()
        raw_tools = getattr(listed, "tools", None) or []

        tools: list[dict[str, Any]] = []
        name_map: dict[str, str] = {}
        skipped = 0

        for tool in raw_tools:
            raw_name = getattr(tool, "name", None)
            if not raw_name:
                skipped += 1
                continue

            safe = _sanitize(raw_name)
            # 极小概率的重名：加数字后缀
            if safe in name_map and name_map[safe] != raw_name:
                suffix = 2
                while f"{safe[:60]}_{suffix}" in name_map:
                    suffix += 1
                safe = f"{safe[:60]}_{suffix}"
            name_map[safe] = raw_name

            schema = _as_schema(_pick_attr(tool, "inputSchema", "input_schema", "parameters"))
            description = getattr(tool, "description", None) or raw_name

            tools.append({
                "type": "function",
                "function": {
                    "name": safe,
                    "description": str(description)[:1024],
                    "parameters": schema,
                },
            })

        if skipped:
            logger.warning("有 %d 个工具缺少名称，已跳过", skipped)

        self._tools = tools
        self._name_map = name_map

    def openai_tools(self) -> list[dict[str, Any]]:
        return self._tools

    def has_tools(self) -> bool:
        return bool(self._tools)

    def tool_names(self) -> list[str]:
        return list(self._name_map.values())

    async def call(self, safe_name: str, arguments: dict[str, Any]) -> str:
        """调用一个工具，返回可直接塞回对话的文本结果。"""
        if self._session is None:
            return "工具不可用：MCP 会话未建立。"

        real_name = self._name_map.get(safe_name)
        if real_name is None:
            return f"未知工具：{safe_name}"

        # MCP 会话不是并发安全的，串行化调用
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(real_name, arguments),
                    timeout=self._call_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("工具 %s 调用超时", real_name)
                return f"工具 {real_name} 调用超时（{self._call_timeout:.0f} 秒）。"
            except Exception as exc:
                logger.exception("工具 %s 调用失败", real_name)
                return f"工具 {real_name} 调用失败：{exc}"

        payload = _render_result(result)

        is_error = getattr(result, "isError", None)
        if is_error is None:
            is_error = getattr(result, "is_error", False)
        if is_error:
            return f"工具返回错误：{payload}"

        # 控制塞回模型的长度，避免单次工具结果撑爆上下文
        if len(payload) > 12000:
            payload = payload[:12000] + "\n…（结果过长已截断）"
        return payload


def _render_result(result: Any) -> str:
    """把 CallToolResult 转成纯文本。兼容 structuredContent 等新字段。"""
    parts: list[str] = []

    for item in (getattr(result, "content", None) or []):
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
            continue
        data = getattr(item, "data", None)
        if data and isinstance(data, (dict, list)):
            parts.append(json.dumps(data, ensure_ascii=False))
            continue
        parts.append(f"[{getattr(item, 'type', 'content')}]")

    if not parts:
        structured = _pick_attr(result, "structuredContent", "structured_content")
        if structured is not None:
            try:
                parts.append(json.dumps(structured, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(structured))

    return "\n".join(parts).strip() or "（工具返回空结果）"
