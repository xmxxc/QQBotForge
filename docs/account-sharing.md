# 账户共享

本功能逐票实施，当前已提供主人账户管理、授权目录、身份绑定与成员私聊查询；部署与合并需审核后单独进行。

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

授权模块的公共消费接口是 `execute(bot, operation, function=..., group=..., member=..., ...)` 和 `decide(bot, function, group, member, resource, action='view', framework_allowed=True)`。消费方必须传入框架拒绝结果，不得用模块放行覆盖框架拒绝；返回允许状态、原因、来源、成员核验状态和版本，不保存或发送账户密码。`qq_event` 通用观察 Hook 位于 Bot 事件入口，在 union 显示身份转换和插件过滤前执行。


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
