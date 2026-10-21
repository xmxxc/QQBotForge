# Standards — PR #46 CI 测试时序修复

固定 base/HEAD `506f1d8ab6fcb009706e6a63d3fd992815323fd2`；只读审查 `pr46-test-fix.diff` 和唯一修改文件 `tests/bot_modules/test_group_lifecycle.py`。文件 SHA-256 `8ef401b09a4436e9eef9c64be7181654104880e7aa08c15b3a4bf022130b6805`，工作树无其他修改。

**硬性规范问题：0；未放宽断言，无必修。**

新增等待以公开 `RecoveryStatus.pending == 0` 为条件，确保失败投递的框架记账结束后再调用 rebuild。现有恢复契约要求在途处理未完成时拒绝 establish，因此等待该边界与产品保证一致。新增 `assert status.failed` 进一步要求失败在记账后仍保留，不能通过等待自动恢复信任。

原有回调结果为 error/cancelled、ready 为 false、订阅取消且 inactive、显式 rebuild 成功等断言全部保留。未修改产品代码、取消语义、等待 helper、超时、参数集合；未删除用例或新增 skip。只有 3 行增补，未引入需要处理的气味。

原 CI run `39541560100` 的失败应在报告中保留为真实失败，并与修正后运行分开记录；本结论仅针对测试时序兼容，不重认后续票已独立审计通过。主集成人提供的生命周期 26 passed 与快轮询 2 passed 属对应修复验证，本次不重复扩大测试。

结束 HEAD、唯一文件哈希与捕获值一致；未修改源码、索引或远端。
