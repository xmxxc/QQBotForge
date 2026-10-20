# 本地处理恢复判定验证

本项测试入口为 `tests/core/test_resume_processing.py`，样例模块为 `tests/fixtures/modules/recovery_consumer/main.py`。测试不连接真实 QQ，不读取真实配置、存储或凭据。

依赖代码起点为 personal `19e53af82fbcc69a7a54c51ac3609f8821ef56fb`（已经包含成员订阅和连接状态接口）；测试起点为连接状态测试 `f1b79115edfc082997d225dd743a3adf81eb3be8`。复用其中的可控 socket/平台替身，不能仅把本项新测试应用到缺少该前置测试的树后宣称组合验证。

从完整源码导出目录运行；只叠加测试资产，不拷贝本机配置或 data：

```bash
python -m pytest tests/core/test_resume_processing.py -q
python -m pytest -q
ruff check core web
mypy core web
```

独立 `ci-assets` 工作树没有源码或 pyproject.toml，直接验证单文件时需要指向源码目录及其 pytest 配置：

```bash
PYTHONPATH=/path/to/source python -m pytest \
  -c /path/to/source/pyproject.toml \
  /path/to/ci-assets/tests/core/test_resume_processing.py -q
```

新增40项：可信补发结束仍等待显式确认；返回回调/后台任务不能确认；通用事件乱序完成；异常、超时、自取消与多订阅者隔离；新会话与旧回调；重复/冲突；共享依赖失效及重建凭据；重建期间收到事件；晚订阅/应用重启/重载；容量溢出；未知协议；多 Bot；真实 ModuleManager 卸载/重载/setup失败；两路径的取消不合作回调；固定补发目标；不可变事件快照和确认来源；appid过滤；非法订阅参数；无接收登记适配器；解析失败缺口；两路径缺失/非法序号；去重窗口之外的迟到事件；INVALID_SESSION；断线前在途事件；自定义网关。

连续性依据来自实际 `WSClient.connect()` 的可控协议帧，经框架接收登记、群生命周期投递回执或通用事件消费者、显式确认及恢复状态查询。未直接 emit 状态或手工推进完成水位。对消费者所承诺状态的测试使用独立最小样例，不作为具体业务模块的生产验收。

测试精确 SHA、源码 SHA、组合与日志见本机 `/tmp/qqbot-worker-3-resume-barrier-result.md`。

`tests/core/test_recovery_application_integration.py` 另有四项完整应用接线：实际 socket 帧经过 `Application`、`BotRegistry` 和真实加载的 `PluginManager`。分别验证群顺序已预登记时仍投递恢复消费者，以及通用/成员恢复消费者等待期间普通插件的慢、快处理器仍可并发完成。未替换应用普通事件入口或插件分发器；仅平台与存储使用临时替身。测试不会把群预登记、普通插件返回或队列排空当作恢复确认。

同文件另有默认及 eager 调度两项回归：通用消费者在回调中主动取消订阅后继续等待，替换订阅必须等待旧回调真正退出、拒绝期间取得的旧凭据，再由新的重建凭据建立基线。Python 3.11 跳过其不支持的 eager 变体。
