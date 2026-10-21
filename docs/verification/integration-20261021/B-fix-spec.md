## Spec

三项原缺陷的实现修复通过；未发现保证放宽或新的代码必修项。复审固定基线 `80647647436e3ffb40aa5c5bd650c7ec5e96fddc` 加scope.json中5个任务拥有文件，检查完整diff及新测试全文，结束哈希一致。

- 人工确认先失效名册与旧cursor、把目标保为不可靠，再等待候选持久化；失败和取消保留失效，新确认只恢复该成员。取消仍排空SQLite，磁盘可能已有新候选，而内存保持历史不可信；重载新代际仍process_restart，不能从盘恢复资格。新观察变更版本后，旧操作清理不覆盖它。
- refresh/roster/member初始_save已进入各自try；写锁排队取消不再留下进行中标记。refresh的G01迁移回归恢复。初始执行中SQLite取消也保守撤销事实和旧cursor。
- group oninput推进请求序号、清结果与分页，A→B→A旧响应被拒绝。

独立原探针重放Python **43 passed**、页面 **12 passed**；另新增“初始写入已经进入SQLite才取消”探针 **3 passed**。均使用真实ModuleManager/Application/鉴权HTTP/临时SQLite与假平台。无源码/PR修改。

文档已额外固定并核对最终 SHA256 `b2368d27a1aaa8f3702a31d0c896eeca5bc884b50147552171518f402c74b9cd`：明确写锁队列取消使用各操作`*_cancelled`，首次保存已进入SQLite执行则保守`persistence_failed`，与三项独立探针一致。代码/测试四文件仍与原scope哈希一致；此文档修订无运行变化。当前Spec必修问题0；局部可重试维持失败关闭保证。完整业务/CI/Standards由集成人另报。详细证据见spec-evidence.md。
