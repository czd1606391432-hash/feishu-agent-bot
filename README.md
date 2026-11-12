# feishu-agent-bot

[![tests](https://github.com/czd1606391432-hash/feishu-agent-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/czd1606391432-hash/feishu-agent-bot/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

一个接在飞书里的 Agent 机器人底座：同事在飞书里给机器人发消息，机器人用大模型理解需求、调用你声明的工具去查数据或办事，再把结果回到对话里。

接入你自己的内部系统不用改代码，在一个 JSON 文件里声明接口即可。必须严格执行的业务规则（比如「没经本人确认不许提交」）可以写成钩子，由代码保证，而不是只靠提示词。

> A Feishu/Lark agent bot skeleton: long-connection events → dedup / placeholder card / timeout → multi-round tool-calling loop → reply. HTTP tools are declared in JSON; business rules that must hold are enforced in code via hooks.

## 能做什么

| 能力 | 说明 |
|---|---|
| **收消息不需要公网地址** | 用飞书的长连接接收事件，服务主动连飞书，放在内网、本机都能跑，不用域名和证书。 |
| **多轮工具调用** | 模型可以连续调用多个工具再给结论。最多 8 轮，最后一轮不再给工具，逼它出结论，不会无限循环。 |
| **JSON 声明工具** | 任意 HTTP 接口写一段 JSON 就成了模型能调用的工具：密钥从环境变量取，参数用占位符填，返回内容可以裁剪。 |
| **飞书官方工具集（可选）** | 通过 MCP 接入飞书官方的 `lark-openapi-mcp`，直接获得读写文档、多维表格、日历等工具。 |
| **钩子** | 工具调用前可以拦截或改参数，调用后可以改写结果，还能注册进程内的本地工具。用来落实不能出错的规则。 |
| **卡片交互** | 先回一张「正在处理…」的卡片，处理完原地更新；卡片上可以带按钮，点按钮等于用户发了一句话。 |
| **会话记忆** | 按「对话 + 用户」隔离，保留最近 20 条，1 小时不说话自动清空。 |
| **自动转所有者** | 机器人建的文档默认归应用所有、发起人打不开，这里会自动把所有者转给发起人。 |

## 快速开始

需要 Python 3.10 以上（飞书和 MCP 的 SDK 要求）。只跑离线测试的话 3.9 也可以。

**1. 在飞书开放平台建应用**：开启机器人能力、订阅事件、申请权限、发布。步骤见 [docs/setup.md](docs/setup.md)，最容易错的两处：

- 事件订阅方式必须选「使用长连接接收事件」。
- 要用卡片按钮的话，还得在「回调配置」里同样选长连接，并订阅 `card.action.trigger`。

**2. 安装**

```bash
git clone https://github.com/czd1606391432-hash/feishu-agent-bot.git && cd feishu-agent-bot
```

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**3. 配置**

```bash
cp .env.example .env
```

至少填三项：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`LLM_API_KEY`。模型走 OpenAI 兼容接口，改 `LLM_BASE_URL` 和 `LLM_MODEL` 就能换厂商。

**4. 先用示例工具跑通**（一个假的 IT 报修系统，不依赖任何外部服务）

```bash
cp examples/custom_tools.example.json custom_tools.json
```

```bash
cp examples/system_prompt.example.md system_prompt.md
```

在 `.env` 里打开确认闸门这个示例钩子：

```text
HOOKS_MODULES=examples.confirm_gate
```

另开一个终端启动示例后端：

```bash
.venv/bin/python examples/demo_api_server.py
```

**5. 自检，然后启动**

```bash
.venv/bin/python check.py
```

```bash
.venv/bin/python main.py
```

`check.py` 会逐项检查依赖、配置、飞书凭证、模型连通性，哪项不过会给出排查建议。启动后看到「机器人已就绪」，就可以在飞书里对机器人说「VPN 连不上怎么办」「帮我报修」试一试。提交工单前它会先让你确认，这一步是代码强制的。

对话里的内置指令：`/reset` 清空上下文，`/help` 查看用法。

## 怎么加工具

在 `custom_tools.json` 里加一段，重启生效：

```json
{
  "name": "get_ticket",
  "description": "按工单号查询报修工单的当前状态。",
  "parameters": {
    "type": "object",
    "properties": {"ticket_id": {"type": "string", "description": "工单号"}},
    "required": ["ticket_id"]
  },
  "request": {
    "method": "GET",
    "url": "${MY_API_BASE}/tickets/{{ticket_id}}",
    "headers": {"Authorization": "Bearer ${MY_API_TOKEN}"},
    "timeout": 30
  },
  "response": {"fields": ["status", "title"], "max_chars": 4000}
}
```

- `${变量名}`：从环境变量取，密钥不进配置文件。
- `{{参数名}}`：模型填的参数，可以用在地址、查询串、请求头和请求体里。
- `response`：只把模型需要的部分交回去，省上下文，也避免把内部字段暴露给模型。

完整写法见 [docs/tools.md](docs/tools.md)。

## 怎么加规则（钩子）

提示词只能要求模型怎么做，模型可能不照做。不能出错的规则要写成代码：

```python
from datetime import datetime

from app.hooks import Block

def setup(hooks):
    @hooks.before_tool
    def office_hours_only(ctx, name, args):
        if name == "send_notice" and not 9 <= datetime.now().hour < 18:
            return Block("现在不是工作时间，通知没有发出。请告诉用户改天再发。")
```

把模块名写进 `.env` 的 `HOOKS_MODULES` 即可。完整的例子是 [examples/confirm_gate.py](examples/confirm_gate.py)：受保护的操作第一次调用一律不执行，只有用户下一条消息本身是确认才放行，而且执行的是用户看过的那份参数。说明见 [docs/hooks.md](docs/hooks.md)。

## 配置

全部在 `.env` 里，模板见 [.env.example](.env.example)。常用的：

| 变量 | 含义 | 默认 |
|---|---|---|
| `FEISHU_DOMAIN` | 开放平台地址。国际版 Lark 填 `https://open.larksuite.com` | `https://open.feishu.cn` |
| `FEISHU_REQUIRE_MENTION_IN_GROUP` | 群里是否必须 @ 机器人才响应 | `true` |
| `FEISHU_BOT_OPEN_ID` | 机器人自己的 open_id。不填的话，群消息里 @ 了任何人机器人都会响应 | 空 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 模型接口 | DeepSeek |
| `MCP_ENABLED` | 是否加载飞书官方工具集，需要本机有 Node.js | `false` |
| `CUSTOM_TOOLS_FILE` | 自定义工具声明文件 | `custom_tools.json` |
| `SYSTEM_PROMPT_FILE` | 系统提示词文件，不存在就用内置的通用提示词 | `system_prompt.md` |
| `HOOKS_MODULES` | 钩子模块，逗号分隔 | 空 |
| `AGENT_MAX_TOOL_ROUNDS` | 一条消息最多调用几轮工具 | `8` |
| `AGENT_TIMEOUT_SECONDS` | 一条消息的总处理时限 | `300` |
| `SESSION_MAX_MESSAGES` / `SESSION_TTL_SECONDS` | 会话保留条数和过期时间 | `20` / `3600` |
| `AUTO_TRANSFER_OWNER` | 机器人建的文档自动转给发起人 | `true` |

三个时限是一层套一层的：单个工具的 `timeout`（默认 30 秒）、单次模型调用（120 秒）、整条消息（300 秒）。接了很慢的接口时，三个都要相应调大。

## 部署

常驻运行用 systemd，模板在 [examples/feishu-agent-bot.service](examples/feishu-agent-bot.service)。只跑一个实例：会话和去重记录都在进程内存里，多开的话同一个人的前后两句话可能落到不同进程，上下文接不上。

## 目录

```text
main.py              入口：起事件循环、连 MCP、加载工具和钩子、启动长连接
check.py             环境自检
config.py            配置，全部来自环境变量
app/
  agent.py           模型与工具的多轮循环
  hooks.py           钩子：拦截、改参数、改结果、本地工具、卡片按钮
  tools.py           JSON 声明的 HTTP 工具，工具路由
  handler.py         消息处理：线程池、去重、占位卡片、超时
  events.py          事件解析、卡片拼装（不依赖 SDK）
  session.py         会话记忆与事件去重
  llm.py             模型适配：OpenAI 兼容接口，另有 Claude 适配器
  mcp_hub.py         MCP 客户端：工具名净化与调用
  ownership.py       建文档后自动转所有者
  feishu_client.py   飞书收发消息的封装
examples/            示例工具、示例后端、示例钩子、systemd 模板
tests/               离线测试
```

## 验证状态

如实说明，方便你判断能信到什么程度：

- **来历**：这套代码是从一个在飞书（中国版）里实际运行的机器人里抽出来的，去掉了原有的业务部分。长连接收消息、占位卡片、多轮工具调用、JSON 声明工具、MCP 接入、自动转所有者、卡片按钮回调，这些机制在原机器人里都实际用过。
- **抽出来之后**：只跑过离线测试（48 个，飞书和模型都是替身）。另有 3 个测试在装了真实 SDK 的环境里跑：核对用到的类和事件对象结构，并让 MCP 客户端对一个本地 MCP 服务做真实调用（lark-oapi 1.7.5、mcp 2.3.0 下通过）。**没有连真实的飞书和真实的模型跑过。**
- **这次新写或改写、没有经过线上验证的部分**：钩子机制（原来的业务规则是直接写在循环里的）；通用的卡片按钮（原来是按业务文案识别后加按钮）；HTTP 工具的发请求部分（从第三方库换成了标准库）；`FEISHU_BOT_OPEN_ID`；系统提示词文件。
- **Claude 适配器**：原代码里就有，但原机器人用的是 OpenAI 兼容接口，这个分支没在线上用过，只测了消息格式转换。
- 国际版 Lark 没有验证过。

## 已知局限

- **用应用身份调用接口**。所有人通过机器人看到的数据权限是一样的，不会按提问人的权限过滤。接入敏感数据前要想清楚。
- **会话在内存里**。重启就没了，也不支持多实例。
- **没有审计日志、没有限流和成本控制**。谁问了什么、调了哪些接口只在运行日志里；一个人反复提问就能一直消耗模型额度。
- **只处理文字消息**。图片、文件会回一句「暂不支持」。
- **群里的 @ 判断**。不填 `FEISHU_BOT_OPEN_ID` 时，消息里 @ 了任何人都会触发机器人。
- **卡片按钮点过之后还在**。重复点击会再发一次同样的话；示例里的确认闸门不会因此重复执行，但你自己写的规则要考虑这一点。
- **启用 MCP 时，应用密钥会出现在子进程的命令行参数里**（官方包的启动方式如此），同一台机器上的其他用户能用进程列表看到。

## 踩过的坑

对接飞书机器人和工具调用时遇到的问题记在 [docs/pitfalls.md](docs/pitfalls.md)，比如「卡片按钮点了没反应」「机器人建的文档发起人打不开」「模型自己替用户确认」。

## 测试

```bash
python3 -m unittest discover -s tests
```

不联网、不需要凭证。没装飞书和 MCP 的 SDK 时，依赖 SDK 的 3 个测试会自动跳过。

## 许可

MIT
