# Standards — PR #41

固定 head `95b05058f323caaf5ff83f29a460cb2823c438a5`；base personal `f797eadcf6196163340e4640de11385433e1ff76`；merge-base `0585773f9ae0ff1a60533d01de04f910fc6201d0`。共享快照元数据恢复 fresh base 后重新固定；审查 `git diff BASE...HEAD` 的全部 11 文件、2110 行。已读 AGENTS、docs/agents/{issue-tracker,domain}.md、group-state 与独立安装文档；无 glossary/ADR。

**硬性文档契约问题：3。**

1. **P1 人工确认未关闭旧证明。** `modules/group_state/state.py:400–419` 直接进入 `_commit_member`；失效仅发生于候选（381–384），失败无清理。违反 `docs/group-state.md:20`“提交等待期间快照保持进行中的不可信状态”“提交失败……该群、名册与成员标为 persistence_failed”，以及 51 的持久化屏障。真实临时 SQLite 写锁时 `pending=1`，snapshot 仍 `roster.complete=true/member.reliable=true`；触发器拒写后两者仍 true。证据：`standards-probes/persistence_probe.py` 和 `persistence-output.txt`。

2. **P2 初次保存的排队取消逃过清理。** `state.py:236–237,311–312,358–359` 的 `await self._save` 在 try 外；取消写锁等待后保留 `roster_refreshing`，违反 `docs/group-state.md:24`“不留下进行中标记”。同一 SQLite 探针已复现。归属为 G02 迁移回归：G01 `09feaaa91c278fd126c9d570f9975e1df9bdeb73` 的 refresh 首次保存仍在 try 内；不应据此要求重新修复 G01。

3. **P2 改群未完全退休页面请求。** `page.html:49` 仅 `groupInput.oninput=clearPage`，没有递增请求序号或清旧输出。违反 `docs/group-state.md:24` 的改群清结果/仅最新请求更新要求。实际页面脚本探针：群 A→B→A 后旧 A 响应恢复名单和分页；单次改群保留旧输出。证据：`standards-probes/page_probe.cjs` 和 `page-output.txt`。

**气味判断：1（非阻塞）。** possible Duplicated Code：上述三个方法重复“失效→首次保存→try→查询→版本检查→提交→abandon”形状，且共同遗漏首次保存的异常边界。可在最小修复时统一保护边界，无需扩大重构。

“可重试”本身符合文档：按群/版本退休、其他 Bot 不受影响；取消排空可能已写磁盘候选，但重载统一 process_restart 撤销可靠性。恢复要求新核验/新提交，不能仅重新读取磁盘恢复证明。问题 1 目前破坏了这项失败关闭前提。

末次 HEAD/远端 head 均不变，工作树干净，299 个捕获文件哈希全一致。full.diff SHA-256 `1293c465489bf3d9da663d6b6294bb0030bec478703aed54b4c67843957d9dad`。未修改源码或远端记录。
