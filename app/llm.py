"""模型适配层。

默认走 OpenAI 兼容协议（DeepSeek / 豆包方舟 / 通义 / 智谱 / OpenAI 都适用），
改 .env 里的 LLM_BASE_URL 和 LLM_MODEL 即可切换厂商。
另外保留一个 Claude 适配器，通过 LLM_PROVIDER=anthropic 启用。

两个适配器对外暴露同一个接口：
    complete(messages, tools) -> AssistantTurn
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串


@dataclass
class AssistantTurn:
    """模型的一次输出：可能是纯文本，也可能带工具调用。"""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMAdapter(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> AssistantTurn: ...


# ---------------------------------------------------------------- OpenAI 兼容


class OpenAICompatAdapter:
    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 4096, client: Any = None):
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=120.0)
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def complete(self, messages, tools):
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (msg.tool_calls or [])
        ]

        # 原样保留 assistant 消息，供下一轮回填对话历史
        raw: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if calls:
            raw["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in calls
            ]

        return AssistantTurn(content=msg.content or "", tool_calls=calls, raw_message=raw)


# ---------------------------------------------------------------- Claude


class AnthropicAdapter:
    """把 OpenAI 风格的消息与工具格式翻译成 Anthropic 的格式，再翻译回来。"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 4096, client: Any = None):
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=120.0)
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    @staticmethod
    def _to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system = ""
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system = m.get("content", "")
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []) or []:
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": args,
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id", ""),
                        "content": m.get("content", ""),
                    }],
                })
            else:
                out.append({"role": "user", "content": m.get("content", "")})
        return system, out

    async def complete(self, messages, tools):
        system, converted = self._to_anthropic(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "system": system,
            "messages": converted,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]

        resp = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(
                    id=block.id, name=block.name, arguments=json.dumps(block.input, ensure_ascii=False)
                ))

        content = "".join(text_parts)
        raw: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            raw["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ]
        return AssistantTurn(content=content, tool_calls=calls, raw_message=raw)


def build_adapter(cfg) -> LLMAdapter:
    if cfg.provider == "anthropic":
        logger.info("模型层：Anthropic %s", cfg.model)
        return AnthropicAdapter(
            api_key=cfg.anthropic_api_key, base_url=cfg.anthropic_base_url,
            model=cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens,
        )
    logger.info("模型层：OpenAI 兼容 %s @ %s", cfg.model, cfg.base_url)
    return OpenAICompatAdapter(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        temperature=cfg.temperature, max_tokens=cfg.max_tokens,
    )
