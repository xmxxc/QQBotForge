# #87 群状态恢复行一致性、原因映射与测试证据：作者自查记录

作者自查，待独立复核；本文件是待发布的 PR 记录工件，尚未发布到 PR／Issue。基线 personal `10254b110102e2e88cc4435fe7bacd84a76f10e7`（已含 #84 合入的 #62 交付，未改动已审 #84 候选）。全部数据为临时 SQLite、假 Bot／群／成员和假平台传输；未读取生产数据，未连接真实 QQ 或游戏平台，未运行 `--check`，未改生产配置。

## 改动

- R1／R2 `modules/group_state/state.py`：快照 `recovery` 新增只读 `pause`（`_pause_reason`，即事实凭据同源的真实判定）与 `rebuilding`（本 Bot 重建锁是否被持有）。`ready`／`phase`／`reason` 与 `_ready`、`_pause_reason`、`rebuild`、事实凭据均未改，恢复屏障不变；新增字段为增量，旧响应缺字段时按 `phase` 回退。
- R1／R2 `modules/group_state/text.py` 与 `page.html`：`recovery_line`／`recoveryLine` 同一规则：重建进行中为“正在重建”；`pause` 优先于 `phase`；未就绪时框架的 `baseline_established`／`replay_processed` 不作原因，改显示模块判定；重建结束未建立基线时框架保留的 `rebuilding` 显示为“上次重建未完成”。
- R3：原因表补 `baseline_required`、`pending_processing`、`replay_processed`、`unverified_transport`、`dependency_not_ready`、`busy`、`unsubscribed`，两份表仍由既有测试比对相等；未知原码保留“其他原因（原码）”。
- R4 `docs/verification/issue-62/README.md`：更正“基线源码的失败均为断言失败”为 15 例 `ModuleNotFoundError`／18 例行为断言／1 例通过、页面新 4 例失败，原表与原日志出处保留。
- R5 `tests/bot_modules/test_group_state.py::test_malformed_platform_role_is_a_historical_error`：以包装 `refresh` 取得其真实返回值，HTTP 响应须等于该返回值；返回值再与公开快照和持久化记录比对；负向对照为先前可靠刷新结果与当前事实可区分（`reliable`、版本）。原有断言保留。

## 测试

- 共享用例 `tests/bot_modules/group_state_recovery.json`（15 例，含消费通知 busy、写入未完成、重建中、重建未完成、框架原因、未知原码、旧响应回退），Python `test_recovery_line_follows_the_shared_cases` 与页面 `the recovery line names the same next step as the QQ reply` 同时读取。
- 真实入口（临时应用、假平台）：`test_busy_consumer_notification_reads_as_processing_in_qq_and_api`（可控消费回调阻塞）、`test_unfinished_write_reads_as_processing_in_qq_and_api`（可控存储写入阻塞）、`test_rebuild_progress_does_not_outlive_the_rebuild`（可控平台查询阻塞、重建失败后再成功）。各自断言 HTTP 快照 `recovery` 等于共享用例、QQ 恢复行等于共享期望、群事实不显示可靠且凭据不可用；忙碌下另以契约正则检查（未就绪、给出下一步、无原码下划线），不依赖可变原因的确切文字。
- `test_framework_recovery_reasons_have_chinese_text`、`test_unknown_reason_falls_back_and_never_reads_as_ready`（负向对照：未知原码与已建立基线原因不显示为就绪）。
- 页面假 DOM／假数据：`a busy notification or unfinished write never reads as ready or as a usable group`。

## 红绿

| 检查 | 基线行为 + 新测试 | 本分支 |
| --- | --- | --- |
| `test_group_state_text.py` + `test_group_state.py` | 23 失败、72 通过 | 95 通过 |
| `test_group_state_page.cjs` | 2 失败（新增两例），17 通过 | 19 通过 |

红证据在本工作树内将三个源文件的行为改动临时还原为基线后运行（保留未被调用的新辅助函数），随后恢复，未替换断言。23 例失败中无导入错误（基线已有 `text.py`）：共享恢复用例 11 例、框架原因中文 7 例、未知回退 1 例、三个真实入口用例、以及由 pytest 调起页面脚本的 `test_group_state_page_request_ordering`。R5 用例在基线上通过：它是测试有效性修正，不是行为修复，不计作行为红证据。

全部 pytest 由总控外置 harness（`verify_candidate.py --node`）在临时副本中对已提交 HEAD 运行，结果见本轮执行报告。

## 限制

- 浏览器与真人验证本轮禁止，未做；仅 Node 假 DOM／假数据。真实 QQ 客户端显示未实测。
- 未重跑 #62 基线红日志，R4 数字依据 #84 独立审查记录。
- 不声称解决 #67 中未定位的偶发问题。
- 机器接口仅增量字段；群内简短回复、群内详情边界与既有可靠性判断未改。
