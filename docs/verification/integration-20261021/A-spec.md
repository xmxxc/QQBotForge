# PR #47 — Spec axis

**结论：0 项本票新增、必须修复的 Spec 问题；未发现缺失、部分实现或范围外行为。** 固定 head `bf462cf97bd61049c44ae53ac207f4f22bf32f34`，base `dfcf5a1948bd646a855179e5ce70e74c321d6dd3`，merge-base `398c6afe70055589d1f12ab35affe66ca6e2396c`；读取共享 `full.diff`、`commits.txt`，覆盖全部 2 文件 / 34 新增行。来源为 PR #47 正文及公开 `docs/module-event-hooks.md:48–62`。

- `:48` 要求准入后、普通 handler 前执行：真实 `Application._on_event` → `PluginManager.dispatch` 的 37 个临时探针场景通过，验证先 `before_event`，再日志／追踪投递／全量记录，再黑名单、维护、非 @ 权限与拦截器，最后 Hook；实现 `core/plugin/_dispatch.py:158–200` 符合。主人仅免维护，仍受黑名单及非 @ 条件限制；仅 @ 其他人／Bot 不进 Hook，@ 自己时可进入。
- `:50` 要求原样返回继续、`None` 截止：真实入口验证 handler 继续、默认回复继续、模块消费时仅一条模块回复且 handler／默认回复不运行。异常日志后继续后续 Hook；取消传播且后续 Hook 不运行，符合现有 `HookManager.pipeline` 规则。
- `:50` 的 owner 自动注销：实际 `ModuleManager.enable → enable → reload → disable → enable`，注册数依次 `1/1/1/0/1`；模块卸载后后续消息不再调用旧 Hook。`:62` 的非消息与无插件管理器路径亦验证。
- personal `f797eadcf6196163340e4640de11385433e1ff76` 的两个 PR 文件与源 SHA 逐字节一致；其他恢复框架差异不属于 #47。

**既有行为及边界（非新增 finding）：** 通用 Hook 注册允许显式重复，owner 注销可全部清理；跨 Bot 共享 HookManager，回调负责按 `event.appid` 识别作用域；注销不排空已取得的 pipeline 列表，在途回调不保证停止。已回复后再抛异常可能继续默认回复，这正是既有异常放行契约，调用者须明确消费；本轮不扩大为通用 Hook 生命周期改造。`owner_only` 是后续插件 handler 的权限条件，新模块命令仍须自行校验主人；“用户追踪已完成”在实现中是已投递后台队列，并非同步落盘保证。

开始／结束 head 与远程 head 均未变化，结束 232 个共享快照文件 hash 全符、source 工作区干净；无需重新固定。

完整证据：[spec-evidence/evidence.md](spec-evidence/evidence.md)、`probe.py`、`probe.log`、`results.json`、`final-version-check.json`、`personal-comparison.txt`。
