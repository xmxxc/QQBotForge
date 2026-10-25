# 独立授权管理

对应 A01 #24。完整安装 `modules/access_control/`，启用“黑白名单授权”；主人后台出现“授权管理”。不要求账户或问答目录。群相关策略另外启用群状态，并完成事实核验和恢复基线；公开策略可在没有群状态时使用。框架组合为 F01 `460c841`／personal 同步 `b23572b`，需先审核 PR #38/#39。

## 一条可演示流程

1. 主人后台选择 Bot，填写消费方 `reports`、归属 `plugin:reports`、功能 `read`、群原生 ID，选择“明确授权”，注册消费方／功能。同 Bot 的同一消费方由另一归属注册会报冲突，重载和重复声明不会覆盖主人已保存策略。
2. 填写资源 `report-r`，保存资源清单。在群状态单独核验目标原生成员，并完成恢复；不能用 QQ 号、显示名或字符串相等代替群身份。
3. 授权页填写该成员、资源和期限，保存“授权调用及查看”，点“解释资格”查看来源、周期和到期时间。普通成员不能管理，群中主人命令只返回简短状态。
4. 成员离群后，真实群状态通知撤销该群所有消费方的明确来源，保留来源 ID、期限和撤销原因；其他群和拒绝保留。重入后即使当前角色为管理员，也不能恢复旧明确授权，须主人重新授权。重启／新会话后仅确认“当前在群”的人工事实不证明旧周期连续，会建立新周期并撤销旧来源。显式角色策略按当前可靠角色计算，不赋予账户管理权。

撤权只依据群状态已接受的事实：成员已离群或成员周期已变化。群状态拒绝采用的迟到、同秒冲突、同 ID 冲突、缺字段事件及重复补发不撤销任何来源，只使该 Bot 的群事实暂停，须重建基线。Bot 加入或被移出群同样只暂停该 Bot 的群相关资格，不撤销明确来源；重建核验后，仅仍有效、未撤销、未到期且成员周期仍成立的来源恢复，核验发现已离群或周期变化的来源随后撤销。同群其他成员的可信入群／退群只使旧决定按版本失效，未受影响成员重新评估后继续可用；新成员只记录事实，不自动授权或联系。

策略选择：`public` 无需个人正向授权；`group` 允许指定群当前可靠成员；`members` 进一步限定原生成员；`role` 明确选择 owner/admin/member；`explicit` 使用有效来源的调用及资源查看权；`owner` 使用 Bot 配置的主人身份。所有策略先检查框架及模块拒绝，再检查主人配置的群／私聊范围。默认群列表为空、私聊关闭，不会开放所有未知群。公开策略不查询群连续性；群资格策略需要当前群事实。

## 消费接口（scoped v1）

- `declare(bot, consumer, owner=..., functions={function: policy})`：稳定声明归属与默认功能；既有策略不被重声明覆盖。
- `declare_resources(bot, consumer, [{id, groups?, enabled?}])`：替换该消费方资源清单。不同消费方同名资源互不共用授权；移除资源不会在重新添加时恢复已撤销来源。
- `manage_access(bot, consumer, operation, function=..., group='', member='', **fields)`：policy、grant、revoke、deny、allow、expiry。grant 可指定 resources、invoke 和 expires_at（Unix 秒或 null）；revoke／expiry 可指定来源 ID。
- `evaluate(bot, consumer, function, group, member, resource='', action='view', framework_allowed=True)`：返回允许／拒绝、原因、来源、周期、角色、版本及有效期。调用动作需要 invoke；查看还需要资源权。传入 false 的框架准入不能被正向策略覆盖。
- 群资格相关原因：`group_facts_unavailable`（群状态未启用）、`group_facts_processing`（恢复补发、消费通知或写入尚在完成，`retryable=true`，稍后重试即可）、`group_facts_recovery_required`（须主人重建恢复基线）、`member_left`（已离群）、`membership_unverified`（成员待核验或事实不可靠）。其他 `retryable` 均为 false。主人解释返回这些原因；面向普通用户的提示由消费方决定，不应据此透露成员事实。
- `decision_current(bot, decision)`：使用前不等待地复查模块代际、消费方版本、实时期限、当前框架拒绝及所需群状态凭据。群状态停用、重载或处理在途会使依赖它的决定失效。不要把客户端回传的决定当作可信凭据。
- `access_snapshot(bot, consumer)`：主人管理副本；不读其他模块数据库来判断权限。
- 入群规则（#73）：`manage_access(bot, consumer, 'join_rule', function=..., group=..., enabled=bool)` 维护 `join_rules`，默认无。仅在群状态 `GROUP_MEMBER_ADD` 消费通知内、该成员原生入群依据当前且事件一致、群在功能范围、无明确拒绝和框架黑名单、同一事件未建立或撤销过来源时，新增 `basis='native_join'` 的持续来源（单功能、本消费方全部资源、invoke、无期限），与同通知的离群撤销同一次持久化。`explicit` 评估中这类来源只在凭据 `join` 当前且周期与事件 ID 一致时计入，决定带 `fact_kind='native_join'` 并由 `decision_current` 用 `join_current` 复查；`group`／`members`／`role` 策略及其他来源不接受入群依据。主人 HTTP／QQ 管理入口只能关闭规则（`enabled=false`），开启只由拥有该消费方的插件代码调用，避免为其他消费方误开。关闭规则与自动写入共用消费方锁：已开始的写入先完成，之后的入群不再新增；已有来源不随关闭撤销。详见 `docs/adr/0001-native-join-admission.md`。

已鉴权 HTTP：`GET /api/ext/access-control/?bot=...&consumer=...` 查看；同路径 POST 操作对象，始终包含 consumer/operation。后台继续使用框架会话与同源写入校验，返回 no-store。主人私聊命令使用同一操作：

```text
授权管理 {"consumer":"reports","operation":"register","owner":"plugin:reports","functions":{"read":{"kind":"public","groups":["假群"],"private":false}}}
授权管理 {"consumer":"reports","operation":"resources","resources":[{"id":"report-r"}]}
授权管理 {"consumer":"reports","operation":"explain","function":"read","group":"假群","member":"假成员","resource":"report-r"}
```

注册同一归属可安全重试；grant 每次新增来源，不应自动重试未知结果。撤销、拒绝、范围或期限调整先使在途决定失效，再保存。范围组、快照／持续批量由 #25 扩展；绑定由 #26 扩展，全员报告由 #27 提供（见下文 A04）。

## 数据与故障范围

新策略按 `(Bot, consumer)` 保存为同一 SQLite 文件中独立 `access_consumers` 行，schema_version=1。逐行按 SQLite 主键读取与校验，单消费方 JSON／结构／版本损坏或写入约束失败只隔离该范围；其他 Bot／消费方继续处理。数据库整体不可用暂停所有 scoped 决定。局部消费行在生命周期撤权失败时保持隔离，群事实和其他消费者仍可完成；恢复后也须通过当前周期与成员状态检查，不能凭磁盘旧来源复活资格。

原有 `access/state` 数据、稳定来源 ID、期限、拒绝、周期和旧接口保留供迁移；新旧数据不会默默合并或扩权。旧账户算法接口仍属于迁移基础，生产账户组合迁移由 #33—#36 交付，旧测试的模拟观察源不能证明新平台接线。未操作生产数据库。启用中途任一步失败或取消时，候选实例只撤销本次注册的 Hook、路由、页面与群状态订阅并关闭存储；路由已被其他实例替换时不删除，其他模块不受影响，可直接再次启用。授权初始化时即订阅已启用群状态；群状态后启用或重载通过可用性通知重新绑定，不等待第一次权限查询。

## 验证边界

使用独立目录实际 ModuleManager、鉴权 HTTP、主人命令、WSClient 原始事件到群状态再到授权；临时 SQLite 触发器验证单行故障，删除临时表验证整体存储失败。身份、资源、账密和平台均为替身。算法、真实框架接线分别有测试入口；浏览器无可用实例，未做视觉核查；未连接真实 QQ 或验证实际平台许可。


## 范围、批量与来源解释（A02）

授权页可明确选择当前功能／功能组／此消费方全部功能，填写功能组 ID 与成员功能后保存；资源选择显式 ID／资源组／此消费方全部资源。资源清单 JSON 可记录分组与 enabled；快照和持续模式需主动选择。任何“全部”都不跨 Bot 或消费方。每消费方最多声明100个功能、5000项资源；显式资源ID选择最多500项，分组／全部解析结果可覆盖5000项，保存与重载使用一致上限。

沿用旧范围算法 `_function_members`／`_resource_members` 的 scoped 接口：`function_group` 操作接受 id/functions；grant 接受 `function_scope={kind:function|group|all,id?}`、`resource_scope={kind:resources|group|all,ids?|id?}`、`mode=snapshot|continuous`。resources 简写与 resource_scope 不可同时给。明确空资源列表只授予调用权，不能查看资源；持续空功能／资源组可以声明未来范围，快照须有当前功能。

快照记录当前解析出的功能和资源，未来加项不自动获得；移除资源仅撤销该来源的对应资源，保留同来源其他资源及调用资格，并记录 partial 撤销历史，资源重新加入不会复活快照查看权。持续来源保留原选择，功能／资源组变化和清单增减时按当前范围重算；重新加入其明确范围可以生效，来源一旦整体撤销则不会复活。源 ID、成员周期、期限与模式可从解释及主人快照追溯。

revoke／expiry 支持 source 指定来源 ID；即使持续功能组当前为空，仍可撤销对应来源。deny／allow 支持 function_scope；明确拒绝对角色及公开策略也优先，重新授权不移除拒绝。按来源 ID 移除拒绝会使其原覆盖的全部功能凭据失效。到期在 evaluate 和 decision_current 中按当前时间检查，无需定时任务；调用与查看可来自不同有效来源，有效期取满足两者的最后覆盖交集。

`bulk_grant/bulk_revoke/bulk_expiry` 使用 `members=[{member:原生ID},...]`，最多500项，禁止与单成员 member 混用。每项返回 saved/result 或 failed/error，另有 success/failed 汇总；部分失败只重试失败项，成功来源保留 ID。grant 重试会新建来源，不承诺请求幂等。主人页面提供逐行原生 ID、批量按钮、来源 ID 与期限编辑，QQ `授权管理 <JSON>` 使用同一管理操作。

消费决定包含 audit version、function_revision 和 shared_revision；单功能策略或来源调整只失效其相关功能，写入期间无关功能继续可用。资源清单、数据库整体故障和模块代际仍为共享失效；真实群事实变化通过群状态凭据影响所有实际依赖方，不能用局部授权版本规避。

A02 自动验证使用真实独立模块／鉴权 HTTP／主人命令及临时 SQLite，覆盖快照和持续范围、分组增减、源级撤销、批量部分重试、实时期限、拒绝优先、重载稳定 ID、多 Bot 和消费方隔离。SQLite 写锁验证单功能调整期间其他功能凭据仍可用。页面脚本语法检查通过；浏览器视觉及真实 QQ 平台许可仍未执行。


## 通用身份绑定与私聊群上下文（A03）

成员在实际 QQ 私聊发送 `身份绑定 申请 <群原生ID> <成员原生ID>`。模块只从 C2C 原始 author.id 记录私聊 OpenID；union 仅作来源元数据，QQ号、union、群 ID 与私聊 ID 字符串相同均不证明关联。请求创建 pending 编号，编号只用于定位申请。申请不要求已有功能授权，也不授予任何资格，不主动联系新入群成员。申请结果回复按框架 `Sender.reply` 的真实返回分别记录，与待审状态无关：平台响应对象（含空响应体 `{}`）为 `sent`；带平台错误码（`code` 或 `err_code`）的响应为 `platform_rejected` 并保留该码（HTTP 状态码同样保留）；只有 `message`、没有消息 ID 的错误体记为 `platform_rejected/unknown`；框架网络层失败 `code=-1` 为 `send_failed/network_error`；返回 `None`（未发送，或框架按其忽略列表吞掉平台错误码）为 `send_failed/no_response`；回复抛出异常为 `send_failed/<异常类型>`；处理被取消时结果不明，尽力记为 `send_unknown/cancelled` 并继续传播取消；该记录写入失败只写错误日志（含申请编号）并使身份资料按存储失败暂停，记录期间再次取消时等待已开始的数据库写入实际结束（不留后台写入）后传播取消，二者都不覆盖原取消。任何失败都不记为已发送。

主人后台“通用身份绑定”查看待审和已绑定记录，填写两侧身份核对依据并明确确认。主人 QQ 命令同用管理入口：

```text
授权管理 {"operation":"binding_list"}
授权管理 {"operation":"binding_confirm","request":"假申请ID","confirmed":true,"evidence":"主人核对群成员与本私聊申请为同一人"}
授权管理 {"operation":"binding_reject","request":"假申请ID"}
授权管理 {"operation":"binding_revoke","group":"假群","member":"假成员"}
```

页面和命令确认均只给 Bot 主人／已登录主人后台；角色策略或后续委托权限不进入此身份入口。确认时要求群状态当前可靠成员与已观察私聊身份；原群成员关联另一私聊身份、同私聊在同群关联另一成员均需先明确撤销和核实，不静默改绑。拒绝／撤销申请不能凭旧编号再次确认。绑定本身跨消费方复用，资格始终按消费方各自策略计算。

`await access.private_contexts(bot, consumer, function, private_id, group='', resource='', action='invoke')` 返回有资格 candidates、selected 和原因。消费方私聊范围需启用；公开私聊直接形成决定，不读取身份绑定或要求群状态。群限定私聊先核验关联，再执行当前群准入／成员／角色／来源／拒绝／期限策略；一个有效群自动选择，多个群须传 group 选择，只展示该身份的合格候选。`身份绑定 群资格 <消费方> <功能> [群] [资源]` 可在本人实际私聊查看候选。绑定冲突或不明确的同群关联不会自动选人。

消费方在最终私发前执行 `access.context_current(bot, selected)`，重查身份版本、当前关联、私聊框架与模块明确拒绝及消费方决定／群事实。私聊身份被拒绝时不会转用群成员身份绕过。切换群、撤销绑定、离群、角色或期限变化使旧结果失效。退群后可保留可靠身份关联，但不会保留或恢复已撤销来源。真实平台是否允许目标私发不由身份关联保证，平台失败只记发送状态。

`binding_snapshot(bot)` 为主人身份管理副本。数据独立于消费方，按 Bot 保存为 `identity_state/state`，包含 private、bindings、requests 与版本；身份局部故障暂停该 Bot 的依赖身份上下文，公开私聊不依赖它。存储整体不可用仍暂停实际依赖方。

旧账户绑定迁移使用 `binding_import` 操作，bundle 包含 private_identities、bindings、binding_requests 三个显式列表。仅接受原始 observed_c2c 观察与有依据的 owner_confirmed 绑定；保留已有绑定ID（旧格式无ID时用原群／成员复合键）、申请ID、来源、证据及 pending/rejected 等状态。已存在的记录按语义兼容并保留现存版本：同一私聊原生ID且现存为 observed_c2c 观察即为同一身份（不比较整条字典，旧 union 不改写新记录）；同群同成员绑定指向同一私聊身份即兼容；同ID申请身份三元组一致即兼容。绑定指向不同私聊身份、同ID申请身份不同和同群私聊身份关联多个成员均拒绝（整批不写入），检查在资格筛选前执行；无新增记录时不写库。返回 `status`（imported／unchanged）、`imported`／`existing` 三类计数及原有 `bindings`／`requests` 总数；按绑定ID归档每轮撤销，并保留群／成员撤销标记；持久保存关联撤销历史，重导入旧记录不能恢复已撤销关联，须新申请并由主人核实。不推断关联，不导入授权／拒绝／成员事实；迁移后的上下文仍需要新模块的当前资格。测试以旧字面数据在临时库验证导入、幂等重试、重载和不扩权；未读取或迁移运行数据库。账户组合实际调用迁移接口留给 #33。

A03 验证：实际独立模块、Application 私聊、主人命令、鉴权 HTTP 与原始 WS 退群／重入；两个消费方共享关联但不串权，多 Bot 分区、多群选择、角色撤回、到期、拒绝、待审／驳回、平台替身私发失败及旧数据导入。浏览器无可用实例，视觉与真实 QQ 许可未验证。


## 全员资格报告与发送前检查（A04）

消费方提交功能、资源和动作，取得含本 Bot 与其他 Bot 的全群资格报告，不自行查询平台名册或解析授权版本：

```python
report = await access.audience_report(bot, consumer, function, group, resource, actions=['invoke', 'view'])
send = dict(consumer=consumer, function=function, group=group, resource=resource, actions=['invoke', 'view'])
access.member_current(bot, report, asker, **send)   # 提问者本人当前是否仍通过所需动作
access.report_current(bot, report, **send)          # 群发前的非等待最终检查
```

两项检查都必须由调用方传入本次实际发送的 Bot、消费方、功能、群、资源与所需动作（关键字参数，无默认值、无旧的两参数入口）；与报告签发时的上下文逐项完全一致才可能通过。`view` 报告不能用于要求 `invoke+view` 的发送，其他消费方／功能／群／资源／Bot 的报告不能搬用，`member_current` 还要求该成员在报告名单中。

受限问答传 `actions=['view']`；账户群发传 `['invoke', 'view']`。报告字段：`complete`（群状态本轮完整名册且可信）、`eligible`、`reason`（eligible/member_ineligible/roster_incomplete/group_facts_unavailable/state_changed）、`roster_reason`、`members`（成员原生ID、名称、bot、self、可靠角色、`private_bound` 及逐动作允许与原因）、`failures`、`failures_complete`、`valid_until` 与不透明的 `credential`。

成员只来自群状态完整名册，名册要求显式末页、角色可识别、无遗漏已知成员且本 Bot 在册；不使用历史活跃名单或人数。名册不完整、分页失败或群状态停用时 `eligible=false`，只列出当前可靠在群的已确认成员，`failures_complete=false`，不宣称列出全部失败者。明确无权与完整性未知分别由每人原因和 `complete/roster_reason` 区分。

报告字段只供展示：本轮名册群凭据、每人每动作决定、签发上下文与最早到期时间封装在不透明的 `credential` 对象内，只有签发它的授权实例接受（复制报告保留同一凭据；凭据不可 JSON 序列化，需要保存或展示时使用管理副本）。改写 `actions`、`valid_until`、`eligible`、成员名单或附加任意证明字段都不能扩大资格或延长期限；授权模块卸载／重载后旧凭据全部失效。`report_current` 不等待地复查名册凭据（群版本、恢复进度、写入者和持久化）、全部决定（策略、来源、拒绝、框架拒绝、模块代际）及最早到期时间。同进程模块可以直接调用任意消费方标识，本校验防止误用和改写报告，不是进程内的身份认证；也不承诺检查与网络发送之间的原子性或平台未投递事件。成员退群、新人观察／入群、角色核验变化、授权撤销／拒绝或到期都会立即使旧报告失效；其他消费方或其他 Bot 的变化不影响。群状态 `facts(bot, group)` 从同一份公开副本一次取得群与逐成员凭据，避免大群逐人复制；3000 人两动作报告约 1.3 秒，最终检查约 0.1 秒（本机临时测试）。

主人管理：后台“全员资格报告”或 `授权管理 {"consumer":"reports","operation":"audience","function":"read","group":"假群","resource":"report-r","actions":["invoke","view"]}`。HTTP 与私聊返回去除 `credential` 的管理副本；群里只给简短状态，普通成员命令不处理。

A04 验证：实际 ModuleManager 独立加载授权、群状态及一个测试消费模块，Application 群消息入口在报告后、发送前检查；原始 WS 退群／入群、临时 SQLite、平台分页替身。覆盖两个 Bot 成员、Bot 不合格、分页失败、检查中退群、新人入群、到期、角色变化、拒绝、消费方与 Bot 隔离及主人鉴权。浏览器无可用实例，未做视觉核查；未连接真实 QQ 或核查平台许可。账户群发改用本接口由 C03 #35 交付，受限问答由 Q05 #32 使用。

## 群成员转私聊上下文（Q05 扩展）

`await access.member_context(bot, consumer, function, group, member, resource='', action='view')` 先按群成员身份计算当前决定，再查找主人已确认的该成员私聊身份，返回 `{context, reason}`。`reason` 为 `selected`、成员不合格原因、`binding_missing`、`binding_unavailable` 或 `private_denied_or_changed`。上下文与 `private_contexts` 候选同形，私发前用 `context_current(bot, context)` 复查绑定版本、私聊拒绝和决定。用于受限问答群内不满足全员可见时私发提问者，也供账户共享群发降级使用；平台是否允许私发仍由发送结果决定。
