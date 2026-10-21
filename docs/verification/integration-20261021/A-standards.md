# Standards — PR #47

固定 head `bf462cf97bd61049c44ae53ac207f4f22bf32f34`；base `dfcf5a1948bd646a855179e5ce70e74c321d6dd3`；merge-base `398c6afe70055589d1f12ab35affe66ca6e2396c`。审查共享 `full.diff` 全部 2 文件、60 行；捕获为 `git diff BASE...HEAD` 和 `git log BASE..HEAD`。

**硬性规范问题：0。** 该框架快照和 main 不含 AGENTS.md、docs/agents、glossary 或 ADR；已读取 personal 中现行 AGENTS 及其 issue-tracker/domain 要求。相关公开规范为 `docs/module-event-hooks.md`、`docs/plugin-development.md`；上下文读取 `core/module/hook.py`、`core/module/manager.py`、`core/bot/event.py` 及完整 dispatcher。

新增 hunk `core/plugin/_dispatch.py:196–200` 放在拦截器后，沿用通用 pipeline 的 None、异常、优先级和 owner 注销行为；`_hook_manager:373–382` 为应用查询适配，未引入业务概念。文档 `docs/module-event-hooks.md:46–62` 与这项实现的声明范围一致。source 中无 before_handler 业务订阅者；before_event 实际入口和现有调用路径已查阅。是否满足所有新增行为的执行验证由独立 Spec 轴报告，不以此处静态规范结论代替。

**气味判断：0。** 全部 Fowler 基线已按差异检查；没有值得要求修复的重复、抽象、命名或职责问题。工具可强制的格式/类型项不重复列为规范发现。

开始和结束 source HEAD 相同，工作树干净，232 个捕获文件 SHA-256 全部一致；末次 GitHub API head 仍为上述完整 SHA。full.diff SHA-256 `623b8bb99872c000fbcd6425eaa49b7efde8f6eaa9774badd08a7754e745bb82`。未修改源码、PR 或票据。
