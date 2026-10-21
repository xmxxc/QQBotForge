# QQBotForge 审计与集成交付（2026-10-21 UTC）

当前代码已形成独立、可审核候选；不合并、不部署、不读取真实凭据或运行数据。父规格 [#18](https://github.com/xmxxc/QQBotForge/issues/18)。所有测试使用临时数据库、假身份、平台／传输替身，真实 Application、ModuleManager、HTTP 与 WS 接线按测试范围执行。

## 当前结论

- **#47**：独立 Standards 0 必修、Spec 0 必修。head `bf462cf97bd61049c44ae53ac207f4f22bf32f34`；37个真实消息入口场景在原head、最新personal及main＋PR47合并树分别通过。personal中的两个补丁文件逐字节相同。
- **G02**：原head `95b05058f323caaf5ff83f29a460cb2823c438a5` 两轴确认人工确认旧证明、初始写入取消与改群往返竞态三项缺口。已关闭#22在该SHA的完成结论不成立；保留历史记录，修复后两轴0剩余必修。修复commit `1adf415995c21da0aa40c7748ba41bf9f40a6e05`。
- **G01**：保留第五轮无必须修复结论，仅复核新personal组合；旧16个框架接线失败全部消失。S1—S3未扩大处理，没有迁移命令到before_handler。
- 全链原始分支head均为新候选祖先；每一后继也包含前一新候选。无改写共享历史、目录覆盖或未解决冲突。后续业务原有额外修复保留。
- **G03及A/Q/C各票不因此获得独立审计通过结论**：本轮只复核同步兼容与相关回归。既有进度记载的 RESUMED 首条消息偶发保守拒绝尚未独立诊断。

## 固定基线与审计快照

| 输入 | 完整SHA |
|---|---|
| personal（未修改） | `f797eadcf6196163340e4640de11385433e1ff76` |
| main（未修改） | `dfcf5a1948bd646a855179e5ce70e74c321d6dd3` |
| PR47 head | `bf462cf97bd61049c44ae53ac207f4f22bf32f34` |
| main＋PR47无冲突tree | `9b4ccf5540a9847c8c0e06ea68fdf348c169b6b6` |
| G01原head | `09feaaa91c278fd126c9d570f9975e1df9bdeb73` |
| G02原head | `95b05058f323caaf5ff83f29a460cb2823c438a5` |
| CI现有资产 | `04d901726cc6d94bae1b2b5524521f997a326e10` |
| F01完整专项资产 | `9790d7df6a55495036e269f018ebd7d61e3fc446` |

PR API base.sha曾停留旧值，实际以fresh fetch/ls-remote固定分支和merge-base。G02快照元数据捕获脚本曾短暂回写旧base，已重新固定后交给两轴；源码未改变。A的232文件、B的299文件在审计结束哈希一致。各次修复与兼容复核另有固定diff、scope和文件hash，见证据目录；变化后的最终文档增补另记hash。

## Standards

[A原审查](integration-20261021/A-standards.md)：0硬性问题。[B原审查](integration-20261021/B-standards.md)：3项公开文档契约差异、1项非阻塞 possible Duplicated Code；保留判断性质，不为气味扩大重构。

[B修复复核](integration-20261021/B-fix-standards.md)、[G03兼容](integration-20261021/G03-compat-standards.md)、[A01兼容](integration-20261021/A01-compat-standards.md)分别0新增必修。

## Spec

[A原审查](integration-20261021/A-spec.md)：0本票新增必修。before_event早于消息日志/准入；before_handler在黑名单、维护、non_at、skip_at_other及拦截器后、普通handler前。Event继续、None消费；异常记录后继续、取消传播。模块重复enable/reload/disable注册数1/1/1/0/1，跨Bot按event.appid。显式重复注册、在途注销不排空、异常后可能继续回复是既有通用规则，不误归新增。

[B原审查](integration-20261021/B-spec.md)：P1×1、P2×2；[完整#22验收及重试分析](integration-20261021/B-evidence.md)。原三项旧审计的卸载迟到IO、作用域cursor、大部分页面顺序保护有效；新增人工确认路径仍漏先失效，初始_save迁出try是G01保护迁移回归，A→B→A是页面补丁遗漏。

[B最终修复](integration-20261021/B-fix-spec.md)：0剩余必修。人工确认先递增群版本、撤销名册/旧cursor及目标可靠性，候选成功后检查active/version再发布；失败/取消保留不可信历史。初始_save进入try，清理检查版本归属，不覆盖较新观察。group input推进请求序号并清结果/分页。真实SQLite新增7回归、Node13项全通过；独立Python43+3、Node12全通过。

**局部可重试的保证**：候选成功落库前不发布；失败只退休本次携带且仍属于本次版本的事实，其他Bot及已持久化无关群不永久隔离。SQLite串行，pending_writes按Bot暂时拒绝该Bot分页。取消排空实际线程后释放写锁；磁盘可能已有候选，内存仍不可信，重载新generation/process_restart撤销信任。修复故障后须主动重新核验、成功提交才恢复，单人重试不重建整群完整。队列取消为*_cancelled，首次写入已进入SQLite则保守persistence_failed；两者旧证明均持续失效。没有放宽授权保证，也没有仅重读磁盘恢复资格。

[G03](integration-20261021/G03-compat-spec.md)和[A01](integration-20261021/A01-compat-spec.md)兼容0新增必修：在detached apply内_merge_member保留成员cycle/历史，成功持久化后等待_notify，返回前再次检查版本/恢复就绪；aclose同时撤销通知/恢复/连接订阅后异步关闭。A01的confirmation_revision、cycle_uncertain/continuity_unknown、rebuild revision及available广播保留。

## 同步矩阵与合并顺序

表内所有业务PR均未合并、保持draft；前置修复全部已纳入候选。下一步均为逐票人工审核；G03/A/Q/C仍需独立审计。完整原head/新head/祖先与独有提交在[同步矩阵JSON](integration-20261021/sync-matrix.json)，不是仅凭log顺序推断。

|票/PR|新交付完整SHA|回归结果|审计状态|
|---|---|---|---|
|G01 [#21](https://github.com/xmxxc/QQBotForge/issues/21) / [PR#40](https://github.com/xmxxc/QQBotForge/pull/40)|`b32a00a78cd48baa343b3d37fc8bf4fee0d7bcf0`|567正常目录；561+6嵌套；独立33|独立审计/新组合复核|
|G02 [#22](https://github.com/xmxxc/QQBotForge/issues/22) / [PR#41](https://github.com/xmxxc/QQBotForge/pull/41)|`1adf415995c21da0aa40c7748ba41bf9f40a6e05`|业务160；601正常目录；595+6嵌套；独立67|独立审计/新组合复核|
|G03 [#23](https://github.com/xmxxc/QQBotForge/issues/23) / [PR#42](https://github.com/xmxxc/QQBotForge/pull/42)|`506f1d8ab6fcb009706e6a63d3fd992815323fd2`|业务182；最终群相关93（含4类消费等待）|同步兼容，逐票独立审计保留|
|A01 [#24](https://github.com/xmxxc/QQBotForge/issues/24) / [PR#43](https://github.com/xmxxc/QQBotForge/pull/43)|`82f23c4ba58011154638ceb1bb040d195bcb0207`|业务206|同步兼容，逐票独立审计保留|
|A02 [#25](https://github.com/xmxxc/QQBotForge/issues/25) / [PR#44](https://github.com/xmxxc/QQBotForge/pull/44)|`9dbd8ddc918d67d37148a50ab7679821873d4007`|218 passed in 18.07s|同步兼容，逐票独立审计保留|
|A03 [#26](https://github.com/xmxxc/QQBotForge/issues/26) / [PR#45](https://github.com/xmxxc/QQBotForge/pull/45)|`232a4ade4ea29eee6cddbc0069b0d4bf80dcdd9d`|230 passed in 17.89s|同步兼容，逐票独立审计保留|
|A04 [#27](https://github.com/xmxxc/QQBotForge/issues/27) / [PR#46](https://github.com/xmxxc/QQBotForge/pull/46)|`98827e71e44e459edd35244e6ecc4083c2e58b1c`|236 passed in 17.94s|同步兼容，逐票独立审计保留|
|Q01 [#28](https://github.com/xmxxc/QQBotForge/issues/28) / [PR#48](https://github.com/xmxxc/QQBotForge/pull/48)|`3a716c64c498d5ad9080d3d99344eb9b8ceb83ab`|241 passed in 18.63s|同步兼容，逐票独立审计保留|
|Q02 [#29](https://github.com/xmxxc/QQBotForge/issues/29) / [PR#49](https://github.com/xmxxc/QQBotForge/pull/49)|`706cd37f1a7c41cf51fb1aa010a5f94ee585d4d5`|246 passed in 19.41s|同步兼容，逐票独立审计保留|
|Q03 [#30](https://github.com/xmxxc/QQBotForge/issues/30) / [PR#50](https://github.com/xmxxc/QQBotForge/pull/50)|`94b5c1f89665f280a69f95a92033c11df63051be`|250 passed in 19.92s|同步兼容，逐票独立审计保留|
|Q04 [#31](https://github.com/xmxxc/QQBotForge/issues/31) / [PR#51](https://github.com/xmxxc/QQBotForge/pull/51)|`a37a2cc0a5860fbefb0af8ef84e58b01b6f562f5`|254 passed in 19.72s|同步兼容，逐票独立审计保留|
|Q05 [#32](https://github.com/xmxxc/QQBotForge/issues/32) / [PR#52](https://github.com/xmxxc/QQBotForge/pull/52)|`d28fe915b5bb7537828e04ea7ace201a1e39fb2b`|259 passed in 20.47s|同步兼容，逐票独立审计保留|
|C01 [#33](https://github.com/xmxxc/QQBotForge/issues/33) / [PR#53](https://github.com/xmxxc/QQBotForge/pull/53)|`f6b0cab25f6ae15442d77f528ae7bc146c1173a0`|178 passed in 15.55s|同步兼容，逐票独立审计保留|
|C02 [#34](https://github.com/xmxxc/QQBotForge/issues/34) / [PR#54](https://github.com/xmxxc/QQBotForge/pull/54)|`a6b6a7601fdb027a45d7cd576d66cda329132a62`|184 passed in 16.20s|同步兼容，逐票独立审计保留|
|C03 [#35](https://github.com/xmxxc/QQBotForge/issues/35) / [PR#55](https://github.com/xmxxc/QQBotForge/pull/55)|`b2dd971d07c5f5bf378d6233bb6ab925745f784c`|190 passed in 18.63s|同步兼容，逐票独立审计保留|
|C04 [#36](https://github.com/xmxxc/QQBotForge/issues/36) / [PR#56](https://github.com/xmxxc/QQBotForge/pull/56)|`4373711974385fb31d7e67565f9b2856146fcf74`|193 passed in 18.13s|同步兼容，逐票独立审计保留|

C04 `4373711` 只追加进度文档；完整运行验证的代码SHA为 `66856c3e15a16591b0dfb4e4c260c0a321e648ac`，两者代码及测试一致。

待审核顺序：框架 **#47→main** 单独交付（personal已含相同实现，#37/#38/#39已合并），业务 **personal → #40 → #41 → #42 → #43 → #44 → #45 → #46 → #48 → #49 → #50 → #51 → #52 → #53 → #54 → #55 → #56**。本报告分支为独立文档交付，不承载业务或框架补丁。

G03冲突在state/docs；A01冲突在main/state。已逐处解决、分别两轴复核。A02以后均为自动无冲突合并；原分支额外修复（如Q04的绑定回执387a332、C03的事实变化分类a0de711）保留。授权/问答/账户实现与各原head逐字比较：G03—Q05仅账户旧导入排序的B01前置差异；C01—C04这些业务目录完全相同。没有将G01目录覆盖G02/G03。

## 精确测试与CI资产

Python3.13.13，pytest9.1.1、pytest-asyncio1.4.0、Ruff0.15.22、mypy2.4.0。新建隔离venv，pip安装source[dev]成功；独立模块物理副本仅core/web/group_state及其测试，无授权/问答/账户目录。

|组合|实际执行结果|
|---|---|
|G01 b32a00a + 04d9017，现有嵌套CI复制|561 passed / 6 skipped|
|同一G01，资产正常目录|567 passed / 0 skipped|
|G01独立安装|33 passed|
|G02 1adf415，仅业务|160 passed|
|G02 + 04d9017，嵌套CI复制|595 passed / 6 skipped|
|同一G02，正常目录|601 passed / 0 skipped|
|G02独立安装|67 passed|
|personal f797ead + 9790d7d，完整框架正常目录|717 passed / 0 skipped|
|C04代码66856c3，仅业务|193 passed|
|C04 + 04d9017，嵌套CI复制|628 passed / 6 skipped|
|C04 + 9790d7d，完整框架/专项+业务正常目录|910 passed / 0 skipped|
|Ruff，G01/G02/各同步批次及C04|全部通过|
|mypy core web main.py，G01/G02/C04|140文件通过|
|mypy --follow-imports=silent --check-untyped-defs，群状态G01 3/G02 4/G03及C04 6文件|通过|

现有共享CI是外部可复用workflow，源revision与测试资产分开。`cp -R ci-assets/tests source/tests` 在已有tests时嵌套到tests/tests；e2e fixture因此计算错根目录，六项报No SPA dist found，源码实际含web/dist。这是原有CI资产装配问题。最小具体改法为`cp -R ci-assets/tests/. source/tests/`；本轮未修改共享CI，而是另行实际执行正常目录组合，六项全部运行，无删用例/放宽断言/新增skip。测试名e2e在此为aiohttp静态路由验证，不等于真实浏览器验收。

F01的9790d7d专项资产未包含在共享CI实际使用04d9017中，已分开运行717及最终910组合，不能以共享CI绿色代替专项。C01原提交已退役93项旧合成账户测试并迁移到模块宿主测试；这是已有业务变更，本轮未删测试，不把193与Q05的259机械比较或宣称旧账户测试仍执行。后续独立审计须继续评估覆盖充分性。

复现（只在临时副本，不在运行目录执行）：

```bash
# SOURCE为上表代码SHA；ASSETS为04d9017或9790d7d完整SHA
mkdir -p /tmp/qqbotforge-verify-source
git archive "$SOURCE" | tar -x -C /tmp/qqbotforge-verify-source
git archive "$ASSETS" tests | tar -x -C /tmp/qqbotforge-verify-source
cd /tmp/qqbotforge-verify-source
python -m pytest tests -q -rfEs
python -m ruff check .
python -m mypy core web main.py
python -m mypy --follow-imports=silent --check-untyped-defs modules/group_state/*.py
# #47真实消息入口专项：PYTHONPATH指向隔离源码，探针创建自己的临时模块目录
PYTHONPATH=/tmp/qqbotforge-verify-source python /path/to/before_handler_probe.py
```

精确日志、红绿证据和可移植before_handler_probe.py见[证据目录](integration-20261021/)。所有快照/报告/日志原始档案仍在执行机`/tmp/qqbotforge-integration-20261021`，源快照保持只读；本交付目录中的历史分轴报告保留其本机路径，中央报告和提交SHA是跨机器复现入口。

## 未完成项目与停止边界

- G03、A01—A04、Q01—Q05、C01—C04未完成本轮逐票独立全审计；当前仅证明同步兼容及所执行回归。
- 未做浏览器视觉/真实交互/无障碍验收。DOM/fetch页面脚本与aiohttp路由通过不替代浏览器。
- 未连接真实QQ，未验证平台成员原生字段、名册/Markdown/按钮/主动私聊许可；未读取真实凭据、运行数据库或日志。
- 未部署、合并、迁移生产数据或改变票据状态。主人保留最终审核合并顺序。
- G01 S1—S3为非阻塞建议；G03旧进度中的RESUMED首条保守拒绝现象仍待独立诊断；查询10秒超时/1000页上限本轮静态检查，未跑满。
- 原始工作树未执行reset/clean/stash/切分支；未提交修改保持原位。独立clone与候选树均位于/tmp，Git原工作树HEAD和状态另行记录。
