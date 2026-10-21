# Standards — A01 同步兼容工作树

仅审同步兼容，固定 base/HEAD `bfab97f584a2cde94b6c1013fd3ae0229af0ddc7`，merge parent `506f1d8ab6fcb009706e6a63d3fd992815323fd2`。读取共享 `scope.json`、聚焦差异 `full.diff` 和 `g03-compat.diff`；规范为当前 AGENTS、docs/agents/{issue-tracker,domain}.md、GLOSSARY、群状态公开文档及原气味基线，无 ADR。

**硬性规范问题：0；无确认新增必修。**

- `modules/group_state/main.py` 保留 A01 的 `group_state_available` 广播和失败 route/page 注销，同时使用 G01 的异步 `aclose`。可用性广播没有改变事实就绪判定；与 `docs/group-state.md`“可重绑通知，不代表基线或事实已可信”一致。
- `state.py:confirm_member/_commit_member` 先失效群版本和目标成员，再候选提交；持久化后检查活动性/同群版本、等待消费者、按恢复状态返回。失败/取消仍经归属版本清理，跨作用域分页和迟到页面保护没有丢失。
- 候选事实保留 `confirmation_generation` 和 `confirmation_revision`；`manual=True/continuity_unknown` 贯通 `_commit_member` 到 `_merge_member`。冷加载的 `cycle_uncertain`、未知间隔人工确认产生新周期、可靠查询清除不确定性和 rebuild 核对旧恢复轮次的规则均保留。符合文档“不据当前在群确认恢复旧明确授权”，没有用 pending 记录替换提交前历史周期。
- 框架前置保护通过 merge 父保留；本次冲突解决没有新增业务专用框架补丁。既有取消失效及终态断言仍存在，无删除用例、放宽断言或新增 skip。

**气味判断：0 个新增需要处理项。** 新参数承载 A01 已有连续性需求，不能视为无需求抽象；不建议扩大本轮重构。

独立只读验证：`test_group_state_audit.py`、四参数消费者等待测试及 `test_access_control.py` → **31 passed**。覆盖失败关闭、取消、周期、晚启用/重载重绑与授权消费兼容；此结果不代表 A01 整票独立审计通过。

结束 HEAD/MERGE_HEAD 不变，24 个快照文件与当前源码哈希全部一致；详见 `standards-hash-check.json`。full.diff SHA-256 `1620dc628d8e4bbaa4b8816f70607057ab2a180ccc6b172158ee9050df195402`。未修改源码、索引或远端。
