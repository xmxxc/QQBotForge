# 0001 原生入群依据与入群自动授权（#73）

状态：作者本地预实施，待独立审查。父规格 #71，依赖 #72。

## 背景

群状态把可信、顺序成立的 `GROUP_MEMBER_ADD` 记为 `status=pending`、`reliable=false`（`join_requires_verification`）；授权模块的明确来源要求成员凭据 `usable` 且 `status=present`。实际 Bot 没有成员查询权限，入群事件是唯一自动依据。直接把入群事件改成“可靠在群”会让所有消费方的群／成员／角色策略、全员报告和身份绑定同时扩权，也会被误读为主人确认、平台单人查询或完整名册。

## 决定

### 事实来源：群状态“原生入群依据”

- 只在群状态恢复订阅回调内产生；不使用普通生命周期订阅、插件事件或消息。条件全部成立：传输为 `websocket`；事件 ID、时间、成员原生 ID 齐全；不是同 ID 冲突或重复；时间严格晚于该成员上一转换（否则为迟到／同秒冲突）；回调开始时本 Bot 的恢复订阅已有基线且无失败（`phase` 为 `ready`／`processing`／`recovering`，可信同会话补发包含在内）。
- 成员事实不变：仍是 `pending`、`reliable=false`，`usable`、完整名册、全员报告语义不变。另记 `native_join = {event_id, timestamp, sequence, session_id, cycle, generation, epoch}`；周期锚点等其他字段照旧。
- 依据只在“同模块代际 + 同依据轮次 + 同入群周期 + 未离群”时当前。依据轮次在每次 `rebuild` 开始时递增；首次 READY、新会话、失败、重启／重载、不可信事件都要求重建，因此旧依据随之失效且不会恢复。离群、新周期和不可信事件暂停时同时清空该记录。可信同会话 RESUMED 不重建，依据保留。
- 公开接口（窄）：
  - `await state.native_join(bot, group, member)`：返回当前依据副本或 `None`，不看暂停状态，只供在群状态消费通知内写入来源。
  - `fact()`／`facts()` 凭据新增 `join`：仅在本 Bot 未暂停且依据当前时为副本，否则 `None`。
  - `state.join_current(proof)`：与 `fact_current` 相同的非等待复查（代际、群版本、恢复进度、写入者、持久化），以 `join` 代替 `usable`。

### 授权来源：消费方入群规则

- 消费方行新增 `join_rules: [{function, group, created_at}]`，操作 `manage_access(..., 'join_rule', function=, group=, enabled=bool)`；默认无规则。模板固定：单功能、本消费方 `resource_scope=all`、`continuous`、`invoke`、无期限；不含管理权，不跨 Bot／群／消费方。
- 唯一自动路径：群状态 `reason == 'GROUP_MEMBER_ADD'` 的消费通知。对有该群规则的消费方，`native_join` 当前、其事件 ID 与通知事件一致、群在该功能群范围内、成员无明确拒绝且不在框架黑名单、尚无同事件来源或同事件撤销时，新增 `basis='native_join'`、`join={event_id,timestamp,sequence,session_id}`、`cycle` 为依据周期的来源。与同一通知中的离群／换周期撤销同锁、同一次持久化。通知（及其后的恢复确认）在写入完成后才结束，不能先就绪后消费。
- 评估：`explicit` 策略下，自动来源只在凭据 `join` 当前且周期、事件 ID 都一致时计入；其他来源仍要求 `usable` 且 `present`。`group`／`members`／`role` 策略和其他消费方不受入群依据影响。仅凭入群依据放行的决定带 `fact_kind='native_join'`，`decision_current` 用 `join_current` 复查。
- 自动来源不随平台查询或主人确认复活：重建后依据失效即不可用，须主人重新授权（主人来源）。关闭规则只阻止之后的入群，已建立的来源与主人来源一样在“授权管理”撤销。
- 失败与取消：持久化失败沿用消费方隔离并使群状态消费通知失败，恢复须重建；取消时 SQLite 写入先排空。即使来源已落盘，重建递增轮次后也不能放行。首次调用从不授予，不设第二条“调用时自动授权”路径。

### 消费方接线：忍者必须死3

- 群记录 `auto_grant`（默认 `false`）；只在群开放且勾选时同步该群 `join_rule`，其余情况移除。状态接口对照两侧。主人在本插件授权时不复用自动来源，另建主人来源。
- 没有自动来源的调用沿用 #72 的未授权通知规则。

## 后果

- 能力分层报告：QQ 官方提供入群事件；源码 WS 订阅 `1<<25` 并经恢复层接线；本 Bot 是否实际收到未实测。
- 遵守 #18 R17：不补全单人核验、完整名册或自动恢复；重启或新会话后自动授权的成员需主人重新授权。
- 不改框架 `core/`。
