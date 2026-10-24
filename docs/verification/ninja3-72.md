# #72 忍者必须死3 作者自查证据索引

作者自查，等待独立增量审查；不是独立审计结论。基线 personal `1e2e66e0136b9166e614ab48f961b187520cbd82`，main `9adc52d96f48aabb51a939af7aca8fc4bf096e07`。全部使用假身份、假账密、临时 SQLite 与假 QQ 发送端；未连接真实 QQ／游戏平台，未运行 #70 `--check`，未读取生产数据。

## 复现命令

```bash
python -m pytest -q tests/bot_modules/test_ninja3.py   # 15 项
python -m pytest -q tests/bot_modules                  # 业务全量
ruff check plugins/ninja3 tests/bot_modules/test_ninja3.py
ruff format --check plugins/ninja3 tests/bot_modules/test_ninja3.py
sed -n '/<script>/,/<\/script>/p' plugins/ninja3/page.html | sed '1s/.*<script>//;$s/<\/script>.*//' | node --check -
```

本机 Python 3.13：第 1 轮 test_ninja3 15 passed；第 2 轮最终 21 passed；tests/bot_modules 全量见第 2 轮末尾。

## 红绿

- 基线无插件：fixture 复制 `plugins/ninja3` 失败（FileNotFoundError），全部用例红。
- 变异（每次只删一处守卫，跑 test_ninja3，随后还原）：

| 删除的守卫 | 失败用例 |
| --- | --- |
| 发送前最终同步检查 | `test_changes_while_query_prepares_never_send_old_content` 全部 8 个参数（改密、停用、关群、撤权、拒绝、离群、授权模块重载、插件重载） |
| 授权仅限主人 | `test_grant_requires_owner_native_single_mention_and_never_crosses_bot_or_group` |
| 只接受唯一 @ 目标 | 同上 |
| 调用决定须为明确授权（群准入策略不替代） | 同上 |
| 恢复屏障返回待处理 | `test_unready_recovery_returns_pending_without_confirming_or_granting` |
| 十分钟通知合并 | `test_unauthorized_attempts_notify_owner_once_per_ten_minutes_and_keep_backend_todo` |
| 资源分类不写入 QQ 群 ID | `test_owner_mention_grant_covers_future_accounts_and_member_queries_publicly` |
| 本插件开放群检查 | `test_denial_wins_and_disable_close_rescope_delete_take_effect` |

- 作者审查修复（未授权先判资格再读账号；通知回复按实际送达区分；频道私信不误称已登记）：新断言在 51f765d 的 service.py 上 3 项失败，修复后通过。

## 继承问题（基线同样存在）

测试 fixture 的假 `log_service` 缺少 `wakeup_update`／`db_fetch_one`，C2C 与 Bot 入群事件会在日志里打印 AttributeError；既有 account/access 测试同样出现，不影响断言。

## 第 2 轮：总控配置权边界（基于 c712ca1）

要求见 #72 总控评论（查询只读、主人收窄保留、两边同时放行、通知例外）。

- 旧新探针（未提交的探针用例：授权管理收窄 → 插件重载 → 已授权成员查询）：
  - 旧 c712ca1：查询把群范围写回 `['group-a']`（授权版本 5→6），并在群内发出账密。失败。
  - 新：回复“本群未开放账号查询”，范围仍为 `[]`，版本不变。通过。
- 新用例在 c712ca1 的 service.py 上失败 2 项：`test_backend_crud_defaults_closed_and_isolated_per_bot`（保存结果需返回同步记录）和 `test_member_queries_never_write_authorization_and_owner_narrowing_survives_reloads`。新代码通过。
- 新增或扩展的用例：
  - `test_member_queries_never_write_authorization_and_owner_narrowing_survives_reloads`：未授权和已授权查询都不改授权版本；经已鉴权“授权管理”接口收窄后，查询、插件重载、授权模块重载都不写回；账号保存不扩大群范围；再次保存群只同步该群，并保留私聊开关、拒绝、来源。
  - `test_both_plugin_scope_and_authorization_must_pass`：授权管理放行但插件关闭的群不发账密。
  - `test_notices_skip_explicit_restrictions_but_record_ordinary_unauthorized_and_unverified`：一般无授权和待核验会通知；明确拒绝、未开放群、框架黑名单不通知。
  - 变化参数新增 `narrow_acl`：查询准备期间收窄授权范围时不发旧内容。
- 作者自查（cb494a8 快照，两轴）后的修正：
  - High：保存任何配置都会用全量账号清单重新声明资源，把主人在授权管理停用的账号重新启用。改为只同步本次保存的账号（`test_owner_resource_narrowing_in_authorization_survives_other_plugin_saves`）。
  - Medium：群同步先读取授权快照，与写入之间有等待，存在丢失更新的风险。改为先读插件记录、最后读取快照再写入（`test_group_sync_reads_authorization_last_so_concurrent_owner_edits_are_kept`）。
  - Low：群状态处理中（可重试）不再通知；恢复基线未完成时回复“暂时无法确认查询资格”（扩展了 `test_unready_recovery_returns_pending_without_confirming_or_granting`）。
  - 新断言在 cb494a8 上失败 3 项，修复后通过。
- 最终变异矩阵（每次删或改一处守卫，跑 21 项）：

| 守卫 | 失败用例 |
| --- | --- |
| 最终同步检查 | change 用例全部 9 个参数 |
| 授权仅限主人／唯一 @／明确授权 | `test_grant_requires_owner_native_single_mention_and_never_crosses_bot_or_group` |
| 恢复屏障 | `test_unready_recovery_returns_pending_without_confirming_or_granting` |
| 十分钟合并 | `test_unauthorized_attempts_notify_owner_once_per_ten_minutes_and_keep_backend_todo` |
| 资源分类不写 QQ 群 ID | `test_owner_mention_grant_covers_future_accounts_and_member_queries_publicly` |
| 插件开放群检查 | `test_both_plugin_scope_and_authorization_must_pass` |
| 查询路径不同步授权 | `test_member_queries_never_write_authorization_and_owner_narrowing_survives_reloads` |
| 授权范围外群不通知 | 同上 |
| 同步保留既有策略字段 | 同上，以及并发用例 |
| 账号同步只改单项资源 | 资源收窄用例，以及停用／删除用例 |
| 群同步最后读取快照 | 并发用例 |
| 可重试不通知 | 恢复屏障用例 |
| 不可靠事实的如实回复 | 恢复屏障用例 |

| 账号保存仅在启用状态变化时同步（改密不恢复授权管理停用） | 资源收窄用例 |

  共 16 处变异，每处都有用例失败。

- 最终快照（9b82711）再审后的修正：Medium，同一账号只改密码也会把授权管理停用的账号重新启用。改为只在新建、删除或启用状态改变时同步，页面显示同步结果。新断言在 9b82711 上失败，修复后通过。
- 已知但不修：
  - 授权模块不可用期间删除的账号会留在授权模块的资源清单里（插件不再查询它）。
  - 群状态长时间停在处理中时只回复“请稍后重试”，不通知主人。处理中状态的模拟方式：临时把 `group_state.pending_writes` 加一，模拟群状态写入在途。

- 假日志服务的 AttributeError 发生在框架用户追踪和日志副任务（`_track_worker`、`db_fetch_one`）中，不在被测入口内。每个入口用例都断言了插件的确切群回复或私聊推送（例如 `忍3` 的“已记录本私聊为主人通知地址”），所以入口没执行就会失败。框架黑名单用例断言回复只有框架模板（内容为 None），插件没有执行。

最终：test_ninja3 21 passed；tests/bot_modules 423 passed（基线 402 + 新增 21）。

## 独立审查 F1 修复（基于 bcf2657）

- **根因**：`plugins/ninja3/page.html` 的“新增”按钮 `id="reset"` 位于 `<form id="account">` 内。表单的具名控件会覆盖 `HTMLFormElement` 的同名成员（[LegacyOverrideBuiltIns]），于是 `$('account').reset` 返回这个按钮，`reset()` 抛出 TypeError。后果：
  - 保存、删除在服务端已完成，页面却报错、不刷新。
  - 编辑 A 后点“新增”清不掉隐藏 id，保存 B 时会以 PATCH 覆盖 A。
- **修法**：只把按钮改名为 `id="account-new"`，并修改唯一的引用 `$('account-new').onclick`。其余按钮按文字或表单提交定位，群表单内没有同类冲突。后端和其他行为不变。
- **仓库回归** `tests/bot_modules/test_ninja3_page.py`：
  - `test_no_form_control_shadows_a_form_method`：静态解析页面，表单内控件的 id／name 不得为 `reset`、`submit`、`requestSubmit`、`checkValidity`、`reportValidity`。不依赖浏览器，CI 中也会运行。
  - `test_account_form_new_edit_delete_and_rejection_in_a_real_browser`：真实 Chrome（playwright）驱动随附页面，`/api/ext/*` 走真实 `handle_ext_route`、真实插件、临时 SQLite，全部是假账密。覆盖以下流程：
    - 新建 A，列表刷新，状态显示本次 `acl_sync`。
    - 编辑 A → 新增 → 填 B → 保存：生成第二个 ID，A 的名称、登录名、密码都不变。
    - 编辑只改指定记录，新增不遗留旧 ID。
    - 删除后表单与列表一致，之后还能正常新增。
    - 后端以重名拒绝时，页面显示错误，服务端数据和表单都保持原样。
    - 全程没有页面异常。
  - 作者增量自查补强：每次保存先清空测试页的旧状态，再等待本次 POST／PATCH 响应与页面完成刷新，逐次比对响应中的 `acl_sync` 和页面提示；新增与删除后显式断言隐藏 id 为空，删除前先进入该记录的编辑状态。
  - 没有 playwright 或 Chrome 时，该用例以中文原因跳过。当前 CI 镜像没有 playwright，所以 CI 不运行浏览器流程，须在有浏览器的环境复核。
- **红绿**：
  - bcf2657 的页面：两项都失败，浏览器用例停在“页面未显示‘账号已保存’：状态='$(...).reset is not a function'”。
  - 修复后：两项都通过。
  - 审查者的覆盖探针：旧页面最终只有 1 个账号（A 被改名为 B），新页面有 2 个账号、无页面错误。审查者的整页探针按旧 id `#reset` 点击，在新页面上找不到该元素，这是探针写死了旧 id，不是产品回归。
- **检查**：
  - ruff check／format 通过。
  - mypy（显式三个插件模块，配置同审查者）通过，0 问题。
  - 平铺 ci-assets `04d901726cc6d94bae1b2b5524521f997a326e10` 的完整测试，在带 playwright 的环境中 866 passed，0 skipped。
- **保留**：F2–F7（P3）未在本轮处理。

## 发送准备边界与目录读取（#74 审查 F1/F2 基础层，2026-10-24）

来源：#74 独立审查 F1（真实 `before_send` 准备之后缺少最终复查）、F2（目录解码完整凭据）。旧文字查询 `账号列表`／`账号 名称` 在本层闭环；菜单、共享选择和降级在 #74 分支合入本层后处理。作者自查，未独立复核。

- **基线**：#72 `129df14178d1f551b06bc131a73aacde74c30255`（tree `10a5d691f06e396c78e833bc3f889fb120897709`）。
- **F1**：插件实例加载时经公开 `app.hook_manager` 以 `math.inf` 优先级注册 `before_send` 回调；敏感回复经 contextvar 携带同步检查，在框架管道末尾、首次 `post_json` 前再执行，变化即拦截并改发不含账号内容的提示。发送前用公开 `list_hooks()` 确认本实例回调是最后一项，否则拒发。检查出错按变化处理，结果未知不重发。
- **F2**：`ExtensionStore.project` 用 SQLite JSON 投影只返回 `id/name/enabled/open_groups`；`账号 名称` 在调用和查看决定都通过后，才 `get` 该一个账号的完整记录。
- **红绿**：`tests/bot_modules/test_ninja3_send_gate.py` 用真实 MessageSender 与 HookManager，仅替换平台 `post_json`，在另一个 `before_send` 准备等待期间注入改密、停用、关群、授权管理收窄、撤权、拒绝、离群、授权模块重载、插件重载。放在 `129df14` 上：21 failed／2 passed（仅两项无变化对照通过）；本层：23 passed。
- **框架缺口（未修复，须独立 main 修复与审查）**：`_HttpMixin._request` 在管道之后仍可能 await `get_token()` 刷新令牌，再发出首个请求；这段窗口内的撤权，任何 `before_send` 回调都看不到。可执行反例在交付证据 `test_gap_token_refresh.py`（缺口存在时通过）。最小接口建议：`_send_with_error_handling` 在 `before_send` 管道之前先完成 `_ensure_client()` 与 `get_token()`，或在 `_request` 首次 `client.request` 前同步调用一个 `before_transport` 检查（仅首发，不含重试）。
