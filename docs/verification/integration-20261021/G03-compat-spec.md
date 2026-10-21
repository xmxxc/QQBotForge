## Spec

本次**G03兼容合并复核未发现必须修复项**；结论只覆盖前置保护迁移及原G03行为保留，不表示#23独立全审计通过。固定原G03 `0a83634cb3864d63435c28158da315abf5838146`、修复G02父 `1adf415995c21da0aa40c7748ba41bf9f40a6e05` 及scope中的24个差异文件。开始/结束哈希一致。

- G01/G02的候选提交、卸载/冷读有效性、SQLite取消排空、版本归属失败清理、公开cursor绑定及页面群往返保护保留。confirm先使旧名册/凭据失效，失败/取消不恢复；初始保存仍在try内。
- `_merge_member`迁入detached apply；单人核验与确认保留变成pending前的previous，避免丢失left状态、周期、加入时间、人工锚点及原生转移信息；角色核验不会复活旧周期，过旧加入日期保持stale_membership。人工确认仍只形成该成员事实。
- 成功候选必须等待`_notify`消费者完成；成员返回再经`_member_result`核对版本、pending_writes和_ready。refresh、roster同样等待通知；原生生命周期保持保存→通知→ack。失败/超时/旧真实写入者与恢复屏障保留。
- async aclose先撤销active，关闭notifications、恢复及连接订阅，再异步关闭SQLite；_ready删failed_bots对应局部重试迁移，仍需active、恢复ready、无pending、无webhook不可信来源及无消费者写入；fact/fact_current另守持久化和事实可靠性，未以重试绕过重建。

独立从固定base archive加24快照文件构建测试树，`pytest tests/bot_modules -q` **93 passed**，包含4类候选提交等待消费者、生命周期/恢复/周期、G01/G02回归及页面脚本。测试适配保留原断言意图：冷读后显式重建、新成员触发真实变化、跨群持久化期间保守遮罩、取消终态在blocker结束后检查。无新增跳过/弱化断言来回避失败。详细范围见spec-evidence.md。
