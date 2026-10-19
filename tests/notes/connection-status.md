# 连接与会话状态验证

运行代码分支：`feat/connection-status-events`，接口契约见 `docs/module-connection-status.md`。
测试分支：`test/connection-status-events`，基于 `ci-assets`，测试及支持文件均位于 `tests/`。
代码基线：main `31247c0f9fbdace5f5b9ca0e04e228a056e0c3aa`。
测试基线：ci-assets `04d901726cc6d94bae1b2b5524521f997a326e10`。

## 覆盖范围

78 项用例通过真实 `WSClient.connect()`、消息循环、BotRegistry/BotInstance 启停及配置同步、
WebhookHandler 和 ModuleManager setup/disable/reload，观察公开查询、订阅结果、实际协议帧及不可变事件上下文。
socket 与 HTTP 使用可控替身，配置和存储使用临时目录，身份、消息和凭据均为样例。

- 首次 READY、新会话、短断 Resume、有效 RESUMED 的固定补发边界，以及消费者尚未完成本地工作时的服务端补发结束。
- 主动停机、正常/异常断开、无效会话重新 Identify、重连耗尽后重启、关闭前已创建或排队的连接请求和来源替换。
- close 不取消 await connect() 的外部任务，也不等待其退出清理；连接循环启动前后的主动关闭、外部取消及再次连接。
- 默认与 eager 任务工厂下同步耗尽后的再次启动；超时回调退出后从 busy 恢复投递，无需重新订阅，并收到后续 Resume 完成通知。
- 两种调度下群通知等待期间的 Resume 边界、同群消息顺序及其他群隔离；取消群订阅后同群连续通知仍可投递。
- 重复或迟到状态、多 Bot 隔离、晚订阅快照、过滤、取消、异常/超时/busy 结果，以及模块 setup 失败或取消、卸载和重载。
- 心跳使用接收序号；Webhook、自定义 WS/API 和网关发现结果的能力限制；缺失/非法身份或序号、倒退边界、非法载荷及启动失败。
- 重复 Hello 保留原轮次并使该轮证明失效；回调调用参数错误逐项记录 error/TypeError，健康订阅继续处理。
- 未支持的整数 opcode 明确报告 unknown/unknown_opcode；缺失或非法 opcode 报告 unknown/invalid_opcode，布尔值和浮点数不会冒充合法操作码。
- 未知操作码保留接收 seq 和恢复轮次；该输入本身不禁止后续有效 RESUMED，也不会恢复此前已经失效的证明。

测试通过真实接收路径驱动状态，不直接调用 publish 或私有协议方法制造连接事实。
RESUMED 只表示服务端本轮补发结束，不表示模块、插件或数据库处理完成。

## 复现

先获取两个分支：

```bash
git fetch https://github.com/xmxxc/QQBotForge.git \
  refs/heads/feat/connection-status-events:refs/remotes/connection-status/code \
  refs/heads/test/connection-status-events:refs/remotes/connection-status/tests
SOURCE_SHA=$(git rev-parse refs/remotes/connection-status/code)
TEST_SHA=$(git rev-parse refs/remotes/connection-status/tests)
COMBINED_DIR=$(mktemp -d /tmp/connection-status-verify.XXXXXX)
git archive "$SOURCE_SHA" | tar -x -C "$COMBINED_DIR"
git archive "$TEST_SHA" tests | tar -x -C "$COMBINED_DIR"
cd "$COMBINED_DIR"
python -m pytest -q tests/core/test_connection_status.py
python -m pytest -q
python -m ruff check .
python -m mypy core web main.py
```

评审固定版本时，将 SOURCE_SHA 和 TEST_SHA 替换为 PR 中列出的准确提交 SHA。
标准组合包含 441 项既有测试与 78 项连接状态测试，共 519 项。
CPython 3.13.13 执行完整组合；CPython 3.11.15 单独执行连接状态测试（74 项通过，4 项 eager 用例因标准库尚未提供该工厂而跳过）。
工具环境：pytest 9.1.1、pytest-asyncio 1.4.0、Ruff 0.15.22、mypy 2.4.0。

CI 当前从 `ci-assets` 读取测试。新增测试尚未进入该分支时，PR 自动 CI 与上述本地组合的覆盖范围不同。
