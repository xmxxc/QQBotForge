# A / Spec 证据

源快照固定于 `bf462cf97bd61049c44ae53ac207f4f22bf32f34`。探针把共享 source 的 `core/` 拷贝到本目录 `runtime/`，所有运行文件及合成模块均留在本目录；没有改动共享 source，未调用生产 `start()`，未读取配置文件或凭据，未建立网络、数据库或真实平台连接。使用真实 `Application._on_event`、`PluginManager.dispatch`、`HookManager.pipeline`、后台追踪调度及 `ModuleManager`；只替换设置读取、Bot 注册表、日志持久化和发送端。合成群、Bot 和用户均为 fake-*。

运行：`PYTHONDONTWRITEBYTECODE=1 /tmp/claude-1000/-opt-qqbot/83e49928-5137-4867-96b9-071d61cd85c5/scratchpad/audit5/venv/bin/python probe.py`。

最终日志为预期未知合成事件和合成异常警告，再输出 `passed: 37`。详细 trace 在 `results.json`。早期探针自身曾因复用已停止测试 worker 的 app 造成等待，后补齐测试清理并重跑；另一次因测试设置遗留导致断言失败，清空设置后完整重跑。两者为测试夹具问题，未修改被审代码，最终所有 37 场景完成。

| 真实消息入口组合 | Hook／后续行为 |
|---|---|
| 普通消费、匹配插件同时存在 | before_event → 日志 → 追踪投递／全量记录 → 拦截器 → before_handler；模块仅回复一次，插件不运行，无默认回复 |
| 返回原 Event，插件命中 | 插件正常执行，无默认回复 |
| 返回原 Event，无插件命中 | 默认回复一次 |
| 用户黑名单／群黑名单 | 不进 Hook，框架回复对应黑名单模板 |
| 维护，非主人／主人 | 非主人不进 Hook；主人进入 |
| 主人同时被拉黑 | 不进 Hook |
| GROUP_AT / C2C / AT_MESSAGE / DIRECT_MESSAGE / MESSAGE_CREATE | 均按消息完整检查链进入 Hook |
| 非 @ 全量群，开关关／开 | 关时拒绝，开时进入 |
| 非 @ 全量群，当前群白名单／其他群白名单 | 当前群进入，其他群拒绝 |
| 仅 @ 其他 Bot／用户，ignore 开／关 | 开时拒绝，关时进入 |
| 同时 @ 自己及其他 Bot／用户 | 进入 Hook |
| @全体／Bot 作者忽略 | Application 记录日志后提前返回，不进 Hook |
| 插件拦截器返回 True | 不进 Hook |
| 自定义非消息事件／无插件管理器 | 不进 Hook |
| A 处于维护，B 独立普通消息 | B 进入，作用域设置以 B 为准 |
| 优先级 0 Hook 抛 RuntimeError，100 Hook 放行 | 先记录错误，后续 Hook 继续 |
| Hook 抛 CancelledError | 传播至真实入口调用方，后续 Hook 不执行 |
| 模块 enable / 重复 enable / reload / disable / enable | Hook 数 1 / 1 / 1 / 0 / 1，各阶段实际消息入口执行 |
| 显式重复注册相同回调 | 运行两次；owner 注销移除全部，既有通用规则 |

非 @ 且未授权时，拦截器仍会运行；这是原 dispatcher 次序，不是 #47 绕过准入。`ignore_at_check=True` 的插件 handler 保留既有例外，而本 Hook 刻意不提供此例外，符合“不满足接收条件不进入”的公开约定。

源码复核：`core/bot/event.py:291–403` 负责全入口顺序；`core/plugin/_dispatch.py:137–200` 负责准入和 Hook；`:202–240` 负责插件及默认回复；`core/module/hook.py:33–91` 负责重复注册、排序、注销与异常语义；`core/module/manager.py:257–321` 负责模块启停／重载。在途 pipeline 使用已排序列表快照，注销未来订阅不撤回快照，这在补丁前已存在；未作强于文档的“卸载排空”断言。

实际调用者：只读查看干净工作树 `/opt/qqbot/QQBotForge-access-control-a01` 的 `291d759911bd91a3e1f8219fca5e96fb7cb72751`。`modules/self_qa/main.py:20` 注册 before_handler，`:22` 为交互回调独立保留 before_event；`entry.py:215–245` 按消息类型、频道、文本、Bot、群、主人及业务范围筛选，未匹配返回 Event，处理后返回 None，非 @ strip 在 `:225–226` 自行完成。这只是调用契约核对，不代表独立审核问答／授权业务。

文档 `docs/module-event-hooks.md:5–7` 明确 before_event 可转换 Event 且必须保留 appid；`:48–50` 的 before_handler 只承诺原样返回或 None，不承诺转换后的新对象替换后续插件事件。因此本实现只取 pipeline 的 None 判断，不构成违反该公开契约。

版本证据：`final-version-check.json` 记录结束 SHA、全文件 hash、工作区状态和远程三个 heads；`personal-comparison.txt` 记录两个 PR 文件逐字节相同。personal 的 HookManager / EventHandler / ModuleContext 额外 recovery 支持属于别的已合入框架工作，通用 Hook 核心仍一致。

限制：纯 Python 传输／平台替身；未验证浏览器、真实 QQ、真实平台返回及生产运行数据。探针不是本次框架 CI 已执行的声明；#47 分支不含测试资产，主集成人后续可将必要回归纳入框架专项。
