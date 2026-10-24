# #75 第二阶段本地预实施证据

本轮授权来自 `/tmp/qqbot-round-next/75-stage2-local.md`，允许在 #72 复核期间做本地离线闭环；不推送／开PR／关闭票／部署或真实登录。第一阶段交付及证据保持原对象可追溯。

## 固定起点

- 第一阶段 head `c3e11992d14e76c28561cba207cfa517ab8cf4a0`，tree `63987f0d713d3cb421212f607d266bc6042e3557`。
- 仅普通 merge 指定 #72 `129df14178d1f551b06bc131a73aacde74c30255`。
- merge commit `66c542cd04eaf6c1646b09bce706c59fc5c18150`，父提交依次为上述两项；tree `44195393fd8eab28273c5e673f431f8a1d607239`。
- 本轮实现增量以该 merge 为 base，独立工作树／原分支不变。不引入 main d182a40、#73/#74 或其他候选；未改 #72 工作树。

## 覆盖与断网

新增业务测试通过真实 PluginManager／模块／消息／鉴权 HTTP，用临时 SQLite 和权限0600的假密钥文件，HTTP协议只替换 httpx transport。入口覆盖主人群聊、私聊和后台普通选择／确认；非主人、跨Bot、跨来源确认和指定密码参数拒绝；多连接并发返回同一活动任务；旧选择失效；随机格式、前导零、旧值回避；同意图密文不因取消／重启改变。

主人可对未提交／明确拒绝任务同意图重试或放弃，未知结果两者均拒绝；群内不列账号名称；任务期间删除群正常完成，旧读取不能把新密码写回；结果落盘瞬时失败有界重试保留确定成功。异常边界覆盖配置／提交取消、再次取消、提交标记回调期间取消、读超时／无效JSON／明确拒绝、原子准备失败、本地实际SQL事务回滚、成功本地失败重启自动／显式仅补本地、缺失密钥继续暂停、通知失败与重试不重新改密；查询目录过滤、最后await期间启动暂停、最后await期间已经完成改密（不得发送旧密码）、只读数据库不可用失败关闭。任务／异常／日志／私聊回执不含待用新密码、旧密码、Cookie、令牌、原始响应或意图密文。

强杀控制：单独 Python 子进程使用真实协议和假 HTTP，在改密分发点 `os._exit(75)`，不运行清理；父进程重开插件后同一加密意图保持、submitting 转 unknown/paused，无协议重发。

协议原122测试复用，原件SHA256未变化。业务测试阻断非回环 DNS／socket连接，主动控制证明游戏平台域名DNS和外部TCP被拒；回环连接仅用于真实应用测试服务器。子进程另行禁止全部DNS/TCP。没有配置真实账号并非断网依据。

## 首次独立审查 F1/F2 修复

依据 `/tmp/qqbot-round-next/75-fix-independent-f1-f2.md`，修复 #75 首次独立审查（issue 评论 6378762175）的 P1 迟到执行器与 P2 已知成功落盘丢失，不改协议、页面或 #70 原件。

- F1：持久执行令牌 runner 加开始／提交前／结果写入三处比较事务（见接线说明“执行权是持久的”）。真实子进程与同一临时库覆盖：确认后迟到 `_start` 遇另一进程 initialize＋release（不登录、正式密码不变、成员查询旧密码合法）；放弃后新选择／确认新任务（旧执行器不领走，新任务暂停保留，再启动仅一次提交）；执行中途被另一进程 initialize 后放弃或同意图重试（旧结果不回写，重试恰好一次提交）；同一确认重复启动只一次提交。
- F2：结果在约20秒有界重试后保留于进程内，后台退避再写，`恢复` 只补本地。真实第二 SQLite 连接 `BEGIN IMMEDIATE` 持写锁、产品默认重试间隔下耗尽（与审查探针相同，把工作连接忙等待设为0，省去每次5秒等待，故约20秒而非最坏约54秒）；持锁期间读取503、恢复422、重试／放弃拒绝且暂停；解锁后恢复只补本地、远端 POST 恰好一次。另测后台自动写入、保留的明确拒绝经恢复与后台写入并发仅写入并通知一次后可放弃，以及进程停止后重启为暂停未知。
- 审查原探针原样重跑：F2 原探针修复后通过；F1 原探针在等待旧执行器进入假改密接口处超时，因为修复后它在登录前就被拒绝，同一断言以不等待该接口的形式写入本仓测试。

## 增量复核 F3 修复

依据 `/tmp/qqbot-round-next/75-fix-cross-process-receipt.md`，修复独立增量复核（issue 评论 6381772426）的 F3/P2：本进程保留结果写入后等待旧回执时，另一进程合法重试，旧回执把 sent 写到新尝试，新尝试成功回执被跳过。修法：回执与 notification 记录按写结果时的 runner 条件化（见接线说明“回执按尝试绑定”），选择允许新尝试并隔离旧回执，而非跨进程串行等待。真实后台入口、真实子进程与第二连接、原协议假HTTP覆盖旧 sent／failed／异常 × 新尝试成功／拒绝，核对实际通知内容与次数、当前 runner／notification 及每次尝试一次远端提交；另测通知取消记 unknown 并释放管理、同一尝试重复回执只发一次、旧尝试迟到回执不发。

## 检查与审查记录

工具环境复用 `/tmp/qqbot-75-venv`；为运行前置已有浏览器回归，仅在该隔离环境补 playwright，使用现有 Chrome。本轮没有改 page.html。

命令和最终结果以本轮外部交付记录为准（避免版本号自引用改变已审树）：

```bash
python -m pytest -q tests
python -m pytest -q tests/bot_modules/test_ninja3_password_tasks.py tests/bot_modules/test_ninja3.py tests/password_protocol
node --test tests/bot_modules/test_account_page.cjs tests/bot_modules/test_group_state_page.cjs
ruff check plugins/ninja3 tests/bot_modules/test_ninja3_password_tasks.py
ruff format --check plugins/ninja3 tests/bot_modules/test_ninja3_password_tasks.py
mypy --strict --follow-imports=silent plugins/ninja3/password_protocol.py plugins/ninja3/password_tasks.py
mypy --follow-imports=silent plugins/ninja3/storage.py plugins/ninja3/service.py plugins/ninja3/main.py
```

作者最终两轴自查绑定精确增量快照，修复及复核记录见外部交付。此为作者自查，非独立批准。完整 #75 与真实发布仍待总控组合、独立审查及真实身份字段核对。

接口、密钥／备份边界、#73/#74组合补丁位置和待来源方确认的脱敏字段问题见 [接线说明](../ninja3-password-tasks.md)。原始两个脚本不改动，不把测试假密码当秘密；未改变授权或群状态公共接口、没有实现 #76/#77/#78。
