# G02 Spec 复核证据

## 固定输入与运行组合

- 审核源：PR #41 `95b05058f323caaf5ff83f29a460cb2823c438a5`；fresh personal `f797eadcf6196163340e4640de11385433e1ff76`；merge-base `0585773f9ae0ff1a60533d01de04f910fc6201d0`。共享 `scope.json` / `full.diff` / `commits.txt` 为捕获输入。PR API持续给旧base19e53af，使用fresh fetch/ls-remote固定真正personal。结束PR APIhead不变、draft/open，远程personal与head未变。
- 开始读取AGENTS、issue-tracker/domain、#18/#22正文及原完整报告/修复交接；不存在根GLOSSARY/ADR。完整PR差异11文件，业务源码/模板/测试均检查。
- `spec-runtime/` 为固定source副本，仅core/web取固定current-base，其余来自PR head。真实ModuleManager、Application消息入口、鉴权HTTP、SQLite；假Bot/群/人、平台查询与发送替身；无QQ、真实凭据或运行数据。它是有来源的测试组合，不是PR原样已集成的声明。
- `git merge-tree --write-tree personal head` 无冲突，实际合并tree `1c9d02b7cfcb24500367258aee34756e01f03c0f`。最终交付应对正式集成候选再做相关验证。旧框架缺接线不是新G02归责；此处不用旧失败/通过数代表新SHA。
- `spec-snapshot-verification.json`：source299个文件结束哈希完全一致。只写自己的spec-runtime/spec-probes/日志和报告。

## #22逐条验收

| 验收原意 | 当前结论 | 证据 |
|---|---|---|
| 复用单成员、分页能力，群原生身份、角色/来源/时间/能力边界 | 实现成立（替身平台范围） | reader.get_json保留缺字段；平台原生ID、不用AppID/union；checked_at/source/角色；真实完整多页及单人HTTP/QQ通过。真实平台字段与许可未核验。 |
| 中断、重复成员/游标、缺页、缺角色、无许可、检查中变化不能错误完整；含本Bot/其他Bot | 部分，不能整体通过 | 既有及独立异常分页全部保守不完整；known conflict、self identity/role、真实观察失效通过。人工确认提交等待/失败/取消仍旧complete，见新P1。 |
| 已知成员缺失解释冲突，不判退群；空值/异常/权限保留未知原因 | 正常和异常分页成立 | known_member_missing保留missing_known_members与历史；reader未知错误不写left；Bot role []/{}按response_unknown。 |
| 单人自动/主人有依据只更新本人，不证明全群；历史与新可靠快照区分 | 部分 | 普通成功/失败路径仅目标变化且名册失效；confirm新群不完整；人工确认对已完整群的等待/失败/取消缺失失效，新P1。 |
| 后台、命令、消费接口版本与更新结果，名单/角色变化使旧证明失效 | 未通过 | 正常变化、旧分页、卸载/重载均通过；人工确认503后version/cursor未变；初始排队取消状态残留；群A→B→A页面复活旧响应。 |
| 真实入口多页/不完整/两Bot群/并发；说明保证边界 | 有充分正常路径证据，修复后需复核 | 153业务+34外部边界通过，新增独立6项Python及1项Node正确断言失败。文档已经承诺上述保护，但本版实现仍不完全匹配。 |

## 旧审计三项及G01迁移

- S1最终SQLite写入跨卸载：roster/member/confirm实际HTTP全部503；load-lock排队snapshot/members抛RuntimeError；冷读/重载回归通过。store线程取消排空与异步关闭保留。
- S2公开cursor为offset.key，key绑定generation/Bot/group/group.version。相同version下跨Bot或群拒绝；后续页必须前页version/generation，旧数字cursor拒绝。并发旧版本探针已修改为从**真实公开第一页获取next_cursor**，保持失效断言，没有削弱断言。
- S3最新请求、Bot切换/往返、单向改群、迟到错误、新请求清空均通过；群往返缺口仍成立。
- G01冷读取、卸载、取消SQLite排空、主人存储错误提示、黑名单/主人鉴权、页面一般顺序、迟到失败不覆盖新观察/较新refresh均由本版既有真实入口测试和旧独立探针复核。refresh初始_save在G01 `09feaaa`的try内，G02迁到try外，因此P2是**迁移回归**。
- G02名册/角色/分页能力均保留，不能以重新目录覆盖G01/G02方式修复。

## 可重试替代整Bot隔离的评估

这是故障隔离策略，不必改变公开授权保证。父规格G05/G07要求事实失败关闭、迟到不能恢复；A08要求局部错误不无谓影响其他Bot/消费者。安全实现可以不让整个Bot永久隔离，但必须逐个退休被失败操作影响的事实与版本；读取到的历史必须不可靠，恢复必须有新核验及新版本，不能重新接受旧cursor。

- **影响范围**：SQLite物理连接串行；pending_writes按Bot，故一个群写入期间该Bot所有成员分页短暂拒绝（422“持久化中”）；其他Bot分页不受该标志影响。`_save`写入整Bot记录，失败时只退休该次携带、未durable且版本未被新操作取代的群；普通已持久化的无关群不需丢弃事实。实际磁盘故障可造成多个写请求失败，各受影响群都必须退休，不声称故障时仍可成功写。
- **失败关闭证据**：refresh/roster/member初始写入触发器失败均HTTP503；群/名册/成员变不可靠，旧cursor无效；去触发器前不会重新完整。最终roster写失败回归也不完整；同群新观察/较新操作不会被迟到失败撤销。**人工确认未满足，P1阻塞**。
- **磁盘/内存**：候选副本成功写盘后才发布，取消时排空实际SQLite线程后释放写锁，避免旧线程压过新观察。写失败SQLite事务回滚，内存留下更高版本的不可信历史；这类内存/磁盘区别可接受，因为重载强制process_restart且新generation、旧cursor失效，绝不从盘直接恢复信任。人工确认取消目前出现“磁盘已更新、内存旧完整”的反向不一致，必须修复；它不是选择局部重试的必要代价。
- **恢复条件**：去除存储故障后主动重新refresh/roster/member/confirm，取得新事实、成功落库再发布；非roster重试不能自动重建全群完整。初始失败三路径重新核验探针通过；不需要重载整个Bot才能恢复。
- **旧证明失效**：常规路径变更先递增群version，使旧roster/version/cursor失效，代际重载另外失效。人工确认等待/失败/取消例外是实现缺口；初始队列取消虽已complete=false却留下进行中标记，恢复体验和错误语义不实。修复它们可维持原失败关闭保证，无需降低保证或擅改产品政策。

## 新探针细节

### P1 人工确认

`spec-probes/test_confirmation_failure.py`三个正确断言：

1. 真实HTTP先完整名册，再SQLite BEGIN IMMEDIATE阻塞confirm-member，snapshot仍complete=true。
2. BEFORE INSERT RAISE(ABORT)令confirm-member503；snapshot.version仍2/roster.complete=true、旧公开cursor接受。
3. 公共confirm_member取消，SQLite实际线程未结束时任务不done（排空正确）；释放锁后取消传播。磁盘角色owner、roster.complete=false；内存角色member、roster.complete=true。完整证明错误留存。

### P2 初始_save取消

`spec-probes/test_initial_save_boundaries.py`：让group-blocker的真实SQLite写入持Bot写锁，再开始group-a的refresh/roster/member并在其等待写锁时cancel，分别残留refreshing/roster_refreshing/verifying。三项正确断言失败。另三项实际初始写失败/旧cursor失效/去故障重试通过。清理必须在版本归属仍当前时运行，避免覆盖较新观察。

### P2 页面

`spec-probes/page-ordering-new.cjs`执行真实固定页面script，DOM/fetch替身。原有11项全通过；新增group-a请求未返→input group-b→input group-a→旧请求返回，next.disabled从true恢复false，旧成员/分页再现。根因oninput不递增latestRequest，isCurrent只比当前值。

## 精确测试命令与结果

Python统一 `/tmp/QQBotForge-G02-audit-20261020-4JDJZH/venv/bin/python`，3.12；复用已安装独立审计环境，没有重新将它声称为本轮独立安装。cwd=`spec-runtime/`。

| 命令 | 当前结果 | 日志 |
|---|---|---|
| `python -m pytest tests -q` | 153 passed，无跳过 | spec-business-tests.log |
| `python -m pytest -o asyncio_mode=auto ../spec-probes/test_good_boundaries.py ../spec-probes/test_independent_boundaries.py -q -s` | 34 passed，无跳过 | spec-prior-probes.log |
| `python -m pytest -o asyncio_mode=auto ../spec-probes/test_confirmation_failure.py -q -s` | 3 failed（正确断言，漏洞确证） | spec-confirmation-failure.log |
| `python -m pytest -o asyncio_mode=auto ../spec-probes/test_initial_save_boundaries.py -q -s` | 3 passed / 3 failed（正确断言，漏洞确证） | spec-initial-save.log |
| `node --test tests/bot_modules/test_group_state_page.cjs` | 11 passed | spec-shipped-page.log |
| `node --test ../spec-probes/page-ordering-new.cjs` | 11 passed / 1 failed（正确断言，漏洞确证） | spec-page-new.log |

未重复旧版全量审计，未把本轮153当作后续票独立通过。未做浏览器视觉、真实QQ/平台许可验证；超时10秒和1000页上限做静态检查，未实际等待/跑满。未复核G03恢复/订阅消费完成保证；它们属于后续独立审计。Standards轴、框架专项/全仓类型检查和CI资产由并行审计/集成人分别汇报。此报告停在只读缺陷证据，修复与分支集成由唯一集成人安排。
