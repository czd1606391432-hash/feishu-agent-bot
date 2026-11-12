# 飞书后台配置

在[飞书开放平台](https://open.feishu.cn/app)操作。国际版 Lark 在 open.larksuite.com，步骤相同。

## 1. 创建应用

「创建企业自建应用」，填名称和图标。在「凭证与基础信息」里拿到 **App ID** 和 **App Secret**，写进 `.env`。

## 2. 开启机器人能力

「添加应用能力」→「机器人」→ 启用。

## 3. 订阅事件

「事件与回调」→「事件配置」：

- 订阅方式选 **「使用长连接接收事件」**。选成 Webhook 的话本项目收不到任何消息。
- 添加事件：**接收消息 `im.message.receive_v1`**。

如果保存时提示应用还没有建立长连接，先把程序跑起来（`python main.py`），再回后台保存。

## 4. 配置卡片回调（要用卡片按钮才需要）

「事件与回调」→「回调配置」：

- 订阅方式同样选 **「使用长连接接收回调」**。
- 添加回调：**卡片回传交互 `card.action.trigger`**。

事件和回调是两处分开的设置，只配了事件的话，按钮能显示但点了没反应。

## 5. 申请权限

「权限管理」里添加。

**必需（收发消息）**

| 权限 | 用途 |
|---|---|
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 |
| `im:message.group_at_msg:readonly` | 读取群里 @ 机器人的消息 |
| `im:message:send_as_bot` | 以机器人身份发消息、更新卡片 |

**自动转所有者（`AUTO_TRANSFER_OWNER=true` 且机器人会建文档时需要）**

| 权限 | 用途 |
|---|---|
| `docs:permission.member:transfer` | 把文档所有者转给发起人 |
| `docs:permission.member:create` | 转移失败时，退一步把发起人加为可管理协作者 |

**启用飞书官方工具集时，按需添加**

工具集要操作什么，就加对应的权限，例如：

| 权限 | 用途 |
|---|---|
| `im:chat:readonly` | 读取群信息 |
| `docx:document:readonly` | 读云文档 |
| `bitable:app:readonly` | 读多维表格 |
| `sheets:spreadsheet:readonly` | 读电子表格 |
| `calendar:calendar:readonly` | 读日历 |

建议先只给只读权限，跑通、确认行为可控之后再开写权限。权限名称以开放平台当前页面为准。

## 6. 发布

「版本管理与发布」→ 创建版本 → 申请发布，等管理员审批。

**改了权限、事件或回调之后，都要重新创建版本并发布才生效。** 只重启程序是不够的。

## 7. 开始使用

- 单聊：在飞书里搜索应用名称，直接发消息。
- 群聊：群设置 →「群机器人」→ 添加机器人，然后在群里 @ 它。

## 可选：启用飞书官方工具集

需要本机装有 Node.js（18 以上）。先确认能运行：

```bash
npx -y @larksuiteoapi/lark-mcp --help
```

然后在 `.env` 里：

```text
MCP_ENABLED=true
MCP_TOOL_PRESETS=preset.default
```

启动日志里会打印加载了多少个飞书工具。工具的可用范围取决于上面申请的权限。

## 可选：填机器人自己的 open_id

不填的话，群消息里只要 @ 了人（不管 @ 的是谁）机器人都会响应。想严格限定为「@ 机器人才响应」，把机器人的 open_id 填到 `FEISHU_BOT_OPEN_ID`。

取法：用开放平台的「API 调试台」调用「获取机器人信息」接口（`/open-apis/bot/v3/info`），返回里的 `open_id` 就是。open_id 是按应用区分的，换了应用要重新取。

## 常驻运行

```bash
cp examples/feishu-agent-bot.service /etc/systemd/system/
```

改好里面的路径后：

```bash
systemctl daemon-reload && systemctl enable --now feishu-agent-bot
```

```bash
journalctl -u feishu-agent-bot -f
```

`.env` 里有密钥，记得收紧权限：

```bash
chmod 600 .env
```
