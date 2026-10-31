# 手动更新 personal

后台「检查更新」「更新到最新」与主人菜单共用 `web.tools._updater.handlers.get_updater()` 返回的 `FrameworkUpdater`；未挂载 Web 面板时传入部署目录 `get_updater(base_dir)` 即可创建同一实例。框架没有定时检查或自动更新任务，旧配置 `auto_update.enabled: true` 也不会启动更新。

## 更新契约

- `await updater.check_for_updates()` 返回 `source`、`environment`、`current_version`、`latest_version`、`gap`、`relation`、`ci_status`、`can_update` 和更新日志。检查失败返回 `error`、`can_update: false`。
- `await updater.update_to_latest()` 或 `await updater.update_to_version(full_sha)` 返回 `success`、`message`，成功时带 `backup_path`、`target_version`、`restart_required` 和 `recovery_reminder`。更新会停止当前应用并自动重启；调用方应在结果中展示群状态恢复提醒。此接口供 #144 主人菜单调用，菜单自身负责主人授权与交互。
- HTTP 保留原有 `/api/update/version`、`check`、`changelog`、`start`、`progress`、`environment` 路径及认证。`POST /start` 接收 JSON 对象，可选 `version`；重复请求在已有任务时返回 409。旧 `force`、`skip_backup`、`auto_restart` 参数不能绕过安全检查、备份或重启。插件市场使用的 `/api/update/mirrors`、`/api/update/mirror`、`/api/update/test-mirrors`（SSE 测速）保持原有语义，只读写市场镜像设置。
- `get_progress()` 与 `get_version_info()['last_update']` 可查询重启后的最终结果和备份路径。进行中的状态为 `switching`、`awaiting_restart`、`awaiting_ws`、`rolling_back`（另记 `phase`、原/目标 SHA 与监护器标识）；终态为 `confirmed`、`rolled_back`、`failed` 或 `rollback_failed`。

## 事务与拒绝条件

更新只获取 `https://github.com/xmxxc/QQBotForge.git` 的 `personal`。获取不在部署仓库里进行：每次新建临时 bare 仓库，清空全部 `GIT_*` 与 `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` 环境变量，设置 `GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null` 和临时 `HOME`，用显式完整 URL 和 refspec fetch，不带凭据助手、额外请求头或代理，禁止重定向。临时仓库只通过 `objects/info/alternates` 复用部署仓库已有对象，以减少下载；随后 `git bundle create` 新对象，在部署仓库 `git bundle unbundle` 导入（只解包，不经过 remote 或传输配置），并核对导入后的提交 tree 与获取结果一致。因此部署仓库的 remote、`url.*.insteadOf`、include/includeIf、代理、`http.extraHeader`、credential 设置都不参与获取。GitHub API 只直连 `api.github.com/repos/xmxxc/QQBotForge`，不读取访问令牌；CI 通过后还会取 GitHub 上该提交的 tree，必须与本地导入的目标 tree 一致。

顺序为（取得部署锁并接管遗留事务）→ 隔离 fetch → 严格后续关系和 personal 归属校验 → CI 与 GitHub tree 校验 → 冲突检查 → SQLite 在线备份 → 暂停插件监视 → `checkout --no-overwrite-ignore --detach`（关闭 hook 与 fsmonitor）→ 核对工作区 → 启动独立监护器（继承部署锁）并确认其就绪 → 停止旧应用 → 监护器启动目标版本。

CI 必须是目标 SHA 自己在 `personal` 上由 push 触发的 `.github/workflows/ci.yml`，最新 run 成功且该 run 的 GitHub Actions `check` 完成并成功。缺失、进行中、失败、API 不可用或响应无效一律拒绝。无 Git、不是项目 Git 根目录、同提交、倒退、分叉、目标不属于 personal、本地冲突、无法备份、应用未运行、监护器未就绪及并发更新也会拒绝。

保留不冲突的暂存、未暂存、未跟踪和忽略文件。路径有交集时保守拒绝，不执行 reset、clean、stash 或强制覆盖；即使未预检出的冲突也由 Git checkout 拒绝。部署仓库中的所有 Git 命令都带 `-c core.hooksPath=<空目录> -c core.fsmonitor=false`，不会执行 post-checkout、reference-transaction 等 hook 或 fsmonitor；checkout 后核对 HEAD 等于目标，且除切换前已有的主人本地改动外，已跟踪文件与目标 tree 无差异，否则回到原提交并拒绝。非 Git、上传部署及缺失框架文件的启动脚本不再下载 ZIP 补全，请重新 Git 部署或拉取镜像。

## 锁与事务

更新用两把操作系统文件锁（POSIX `fcntl.flock`），持有进程退出（包括 SIGKILL）时由内核自动释放；文件本身从不删除，不按修改时间或超时判断持有者失效，“持有者是否存活”只看能否取得锁：

- 部署锁 `data/update.lock`：由正在推进更新的进程持有。发起更新的进程取得后，启动监护器时通过继承同一打开文件交给它，因此旧进程退出时锁不会出现空档；监护器在确认或回滚完成后退出即释放。
- 状态锁 `data/update_state.lock`：`update_pending.json` 的每次“读取—核对事务 ID 与预期状态—写入”都在其中完成。持有者暂停多久都不会被他人夺走。

每次更新生成唯一事务 ID，写入 pending 状态并作为监护器参数，监护器再通过 `ELAINABOT_UPDATE_TRANSACTION` 交给新进程。新进程只确认自己的事务；监护器在停止进程或回滚前先把状态从 `awaiting_ws` 原子改为 `rolling_back`，若已被确认则直接退出、不停止任何进程。

仅支持已验证具有可靠跨进程 `flock` 和文件描述符继承语义的部署文件系统。不支持 flock 的文件系统、未验证的 NFS/SMB 等网络文件系统不提供更新器支持；POSIX 平台本身不足以证明文件系统满足条件。部署验收需在同一部署卷验证互斥与锁继承。锁操作报告 `ENOLCK`、`EOPNOTSUPP` 等错误时拒绝获取更新锁，保留事务与备份，不降级为文件时间戳锁；更新接口提示无法获取更新锁，启动恢复记录锁错误。文件系统静默忽略锁或不能可靠传播锁时，一次调用无法自动识别，必须启用前验证。

Windows 没有等价的继承式文件锁交接；检查、更新到最新、指定版本、主人菜单接口与 HTTP start/environment 均在任何锁操作前明确返回“此平台不支持框架更新”（`start.bat` 的框架启动路径保持不变）。

## 接管

状态仍处于进行中、但部署锁无人持有，说明推进它的进程已死。下一次启动（`verify_startup`）或下一次发起更新时取得部署锁后接管：

- 启动前先持久化 `phase=starting_target`。独立启动器在执行目标应用（包括导入）前，持有状态锁核对事务，记录目标 PID、`create_time`、进程组，再 `exec` 应用（保留身份）。即使监护器在 Popen 成功而身份尚未写入时被 SIGKILL，接管与启动器仍按状态锁串行：接管先改状态，迟到的启动器不执行应用；启动器先写身份，接管便可停止它。
- 若监护器死亡、HEAD 正是目标（`awaiting_restart`/`awaiting_ws`），仅目标自身或尚未启动目标的事务允许当前启动进程在 `ws_timeout` 内验证 WS；另一个启动进程不能收养仍存活的旧目标。成功即确认，超时则回滚并重启。
- 其余情况（`switching`、`rolling_back`，或 HEAD 不是目标）一律完成回滚：先按记录的身份停止目标进程组（包括重新挂到其他父进程的组成员），确认非 zombie 成员全部退出，再看 HEAD 实际停在哪里，未回到原 SHA 才 checkout，核对 HEAD 后再写终态；可重复执行。若接管改变了运行中进程的代码，启动时会请求重启。
- 停止前核对 PID、`create_time` 和进程组，不向复用的 PID 发送信号。停止失败或进程组身份无法确认时拒绝 checkout 和终态、保留现场。目标本身发现正在回滚时先请求退出重启，由下一次启动接管，不在自己的运行代码上执行回滚。
- 接管完成后即可正常发起下一次更新。

## 备份与回滚

每次更新在 `data/backup/update-<随机标识>/` 创建私有目录，使用 SQLite backup API 备份框架及扩展目录内的 `.db`、`.sqlite`、`.sqlite3`，包括已提交的 WAL 内容；跳过 Git、虚拟环境、引导缓存及历史备份，拒绝数据库符号链接。备份目录必须解析到部署目录内部；失败清理先核对目录位置和本次创建标识。目录包含数据库相对路径清单和切换前复制的独立监护脚本。

监护脚本不导入目标应用代码。旧进程退出后启动目标应用，默认 90 秒内必须收到应用基于真实连接状态发出的 WS ready/replay_complete 确认；仅 HTTP 健康或 TCP socket 打开不能确认。目标启动即崩溃或 WS 超时，监护器先停止目标进程及其子进程，再切回原 SHA 并重启一次。回滚被本地改动阻止时记为 `rollback_failed`，保留原/目标 SHA 与备份路径供人工处理。

数据库备份保留用于人工恢复，回滚代码不会自动覆盖数据库，避免丢弃更新后收到的数据。当前实现不执行依赖安装或数据库迁移；上线候选应自行保证旧代码可读取其写入的数据。主机掉电或文件系统损坏导致状态文件丢失、进程管理器在监护器之外另行拉起实例，不在保证范围内。

更新结果携带「重启后请确认群成员并使用『群状态 恢复』重建群状态」；监督启动会设置现有 `ELAINABOT_RESTART_RECOVERY=1`，供 #144 的重启恢复卡片复用。

## 信任模型

更新器防的是：更新来源被替换或经过第三方（镜像、代理、URL 重写、同名 remote、额外认证头、凭据助手）；更新到 personal 之外或 CI 未通过的提交；本地导入内容与 GitHub 上 CI 通过的提交不一致；切换中途进程被杀、暂停或并发更新导致的半新半旧、误回滚和锁失效；checkout 时执行部署仓库中的 hook 或 fsmonitor。

更新器不防：已经能修改应用代码、`data/` 或部署仓库对象库的本地程序或用户（它们本就能直接改变运行的代码）；部署仓库中主人自行配置的 checkout 内容过滤器（`filter.*`，如 LFS）；GitHub 账号、仓库或 CI 本身被攻破；依赖安装与数据库迁移。主人本地未提交文件保持原样，只要不与目标改动冲突。

禁用 Git hooks 使用的空目录由进程级 `TemporaryDirectory` 管理，正常退出自动清理；SIGKILL 无法执行退出清理。

## 上游同步

`sync-upstream.yml` 每周一 05:23 UTC 检查 ElainaCore/ElainaBot_v2 main。仓库与上游属于同一 fork 网络，因此直接用 `ElainaCore:main` 为 head 开 PR 到本仓库 main；已有 PR 则复用，无新提交则退出。不创建或推送同步分支，不合并 PR。

仅 proposal/report job 获得 `contents: read` 与 `pull-requests: write`，不执行上游代码；验证 job 只有 `contents: read`，检出 PR 合并提交，核对两个父 SHA，再叠加固定 CI 资产运行 Ruff/Mypy/Pytest。新增 action 引用固定完整 SHA，不使用 PAT、发布密钥或 `secrets: inherit`。仓库需允许 GitHub Actions 创建 PR。

GITHUB_TOKEN 创建的 PR 不触发普通 pull_request workflow，因此上述只读 job 显式跑 CI 并把日志链接和精确 SHA 写回 PR。该 run 绑定定时工作流的 main 版本，不冒充 PR 的必需检查；合并前主人仍需安排并核对必需检查。

## 前端维护

本仓库只提供编译后的 `web/dist`。旧 Update 模块不能显示 CI/差距且固定渲染镜像和上传控件，因此直接将 `web/dist/assets/Update.js` 改为可读 ES module，复用现有 Vue 运行时、请求客户端、CSS 和组件 scope ID；没有新增构建链或依赖。Node 渲染探针验证 pending CI 时禁用更新按钮及页面字段。

插件市场继续沿用自身历史镜像设置，传输代码已移至 `_market/`；旧 Python 镜像配置读写方法仅作市场兼容，不参与框架更新。
