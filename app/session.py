"""会话上下文。

按「会话 + 用户」隔离，每个 key 保留有限条历史，超时自动清空。
默认放在进程内存里；要多实例部署时把 SessionStore 换成 Redis 实现即可，
对外接口保持不变。
"""

from __future__ import annotations

import threading
import time
from typing import Any


class SessionStore:
    def __init__(self, max_messages: int = 20, ttl_seconds: int = 3600):
        self._max = max_messages
        self._ttl = ttl_seconds
        self._data: dict[str, list[dict[str, Any]]] = {}
        self._touched: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(chat_id: str, user_id: str) -> str:
        return f"{chat_id}::{user_id}"

    def history(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked(key)
            return list(self._data.get(key, []))

    def extend(self, key: str, messages: list[dict[str, Any]]) -> None:
        with self._lock:
            self._expire_locked(key)
            bucket = self._data.setdefault(key, [])
            bucket.extend(messages)
            # 只按条数裁剪；裁剪后若首条是 tool 结果，往前多丢一条，
            # 避免出现「有 tool 结果但没有对应的 assistant 工具调用」的残缺历史
            if len(bucket) > self._max:
                bucket[:] = bucket[-self._max:]
                while bucket and bucket[0].get("role") == "tool":
                    bucket.pop(0)
            self._touched[key] = time.time()

    def clear(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._touched.pop(key, None)

    def _expire_locked(self, key: str) -> None:
        last = self._touched.get(key)
        if last is not None and time.time() - last > self._ttl:
            self._data.pop(key, None)
            self._touched.pop(key, None)


class Deduplicator:
    """飞书事件会重投，用 message_id 去重，避免机器人重复回复。"""

    def __init__(self, capacity: int = 4096):
        self._capacity = capacity
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = time.time()
            if len(self._seen) > self._capacity:
                oldest = sorted(self._seen.items(), key=lambda kv: kv[1])[: self._capacity // 4]
                for k, _ in oldest:
                    self._seen.pop(k, None)
            return False
