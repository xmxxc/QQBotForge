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

本机 Python 3.13：test_ninja3 15 passed；tests/bot_modules 417 passed（基线 402 + 新增 15）。

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
