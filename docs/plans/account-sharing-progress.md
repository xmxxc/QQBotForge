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
| 账户组合 | [#33](https://github.com/xmxxc/QQBotForge/issues/33)—[#36](https://github.com/xmxxc/QQBotForge/issues/36) | C01—C04 已实现／验证，draft PR #53—#56 |

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

新规划 18 张子票 #19—#36 均已实现、验证并关闭；父规格 #18 保持打开，待主人最终审核。全部 PR 为 draft：

| 票 | 提交 | PR | 目标 |
| --- | --- | --- | --- |
| A04 #27 | `f26aff0` | #46 | personal |
| Q01 #28 | `6461759`（框架 `bf462cf`） | #48（框架 #47→main，同步 #39） | personal |
| Q02 #29 | `6833097` | #49 | personal |
| Q03 #30 | `e33def9` | #50 | personal |
| Q04 #31 | `00be98b`、绑定回执修复 `387a332` | #51 | personal |
| Q05 #32 | `00e4c8c`、`1e169ff` | #52 | personal |
| C01 #33 | `ad70979`、`2b99c37` | #53 | personal |
| C02 #34 | `6f44837` | #54 | personal |
| C03 #35 | `a0de711`、`1fc289a` | #55 | personal |
| C04 #36 | 见 PR #56 | #56 | personal |

业务分支依次叠加（`feat/access-control-a04` → `feat/self-qa-q01…q05` → `feat/account-sharing-c01…c04`），工作树 `/opt/qqbot/QQBotForge-access-control-a01`。框架改动单独在 `feat/module-handler-hook`（PR #47→main），并合入同步分支 `integrate/bot-modules-framework` `e501c95`（PR #39）。建议合并顺序：#38/#47 → #39 → #37 → #40…#56。

验证组合：框架同步 `e501c95` + 业务 `feat/account-sharing-c04`；`/tmp/qqbotforge-a01-runner` 中 `/opt/qqbot/QQBotForge-account-sharing/.venv/bin/python -m pytest -q`，147 passed（连续三次）。推送使用 SSH 地址 `git@github.com:xmxxc/QQBotForge.git`。

未完成／待主人决定：浏览器视觉核查（会话内无浏览器）、真实 QQ 与平台许可（名册、Markdown／按钮、主动私聊）、运行数据库迁移、合并与部署。已知现象：可信 RESUMED 刚完成时群状态在途保护可能使第一条成员消息暂被拒（约一成），后续正常。旧账户合成观察源测试 93 项已退役，由真实模块测试替代。安装／升级／迁移／排障见 [安装说明](../bot-modules-installation.md)。

## 2026-10-21 审计与集成更新（当前审核依据）

上文为实施时的历史记录，不以旧测试通过数或票据关闭状态证明新组合全部验收。当前远端 personal 固定为 `f797eadcf6196163340e4640de11385433e1ff76`；#37、#38、#39 已由主人合并；#47 对 main 的 PR 仍待审核，personal 中相同 Hook 已补审。

- #47 独立 Standards／Spec 均无本票新增必修，真实 Application 37 场景在 PR 源码、personal、main＋PR47 合并树分别通过。通用 Hook 的异常后继续和在途注销不排空等既有边界明确记录。
- G01 在新 personal 上真实无冲突组合 `b32a00a`：按现有 CI 嵌套复制资产 561 passed／6 路径误判跳过；正常目录资产 567 passed／无跳过，独立安装33通过；旧16个失败全部消失。未扩大 S1—S3 非阻塞建议。
- G02 `95b0505` 虽有153业务测试及绿色CI，独立复审仍确认人工确认旧证明未失效、首次写入排队取消残留及改群往返页面竞态。因此 #22 在该 SHA 的历史完成结论不成立，本段明确更正，不改写旧完成记录或票据时间线。修复 `1adf415` 两轴复核无剩余必修；业务160、独立安装67、正常目录601通过；初始取消、真实SQLite写失败/排空和旧游标失效回归保留。
- G03/A01 人工兼容合并已分别进行两轴复核，保留成员周期、恢复屏障、消费完成和 A01 额外修复；后续各分支按原祖先串行合并，原有额外修复保留。最终 C04 源码组合 `66856c3` 业务193通过；完整 F01 资产 `9790d7d` 正常目录组合910通过、无跳过，Ruff及框架/群状态类型检查通过。
- 同步回归不能代替 G03、A01—A04、Q01—Q05、C01—C04 的逐票独立审计。此前记载的 RESUMED 首条消息偶发保守拒绝现象尚未独立诊断；浏览器、真实QQ/平台许可、生产迁移均未验证。
- 共享CI未修改。现有 `cp -R ci-assets/tests source/tests` 会嵌套到 tests/tests，使六项 SPA 检查找错根目录；已额外用正常目录执行，未删用例、放宽断言或新增跳过。C01原始提交退役93项旧合成账户测试，不属于本轮同步删测，后续票独立验收仍须评估覆盖。

完整提交／PR同步矩阵、分轴报告、精确命令及测试资产来源见[本轮交付报告](https://github.com/xmxxc/QQBotForge/blob/docs/audit-integration-20261021/docs/verification/integration-20261021.md)。本轮不合并、不部署、不触碰真实运行数据，所有业务PR继续保持draft。

发布后 CI 补充：首轮 #46 run `39541560100` 的消费者失败恢复测试曾失败（1 failed／670 passed／6 既有路径误判跳过）。快速轮询临时副本稳定复现：订阅结果已有，公开恢复状态仍 `pending=1/failed=true`，框架正确拒绝尚未完成投递登记时的重建。修正只增加等待 `pending=0` 及 `failed` 断言后执行原重建成功断言，保留全部旧断言，不改产品或跳过设置；`9450db5` 从 G03 串行同步到后续各分支，各批26项生命周期回归通过。首轮失败保留，不以重跑绿色代替原因分析；最新精确交付 SHA 和复验仍以独立报告矩阵为准。

### 群状态收尾（2026-10-21）

旧批准 SHA 与新修复 SHA 分开记录：#40 旧审计 `b32a00a` → 新 `5053608`；#41 旧复审 `1adf415` → 新 `0a34cf2`。新 head 尚未独立审计。

- G01：主人命令回复异常保持已消费、记录错误、不重发也不落入普通插件；setup 注册后失败／取消撤销本候选 Hook、自有路由与页面并关闭候选，不删除他模块或已替换的路由。业务131、正常目录572通过。
- G02：名册各出口不再残留 `roster_refreshing`；未发布候选不再把版本号记为已落库，真实 SQLite 下并发观察写失败正确标 `persistence_failed`；重复／循环／身份矛盾／本 Bot 缺席／页数上限的轮次不授予成员可靠性，单纯中断规则不变。下游受众核验只会减少可用成员，未放宽保证。文档补字段可信度与 G03 依赖，页面状态行标注完整／已核验／进行中／待核验／未知／历史。业务177、正常目录618通过。
- 固定快照 Standards／Spec 两轮：0 必修。
- 同步：G03 手工解决 main/state 冲突并保留 `status=left`；A01 保留可用性广播并在其失败时同样撤销；A02—C04 自动合并无冲突，各批业务回归与 Ruff 通过，业务目录未改。C04 + F01 资产 930 passed／0 skipped。全部远程 CI 成功，PR 保持 draft。
- 待定：真实平台 `/members` 是否含 Bot 自身（否则所有名册判冲突，失败关闭）；逐票独立审计、浏览器、真实 QQ 未做。
