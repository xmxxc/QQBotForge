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

  共 15 处变异，每处都有用例失败。处理中状态的模拟方式：临时把 `group_state.pending_writes` 加一，模拟群状态写入在途。

- 假日志服务的 AttributeError 发生在框架用户追踪和日志副任务（`_track_worker`、`db_fetch_one`）中，不在被测入口内。每个入口用例都断言了插件的确切群回复或私聊推送（例如 `忍3` 的“已记录本私聊为主人通知地址”），所以入口没执行就会失败。框架黑名单用例断言回复只有框架模板（内容为 None），插件没有执行。

最终：test_ninja3 21 passed；tests/bot_modules 423 passed（基线 402 + 新增 21）。
