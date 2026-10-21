# G02 最终修复 Spec 工作树复审证据

## 固定范围

`scope.json`：base `80647647436e3ffb40aa5c5bd650c7ec5e96fddc`，起始PR41 `95b05058f323caaf5ff83f29a460cb2823c438a5`，personal `f797eadcf6196163340e4640de11385433e1ff76`。这是干净真实合并候选上的5个任务拥有文件的工作树审查，不是移动源分支承诺。读取full.diff/new-files.txt，以及files/内所有内容。结束5文件哈希与scope完全一致。未动源码、PR、票据。

新`spec-runtime/`用git archive精确base独立展开，再只覆盖scope.files固定内容。独立旧探针复制至新`spec-probes/`，只改fixture/source路径到新运行树；保留全部错误/跨作用域/失效断言和真实公开cursor获取。旧报告与证据完全保留。

## 复核结果

1. 人工确认先对live群version递增，失效roster及公开cursor，目标member变unreliable/pending。候选内才保存新角色，写成功且group.version仍归属本操作才发布。failure/cancel调用_abandon，归属不同就不改较新群事件；归属仍当前则保留历史unreliable，名册不完整。实际HTTP失败503、等待snapshot不完整、取消SQLite排空后仍不完整全部通过。
2. 取消confirm最终写入，真实SQLite线程排空后才传播CancelledError；磁盘已写入新owner/complete=false，内存仍旧member但unreliable、complete=false，符合历史/候选分离。重载一律process_restart，新代际不能让已写盘候选直接成为可靠证明。
3. 初始_save在try内覆盖写锁队列取消；refresh不再遗留refreshing，roster不再遗留roster_refreshing，member不再遗留verifying。旧cursor不再有效。已有新事件版本不被清理覆盖。
4. _abandon对真正SQLite/OSError失败额外撤销群和全部成员可靠性，保持当前公开文档中持久化失败的群事实失败关闭保证；仍先核对归属，迟到失败不破坏较新事实。
5. 页面每次群输入推进序号，清output/status/page，群A→B→A旧响应无法复活旧名单、状态或next_cursor；已有Bot/new request/late error保护全绿。
6. 局部可重试没有放宽#18/#22保证。故障群必须重新可靠核验、成功落库，单人重试不恢复全群完整；不强制整Bot永久隔离符合故障范围原则。原完整分页/错误分页/自身Bot/已知冲突/角色/多分区/并发用例仍通过。

## 独立执行

cwd=`spec-runtime/`；Python=`/tmp/QQBotForge-G02-audit-20261020-4JDJZH/venv/bin/python`。

| 命令 | 结果 | 日志 |
|---|---|---|
| `python -m pytest -o asyncio_mode=auto ../spec-probes -q -s`（新增executing-cancel文件写入前） | 43 passed；含旧34边界+原新增3confirm+6initial-save | spec-independent.log |
| `node --test ../spec-probes/page-ordering-new.cjs` | 12 passed | spec-independent-page.log |
| `python -m pytest -o asyncio_mode=auto ../spec-probes/test_newer_and_initial_cancel.py -q -s` | 3 passed | spec-executing-cancel.log |

第一组原6个Python失败现通过；页面原ABA失败现通过；没有削弱断言。新文件名中的newer并未声称额外新事件用例，本次实际3项均为执行中SQLite首次写入取消；较新事件另由任务新增真实入口test_cancelled_confirmation_preserves_newer_same_group_observation覆盖和静态归属审查确认。

## 文档精确性剩项

执行中首次_save的SQLite取消与“仍排队等write_lock的取消”不同：前者由_save对其携带未持久化版本保守退休，公开reason=persistence_failed，外层_abandon看到已变化版本不会覆盖；三条日志均确认。实际SQLite事务可能成功，reason表达“本次操作未能完成可发布持久化确认”的保守状态，不能据此认为事务必定没写入。

文档目前仅说明各操作_cancelled，需补充分支说明以满足本轮错误语义一致性。无需改变运行保证/回滚线程排空，也不需增整Bot隔离。此处不独立重写源码。

未做浏览器视觉、真实QQ或平台许可；全业务/全仓工具/框架专项与CI由主集成人分别报告。此复审不覆盖G03及后续票独立审计。

## 最终文档增量重新固定

集成人按上述精确性建议更新文档后，已完整读取当前diff并重新核对。最终doc SHA256=`b2368d27a1aaa8f3702a31d0c896eeca5bc884b50147552171518f402c74b9cd`，副本`spec-final-doc.md`。明确区分写锁队列取消与进入SQLite执行后的保守persistence_failed，两者都退休旧证明、需重新核验；与独立三项executing-cancel探针匹配。其余代码/测试四文件对scope原哈希仍全相同。此文档变化单独固定，不覆盖历史共享snapshot。文档问题已解决，当前最终Spec必修0。
