# 三模块与账户共享实施进度

更新：2026-10-20。当前父规格 [#18](https://github.com/xmxxc/QQBotForge/issues/18)，完整[实施清单](bot-modules-tickets.md)。实施已启动；原始工作树未提交规划与框架改动保持原样，业务及框架使用独立工作树。

## 当前基线

- 扩展工作树 `/opt/qqbot/QQBotForge-account-sharing`，分支 `personal`，审计 HEAD 为 `3509cd9`。原有未提交框架、探针、项目配置和指引保留。
- 账户业务自 `61dfff0` 后未变更，包含主人管理、授权／匹配算法、绑定、按钮、群发策略及待办；当前生产成员查询仍默认拒绝。
- 2026-10-20 审计重跑账户业务测试：86 passed，主要使用显式模拟观察源；它不能证明新独立问答、群状态或真实接线已完成。
- 隔离验证：只有问答目录时因导入授权存储而失败；仅补存储文件、不启用授权后可回复静态文字。B01 负责解除安装耦合。
- 群生命周期和连接事实接口已进入本地 main，personal 另含本地恢复机制及后续修复；F01 负责核对精确版本和实际契约，G03 再接入。授权当前 setup 尚未订阅这些接口。

## 交付状态

| 阶段 | 新票 | 当前状态 |
| --- | --- | --- |
| 基础兼容与框架契约 | [#19](https://github.com/xmxxc/QQBotForge/issues/19)、[#20](https://github.com/xmxxc/QQBotForge/issues/20) | 已实现与验证，票据关闭；draft PR #37、#38、#39 保持待审核 |
| 完整群状态 | [#21](https://github.com/xmxxc/QQBotForge/issues/21)—[#23](https://github.com/xmxxc/QQBotForge/issues/23) | G01—G03 已实现／验证，票据关闭；draft PR #40、#41、#42 保持待审核 |
| 通用授权 | [#24](https://github.com/xmxxc/QQBotForge/issues/24)—[#27](https://github.com/xmxxc/QQBotForge/issues/27) | A01 #24 已实现／验证，draft PR #43；A02 #25 已实现／验证，draft PR #44；A03 #26 已实现／验证，draft PR #45；A04 #27 已实现／验证，draft PR #46 |
| 独立问答 | [#28](https://github.com/xmxxc/QQBotForge/issues/28)—[#32](https://github.com/xmxxc/QQBotForge/issues/32) | Q01—Q05 已实现／验证，draft PR #48—#52（框架前置 draft PR #47，已同步 #39） |
| 账户组合 | [#33](https://github.com/xmxxc/QQBotForge/issues/33)—[#36](https://github.com/xmxxc/QQBotForge/issues/36) | 已有业务基础；待接口迁移和完整验收 |

已有实现与新票完成是不同状态。框架提供接口、消费者接入、自动测试和真实平台许可分别报告。当前发布动作及原票关闭读回结果记录在实施清单与 GitHub 时间线。

## 旧票与历史

原 #2、#4—#14、#16、#17 由新父规格及子票承接，按“规划替代”关闭；原 #3 的完成状态保留。新票不继承旧票的完成标记。[对应关系](bot-modules-tickets.md#原票承接)和[旧进度档案](../archive/account-sharing-v1/account-sharing-progress.md)可追溯。历史 PR #15 已关闭，未合并。

## 后续执行

先推进 B01 与 F01，再按群状态、授权、问答、账户顺序交付；可用技术依赖见新清单。每票需具备可演示入口和验收证据才关闭。业务 PR 目标 personal，框架 PR 目标 main。已确认规划保存在本实施分支，GitHub 父规格完整收录目标行为和账户规则；原始工作树仍保留未提交副本。

本轮未执行真实 QQ、浏览器核查、部署或生产数据迁移；现有配置、凭据、数据库、日志及运行实例保持原样。


## 本轮实施记录

- B01 #19：`0585773`，`feat/bot-modules-b01`，draft [PR #37](https://github.com/xmxxc/QQBotForge/pull/37) 目标 personal。业务 93 passed；单目录加载、静态公开回复、旧数据管理／拒绝／重启及写入失败重试通过，Ruff／存储 mypy 通过。[安装与兼容](../bot-modules-installation.md)、[上游固定提交](../research/bot-modules-reuse.md)。
- F01 #20：框架 `460c841`，draft [PR #38](https://github.com/xmxxc/QQBotForge/pull/38) 目标 main。personal 同步 `b23572b`，draft [PR #39](https://github.com/xmxxc/QQBotForge/pull/39)；core/web 与框架提交完全一致。测试资产 `9790d7d` 从 ci-assets 独立提交，发布到 `test/module-contract-f01`。完整框架 712 passed／5 skipped，专项 271 passed／5 skipped，Python 3.13 eager 4 passed。两条生产分支均未合并。规划中的 `docs/module-recovery.md` 等框架文档随同步 PR #39 进入 personal；当前业务分支可先查阅 [F01 固定版本](https://github.com/xmxxc/QQBotForge/blob/460c841/docs/module-recovery.md)。
- G01 #21：`75c76e8`，`feat/group-state-g01`，draft [PR #40](https://github.com/xmxxc/QQBotForge/pull/40)；临时组合框架 `b23572b` 与业务目录，通过独立 ModuleManager、实际 Application 消息入口、鉴权 HTTP 和主人命令验证。G01 业务组合 99 passed；事实历史／刷新错误、Bot 隔离、重载及在途失效均覆盖。双轴审查通过，票据已关闭。
- G02 #22：`4498f0d`，`feat/group-state-g02`，draft [PR #41](https://github.com/xmxxc/QQBotForge/pull/41)；原始分页、角色与本 Bot 身份核验、单人事实和版本分页已完成。组合业务 118 passed，Ruff／四文件 mypy 通过；双轴复核与持久化失败／延迟、并发旧响应的先失败后通过回归均已完成。票据关闭。
- 浏览器尝试：CUA 返回无可用浏览器（apps=[]、browsers=[]），G01 未完成浏览器核查。真实 QQ 平台许可、生产数据迁移及部署均未执行。
- 原始工作树 `/opt/qqbot/QQBotForge-account-sharing` 的已跟踪改动已与起点二进制 diff 备份核对一致；实施文件位于 `/opt/qqbot/QQBotForge-bot-modules`。框架与同步分别位于 `QQBotForge-framework-f01`、`QQBotForge-bot-modules-framework-sync`，测试资产位于 `QQBotForge-f01-test-assets`。

G03 #23：`fe873ac` 及成员周期锚点修正 `0a83634`，draft [PR #42](https://github.com/xmxxc/QQBotForge/pull/42) 目标 personal。实际 WS 接线及消费屏障、异常、重载、SQLite 延迟和原生事件冲突验证通过，组合业务 140 passed；双轴最终无剩余发现。A01 #24：`bfab97f`，draft [PR #43](https://github.com/xmxxc/QQBotForge/pull/43)，目标 personal；完整业务160 passed，授权20项，双轴最终0剩余发现，票据关闭。A02 #25：`57debf6`，draft [PR #44](https://github.com/xmxxc/QQBotForge/pull/44)，目标 personal；完整业务172 passed，范围专项12项，双轴最终0剩余发现，票据关闭。A03 #26：代码 `b8d3297`，draft [PR #45](https://github.com/xmxxc/QQBotForge/pull/45)，目标 personal；完整业务184 passed，绑定专项12项，Ruff／三文件mypy／脚本语法通过，双轴最终0剩余发现，票据关闭。工作树 `/opt/qqbot/QQBotForge-access-control-a01`，分支 `feat/access-control-a03`。优先按群状态 → 授权 → 独立问答 → 账户共享推进；未完成票不继承旧验收状态。所有 PR 保持 draft，合并与部署由主人决定。


## 当前位置

授权 A04 #27：`f26aff0`，PR #46。问答 Q01 #28 `6461759` PR #48；Q02 #29 `6833097` PR #49；Q03 #30 `e33def9` PR #50；Q04 #31 `00be98b` PR #51（含 #26 回执修复 `387a332`）；Q05 #32 `00e4c8c`＋`1e169ff` PR #52。框架前置 `before_handler` Hook `bf462cf`，draft PR #47 目标 main，已合入同步分支 `integrate/bot-modules-framework` `e501c95`（PR #39）。业务分支依次叠加，最新 `feat/self-qa-q05`。新规划 18 票中 #19—#32 共 14 票完成，#33—#36（账户组合）待实施。

验证组合：框架同步 `e501c95` + 业务 `feat/self-qa-q05`，`/tmp/qqbotforge-a01-runner` 指向同步工作树 core/web 和业务工作树 `/opt/qqbot/QQBotForge-access-control-a01` 的 modules/plugins/tests；命令 `/opt/qqbot/QQBotForge-account-sharing/.venv/bin/python -m pytest -q`，213 passed。推送使用 SSH 地址 `git@github.com:xmxxc/QQBotForge.git`。浏览器和真实 QQ／平台许可未验收；未迁移生产数据、合并或部署，全部 PR 保持 draft。

账户迁移须知：旧账户插件多处把 `Sender.reply` 结果当作 `(success, data)` 元组（真实返回平台响应 dict；`send_to_user` 返回 `(ok, data, payload)`），C01—C03 改用新接口时一并修正。授权已提供 `audience_report`／`report_current`、`member_context`／`context_current` 与 `private_contexts`，问答提供 `match(admit=…)`。

下一张：C01 #33（账户插件迁移为组合消费者），之后 C02—C04。
