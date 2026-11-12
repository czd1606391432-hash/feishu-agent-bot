"""机器人新建的飞书文档，自动把所有者转给发起人。

背景：机器人以「应用身份」调用工具创建多维表格/文档/电子表格时，文档所有者是应用本身，
发消息的人既看不到也改不了。这里在创建类工具返回后，按返回值里的 token 把所有者转给发起人，
并让应用保留「可管理」权限，后续还能继续往里写数据。

用到的应用权限：docs:permission.member:transfer（转移所有者）、
docs:permission.member:create（转移失败时退而求其次：把发起人加为可管理协作者）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 工具名 → (云文档类型, 从返回值里取 token 的正则)。工具名是飞书官方 MCP 的工具名净化后的形式。
CREATE_TOOLS: dict[str, tuple[str, str]] = {
    "bitable_v1_app_create": ("bitable", r'"app_token"\s*:\s*"([A-Za-z0-9]+)"'),
    "docx_v1_document_create": ("docx", r'"document_id"\s*:\s*"([A-Za-z0-9]+)"'),
    "sheets_v3_spreadsheet_create": ("sheet", r'"spreadsheet_token"\s*:\s*"([A-Za-z0-9]+)"'),
}

_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "exp": 0.0}


def _domain() -> str:
    return os.getenv("FEISHU_DOMAIN", "https://open.feishu.cn").rstrip("/")


def _tenant_token() -> str:
    with _lock:
        if _token_cache["token"] and time.time() < float(_token_cache["exp"]) - 120:
            return str(_token_cache["token"])
        body = json.dumps({"app_id": os.getenv("FEISHU_APP_ID", ""),
                           "app_secret": os.getenv("FEISHU_APP_SECRET", "")}).encode()
        req = urllib.request.Request(_domain() + "/open-apis/auth/v3/tenant_access_token/internal",
                                     data=body, headers={"Content-Type": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败：{data.get('msg')}")
        _token_cache["token"] = data["tenant_access_token"]
        _token_cache["exp"] = time.time() + int(data.get("expire", 7200))
        return str(_token_cache["token"])


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(_domain() + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _tenant_token()})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except Exception:
            return {"code": exc.code, "msg": str(exc)}


def give_to_user(token: str, doc_type: str, open_id: str,
                 post: Callable[[str, dict], dict] | None = None) -> str:
    """把文档所有者转给 open_id；失败则加为可管理协作者。返回给模型/用户看的一句话。"""
    post = post or _post
    member = {"member_type": "openid", "member_id": open_id}
    r = post(f"/open-apis/drive/v1/permissions/{token}/members/transfer_owner"
             f"?type={doc_type}&need_notification=true&remove_old_owner=false"
             f"&stay_put=false&old_owner_perm=full_access", member)
    if r.get("code") == 0:
        logger.info("已把 %s %s 的所有者转给发起人", doc_type, token)
        return "已自动把这份文档的所有者转给你，你可以直接打开、编辑和分享。"
    logger.warning("转移所有者失败 %s %s：%s %s", doc_type, token, r.get("code"), r.get("msg"))
    r2 = post(f"/open-apis/drive/v1/permissions/{token}/members?type={doc_type}&need_notification=true",
              dict(member, perm="full_access"))
    if r2.get("code") == 0:
        logger.info("已把发起人加为 %s %s 的可管理协作者", doc_type, token)
        return "已自动把你加为这份文档的「可管理」协作者。"
    logger.warning("添加协作者也失败 %s %s：%s %s", doc_type, token, r2.get("code"), r2.get("msg"))
    return f"注意：文档已创建，但没能给你开权限（{r2.get('msg') or r.get('msg')}），请联系管理员。"


def after_tool_call(tool_name: str, output: str, open_id: str | None,
                    post: Callable[[str, dict], dict] | None = None) -> str | None:
    """创建类工具调用后执行；不是创建类或没有发起人时返回 None。"""
    spec = CREATE_TOOLS.get(tool_name)
    if not spec or not open_id:
        return None
    doc_type, pattern = spec
    m = re.search(pattern, output or "")
    if not m:
        return None
    try:
        return give_to_user(m.group(1), doc_type, open_id, post)
    except Exception as exc:  # 权限处理失败绝不能影响主流程
        logger.warning("自动转移所有者异常：%s", exc)
        return None


def install(hooks, post: Callable[[str, dict], dict] | None = None) -> None:
    """把「自动转所有者」挂成一个 after_tool 钩子。"""
    import asyncio

    @hooks.after_tool
    async def _transfer(ctx, name, args, output):
        if name not in CREATE_TOOLS:
            return None
        note = await asyncio.to_thread(after_tool_call, name, output, ctx.requester_open_id, post)
        return f"{output}\n\n[系统] {note}" if note else None
