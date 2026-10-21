# F01 框架公开契约验收

核对日期：2026-10-20，对应 QQBotForge #20。此记录仅验收框架契约，群状态和授权接入分别由 #23、#24 完成。

## 精确版本

| 来源 | 固定版本 | 核对结果 |
| --- | --- | --- |
| [ElainaBot v2 upstream/main](https://github.com/ElainaCore/ElainaBot_v2/commit/b410938ed5aead906f9e5256228108057bb5dbd9) | `b410938ed5aead906f9e5256228108057bb5dbd9` | 已包含 #108 群事件、#110 连接及通用接收 Hook；没有本地恢复确认契约 |
| QQBotForge origin/main | `398c6afe70055589d1f12ab35affe66ca6e2396c` | 相关事件、连接及消息入口与上游一致；本票以此为框架起点 |
| 本地 personal | `3509cd9` | 在框架旧基线上已有本地恢复及多轮修复；消费者未接入，不能视为业务验收 |
| 远端 personal | `19e53af82fbcc69a7a54c51ac3609f8821ef56fb` | 尚无本地恢复；业务 B01 基于此版本独立交付 |
| 本票框架交付 | `feat/module-recovery-f01` | 将既有恢复实现及修复移植到当前 main，再修复群顺序预登记跳过恢复登记的兼容问题；最终源码提交在 PR／票据结果中固定 |

复用 personal 的 `ce464e3`、`3498e71`、`dfcc431`、`0db9e37`、`8ff1a04`、`3509cd9`。上游已含的群队列优化、BotRegistry 查询、before_event 和显式类型分发不重复移植。没有取走原工作树未提交修改。

## 消费接口与信任

- `group_lifecycle_api_version == 1`：四类群事件保留原生身份、来源、时间、事件 ID、序号；回调完成／失败／超时／取消／卸载结果在同群后续业务派发前登记。普通插件启停、维护和黑白名单不屏蔽观察。
- `connection_status_api_version == 1`：只报告连接及协议事实，`READY`、`RESUMED` 和 `replay_complete` 不代表本地业务已处理。
- `recovery_api_version == 1`：同会话可靠补发边界、已建立基线、边界内处理成功和显式确认缺一不可；首次启动、新会话、已知缺口、未知来源、失败和依赖失效均需重建。
- 群事件只通过恢复订阅应用一次，不再另订阅普通群生命周期重复写入。非群事件的恢复登记独立于群顺序登记，预登记群顺序不能跳过恢复消费者。
- `acknowledge` 必须位于持久化及承诺工作完成之后。后台任务启动、普通插件返回及迟到确认不能恢复信任。旧写入者实际退出前，同安装槽位新订阅不能建立基线，退出也使期间取得的重建凭据失效。
- 官方 QQ WS 是当前连续恢复支持来源；Webhook、自定义网关和未登记适配器明确不具备该证明，仍可使用其实际支持的事件事实。

详细字段、订阅／卸载和依赖语义见[本地恢复契约](../module-recovery.md)、[群事件](../module-group-lifecycle.md)、[连接事实](../module-connection-status.md)与[接收 Hook](../module-event-hooks.md)。框架不能撤销消费者已经发生的外部写入，也不证明平台未投递事件或查询与发送的原子一致性。

## 可复现实例与证据

测试资产独立提交到 `ci-assets`，不安装到运行插件目录。`tests/fixtures/modules/recovery_consumer/main.py` 展示真实模块加载、消费与确认；`tests/core/test_recovery_application_integration.py` 从可控 WS 帧经过真实 Application、BotRegistry、ModuleContext 与 PluginManager 验证接线。网络、存储及身份使用替身和临时目录。

从最终源码提交导出干净副本，叠加固定测试资产版本后运行：

```bash
mkdir -p /tmp/qqbotforge-source
git archive SOURCE_SHA | tar -x -C /tmp/qqbotforge-source
# 在单独测试资产 clone 中导出；不要覆盖运行目录。
git -C /path/to/test-assets archive ASSET_SHA tests | tar -x -C /tmp/qqbotforge-source
cd /tmp/qqbotforge-source
python -m pytest -q
python -m pytest tests/core/test_recovery_application_integration.py -q
```

测试版本及结果（源码、测试资产最终 SHA 在票据／PR 固定）：

| 验证层 | 结果 |
| --- | --- |
| Python 3.11.15 全部框架测试 | 712 passed、5 skipped；跳过的是 Python 3.12+ eager 调度变体 |
| 成员／连接／恢复专项 | 271 passed、5 skipped |
| Python 3.13.13 eager 专项 | 4 passed、140 deselected；使用既有环境的 Python 包路径，无新增运行依赖 |
| 静态检查 | 8 个相关框架文件 Ruff 通过；恢复实现 mypy `--check-untyped-defs --follow-imports=silent` 通过 |
| 算法与真实框架接线 | 原始协议帧、同群前置屏障、基线、依赖、乱序／缺口、卸载／重载、超时和旧写入者均有测试；预登记跳过恢复用例先失败后通过 |
| 浏览器 | 未执行，本票无页面改动 |
| 真实平台许可 | 未执行，未使用真实 QQ、凭据或平台成员数据 |

此结果不启用业务模块、不迁移生产数据、不改变运行实例。PR 保留 draft，合并及部署由主人决定。
