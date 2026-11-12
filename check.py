"""环境自检。先跑这个，全绿了再跑 main.py。

    python check.py

逐项检查：Python 依赖 → Node/npx → 配置完整性 → MCP 能否连上并列出工具
→ 模型能否调通 → 飞书凭证是否有效。任何一项失败都会打印具体的排查建议。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys

OK = "\033[32m✓\033[0m"
NG = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

failures = 0


def report(ok: bool, title: str, detail: str = "", hint: str = "") -> None:
    global failures
    mark = OK if ok else NG
    print(f"{mark} {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1
        if hint:
            print(f"    → {hint}")


def check_python_deps() -> bool:
    missing = []
    from config import config
    wanted = [("lark_oapi", "lark-oapi"), ("dotenv", "python-dotenv")]
    if config.mcp.enabled:
        wanted.append(("mcp", "mcp"))
    wanted.append(("anthropic", "anthropic") if config.llm.provider == "anthropic" else ("openai", "openai"))
    for mod, pkg in wanted:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    report(not missing, "Python 依赖", "已安装" if not missing else f"缺少 {', '.join(missing)}",
           "pip install -r requirements.txt")
    return not missing


def check_node() -> bool:
    from config import config
    if not config.mcp.enabled:
        return True
    npx = shutil.which("npx")
    if not npx:
        report(False, "Node.js / npx", "未找到",
               "安装 Node.js LTS：https://nodejs.org 。lark-openapi-mcp 需要它")
        return False
    try:
        ver = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        ver = "未知版本"
    report(True, "Node.js / npx", ver)
    return True


def check_config() -> bool:
    from config import config
    problems = config.validate()
    report(not problems, "配置文件", "完整" if not problems else "；".join(problems),
           "复制 .env.example 为 .env 并填写")
    if not problems:
        print(f"    飞书地址：{config.feishu.domain}")
        print(f"    模型：{config.llm.model} @ {config.llm.base_url}")
    return not problems


async def check_mcp() -> bool:
    from app.mcp_hub import MCPHub
    from config import config

    if not config.mcp.enabled:
        print(f"{WARN} MCP — 已在配置中关闭，跳过")
        return True

    command, args = config.mcp.command(config.feishu)
    hub = MCPHub(command, args)
    try:
        await asyncio.wait_for(hub.start(), timeout=150)
    except Exception as exc:
        report(False, "MCP 连接", str(exc)[:200],
               "手动试一下：npx -y @larksuiteoapi/lark-mcp --help；"
               "并确认 FEISHU_DOMAIN 正确")
        return False

    tools = hub.openai_tools()
    report(True, "MCP 连接", f"可用工具 {len(tools)} 个")
    for t in tools[:8]:
        print(f"    · {t['function']['name']}")
    if len(tools) > 8:
        print(f"    · …另有 {len(tools) - 8} 个")
    await hub.stop()
    return True


async def check_llm() -> bool:
    from app.llm import build_adapter
    from config import config

    try:
        adapter = build_adapter(config.llm)
        turn = await asyncio.wait_for(
            adapter.complete([{"role": "user", "content": "只回复两个字：收到"}], None),
            timeout=60,
        )
    except Exception as exc:
        report(False, "模型调用", str(exc)[:200],
               "检查 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL；"
               "国内网络访问境外模型需要确认网络可达性")
        return False
    report(True, "模型调用", f"返回：{turn.content.strip()[:40]}")
    return True


def check_feishu_auth() -> bool:
    from app.feishu_client import build_client
    from config import config
    from lark_oapi.api.im.v1 import ListChatRequest

    try:
        client = build_client(config.feishu.app_id, config.feishu.app_secret, config.feishu.domain)
        resp = client.im.v1.chat.list(ListChatRequest.builder().page_size(1).build())
    except Exception as exc:
        report(False, "飞书凭证", str(exc)[:200], "检查 APP_ID / APP_SECRET / FEISHU_DOMAIN")
        return False

    if resp.success():
        report(True, "飞书凭证", "鉴权通过")
        return True

    # 鉴权通过但缺权限也会走到这里，分开提示
    if resp.code in (99991663, 99991664, 99991661):
        report(False, "飞书凭证", f"code={resp.code} {resp.msg}",
               "凭证或域名不对，或应用未发布")
        return False
    report(True, "飞书凭证", f"鉴权通过（接口返回 code={resp.code}，多半是权限未开，不影响启动）")
    return True


def check_tools() -> bool:
    from app.hooks import load_hooks
    from app.tools import CustomToolRegistry
    from config import config

    reg = CustomToolRegistry(config.agent.custom_tools_file)
    reg.load()
    try:
        hooks = load_hooks(config.agent.hooks_modules)
    except Exception as exc:
        report(False, "钩子模块", str(exc)[:200], "检查 HOOKS_MODULES 里的模块名，以及模块里是否有 setup(hooks)")
        return False
    report(True, "自定义工具与钩子",
           f"自定义工具 {len(reg.openai_tools())} 个，本地工具 {len(hooks.tool_schemas())} 个")
    return True


async def main() -> int:
    print("=" * 56)
    print(" 飞书 Agent 环境自检")
    print("=" * 56)

    if not check_python_deps():
        print("\n依赖没装齐，后续检查跳过。")
        return 1
    check_node()
    if not check_config():
        print("\n配置不完整，后续检查跳过。")
        return 1
    check_tools()
    check_feishu_auth()
    await check_llm()
    await check_mcp()

    print("-" * 56)
    if failures:
        print(f"{NG} 有 {failures} 项未通过，按上面的提示处理后再跑 main.py")
        return 1
    print(f"{OK} 全部通过，可以启动：python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
