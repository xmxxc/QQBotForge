## Spec

A01前置同步兼容复核：**未发现必须修复项**。固定原A01 `bfab97f584a2cde94b6c1013fd3ae0229af0ddc7`、修复G03父 `506f1d8ab6fcb009706e6a63d3fd992815323fd2`，检查完整差异及g03-compat差异。开始/结束scope中24文件哈希一致。

- setup保留`group_state_available(candidate)`，晚启用/重载的授权消费者可及时重绑；通知不表示可信事实。失败时注销路由/页面、异步aclose，撤销实例和订阅，并排空SQLite关闭；成功后才保留_instance。
- confirm的候选提交保留A01额外`confirmation_revision`；`continuity_unknown`从当前恢复phase传入detached apply中的_merge_member。previous取自pending前原成员，保留cycle/left/cycle_uncertain。未知间隔人工确认产生新周期，不能用“目前在群”恢复旧明确授权。
- 冷加载cycle_uncertain、可靠新事实清除不确定、非manual原生加入防重复换周期、rebuild以previous_revision核对当前人工依据的额外修复保留。旧代际/旧恢复轮次人工确认不能跳过平台核验。
- G01/G02先失效、取消/异常归属清理、卸载有效性、候选落库后发布、公开cursor绑定及页面保护未被覆盖。成功提交仍等G03通知，再校验版本/pending/ready；可重试不绕过恢复基线或消费完成屏障。

独立固定base archive加24文件运行`pytest tests/bot_modules -q`：**113 passed**，包括群状态、恢复、消费者、授权消费、晚启用/重绑、未知间隔旧grant不复活及已知G01/G02失败关闭回归。未新增跳过/删除断言。详细证据见spec-evidence.md。本次仅同步兼容审查，不代表A01或G03全票独立审计通过；未做浏览器或真实平台验证。
