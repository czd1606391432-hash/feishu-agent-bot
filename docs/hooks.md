# 钩子

## 为什么需要

提示词里写「提交前必须先让用户确认」，模型大多数时候会照做，但不能保证每一次。要防住的是这几种情况：

- 模型在同一轮里自己认定「用户已确认」，然后直接提交。
- 用户确认之后，模型再次调用时参数和用户看到的不一样了。
- 规定了先做 A 再做 B，模型跳过 A 直接做 B。

凡是出一次错就有实际后果的规则，都应该由代码来保证。钩子就是做这件事的地方。

## 怎么启用

写一个 Python 模块，里面有一个 `setup(hooks)` 函数，然后在 `.env` 里登记：

```text
HOOKS_MODULES=my_rules,examples.confirm_gate
```

模块从程序的运行目录导入，多个用逗号分隔。

## 挂载点

### 追加提示词

```python
hooks.prompt("【规则】提交类操作第一次调用不会真的执行，系统会要求你先让用户确认。")
```

规则由代码强制，但还是应该告诉模型有这条规则，否则它被拦下时会困惑、反复重试。

### 工具调用前：`before_tool`

```python
from app.hooks import Block

@hooks.before_tool
def gate(ctx, name, args):
    if name == "delete_record":
        return Block("不允许删除。请告诉用户联系管理员。")     # 拦截：工具不执行，这句话当作结果交给模型
    if name == "search":
        return dict(args, department=lookup_department(ctx.requester_open_id))   # 改参数：由代码补上，模型绕不过去
    return None                                                # 放行
```

多个 `before_tool` 按登记顺序执行，有一个拦截就不再往下。钩子自身抛异常时，这次工具调用**不会执行**——检查坏了就宁可不做，不能因此绕过检查。

### 工具调用后：`after_tool`

```python
@hooks.after_tool
def trim(ctx, name, args, output):
    if name == "get_document":
        ctx.state["last_doc"] = args.get("doc_id")      # 记录状态，供后面的规则用
        return output.split("\n## 内部备注")[0]           # 改写结果：只把该给模型看的部分留下
    return None                                         # 不改
```

`after_tool` 抛异常会被忽略并记日志，不影响工具结果。

### 本地工具：`tool`

不走 HTTP、直接在进程里执行的工具：

```python
@hooks.tool("today", "返回今天的日期", {"type": "object", "properties": {}})
def today(ctx, args):
    return datetime.now().strftime("%Y-%m-%d")
```

函数可以是普通函数，也可以是 `async`。本地工具同样会经过 `before_tool` 和 `after_tool`。

### 消息进模型之前：`on_message`

```python
@hooks.on_message
def collect(msg):
    if msg.state.get("waiting_for_code") and msg.text.isdigit():
        msg.state["code"] = msg.text
        return "验证码已收到。"        # 直接回复，这条消息不交给模型
    return None                        # 继续走正常流程
```

适合两类事情：收集不应该进模型的信息；根据用户刚说的话更新状态。机器人收到的每条文字消息都会经过这里，包括群里没有 @ 它的（前提是应用有接收群内全部消息的权限，否则飞书只会推送 @ 它的消息），用 `msg.addressed` 判断是不是在对机器人说话。

### 卡片按钮：`card_buttons` 和 `card_action`

```python
@hooks.card_buttons
def buttons(ctx, answer):
    if ctx.turn.get("need_confirm"):
        return [{"text": "确认", "say": "确认", "type": "primary"}, {"text": "取消", "say": "取消"}]
```

三种按钮：

| 写法 | 点了之后 |
|---|---|
| `{"text": "确认", "say": "确认"}` | 等于点击的人在对话里发了「确认」 |
| `{"text": "打开", "url": "https://…"}` | 跳转链接 |
| `{"text": "选它", "value": {"op": "pick", "rank": 2}}` | 自定义回调值，配合 `card_action` 翻译成一句话 |

```python
@hooks.card_action
def pick(value):
    if value.get("op") == "pick":
        return "选第%d个" % int(value["rank"])
```

按钮点击最终都会变成一句用户消息，走和打字完全相同的流程。这样所有规则只需要写一遍，不用为按钮另写一套。

## 上下文里有什么

`before_tool`、`after_tool`、本地工具、`card_buttons` 拿到的 `ctx`：

| 属性 | 内容 |
|---|---|
| `ctx.user_text` | 用户这一轮发的原话。模型改不了它，所以可以放心用来判断「用户到底说没说确认」。 |
| `ctx.requester_open_id` | 发消息的人。 |
| `ctx.session_key` | 会话标识（对话 + 用户）。 |
| `ctx.turn_id` | 这是该会话的第几轮。用来区分「这一轮刚发生的」和「之前就有的」。 |
| `ctx.state` | 会话级状态，一个 dict，跨轮保留，一小时不用自动清掉，用户发 `/reset` 也会清掉。 |
| `ctx.turn` | 本轮状态，这一轮结束就丢。 |

状态都在内存里，重启会丢。规则设计时要让「状态丢了」落在安全的一边（比如待确认的内容丢了，结果应该是重新确认，而不是直接执行）。

## 完整例子：确认闸门

[examples/confirm_gate.py](../examples/confirm_gate.py)，五十行左右，做了三件事：

1. 受保护的工具第一次被调用时不执行，把参数记成「待确认」，让模型先给用户看。
2. 只有用户的**下一条**消息本身是确认（有确认词、没有修改词）才放行。模型在同一轮里再调一次没用；用户中间说了别的，待确认的内容就作废。
3. 放行时执行的是当初记下来的那份参数，不是模型这次给的。

另一个在实际使用中有用的规则（没有写成示例）：很慢的工具一轮只许调一次。模型遇到超时会立刻重试，结果是两个慢请求互相拖累。在 `before_tool` 里用 `ctx.turn` 记一下，本轮第二次调用直接拦掉即可。
