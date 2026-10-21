# Standards — G02 最小修复工作树

固定 base `80647647436e3ffb40aa5c5bd650c7ec5e96fddc`（PR #41 `95b05058f323caaf5ff83f29a460cb2823c438a5` 与 personal/G01 的真实合并候选）。审查共享 `scope.json`、152 行 `full.diff` 和 `new-files.txt` 指向的新测试全文；共 5 个任务拥有文件。规范继续采用 AGENTS、docs/agents/{issue-tracker,domain}.md、docs/group-state.md 和原 Fowler 基线。

**硬性规范问题：0。无新增必须修复项。**

- `state.py`：人工确认在进入数据库等待前递增群版本，退休旧名册及分页凭据，并撤销目标成员的可靠性；异常清理受操作版本约束。`_abandon` 的持久化失败分支同时退休受影响群及成员，保留历史字段。符合公开失败关闭及“可重试”保证，没有改成自动恢复旧完整证明。
- `state.py`：三个首次 `_save` 移入相应 try 边界，排队取消能进入本操作的归属清理；没有更改 G01 已有取消排空存储实现。
- `page.html`：改群时递增请求编号并清结果/分页，群标识往返也不能恢复旧请求。与新文档承诺及既有 Bot 切换规则一致。
- 文档保留旧组合通过与复审缺口的区别，未静默宣称关闭票重新全面通过；新增测试未删除或放宽原断言，也未增加 skip。

**气味判断：0 个新增阻塞建议。** 原报告指出的 possible Duplicated Code 仍属非阻塞判断；本次在现有归属边界内最小修改合理，不要求额外抽象或重构。

只读验证：`PYTHONDONTWRITEBYTECODE=1 /opt/qqbot/QQBotForge-account-sharing/.venv/bin/python -m pytest tests/bot_modules/test_group_state_audit.py -q` → **7 passed**；`node --test tests/bot_modules/test_group_state_page.cjs` → **13 passed，0 skipped**。使用原测试的临时数据库、假身份、发送/平台替身。

结束 HEAD 仍为 base；5 个快照文件与当前源码均逐个匹配捕获 SHA-256。详见 `standards-hash-check.json`。full.diff SHA-256 `cbb1c70f7cce905aa9fd1cba3071ceae21475e1d174782ded1835a5e96d82875`。未修改源码、PR 或票据。本结论不替代独立 Spec 轴及后续业务票审计。

## 最终文档增补复核

仅复核 `docs/group-state.md` 相对捕获文档的新句子：首次保存尚在写锁队列时取消，操作清理产生 `*_cancelled`；已进入 SQLite 时则先排空线程，由 `_save` 保守标记 `persistence_failed`，外层版本归属检查保留该结果。此处与现有实现一致，并明确两种情况都撤销旧证明、恢复须重新核验，未放宽授权保证。新增必修问题：0。

当前文档 SHA-256：`b2368d27a1aaa8f3702a31d0c896eeca5bc884b50147552171518f402c74b9cd`。原 scope/快照保留为历史；其余 4 个任务拥有文件逐个核对仍与原捕获哈希一致。按文档范围无需重跑代码测试；未修改源码或远端。
