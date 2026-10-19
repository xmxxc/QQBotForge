# PR #110 真机验收

针对框架新增的连接状态订阅、查询、会话代际和事件上下文进行验收。

- PR：https://github.com/ElainaCore/ElainaBot_v2/pull/110
- 本包核对的代码提交：`a03b75328730875607b4840bc5372810985fc31a`
- 本包是临时诊断工具，独立于 PR。它不会配置机器人，也不会自行发起 QQ 登录。
- `drop-ws` 会真实断开指定 AppID 当前的一个 WS，随后由框架原有重连机制接管。仅在测试机器人上执行。


## personal 分支快速使用

本分支已包含 PR #110 的框架实现和本页所述观察工具，无需再下载测试包或切换到 PR 分支。
在你的测试部署更新 `personal` 后，重启框架，在模块管理中启用“连接状态真机观察”，并在插件管理中确认 `connection_context_probe.py` 已启用。

在 QQ 私聊测试 Bot 或群内 @Bot 发送 `连接状态验收 before`，随后在框架根目录执行：

```bash
python connection_status_probe_control.py snapshot
python connection_status_probe_control.py drop-ws YOUR_APPID
```

第二条命令会真实断开这个 AppID 的当前 WS；等待自动重连后，发送 `连接状态验收 after`，再查看汇总：

```bash
python connection_status_probe_control.py report YOUR_APPID
```

将 `YOUR_APPID` 替换为测试机器人的 AppID。首次就绪、恢复与消息上下文可先验收，其他项目按下文逐项操作。
下面的“准备测试部署”是从源码另建环境的可选说明；当前 `personal` 部署可直接从第 2 节开始。

## 1. 准备包含 PR 的测试部署

建议使用测试机器人。同一 AppID 测试时只运行一个实例，避免另一个实例登录影响结果。

已有该 PR 源码的测试部署可直接跳到下一节。新建源码部署的 Linux 示例：

```bash
git clone https://github.com/ElainaCore/ElainaBot_v2.git qqbot-pr110-test
cd qqbot-pr110-test
git fetch origin pull/110/head:pr-110
git switch pr-110
git rev-parse HEAD
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

使用 Python 3.11 或更新版本。如果 PR 后续发生变化，`HEAD` 可能不同，需要按新代码重新核对。
Docker 部署必须使用包含该 PR 的镜像；仅把本包复制到旧镜像，不能补上框架 API。

将本包解压到框架根目录，与 `main.py` 同级。文件应为：

```text
connection_status_probe_control.py
modules/connection_status_probe/main.py
plugins/alone/connection_context_probe.py
```

之后按平时方式填写测试 Bot 配置，不需要将密钥发给任何人。为测试官方 WS 恢复能力，使用默认官方 API（`api_version: new`，不覆盖 API 地址）及自动发现网关：

```yaml
websocket:
  enabled: true
  custom_url: ""
  reconnect_interval: 5
  max_reconnects: -1
```

这只是机器人配置内的 WS 字段，不能用这一段覆盖整个 `bot.yaml`。
即使填入官方域名，手工设置 `custom_url` 也会被视为未验证的来源，不授予恢复证明能力。

## 2. 加载并确认观察工具

复制文件后启动/重启框架（源码为 `python main.py`），使新模块被发现。
在现有 Web 面板的模块管理中启用 **连接状态真机观察**（目录名 `connection_status_probe`）。
确认插件管理已加载 `connection_context_probe.py`，必要时启用它。

在同一部署目录、同一 Python 环境中执行：

```bash
python connection_status_probe_control.py snapshot
```

预期输出 `ok: true`，`result.states` 中包含目标 AppID 的状态。官方 WS 成功连接后，`capabilities.session_resume` 和 `capabilities.replay_boundary` 应为 `true`。
`result.results` 记录最近回调结果；观察模块正常时应为 `outcome: success`、`error_type: null`。

观察文件：

```bash
tail -f modules/connection_status_probe/data/status.jsonl
```

Windows 可用文本编辑器查看该文件，或用 PowerShell：

```powershell
Get-Content modules/connection_status_probe/data/status.jsonl -Wait
```

每行是一条 JSON。`kind: status` 是状态通知，`snapshot` 是主动查询，`message_event` 是验收消息的真实事件上下文。
按 `context.appid` 筛选目标 Bot；按 `run` 区分模块的不同加载轮次。
控制命令应逐条执行，等上一条返回后再执行下一条。
Docker 中命令应在运行框架的容器内执行，确保读写同一个模块数据目录。

## 3. 正常启动与消息上下文

首次启用模块时，已有连接会先收到 `is_snapshot: true` 的当前快照，这不代表刚重新登录。
要观察完整启动过程：保持观察模块启用，再重启框架。

预期状态主线为 `connecting → connected → identifying → ready`。
有效 READY 应为 `login_kind: first`、`continuity: unknown`；`runtime_id`、`bot_generation`、`connection_generation`、`session_generation` 均有值。

在 QQ 私聊测试机器人，或在测试群中 @机器人发送：

```text
连接状态验收 before
```

预期回复 `已记录连接状态验收：websocket`，同时出现 `kind: message_event`、`tag: before`，其 `context` 不为空。
对应消息与当前连接状态的 AppID、运行代际、Bot 代际、会话代际和连接代际应一致；`receive_index`、`sequence` 随事件读取变化，不要求与稍后查询的快照完全相同。

## 4. 短断线后恢复旧会话（核心项）

先执行一次 `snapshot`，留存断线前的 `session_generation` 和 `connection_generation`。
将下面的 `YOUR_APPID` 换成测试机器人的实际 AppID：

```bash
python connection_status_probe_control.py drop-ws YOUR_APPID
```

命令仅中断该 Bot 当前 socket，不停止进程、不调用会清除会话参数的 `WSClient.close()`。
`ok: true` 只说明已请求断开；是否恢复成功要看之后的状态。

预期观察到断开、重连、`resuming`，平台接受恢复后出现：

```text
phase: replay_complete
login_kind: resume
continuity: resumed
```

同时核对：

- `runtime_id`、`bot_generation`、`session_generation` 与断线前相同。
- `connection_generation` 改变，`resume_attempt_id` 有值；再次短断线恢复时，恢复轮次标识再次改变。
- `resuming` 的 `resume_from_sequence` 有值；`replay_complete` 的 `resume_from_sequence`、`replay_end_sequence`、`replay_end_receive_index` 均有值。
- 随后发送 `连接状态验收 after`，确认正常回复和事件上下文，且会话代际仍相同。
- 再收几条消息后查询，接收进度可继续变化，已记录的本轮补发结束边界保持不变。

如要观察实际补发，可在默认约 5 秒重连等待期间发送带编号的验收消息，例如 `连接状态验收 gap1`、`连接状态验收 gap2`，再核对收到的事件与补发边界。平台实际投递、权限和订阅范围决定能观察到哪些事件；本测试不预设平台保存所有历史。

如果平台拒绝恢复、改为新 READY，出现新会话和 `continuity: unknown` 是如实降级；这次不能算覆盖了成功 Resume 分支。可在连接稳定后重新做一次短断线，并保留实际结果。

QQ 协议中 RESUMED 表示本轮服务端补发结束；它不表示所有插件、数据库写入和后台任务都已处理完成。参见 [QQ 官方连接与事件协议](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)。

## 5. 主动关闭 WS 后重新启用

保持进程、机器人总开关和 AppID/凭据不变，只将该 Bot 的 **`websocket.enabled`** 改为 `false` 并保存，等待 `stopped`；随后改回 `true` 并保存。

预期新连接收到 READY 后：`login_kind: new`、`continuity: unknown`；`bot_generation` 不变，`session_generation`、`connection_generation` 改变。

这里测试的是同一 Bot 建立新会话。若操作的是机器人总开关，可能重建整个 Bot 实例，不能沿用本项“Bot 代际不变”的预期。

## 6. 模块卸载、重新加载与进程重启

在面板禁用观察模块：应出现 `kind: unloaded`、`subscription_active: false`，原 `run` 后续不再产生状态通知。
重新启用：应出现新的 `run`、新的 `subscription_id`，并收到 `is_snapshot: true` 的当前状态。
保持 Bot 在线时，单独重载观察模块不应改变 Bot 的会话代际，也不应出现旧订阅重复回调。

再保持模块启用并重启整个框架：`runtime_id` 必须改变，Bot/会话代际也更新；首次有效 READY 为 `first / unknown`。
进程重启不应被报告为延续旧进程会话的 `resumed`。

## 7. 按需扩展

- 多 Bot：对 A 执行 `drop-ws`，B 应保持原连接/会话；状态与消息的 AppID 不串。
- 群生命周期：在你有权限的测试群触发一次成员变化，观察 `kind: group_event` 及非空 `context`，与同一 Bot 的对应连接关联。
- Webhook：使用已正常部署的回调来源，查询其状态应为 `unknown`、三项能力均为 `false`；收到回调事件本身不应制造 WS 的 `replay_complete`。
- 自定义网关：恢复及补发能力应为 `false`，即使出现同名 RESUMED，也不能被认作官方恢复证明。

异常帧、回调超时、旧来源晚到等边界条件由独立的 65 项自动回归覆盖，见 [测试资产](https://github.com/xmxxc/QQBotForge/commit/c9f9efefcbf7b418528422bb962a35cd4aed51ed)。无需向真实 QQ 连接注入伪造协议帧来复现。

## 记录和清理

任意阶段均可生成中文验收汇总（本命令只读取日志，不触发断线）：

```bash
python connection_status_probe_control.py report
# 也可只看一个 Bot：
python connection_status_probe_control.py report YOUR_APPID
```

汇总会列出首次就绪、同会话恢复、新会话、消息上下文、进程重启和模块重载是否“已观测”，并写入 `modules/connection_status_probe/data/report.json`。
恢复项必须同时匹配断线前的会话、该轮恢复尝试和补发结束边界，不能只凭重新连上就算覆盖。
“未覆盖”表示日志尚不足以确认，需按对应步骤补测，不自动视为功能失败。观察工具本身无法证明全部业务处理或平台历史完整性。
汇总使用该数据文件中的全部历史记录；如果要重新验收一轮，先禁用观察模块，再将 `status.jsonl` 移到单独备份文件，然后重新启用。

记录各项“通过 / 失败 / 未覆盖”，保留失败时的 `reason`、代际、恢复轮次和边界字段。只看到重新连上，不能单独判定 Resume 项通过。

本观察工具不记录原始 `session_id`、消息正文、Secret、Token 或群/用户身份；AppID 和你主动提供的验收标签仍会记录。分享前可遮盖 AppID；建议只提供本包的 `status.jsonl`，不要打包配置或框架完整日志。

测试完成后，在面板禁用观察模块与诊断插件，再移除本次添加的三个工具文件；诊断数据按需单独保留或删除。不要将临时诊断文件提交到框架 PR。

如果控制命令超时，先查看模块是否启用以及模块日志。确认前不要重复发断线命令；超过 10 秒的请求不会再执行，模块重新启用时也会清除残留请求。
