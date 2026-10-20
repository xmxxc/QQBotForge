> 本文保留旧版账户共享的管理入口和操作说明，供迁移与排查使用。当前规划已采用群状态、授权、自助问答三个模块；模块职责、框架能力及实际接入状态以[总体规格](specs/bot-modules.md)和[当前进度](plans/account-sharing-progress.md)为准。新功能尚待实施，下文不作为新架构已完成的证据。

# 账户共享

## 当前架构（C01 #33 起）

账户共享插件只保存账户、账户规则引用、群发意向与主人结果；它组合三个模块：

- **群状态**：成员是否在群、入群周期、角色和完整名册。成员核验与名册在“群状态”页面或 `群状态 核验／确认／名册` 命令中处理，插件不再提供核验命令。
- **黑白名单授权**：账户插件注册为消费方 `account_sharing`（归属 `plugin:account_sharing`，功能 `account_sharing`，默认明确授权、未开放群、允许私聊上下文）；每个账户是一项资源（分组＝账户分组，停用＝资源停用）。删除账户即移除资源，直接授权随之失去该账户，同名新账户获得新 ID、不继承旧授权。身份绑定统一使用 `身份绑定 申请` 与授权管理页确认。
- **自助问答**：账户关键词规则保存在问答命名空间 `account_sharing`，`resource` 规则只引用账户 ID，不含账密；匹配时通过 `admit` 先按当前资格过滤再排序。

主人入口不变：`账户管理 新增／修改／删除／列表／预览（仅私聊）／批量账户／规则*／授权／撤权／拉黑／解除拉黑／期限／批量授权／批量撤权／批量期限／解释／功能组／白名单`，以及新增 `账户管理 迁移` 与 `账户管理 恢复来源 {"group":…,"member":…}`。非主人、群问答委托者都没有账户管理权。授权成功只保存并报告结果，不自动发送密码。

查询：群内命中有权账户时先提示将私发，再通过授权模块 `member_context` 找到主人确认的私聊身份，发送前 `context_current` 复查后私发；缺少绑定时给出 `身份绑定 申请 <群ID> <成员ID>` 引导；私聊中通过 `private_contexts` 解析有效群上下文。停用账户立即不可查。群发（主人开启且全员含 Bot 有调用和查看资格）改用授权模块全员报告，详见 C03 #35。

旧数据迁移：`账户管理 迁移`（或后台“迁移旧授权与绑定”，`POST /api/ext/account-sharing/migration`）在同一授权数据库内读取旧版授权状态，导入到消费方：保留来源 ID、期限、仍存在的账户资源与模式，拒绝与撤销历史原样导入，旧白名单群只在新策略未设置群时作为策略群；旧插件的私聊身份、绑定与申请经授权模块 `binding_import` 导入。导入的授权来源绑定到 `legacy:<旧周期>`，在主人于群状态核验成员当前在群后执行“恢复来源”前一律不生效；退群仍会撤销它们；已撤销来源不会恢复；重复迁移按 ID 跳过。迁移结果写入主人操作记录并尝试通知。运行实例数据迁移需另行安排，本次只用临时副本验证。

日志说明：框架消息日志可能记录回复正文（包括私发的账户信息）；插件只在操作记录中保存授权、绑定、发送状态及错误码，不宣称账密不会进入现有日志。

> 以下各节记录迁移前的旧实现与行为基线，供核对；新实现以上文和各票说明为准，C02—C04 完成后整体改写。


交付目录为 `modules/access_control/`、`modules/self_qa/` 和 `plugins/account_sharing/`，框架源码保持原样。存储随授权模块提供，使用现有上下文数据路径，三个扩展共同复用。

新增管理入口见[核验、群发检查与待办](account-sharing-management.md)。

当前可使用主人账户管理与私聊预览、自助问答匹配、授权记录、成员核验、名册同步、群发检查和待办管理。可靠生命周期事件接入依赖 #16，成员敏感查询暂停，后台显示此限制。以下成员目录、绑定、关键词、按钮及发送流程由模拟事件源验证，生产接入仍待验收；详见[扩展接入检查](plans/account-sharing-extension-boundary.md)。部署与合并需审核后单独进行。

插件加载后，后台扩展页面选择“账户共享”，填写当前 Bot 的 AppID 并加载账户。页面沿用后台会话，支持新增、修改、启停和删除。默认启用新账户；每条账户使用独立稳定 ID，同名账户不共享身份或授权。所有数据按 AppID 分区。

QQ 管理命令使用当前 Bot 的 `owner_ids`：

```text
账户管理 新增 {"name":"演示账户","login":"fake-user","password":"fake-password"}
账户管理 列表
账户管理 修改 <账户ID> {"password":"new-fake-password"}
账户管理 修改 <账户ID> {"enabled":false}
账户管理 删除 <账户ID>
账户管理 预览 <账户ID>
```

预览仅在主人 C2C 私聊允许，读取最新启用账户；群内管理只返回状态，详情在主人私聊或已登录后台查看。不要在群内输入含真实密码的管理命令。页面与命令共享 `plugins/account_sharing/data/account-sharing.db`，卸载插件移除扩展入口，数据保留供下次加载。

沿用框架现有 SQLite 持久化和日志机制。共享密码需要可恢复保存；发送的消息正文可能被框架记录，不能认为发送成功意味着日志没有记录。请限制数据库、后台和日志的访问权限。

## 管理接口

所有接口均要求现有面板会话，写请求沿用现有同源校验。统一返回 `success` 和 `data` 或 `error`；JSON/字段不合法返回 422、未知 Bot/账户返回 404、方法不适用返回 405、停用预览返回 409。通过 `?bot=<AppID>` 选择已配置 Bot。

- `GET /api/ext/account-sharing/accounts`：主人账户列表。
- `POST /api/ext/account-sharing/accounts`：新增，必填 `name`、`login`、`password`；可选 `enabled`、`description`、`url`。
- `PATCH /api/ext/account-sharing/accounts/<ID>`：修改上述字段，未提供字段保持不变。
- `DELETE /api/ext/account-sharing/accounts/<ID>`：删除。

新增请求会生成新 ID，不能无条件重试；修改为设置目标值，可重试；删除不存在账户返回 404。

测试使用临时 SQLite、假身份、假账密和平台替身。未进行真实 QQ、浏览器或 Bot 部署核查。

## 指定群成员授权与目录

先在现有模块管理中启用“黑白名单授权”（`access_control`），再配置白名单并授予成员调用及单账户资源资格。模块未启用或存储异常时成员查询停止。群白名单仅允许此功能在该群使用，不会自动授权成员；新成员和普通聊天不会触发主动联系。

```text
账户管理 白名单 {"group":"fake-group","enabled":true}
账户管理 授权 {"group":"fake-group","member":"fake-native-member","identity_type":"group_member_openid","confirmed":true,"resources":["账户ID"]}
账户管理 撤权 {"group":"fake-group","member":"fake-native-member"}
账户管理 拉黑 {"group":"fake-group","member":"fake-native-member","scope":"member"}
账户管理 解除拉黑 {"group":"fake-group","member":"fake-native-member","scope":"member"}
账户管理 解释 {"group":"fake-group","member":"fake-native-member","resource":"账户ID"}
```

主人在群内授权可 @单个目标，JSON 中省略 `member` 和 `identity_type`；须保留 `confirmed:true`，表示主人已核对成员当前在群。私聊及 Web 使用明确的群原生 OpenID，不能把数字 QQ 号或 union 显示身份当作原生身份。Web 可从模块观察到的成员选择；该列表仅用于选人，不代表完整名册。拒绝可以选择整个群或指定成员，优先于授权。撤权只影响当前群、当前功能。

成员发送 `账户列表` 仅列出本群当前有权使用的启用账户，不显示账号或密码。未授权命中入口时使用统一简短提示，同一 Bot/群/用户十秒限频，普通聊天不回应。

`GET /api/ext/account-sharing/access?bot=<AppID>` 返回主人可见的准入、成员、授权及拒绝状态；`POST` 接受与上面命令同形的 JSON，另加 `operation`（`whitelist/grant/revoke/deny/allow/explain`）。此接口同样要求现有面板会话。授权新增来源不能无条件重试；白名单、撤销和拒绝设置是目标状态操作。

授权模块的公共消费接口是 `execute(bot, operation, function=..., group=..., member=..., ...)` 和 `decide(bot, function, group, member, resource, action='view', framework_allowed=True)`。消费方必须传入框架拒绝结果，不得用模块放行覆盖框架拒绝；返回允许状态、原因、来源、成员核验状态和版本，不保存或发送账户密码。模块提供 `observe(event)` 消费入口；当前框架没有已接通的可靠观察源。`snapshot()` 返回 `capabilities.lifecycle_events:false`，`decide()` 拒绝敏感查询并给出 `event_integration_unavailable`。此能力状态不接受管理 API 设置。


## 绑定、私聊群选择及发送状态

授权保存后，后台记录授权、绑定、通知及发送状态。没有可靠映射时进入待绑定，按平台能力在授权群提供引导；此操作不发送密码。通知失败也不会撤销已保存的授权。

成员先私聊本 Bot：

```text
账户绑定 <群原生ID> <成员原生OpenID>
```

Bot 从实际 C2C 事件记录本次私聊身份，只创建待审申请。主人在后台刷新“待绑定申请”，核对群成员与此私聊申请为同一人，填写核对依据并确认；也可在主人命令执行：

```text
账户管理 确认绑定 {"request":"申请ID","confirmed":true,"evidence":"已通过可信方式核对两侧身份"}
账户管理 结果
```

申请编号不具有自动绑定权限，字符串相同也不会自动绑定。请仅在确实核对两侧身份后确认，不把公开群信息当作身份凭证。后台展示的群原生 ID、私聊 ID 和 union 显示身份可能不同，不能互换。已有经主人确认的可靠关联可复用；每次查询仍检查相应群的当前资格。

成员发送 `账户列表` 或 `账户查询 <账户ID>`。私聊只有一个有效授权群时自动选择；多个群时使用 `账户群 <群ID>` 选择，再重新计算目录。群内查询先提示将尝试私发，然后仅向已确认的私聊身份发送账密；缺少绑定则引导申请，绝不把群成员 OpenID 当作私聊 ID。

私发可能返回无好友关系、拒收、权限不足或网络失败等状态。失败会记录代码并报告，账密不会回退群发，已保存授权继续保留。群内只有真实私发成功后才报告“已私发”。此阶段所有成员账密查询均为私发。

主人 QQ 私聊命令提供经过实际事件观察的主人 C2C 通知目标。未观察到可靠主人私聊身份或平台无法发送时，操作结果保留在已登录后台，不直接假设 `owner_ids` 中的群身份可用于私聊。主人可先私聊执行 `账户管理 列表`，建立当前 Bot 的已验证通知场景。

管理接口新增 `GET /bindings`（返回待审申请与已确认映射）、`POST /bindings`（确认，必填 `operation:confirm`、`request`、`confirmed:true`、`evidence`）及 `GET /operations`，路径仍以 `/api/ext/account-sharing/` 开头并要求 `bot` 与现有会话。绑定确认设置目标映射，重复确认不会授予额外权限；确认前始终重新检查授权资格。管理命令与接口使用同一绑定流程。


群消息中平台实际提供的群原生身份与 union 标识关系由授权模块分别保存，供群身份黑名单在私聊查询时继续生效，不用于自动推导私聊绑定。开启 union 显示且已有框架用户黑名单、但尚未可靠观察群主体显示身份时，私聊查询保守暂停，需先取得可靠群身份事件。主人配置变更后，旧主人通知目标会在发送前重新核对，不再向已移除的主人发送操作通知。


## 自助问答与关键词查询

在模块管理启用 `self_qa`（自助问答）。账户共享页面的规则区支持多关键词、逐个关键词启停、整条规则启停、精确/包含匹配、优先级、账户引用或静态文字。账户规则仅保存稳定账户 ID，修改密码不需要修改规则。

```text
账户管理 规则新增 {"name":"示例别名","target":"账户ID","keywords":["测试别名","另一别名"]}
账户管理 规则修改 <规则ID> {"mode":"contains","priority":10}
账户管理 规则修改 <规则ID> {"keywords":[{"text":"测试别名","enabled":false},"另一别名"]}
账户管理 规则修改 <规则ID> {"enabled":false}
账户管理 规则列表
账户管理 规则删除 <规则ID>
```

规则默认精确匹配。先匹配，再由消费方筛选当前有权候选：精确优先；包含优先级数字较大者优先，未指定时按 0 处理并以较长命中关键词优先，显式同优先级保持并列。并列时只列有权候选，相同账户仅一次；未命中的普通聊天不回应。账户查询读取最新账户并重新鉴权，停用、删除或撤权即不能通过旧关键词获取内容。静态规则使用 `kind:"text"`，`target` 为回复文字；账户插件要求调用资格、可靠绑定并严格私发，静态内容不作为外部动作执行。

管理接口新增 `GET/POST /rules`、`PATCH/DELETE /rules/<ID>`，路径与会话要求沿用前文。新增必填 `name`、`target`、`keywords`；可选 `kind`（`resource/text`）、`mode`（`exact/contains`）、`priority`（整数或 null）、`enabled`。`keywords` 接受字符串及 `{text,enabled}` 对象混合列表。新增生成 ID，不能盲目重试；修改设置字段值，可重试。

可复用模块接口为 `manage(bot, namespace, operation, data, key)`、`match(bot, namespace, text)`、`best(candidates)`；返回稳定规则/目标引用及排序信息，消费方先筛选授权再排序、去重和解释目标。`answer(bot, namespace, text, send, allowed=False)` 提供显式准入后的静态回复适配，不注册聊天监听，不执行外部动作。模块不依赖账户存储或授权数据，另一个消费方可以使用自己的命名空间及准入策略。账户插件仅消费 `account_sharing` 命名空间。

## 账户按钮、分页与文字降级

账户列表、群选择及歧义候选优先发送按钮。每个账户各有一个直接查询按钮；每页最多六项，提供上一页、下一页和通用“账户列表”入口。点击仍按实际点击者、当前 Bot、群上下文及当前资格判断；按钮只带短期查询引用，不包含账号或密码。个人菜单限原调用者、原场景及入群周期，十五分钟后失效；失效时重新发送 `账户列表`。

也可发送 `账户列表 2` 等页码，或使用消息中列出的 `账户查询 <ID>`、`账户群 <群ID>` 和 `账户翻页 <引用>`。按钮被平台拒绝或未启用 Markdown 时，自动发送这些等价文字操作；客户端不支持按钮时也可直接使用文字。翻页重新生成当前目录和权限，不能借旧页面恢复已经撤销、到期、停用或删除的账户。

交互回调显式鉴权，沿用框架现有 ACK 倒计时和传输响应机制。ACK 只确认收到交互，查询、私发和失败反馈分别处理；群内点击账户按钮不会把密码作为普通群回复。私发失败仍保留真实状态和绑定引导。

实现按 QQ 官方[按钮协议](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)构建回调按钮，使用[互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)中的群原生成员或 C2C 身份。自定义按钮需要相应平台许可；自动测试验证拒绝后的文字降级，不代表真实 Bot 已获许可。每条键盘最多五行、每行最多五个按钮，当前布局每行两个个人操作。


C2C 普通消息实际提供的原生私聊 ID 与私聊 union 显示身份也分别保存，供缺少 union 字段的按钮回调核对框架黑名单；它与群内的 union 关联是不同记录。未知显示关系且存在相应框架用户拒绝配置时，回调保守暂停，先取得可靠的普通私聊身份事件。发送前会检查查询主体和私聊目标当前适用的拒绝规则。

## 分组与一次／持续批量授权

账户可设置 `groups:["演示组"]`；账户分组与功能授权组独立。页面有不同选择控件：功能范围选择单功能、主人定义的功能授权组或全部已注册功能；账户范围选择当前勾选账户、账户分组或全部账户。`snapshot`（一次）固定保存当前功能和账户 ID，后续新增不继承；`continuous`（持续）重新计算当前及未来符合选择的功能／账户。停用账户立即不可查，删除 ID 后相关单账户来源清理，同名新账户不会继承旧 ID 的授权。持续账户组成员移出后立即失去该来源，一次来源仍保留当时选中的 ID。

```text
账户管理 修改 <账户ID> {"groups":["演示组"]}
账户管理 功能组 {"id":"常用功能","functions":["account_sharing"]}
账户管理 批量授权 {"group":"fake-group","members":[{"member":"fake-member","identity_type":"group_member_openid","confirmed":true}],"function_scope":{"kind":"group","id":"常用功能"},"resource_scope":{"kind":"group","id":"演示组"},"mode":"continuous"}
账户管理 批量撤权 {"group":"fake-group","members":[{"member":"fake-member"}],"source":"来源ID"}
账户管理 批量账户 {"operation":"update","ids":["账户ID"],"data":{"enabled":false}}
```

相应授权 API 的 `operation` 为 `function_group/bulk_grant/bulk_revoke`。单人 `grant` 也接受 `function_scope`、`resource_scope` 与 `mode`；默认保持单功能、指定账户和一次授权。功能范围格式分别为 `{kind:"function",id}`、`{kind:"group",id}`、`{kind:"all"}`；资源范围格式为 `{kind:"resources",ids:[...]}`、`{kind:"group",id}`、`{kind:"all"}`。不能混用旧 `resources` 和 `resource_scope`。每个授权有独立来源 ID，撤权指定 `source` 只移除此来源，不提供时撤销该管理功能在本群为该成员创建的所有来源；其他来源继续生效，明确拒绝仍优先。

批量成员操作最多 500 项，返回逐项 `saved/failed` 和成功／失败计数；部分失败须逐项处理，不能假定全部成功。`POST /api/ext/account-sharing/account-batches` 接受 `{operation:"update",ids:[...],data:{...}}` 或 `{operation:"delete",ids:[...]}`，同样返回逐项结果。新增账户及新增授权来源会生成新 ID，重试前先核对已保存项。

通用模块消费方通过 `register_function(bot,function)` 注册功能，以 `register_resources(bot,namespace,[{id,groups,enabled}])` 发布该命名空间的完整资源清单。`decide(...,resource_namespace=...)` 可显式共享同一资源清单，默认命名空间为自身功能；调用资格、群白名单和成员核验始终单独检查。账户共享管理入口固定使用自身账户命名空间。旧单功能／单资源授权按一次来源兼容读取，不扩展为未来范围。

## 范围拒绝、期限与主人解释

拉黑沿用 `scope:"member"` 或 `scope:"group"` 选择主体，另用 `function_scope` 选择单功能、功能授权组或全部功能。功能授权组及全部范围按当前注册集合解释，新增符合范围的功能同样受拒绝约束。每条拒绝有独立 ID；解除指定 `source` 只移除该条，其他适用拒绝继续生效。框架黑名单、模块拒绝始终优先，重新授予权限不解除拉黑。

```text
账户管理 拉黑 {"group":"fake-group","member":"fake-member","scope":"member","function_scope":{"kind":"all"}}
账户管理 解除拉黑 {"group":"fake-group","member":"fake-member","scope":"member","source":"拒绝ID"}
账户管理 期限 {"group":"fake-group","member":"fake-member","source":"授权来源ID","expires_at":2000000000}
账户管理 批量期限 {"group":"fake-group","members":[{"member":"fake-member"}],"expires_at":null}
账户管理 解释 {"group":"fake-group","member":"fake-member","resource":"账户ID","action":"private"}
```

单人及批量授权可设置 `expires_at`，为 Unix 秒数；省略或 `null` 为长期，到达该秒立即到期。页面使用本地时间输入并转换为 Unix 秒。`set_expiry`／`bulk_expiry` 修改已有来源的期限，不创建新来源；未填写 `source` 时处理当前管理功能在此群为该成员保存的全部来源，找不到来源会明确失败。批量返回逐项结果与计数，不能把失败项当成已修改。修改期限不会解除拉黑或重新授予已撤销的来源。

解释 API 的 `action` 默认为 `view`（资源资格），也支持 `invoke`（只检查功能调用）及账户插件的 `private`（同时需要可靠私聊映射）。缺少绑定不影响普通资源资格；私聊解释返回 `qualification_allowed`、`binding` 并可给出 `pending_binding`。解释列出适用拒绝、授权来源及其 `active/expired` 状态、期限、成员状态、判断时间和版本；原因区分框架拒绝、模块拒绝、群未准入、待核验、无调用资格、无资源资格、到期与停用。详情仅供主人及已登录后台，私聊解释同时复用实际发送的接收者框架黑名单检查，不预测平台是否接受发送；普通成员只收到统一拒绝提示。

目录、关键词和旧按钮均实时重算。发送前在读取最新内容并检查私聊目标后再次判断权限和当前时间；到期无需等待后台清理。旧单功能拒绝仍按单功能解释，旧无期限授权保持长期，授权状态格式升级到 v3，随后续保存写回。通用模块另提供同步 `decision_current(bot,decision)`，供消费方在异步准备后、实际派发前检查版本和 `valid_until`；它不能代替重新计算授权。多个仍有效来源按可用的最长持续期限组合，某个冗余短期来源到期不会废除其他有效来源。

## 退群、重入与原生成员周期

授权模块的 `observe(event)` 已实现 `GROUP_MEMBER_ADD/REMOVE` 的状态处理，使用群原生 `group_openid + member_openid`。当前缺少独立于插件、先于查询调度的可靠框架观察接口，尚未完成生产接线；下述行为由模拟事件源验证，不能作为实际安装后的自动撤权承诺。退出事件不使用可能为空的用户 OpenID 或 union 显示身份代替群成员身份。

有可靠时间顺序的退出使成员变为 `left`，撤销此 Bot／此群／此成员的所有功能及资源来源，保存无账密的撤销历史；其他群和其他 Bot 独立，拒绝记录保留。再次加入创建新周期，状态为 `unverified`，不授权、不私聊、不邀请绑定。主人须重新授予当前选定范围，原绑定可以保留，但旧来源和个人按钮不恢复。主人也可在已核对当前在群后显式授权，新授权进入新的周期，不顺带恢复待核验的旧来源。

```text
账户管理 成员 {"group":"fake-group","member":"fake-member"}
```

`POST /access` 的 `operation:"members"` 返回所选群（可省略 member 查看全群已记录主体）的成员周期、核验原因、撤销历史及群事件版本；页面“成员周期与撤销历史”提供同一入口。这仍是历史识别主体，不能证明全群名册完整。解释会显示 `left_group`、`pending_verification` 以及当前周期。

平台时间戳为 Unix 秒，不能证明同秒不同事件的顺序。仅可靠事件 ID 确认的相同事件直接幂等忽略；无 ID 的相同类型/秒数不能证明重复，也不能在重新授权后忽略；未识别的迟到、冲突、缺少时间或缺少原生身份会暂停相关已存在主体，进入 `pending` 等待核验，不猜测恢复。每主体保留最近 128 条去重记录；更旧且无法确认的事件同样保守暂停。新主体尚无授权时可保持未核验。已知变更先同步影响内存资格及版本，再串行保存历史；已经在准备中的菜单和查询不能沿用旧版本发送。框架外尚未送达的事件不在本进程已知状态之内。
