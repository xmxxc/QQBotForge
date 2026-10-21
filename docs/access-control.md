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

已鉴权 HTTP：`GET /api/ext/access-control/?bot=...&consumer=...` 查看；同路径 POST 操作对象，始终包含 consumer/operation。后台继续使用框架会话与同源写入校验，返回 no-store。主人私聊命令使用同一操作：

```text
授权管理 {"consumer":"reports","operation":"register","owner":"plugin:reports","functions":{"read":{"kind":"public","groups":["假群"],"private":false}}}
授权管理 {"consumer":"reports","operation":"resources","resources":[{"id":"report-r"}]}
授权管理 {"consumer":"reports","operation":"explain","function":"read","group":"假群","member":"假成员","resource":"report-r"}
```

注册同一归属可安全重试；grant 每次新增来源，不应自动重试未知结果。撤销、拒绝、范围或期限调整先使在途决定失效，再保存。范围组、快照／持续批量由 #25 扩展；绑定和全员报告仍待 #26—#27。

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
