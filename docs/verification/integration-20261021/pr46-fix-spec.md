## Spec

**0 必须修复项。** 本次仅审查PR #46 CI run `39541560100`暴露的测试时序修正，不扩大为G03/A04独立全审计。固定base G03 `506f1d8ab6fcb009706e6a63d3fd992815323fd2`，共享差异`pr46-test-fix.diff`，唯一文件`tests/bot_modules/test_group_lifecycle.py`最终SHA256=`8ef401b09a4436e9eef9c64be7181654104880e7aa08c15b3a4bf022130b6805`，结束复核一致。

增加的等待使用公开`RecoveryStatus.pending`，符合既有恢复契约：消费者通知结果已经发布，不等于框架已完成投递结果记账。`RecoverySubscription._finish`才移除pending、递增revision并记录失败；`establish`明确拒绝pending或仍在运行的回调。原用例在通知result出现后立即rebuild，可能与框架结束登记竞争；产品在pending=1时保守拒绝重建符合保证。

修正先等待公开pending归零，新增`assert status.failed`，随后保留原`assert rebuild True`。它更严格证明：框架完成登记后仍失败关闭，只有显式可靠重建才能恢复。原error/cancelled状态、ready=False、取消订阅和inactive断言全部保留，无跳过、断言放宽、超时调整或产品代码变化；不通过睡眠猜测结束时间。

集成人报告的26生命周期回归及零延迟快速轮询两例通过，与差异语义一致；本审查未重复全套测试。原CI失败应在交付报告保留，修复后串行同步所有含此测试的后续分支并执行相关回归，不将本次等待修正表述为原CI通过或后续票全审计完成。无源码、PR或远程操作。
