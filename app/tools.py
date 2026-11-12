"""自定义 HTTP 工具 + 工具路由。

让你把任意 REST API 声明成模型可调用的工具，不用改代码——只改 custom_tools.json。

设计要点：
  · CustomToolRegistry 和 MCPHub 暴露同样的三个方法
    （openai_tools / has_tools / call），所以 ToolRouter 能把两者
    透明地拼在一起，Agent 那边一行都不用改。
  · 密钥不写进 JSON，写 ${ENV_VAR}，运行时从环境变量取。
  · 模型填的参数用 {{param}} 占位，支持在 url / query / body / header 里替换。
  · response 段可以裁剪返回内容，只把模型真正需要的字段塞回上下文。
  · 发请求只用标准库（urllib 放在线程里跑），不额外引入 HTTP 依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PARAM_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

DEFAULT_MAX_RESULT_CHARS = 12000


class CustomToolError(Exception):
    pass


class HttpTimeout(Exception):
    pass


def format_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """模型返回的 arguments 可能是 JSON 字符串，也可能已是 dict。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        logger.warning("无法解析工具参数：%s", raw[:200])
        return {}


def _expand_env(value: str) -> str:
    """把 ${VAR} 换成环境变量的值。变量不存在则报错，避免带着空密钥去请求。"""
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        got = os.getenv(name)
        if got is None:
            raise CustomToolError(f"环境变量 {name} 未设置（在 .env 里补上）")
        return got
    return _ENV_REF.sub(sub, value)


def _fill_params(value: Any, params: dict[str, Any]) -> Any:
    """递归替换 {{param}}。整个字符串就是一个占位符时，保留原始类型（数字/布尔/数组）。"""
    if isinstance(value, str):
        whole = _PARAM_REF.fullmatch(value.strip())
        if whole:
            return params.get(whole.group(1))
        return _PARAM_REF.sub(lambda m: str(params.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {k: _fill_params(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_params(v, params) for v in value]
    return value


def _fill_url(url: str, params: dict[str, Any]) -> str:
    """URL 里的 {{param}} 要做转义，免得参数里的 / ? # 把路径改掉。"""
    return _PARAM_REF.sub(lambda m: urllib.parse.quote(str(params.get(m.group(1), "")), safe=""), url)


def _drop_empty(mapping: dict[str, Any]) -> dict[str, Any]:
    """去掉没填的可选参数，避免发出 ?month= 这种空查询串。"""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def _dig(data: Any, path: str) -> Any:
    """按点号路径取值，例如 "data.items"。取不到返回 None。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _shape_response(payload: Any, shape: dict[str, Any]) -> Any:
    """按 response 配置裁剪返回值。

    response.extract: "data.summary"      只取这一个字段（保留原始文本格式）
    response.fields:  ["a", "b", "c"]     只保留这几个顶层字段
    两者都没配就原样返回。
    """
    if not shape or not isinstance(payload, (dict, list)):
        return payload

    extract = shape.get("extract")
    if extract:
        got = _dig(payload, str(extract))
        return got if got is not None else payload

    fields = shape.get("fields")
    if fields and isinstance(payload, dict):
        return {k: payload[k] for k in fields if k in payload}

    return payload


def http_send(method: str, url: str, headers: dict[str, str], query: dict[str, Any],
              body: Any, timeout: float) -> tuple[int, str, str]:
    """发一个 HTTP 请求，返回 (状态码, Content-Type, 响应文本)。超时抛 HttpTimeout。"""
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
    data = None
    hdr = dict(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdr.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read().decode("utf-8", "replace")
    except (socket.timeout, TimeoutError) as exc:
        raise HttpTimeout(str(exc))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise HttpTimeout(str(exc.reason))
        raise


class CustomToolRegistry:
    """从 JSON 文件加载一组 HTTP 工具。"""

    def __init__(self, path: str | Path | None, sender: Callable[..., tuple[int, str, str]] = http_send):
        self._path = Path(path) if path else None
        self._send = sender
        self._specs: dict[str, dict[str, Any]] = {}
        self._tools: list[dict[str, Any]] = []

    def load(self) -> None:
        if self._path is None:
            return
        if not self._path.exists():
            logger.info("未找到自定义工具配置 %s，跳过", self._path)
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("自定义工具配置不是合法 JSON：%s", exc)
            return

        if not isinstance(raw, list):
            logger.error("自定义工具配置的最外层必须是数组 []")
            return

        specs: dict[str, dict[str, Any]] = {}
        tools: list[dict[str, Any]] = []

        for index, item in enumerate(raw):
            try:
                name = item["name"]
                description = item["description"]
                request = item["request"]
                if not request.get("url"):
                    raise KeyError("request.url")
            except (KeyError, TypeError) as exc:
                logger.error("第 %d 个自定义工具配置不完整，缺少 %s，已跳过", index + 1, exc)
                continue

            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                logger.error("工具名 %r 不合法（只能是字母数字下划线连字符，≤64 字符），已跳过", name)
                continue

            specs[name] = item
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(description)[:1024],
                    "parameters": item.get("parameters") or {"type": "object", "properties": {}},
                },
            })

        self._specs = specs
        self._tools = tools
        if tools:
            logger.info("已加载自定义工具 %d 个：%s", len(tools), "、".join(specs))

    # ---- 与 MCPHub 一致的接口 ----

    def openai_tools(self) -> list[dict[str, Any]]:
        return self._tools

    def has_tools(self) -> bool:
        return bool(self._tools)

    def owns(self, name: str) -> bool:
        return name in self._specs

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._specs.get(name)
        if spec is None:
            return f"未知工具：{name}"

        req = spec["request"]
        try:
            # 先展开环境变量、再填模型给的参数：顺序反过来的话，模型（或被注入的内容）
            # 只要把参数写成 "${某个密钥}"，就能让密钥被代入请求发出去
            url = _fill_url(_expand_env(req["url"]), arguments)
            method = str(req.get("method", "GET")).upper()
            timeout = float(req.get("timeout", 30))

            headers = {
                k: str(_fill_params(_expand_env(str(v)), arguments))
                for k, v in (req.get("headers") or {}).items()
            }
            query = _drop_empty(_fill_params(req.get("query") or {}, arguments))
            body = _drop_empty(_fill_params(req["json"], arguments)) if "json" in req else None
        except CustomToolError as exc:
            logger.error("工具 %s 配置有问题：%s", name, exc)
            return f"工具 {name} 配置有问题：{exc}"
        except Exception as exc:
            logger.exception("工具 %s 参数处理失败", name)
            return f"工具 {name} 参数处理失败：{exc}"

        logger.info("调用自定义工具 %s %s %s", name, method, url)

        try:
            status, ctype, text = await asyncio.to_thread(self._send, method, url, headers, query, body, timeout)
        except HttpTimeout:
            return (f"工具 {name} 请求超时（{timeout:.0f} 秒）。"
                    f"如果这个接口本来就慢，把配置里的 timeout 调大。")
        except Exception as exc:
            logger.exception("工具 %s 请求失败", name)
            return f"工具 {name} 请求失败：{exc}"

        shape = spec.get("response") or {}
        max_chars = int(shape.get("max_chars", DEFAULT_MAX_RESULT_CHARS))

        # 尽量按 JSON 解析，好做裁剪
        parsed: Any = None
        if "json" in ctype.lower() or text.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None

        if status >= 400:
            detail = json.dumps(parsed, ensure_ascii=False) if parsed is not None else text
            return f"接口返回 HTTP {status}：{detail[:2000]}"

        if parsed is not None:
            shaped = _shape_response(parsed, shape)
            if isinstance(shaped, str):
                text = shaped                       # extract 出来的就是纯文本，保持原样
            else:
                text = json.dumps(shaped, ensure_ascii=False)

        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（结果过长已截断）"
        return text or "（接口返回空内容）"


class ToolRouter:
    """把 MCP 工具和自定义工具合并成一份工具表，并按名字分发调用。"""

    def __init__(self, mcp_hub: Any | None = None, custom: CustomToolRegistry | None = None):
        self._hub = mcp_hub
        self._custom = custom

    def openai_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        # 自定义工具优先，重名时盖过 MCP 的同名工具
        for source in (self._custom, self._hub):
            if source is None:
                continue
            for tool in source.openai_tools():
                name = tool["function"]["name"]
                if name in seen:
                    logger.warning("工具名冲突 %s，已忽略后一个", name)
                    continue
                seen.add(name)
                tools.append(tool)
        return tools

    def has_tools(self) -> bool:
        return bool(self.openai_tools())

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._custom is not None and self._custom.owns(name):
            return await self._custom.call(name, arguments)
        if self._hub is not None:
            return await self._hub.call(name, arguments)
        return f"未知工具：{name}"
