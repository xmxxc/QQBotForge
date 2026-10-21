# 模块接收事件 Hook

模块可通过 `ModuleContext.register_hook()` 或 `@ctx.hook()` 注册通用的 `before_event` 管道。Webhook、WebSocket 和自定义适配器共用此入口。

该 Hook 在机器人校验、群生命周期等待、事件去重和身份转换之后，在内置生命周期处理、消息记录、未知事件报告和插件分发之前执行。没有订阅者时沿用原处理路径。

回调接收 `Event`，可原样返回、返回转换后的 `Event`，或返回 `None` 表示事件已经接管。替换事件必须保留原 `appid`。Hook 按 priority 从小到大运行，返回 `None` 后停止后续 Hook 和内置处理。异常沿用通用 Hook 管道规则：记录错误并让后续回调继续接收当前事件。因此接管私有事件的模块应自行处理异常，并明确返回 `None`。

```python
async def setup(ctx):
    @ctx.hook('before_event', priority=0)
    async def process_custom_event(event):
        if event.event_type != 'CUSTOM_TASK_COMPLETE':
            return event
        # 在模块中校验、转换并持久化自定义事件。
        # 如需交给插件，可调用下面的通用分发入口。
        return None
```

模块禁用时，模块管理器按 owner 自动注销 Hook。若服务需提前停止接收，可调用 `app.hook_manager.unregister('before_event', callback)` 注销自己的回调。

## 仅向显式订阅者分发

`await app.plugin_manager.dispatch(event, sender, explicit_only=True)` 仅匹配 `event_types` 显式包含当前事件类型的处理器。未声明 `event_types` 的通配处理器不会收到事件。正则、优先级、机器人绑定、场景限制、block 及处理器执行规则沿用原分发流程。省略此参数时保持原行为。

```python
from core.application import get_app

app = get_app()
bot = app.get_bot(event.appid)
await app.plugin_manager.dispatch(event, bot.sender, explicit_only=True)
```

插件使用现有装饰器显式订阅：

```python
from core.plugin.decorators import handler

@handler(r'^my-plugin:', event_types={'CUSTOM_TASK_COMPLETE'})
async def on_custom_task(event, match):
    pass
```

Hook 和分发能力不包含任何具体业务事件、接口路径或业务参数。

## 插件处理器之前的模块消息 Hook

普通消息由模块应答时使用 `before_handler` 管道，而不是 `before_event`。它在插件分发内部执行：消息记录、用户追踪、全量群记录、黑名单、维护模式、非@群消息权限（含忽略仅@其他 Bot／用户）和插件拦截器都已完成，随后才轮到插件处理器。不满足这些接收条件的消息不会进入此 Hook。

回调接收 `Event`，原样返回则继续匹配插件处理器及默认回复；返回 `None` 表示模块已处理，后续插件处理器和默认回复不再执行。异常、优先级和按 owner 自动注销规则与其他 Hook 相同。`non_at_message.strip_bot_name_at` 只影响插件正则匹配，模块需要时自行处理 `event.content`。

```python
async def setup(ctx):
    @ctx.hook('before_handler', priority=100)
    async def answer(event):
        if not await my_rules_match(event):
            return event
        await reply(event)
        return None
```

非消息事件和没有插件管理器的进程不会触发此 Hook。
