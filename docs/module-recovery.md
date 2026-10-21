# 模块本地处理与恢复判定（v1）

`ctx.subscribe_recovery_events` 是主动参与的状态消费者契约。QQ 官方 WS 的可信补发边界、该边界内相关事件成功处理及显式确认、同会话的已建立基线、没有已知失败或缺口，必须同时成立，才能报告 `ready=True`。连接事实接口本身不表示消费者已经恢复。

## 接入

```python
async def setup(ctx):
    async def update(delivery):
        event = delivery.event
        # 这四类事件仍通过框架群生命周期订阅及其真实处理结果投递。
        await state.apply_member_event(event)
        delivery.acknowledge()  # 必须在承诺的状态更新完成后调用

    subscription = ctx.subscribe_recovery_events(
        update,
        event_types={'GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE',
                     'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT'},
        appid='your-appid',
        timeout=5.0,
        max_pending=128,
    )
    return subscription
```

不要同时用普通群生命周期订阅重复更新同一状态。普通订阅保持原有契约，不会自动加入恢复判定。

- `ctx.recovery_api_version == 1`。
- `event_types` 是非空事件名集合，不能包含 `READY`、`RESUMED`。只覆盖显式声明的类型，框架不解释业务依赖。
- `appid=None` 接收各 Bot 的事件，进度与失败仍按 Bot 和传输独立。
- `timeout` 是单次回调开始执行后的有限正数秒；`max_pending` 是每 Bot/传输的最大在途确认数，默认 128。
- `depends_on=(other_subscription,)` 声明同一应用内已有的恢复订阅依赖。创建时拒绝失效或外部应用的依赖，新增订阅只依赖已有订阅，形成无环图。
- 返回 `RecoverySubscription`，提供 `subscription_id`、`owner`、`active`、`get_status`、`establish`、`invalidate`、幂等 `cancel`。

`delivery.event`：上述四类是原有不可变 `GroupLifecycleEvent`，保留原生群/成员身份、平台事件 ID、序号、时间和连接上下文；其他 Dispatch 类型是不可变 `RecoveryEvent`，包含 `appid`、`event_type`、`event_id`、递归只读的原始 `raw` 和 `connection_context`。通用消费者和普通插件不共享可变事件对象。

`delivery.context` 是实际接收时的连接上下文。`delivery.acknowledge()` 返回确认是否仍有效；超时、会话替换、取消或卸载后的迟到确认返回 False。确认只表明消费者承诺的工作已经完成；它还必须结合框架观察到的回调成功返回。先确认再抛异常仍然失败。回调返回却未确认记为 `unconfirmed`，启动后台任务后返回不能使订阅恢复。

## 初次启动与重建基线

首次 READY、进程重启、新会话、晚加入或重载订阅都没有自动可信的历史状态。消费者必须自行从可靠来源建立状态，再显式提交基线：

```python
checkpoint = subscription.get_status(appid)
# 在消费者自己的锁/事务安排中完成状态重建，处理与事件回调的互斥。
await state.rebuild_from_authoritative_source()
if not subscription.establish(checkpoint):
    # 期间发生相关事件、连接变化、依赖变化、尚有处理或旧回调未退出。
    # 本次凭据已失效，需要重新取凭据并重新重建，不能只重试确认。
    schedule_rebuild()
```

应在 `get_connection_status(appid).phase` 为 `ready` 或可信 `replay_complete` 时尝试重建。`establish` 不进行平台查询，也不验证重建算法；它是消费者对可靠基线的明确声明。重建与事件回调的状态写入互斥由消费者负责。重建期间任何相关接收、处理结果、状态/代际或依赖凭据变化都会拒绝旧 checkpoint。不能把磁盘旧数据加载完直接当作连续性证明。

调用者需要主动失效共享事实时：

```python
subscription.invalidate(appid, reason='consumer_invalidated')
```

失败保持到显式重建并建立新基线。依赖者会同步失效；上游依赖恢复后，依赖者也必须重建自己的状态。框架不决定哪些业务命令暂停。

## 查询进度

`subscription.get_status(appid, transport='websocket')` 返回不可变 `RecoveryStatus`：

| 字段 | 含义 |
| --- | --- |
| `subscription_id`、`appid`、`transport` | 独立消费者和来源范围 |
| `revision`、`context` | 本地进度版本和最近连接转换上下文；不作为平台完成序号 |
| `phase`、`reason`、`ready` | 判定及原因 |
| `pending` | 当前会话所有在途确认数量 |
| `boundary_pending` | 当前固定补发边界内尚未成功完成的数量，含断线前已接收但未完成的事件 |
| `replay_end_sequence`、`replay_end_receive_index` | 来源接口提供的固定补发结束位置 |
| `failed` | 本消费者是否存在必须重建的已知失败 |
| `dependencies` | 声明的依赖快照，作为基线凭据的一部分 |

`phase`：`needs_baseline`（含失败）、`unsupported`、`recovering`、`processing`、`ready`、`dependency_failed` 或 `closed`。`failed=False` 不代表已就绪，例如初次启动和等待处理。

首次在线建立基线后，相关事件在途时暂时为 `processing`。同一会话恢复后，`ready` 明确对应固定补发边界：边界之后的新事件不会把目标无限向前移动，可能同时出现 `ready=True`、`pending>0`、`boundary_pending=0`。这不能作为“所有最新在线事件均已处理完”的证明。边界之后的新失败仍会撤销当前信任。

没有通过等待所有插件、清空队列、`_on_event` 返回或异步连接通知处理成功来推断就绪。

## 隔离、顺序及异常

- 按 runtime、Bot 实例、会话区分基线；按连接/恢复轮次和接收位置固定补发范围。恢复同一会话时保留断线前在途记录；新会话使旧凭据失效并取消旧回调。旧确认不能改变新会话的进度。
- 群生命周期继续使用既有 Bot/群接收顺序、回调超时及处理结果；其他事件独立并发，不占用普通插件的并发槽位。消费者自行安排其状态更新顺序；框架的未完成集合保证乱序完成不能越过任何边界内的未完成事件。
- 群顺序预登记和恢复登记各自幂等；适配器已登记群顺序时，应用接收入口仍须登记恢复处理。普通分发被扩展接管或变换，不能替代确认，也不能改写接收时已经记录的恢复事件范围。
- 每次确认、回调异常、自取消、超时、取消订阅、模块卸载和 setup 失败都经过实际生命周期处理。卸载/重载注销原订阅，新订阅拥有新身份和空基线。
- 取消不合作的回调不会被无限等待；其实际运行仍占该消费者/Bot 的容量，退出前不能建立基线。注销或会话替换后，尚未退出的真实回调由管理器继续持有，按模块安装槽位、Bot、传输保留隔离屏障；同范围的新订阅及其他订阅不能建立基线，新事件不再调用这些消费者并记为 `old_callback_running`。重复取消、卸载或重载不能绕过屏障启动更多写入者，无关模块和 Bot 继续处理。单个仍有效订阅超时继续按订阅隔离；其注销后仍活跃的回调也转交上述屏障。
- 安装槽位身份在 `ModuleContext` 创建时由模块目录捕获，独立于可修改、不保证唯一的展示名 `owner`。相对路径和父目录符号链接归一化后，同一槽位跨重载、元数据更名、更新和 `ModuleManager` 重建保持一致；不同模块目录或不同管理根目录的槽位互不影响。最后一层模块目录符号链接保留为安装入口，因此它切换到新版本目录仍受原屏障约束。直接创建 `ModuleContext(name, module_dir, hooks)` 也遵循相同规则；移到另一安装槽位视为另一模块，若确实共享状态需显式声明依赖。
- 回调真正退出并完成记账后会更新 `revision`：仍有效订阅的超时回调只更新自身 Bot/传输的进度；注销或会话替换后的旧回调沿用上述同范围屏障。退出完成记账前不能建立基线，期间取得的 checkpoint 即使在退出后提交也会被拒绝，必须退出后重新取凭据并重新重建状态。超时失败不会因回调晚返回、晚确认、晚抛异常或自取消而清除。消费者不能让派生后台任务越过自己的确认承诺；框架无法撤销已经发生的业务写入。
- 回调内部主动取消自己的订阅时，仍未退出的真实回调也保留上述屏障；启用 eager 任务调度不会绕过这一约束。
- 超过容量标为 `overflow` 并撤销信任，后续成功不能抹去缺口。普通插件和无关消费者继续处理。此上限约束本机制在途凭据与通用消费者任务；既有群生命周期接收队列沿用原实现。
- 同会话最近 256 个相关序号保留载荷摘要，用于抑制重复。相同序号但不同契约载荷标为 `conflicting_duplicate`；超出记录窗口的旧序号或倒退序号标为 `late_event` 并要求重建。没有无限去重或外部发送恰好一次承诺。
- 缺失序号、无法匹配来源、适配器未登记实际接收、协议未知/失败、事件解析或登记异常均不能恢复。WebSocket Dispatch 的 `t` 必须为非空字符串；缺失、空值和其他类型会在解析前以 `invalid_dispatch_type` 关闭该来源的恢复信任。解析后的事件名还必须与协议 `t` 一致；兼容平铺载荷若改变类型，会在接收登记及任何消费者投递前以 `dispatch_type_mismatch` 拒绝，并撤销该来源的信任。后续 `RESUMED` 或成功处理不能自动消除这些缺口。Webhook 和直接调用事件解析器的适配器继续保留平铺入群申请兼容。单个消费者处理失败仅影响其自身 Bot/来源和显式依赖者；接收链路存在共同缺口时，同来源所有实际依赖消费者都会失效。

## 适用范围

连续恢复首版只支持已有连接接口认定可信的 QQ 官方 WS。Webhook、自定义 URL/网关等返回 `unsupported`，即使收到同名 RESUMED，也不能建立本契约的连续恢复保证。它们仍保持原有事件和状态事实覆盖。

依据：[QQ 官方事件订阅与恢复协议](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)。本机制只组合实际接收、可信协议边界及已确认的本地处理，不承诺无限离线补发、发现平台尚未投递的事件、跨进程连续性、查询与发送原子一致或具体业务验收。
