## Spec

**不能通过；3 项确认问题（P1×1、P2×2）。** 对象固定 PR #41 `95b05058f323caaf5ff83f29a460cb2823c438a5`，personal `f797eadcf6196163340e4640de11385433e1ff76`，merge-base `0585773f9ae0ff1a60533d01de04f910fc6201d0`。开始/结束源码299文件哈希一致、远程head/personal一致；共享scope曾被捕获脚本短暂写回旧API base，审阅已重新固定上述fresh-fetch基线，源码未变。完整差异11文件。

1. **P1：人工确认未撤销旧完整证明。** #22要求“名单或角色变化使旧证明失效”，父规格要求“持久化延迟不能恢复旧信任”。`state.py:400–419` 未在提交前失效，也无异常/取消清理。真实鉴权HTTP、临时SQLite锁/触发器复现：完整名册后人工把member改owner，提交等待时snapshot仍complete=true；HTTP503后旧version、完整证明、公开cursor继续有效；取消虽排空SQLite线程，内存仍旧complete=true/member，磁盘已complete=false/owner。三项正确断言失败。必须在操作开始失效旧证明，失败/取消不能恢复，清理仍核对版本归属。

2. **P2：初始写入排队取消留下永久进行中。** 文档承诺“取消…不留下进行中标记”。`state.py:236/313/359` 初始 `_save` 在try外。真实SQLite写锁让另一群持有Bot写锁，再取消排队refresh/roster/member；分别残留refreshing/roster_refreshing/verifying，三项失败。refresh是G01 `09feaaa`已保护路径的迁移回归；roster/member为G02新增。应把初始写入纳入版本归属清理。

3. **P2：群选择往返复活旧页面响应。** 页面承诺“修改群标识…只有…最新请求…更新页面”。`page.html:47` oninput仅clearPage，不推进请求序号。A→B→A后旧A响应恢复旧名单及next_cursor；实际页面脚本新增用例失败。Bot往返、新请求及单向改群已有保护。

旧审计S1卸载/迟到读取、S2 Bot/群/代际/版本分页绑定及S3大部分页面竞态已修复；既有业务153、独立旧边界探针34通过。新发现不能被这些通过数抵消。可重试策略无需放宽产品保证；修复上述失败关闭缺口后，可保持局部影响和显式重新核验恢复。完整验收、恢复策略、命令、日志见 `spec-evidence.md`。无修复/push/票据操作。
