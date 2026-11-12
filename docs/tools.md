# 自定义工具

把一个 HTTP 接口声明成模型能调用的工具。写在 `custom_tools.json`（文件名可用 `CUSTOM_TOOLS_FILE` 改），最外层是一个数组，每一项是一个工具。改完重启生效。

## 完整写法

```json
{
  "name": "create_ticket",
  "description": "提交一张 IT 报修工单。有副作用：提交前必须先给用户确认。",
  "parameters": {
    "type": "object",
    "properties": {
      "title": {"type": "string", "description": "一句话标题"},
      "urgency": {"type": "string", "enum": ["普通", "紧急"]}
    },
    "required": ["title"]
  },
  "request": {
    "method": "POST",
    "url": "${MY_API_BASE}/tickets",
    "headers": {"X-Api-Token": "${MY_API_TOKEN}"},
    "query": {"source": "bot"},
    "json": {"title": "{{title}}", "urgency": "{{urgency}}"},
    "timeout": 30
  },
  "response": {"extract": "data.summary_md", "max_chars": 4000}
}
```

| 字段 | 说明 |
|---|---|
| `name` | 工具名。只能用字母、数字、下划线、连字符，最长 64 个字符。 |
| `description` | 给模型看的说明。写清楚什么时候该用、有没有副作用。模型主要靠它决定用不用这个工具。 |
| `parameters` | 参数的 JSON Schema。每个参数都写上 `description`。 |
| `request.method` | 默认 `GET`。 |
| `request.url` | 接口地址。 |
| `request.headers` | 请求头。 |
| `request.query` | 查询串参数。 |
| `request.json` | JSON 请求体。 |
| `request.timeout` | 超时秒数，默认 30。 |
| `response` | 怎么裁剪返回内容，见下。 |

## 两种占位符

**`${变量名}`：环境变量。** 用来放地址和密钥，值写在 `.env` 里，不进这个 JSON 文件。变量没设置时，调用会直接报「环境变量未设置」，不会带着空密钥去请求。

**`{{参数名}}`：模型填的参数。** 可以用在 `url`、`headers`、`query`、`json` 里。

- 整个值就是一个占位符时（`"limit": "{{limit}}"`），保留参数原来的类型，数字还是数字、数组还是数组。
- 占位符夹在文字里时（`"共{{n}}项"`），按文本拼进去。
- 写在 `url` 里的参数会做转义，参数里的 `/`、`?` 改不了请求路径。
- 模型没填的可选参数不会发出去，不会出现 `?month=` 这种空参数。

环境变量先展开，然后才填模型的参数。所以模型给的参数里即使出现 `${某个密钥}` 这样的文字，也只会被当成普通文本，不会把密钥带出去。

## 裁剪返回内容

接口返回的东西往往比模型需要的多得多。不裁剪的话既浪费上下文，也可能把内部字段暴露给模型，进而出现在回复里。

| 写法 | 效果 |
|---|---|
| `"fields": ["total", "items"]` | 只保留这几个顶层字段。 |
| `"extract": "data.summary_md"` | 只取这一个字段（点号表示层级）。取出来是文本的话原样交给模型。 |
| `"max_chars": 4000` | 超过这个长度就截断，默认 12000。 |

`extract` 和 `fields` 同时写时只有 `extract` 生效。

一个实用的做法：让后端直接返回一段写好的 Markdown（带上需要的链接），工具用 `extract` 把它取出来。比起把一大段 JSON 丢给模型让它自己组织语言，这样链接和数字更不容易在转述时丢失或出错。

## 出错时模型看到什么

工具不会抛异常让对话中断，而是把错误当成结果交给模型，由它向用户说明：

| 情况 | 模型看到 |
|---|---|
| 接口返回 4xx / 5xx | `接口返回 HTTP 404：<响应内容>` |
| 超时 | `工具 xxx 请求超时（30 秒）…` |
| 连不上 | `工具 xxx 请求失败：<原因>` |
| 环境变量没设置 | `工具 xxx 配置有问题：环境变量 XXX 未设置` |

## 和飞书官方工具集的关系

自定义工具和 MCP 工具合并成一份工具表交给模型。重名时自定义工具优先，可以用这一点覆盖掉某个官方工具。

## 示例

[examples/custom_tools.example.json](../examples/custom_tools.example.json) 里有三个工具（查知识库、查工单、提交工单），配套的假后端是 [examples/demo_api_server.py](../examples/demo_api_server.py)。
