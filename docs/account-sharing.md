# 账户共享

本功能逐票实施，当前已提供主人账户管理和私聊预览；部署与合并需审核后单独进行。

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
