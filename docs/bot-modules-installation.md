# 三模块与账户共享：安装、升级、迁移与排障

对应父规格 #18 与子票 #19—#36。四个扩展目录分别交付，依赖只能从上往下：

```text
plugins/account_sharing  ──►  modules/self_qa ──(可选，受限规则/委托)──►  modules/access_control  ──►  modules/group_state
         └──────────────────────────────────────────────────────────────►┘
```

群状态只提供事实；授权消费群状态，管理资格、身份绑定和全员报告；问答独立运行，受限规则按需复用授权；账户共享组合问答与授权。下层不导入上层（`tests/bot_modules/test_account_c04.py::test_dependencies_point_one_way` 静态检查）。

## 框架要求

- QQBotForge 框架需包含：本地恢复 v1 与群生命周期／连接事实（F01 `460c841`，PR #38）以及模块 `before_handler` Hook（`bf462cf`，PR #47）。业务验证组合是同步分支 `integrate/bot-modules-framework` `e501c95`（PR #39）。
- 旧框架上：群状态无法建立基线，授权的群资格全部不可用；问答仍可作为匹配接口，但不会自动回复消息。

## 安装（按需选择）

每个目录完整复制（含 `storage.py`、`page.html` 等），在模块管理中启用模块，插件随插件目录加载：

| 想用的功能 | 需要的目录 | 说明 |
| --- | --- | --- |
| 查看群／成员事实 | `modules/group_state/` | 见 [群状态](group-state.md)。首次启用后点“重建恢复基线”。 |
| 通用授权与身份绑定 | `group_state` ＋ `modules/access_control/` | 见 [授权](access-control.md)。公开策略可不依赖群状态。 |
| 公开问答 | `modules/self_qa/` | 见 [自助问答](self-qa.md)。不需要其他目录。 |
| 受限问答／群委托 | `self_qa` ＋ `access_control` ＋ `group_state` | 授权不可用时公开问答照常。 |
| 账户共享 | 以上三个模块 ＋ `plugins/account_sharing/` | 见 [账户共享](account-sharing.md)。 |

## 配置要点

1. 群状态：后台“群状态”选择 Bot，刷新已知群；需要全员群发时对该群执行“刷新完整分页名册”（需平台名册许可）。重启、重载或断线后执行“重建恢复基线”（或私聊 `群状态 恢复`）。
2. 授权：后台“授权管理”。各消费方（如 `self_qa`、`account_sharing`、`reports`）的策略、名单、来源、期限、拒绝分别管理；身份绑定在同页“通用身份绑定”确认。
3. 问答：后台“自助问答”设置启用的群与私聊（默认全部关闭），新增规则；受限规则在授权页对消费方 `self_qa` 授权；按群开放委托。
4. 账户：后台“账户共享”管理账户与关键词规则；“白名单”设置准入群，再对成员明确授权；成员私聊 `身份绑定 申请 <群ID> <成员ID>`，主人在授权页确认；需要时按群／账户开启群发。

## 数据位置

| 扩展 | 数据库（相对各自目录） | 主要集合 |
| --- | --- | --- |
| 群状态 | `data/group-state.db` | 每 Bot 群／成员事实、名册、恢复 |
| 授权 | `data/access-control.db` | `access_consumers`（每消费方一行）、`identity_state`、旧版 `access/state` |
| 问答 | `data/self-qa.db` | `qa_rules:<命名空间>`、`qa_settings` |
| 账户 | `data/account-sharing.db` | `accounts`、`group_delivery`、`operations`、`owners`、`contexts`，以及待迁移的旧身份集合 |

均为 SQLite v1 `extension_records(bot, collection, id, payload)`；高于支持版本的库拒绝打开。

## 存储副本同步（#61）

为保证各目录可单独安装，每个扩展各带一份 `ExtensionStore`，只依赖标准库，不导入其他扩展或公共包：

- `modules/group_state/storage.py`
- `modules/access_control/storage.py`
- `modules/self_qa/storage.py`
- `plugins/account_sharing/storage.py`

这几份不要求字节一致，也不要为消除差异把任何一份换成旧副本。修改共同行为时须同步全部副本，并运行 `tests/bot_modules/test_extension_storage_contract.py`；新增带 `ExtensionStore` 的扩展存储时，须加入该测试与上面的清单（测试会扫描 `modules/*/storage.py`、`plugins/*/storage.py` 并核对本清单）。

共同行为契约（四份均用真实临时 SQLite 参数化验证）：

- 首次使用时才打开，自动创建父目录；schema 为 v1 `extension_records`，主键 `(bot, collection, id)`，`user_version=1`，`journal_mode=WAL`。
- `user_version` 高于 1 的库拒绝打开（`RuntimeError`），不改写该库，重试仍拒绝。
- 记录按 Bot、集合、ID 隔离；`all` 按首次写入顺序返回，`get` 缺失返回 `None`；`put` 为原位更新（不改变顺序），`delete` 只删一条，删除不存在的记录无副作用。
- 每次写入立即提交，另一连接可见；关闭后重开仍在。payload 为 `ensure_ascii=False` 的 JSON。
- 并发写入经同一锁串行，不丢失；`close` 等待正在执行的写入完成后再关闭连接，之后任何读写都报“扩展存储已关闭”；`close` 可重复调用。

有意差异（测试只约束列出的副本）：

| 行为 | 群状态 | 授权 | 问答 | 账户 |
| --- | --- | --- | --- | --- |
| `keys(bot, collection)` 按写入顺序返回 ID | — | 有 | — | — |
| `get(..., missing=默认值)` 缺失时返回指定值 | — | 有 | — | — |
| 调用方被取消（含重复取消）时先排空在途 SQLite 写入再传播取消，取消的写入不会之后在后台提交 | 有 | 有 | 未承诺 | 未承诺 |

问答与账户目前直接 `await asyncio.to_thread(...)`：调用方被取消时取消立即传播，已交给存储线程的写入仍可能随后提交。是否为它们引入排空语义需另行确认规格，不随本清单强加。新增或取消差异时须同时更新本表与测试中的 `EXTRAS`／`GET_MISSING_KEYWORD`／`DRAINS_CANCELLED_WRITES`。

## 升级与迁移（由主人另行安排）

1. 停用扩展并确认连接关闭，用 SQLite 备份或停机一致性副本保存四个库（不要丢弃运行中的 WAL／SHM）。
2. 先在临时副本上演练：替换目录 → 启用模块 → 群状态重建基线 → 账户 `账户管理 迁移`（或 `POST /api/ext/account-sharing/migration`）。
3. 迁移结果：账户、规则、操作记录原地保留；旧授权来源（ID、期限、仍存在的账户、模式）、拒绝与撤销历史导入授权消费方 `account_sharing`，来源绑定 `legacy:<旧周期>` 不生效；旧白名单群只在新策略未设群时作为策略群；旧插件身份绑定与申请导入授权模块。已撤销来源不会恢复，重复执行按 ID 跳过。
   - 迁移前私聊过 Bot 的身份（含主人发送 `账户管理 迁移` 这一条私聊本身）已被授权模块记录为 `observed_c2c`。同一 Bot、同一私聊原生 ID 的旧记录视为同一观察：保留新记录，旧 `union_ids`／`current_union_id` 只留在旧库作来源资料，不改写新记录，也不据此推断绑定。新模块已有同群同成员、同私聊身份的绑定时保留新绑定（含主人新确认的 ID 与依据）；同 ID 申请保留新状态（已确认／拒绝／撤销不被旧记录覆盖）。
   - 仍拒绝（整个身份阶段不写入）：同群同成员已关联另一私聊身份、同群同一私聊身份关联多个成员、旧私聊身份无原观察来源、绑定缺少已观察私聊身份、已撤销且当前无同一私聊身份绑定的关联、同 ID 申请指向不同身份。主人撤销后又重新确认同一成员与同一私聊身份时，旧记录与当前绑定兼容并保留当前绑定。旧库中被撤销的关联会让此后每次迁移的身份阶段都报告失败（不恢复，须新申请并由主人核实），这是预期的拒绝而非故障。授权阶段与身份阶段相互独立，一方失败不回滚另一方，结果分别报告。
   - 结果：每个阶段带 `status`——授权 `imported`／`unchanged`／`failed`，身份 `imported`／`unchanged`／`not_run`（旧库无身份记录）／`failed`，规则检查 `checked`／`failed`；失败附 `error`。身份阶段另含 `imported`／`existing` 分类计数。顶层 `status` 为 `imported`／`unchanged`／`partial`／`failed`。主人操作记录写入真实的 `authorization`、`binding`、`rules`、`migration`（读取操作记录的调用者注意：`binding` 现可为 `imported`／`unchanged`／`not_run`／`failed`／`cancelled`，`rules`、`migration` 为新增字段），通知不再固定显示 imported。后台接口全部导入阶段失败时返回 422（`success:false`，`data` 为同一报告），其余返回 200。原有字段（授权计数、身份 `bindings`／`requests` 总数、规则 `total`／`dangling`）保持不变，旧调用者只需改为先看 `status`。
   - 取消：顶层为 `cancelled`（进行中阶段 `cancelled`、未开始阶段 `not_run`），取消始终原样传播。无论取消发生在阶段中还是阶段结束后写记录时，都先在独立任务中完成操作记录再传播取消；每段等待 10 秒，超时则取消写入任务再等一段，后续取消不替换原取消。写入失败时不写记录，只记脱敏错误日志（操作 ID、异常类型、各阶段状态）；等待超时日志记“结果未知”，已交给存储线程的写入之后仍可能落盘（内容为当时的阶段与总体状态）。通知留待“重试通知”。可直接重新迁移，已提交的授权按 ID 跳过并报告 unchanged。
   - 畸形旧授权：旧授权记录或其 function_scope／resource_scope／resources／functions（含 resource_scope.ids）结构不符时报告 failed（旧授权记录结构无效）；旧白名单键等由共享加载失败的报告 failed（旧资料结构无效）。身份阶段照常独立执行，可形成 partial。
4. 逐人恢复：在群状态核验成员当前在群后，`账户管理 恢复来源 {"group":"…","member":"…"}`（待办中列出 `legacy_adoption`）。到期来源恢复后仍到期，拒绝仍生效。
5. 核对无误后再在运行实例执行；保留原目录与备份供回滚。回滚不会把旧“在群”记录当作当前资格。

## 故障排查

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| 成员一直“无权限” | 群不在账户白名单；只有角色／群策略而无明确授权；成员未在群状态核验；恢复基线未建立 | 授权页“解释资格”；群状态核验成员／重建基线 |
| 群里查询只提示私发 | 未开启该群／账户群发；全员报告不合格（名册不完整、Bot 无授权、新人） | 主人私聊 `账户管理 群发 {…}` 查看逐人原因 |
| 私发失败 | 未确认身份绑定；平台主动消息限制（返回码已记录） | 成员申请身份绑定；按返回码处理平台许可 |
| 问答不回复 | 群／私聊未启用；规则停用；框架黑名单、维护或非@设置；旧框架缺 `before_handler` | 问答“测试匹配”；检查框架设置与版本 |
| 受限问答仅提示 | 授权不可用、全员不可见或提问者无权 | 授权页对 `self_qa` 授权；需要私发时完成身份绑定 |
| 刚断线恢复后第一条消息被拒 | 群状态在途保护的保守行为 | 稍后重试；已在 C03 记录 |
| 页面或接口 503 | 对应存储不可用或消费方行损坏（只隔离该范围） | 查看日志，修复后重载模块 |

## 验证状态（截至本次交付）

- 自动测试：`cd /tmp/qqbotforge-a01-runner && .venv/bin/python -m pytest -q`（core/web 指向 `e501c95`，modules/plugins/tests 指向业务分支），147 passed，连续三次通过。使用真实 ModuleManager、PluginManager、Application、WSClient 原始帧、鉴权 HTTP 与主人命令，临时 SQLite、假身份与平台替身。旧 `tests/account_sharing` 合成观察源套件已退役。
- 浏览器：会话内无可用浏览器，所有页面只做了脚本语法检查，未做视觉核查。
- 真实平台：未连接真实 QQ，未核查名册接口许可、Markdown／按钮许可、主动私聊配额。
- 生产数据：未读取或迁移运行数据库；未合并、未部署。
- 日志：框架消息日志可能记录回复正文（含私发账密）。
