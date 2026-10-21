# Standards — G03 兼容合并工作树

固定 HEAD/base `0a83634cb3864d63435c28158da315abf5838146`，待合并父 `1adf415995c21da0aa40c7748ba41bf9f40a6e05`。审查共享 `full.diff`（24 文件、2860 行）和 `g02-compat.diff`；读取当前 AGENTS、docs/agents/{domain,issue-tracker}.md、GLOSSARY、群状态及恢复契约，无 ADR。工作树仍为未提交合并，索引保留 UU 标识但文件已无冲突标记。

**硬性规范发现：0；未确认本次兼容合并新增必修问题。**

- `state.py` 候选提交保留 G02 的提交后活动性/版本检查、失败关闭和首次保存异常边界。名册/单人/人工事实通过 `_merge_member` 保留 G03 周期语义；单人操作保留提交前历史副本，避免 pending 标记覆盖周期判断。
- 四类候选操作提交后均等待 `_notify`，再按群版本及恢复状态降级公开结果。`_ready`、`fact_current`、恢复基线和旧真实写入者屏障仍存在。`main.py` 的 setup 失败和 teardown 使用异步关闭；模块先撤销有效性再排空 SQLite，符合公开卸载保证。
- G01 测试仍断言冷重载历史不可靠；新增显式 rebuild 对应 G03 文档要求。已知成员普通聊天不重复观察，因此迟到失败测试改用新成员有实际语义依据。跨群 pending 返回保守结果，完成后仍检查可靠性。初始排队取消的终态断言移到同 Bot 无关写入结束后，失效、版本递增及取消原因断言均保留；无删除用例或新增 skip。
- 框架文件来自前置父分支同步，不在业务模块散落新框架补丁；提交时仍须保留 merge 追踪，遵守 docs/agents/issue-tracker.md 的框架/业务分支职责。

**气味判断：0 个新增必须处理项。** 原重复操作骨架建议仍为非阻塞；当前兼容范围无需新增抽象。

独立只读验证：四参数候选消费者等待测试及完整 `test_group_state_audit.py` → **11 passed**；使用临时 SQLite、假身份与 socket/平台替身。未声称 G03 整票独立审计通过。

结束 HEAD/MERGE_HEAD 不变，24 个捕获文件与当前源码哈希全部一致，见 `standards-hash-check.json`。full.diff SHA-256 `5c59a2257cc1a97b8ee9e8c522464fd8169a3db90907597bf7786675c74a0f99`。未修改源码、索引或远端记录。
