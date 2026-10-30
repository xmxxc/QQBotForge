# 手动更新 personal

后台「检查更新」「更新到最新」与主人菜单共用 `web.tools._updater.handlers.get_updater()` 返回的 `FrameworkUpdater`；未挂载 Web 面板时传入部署目录 `get_updater(base_dir)` 即可创建同一实例。框架没有定时检查或自动更新任务，旧配置 `auto_update.enabled: true` 也不会启动更新。

## 更新契约

- `await updater.check_for_updates()` 返回 `source`、`environment`、`current_version`、`latest_version`、`gap`、`relation`、`ci_status`、`can_update` 和更新日志。检查失败返回 `error`、`can_update: false`。
- `await updater.update_to_latest()` 或 `await updater.update_to_version(full_sha)` 返回 `success`、`message`，成功时带 `backup_path`、`target_version`、`restart_required` 和 `recovery_reminder`。更新会停止当前应用并自动重启；调用方应在结果中展示群状态恢复提醒。此接口供 #144 主人菜单调用，菜单自身负责主人授权与交互。
- HTTP 保留原有 `/api/update/version`、`check`、`changelog`、`start`、`progress`、`environment` 路径及认证。`POST /start` 接收 JSON 对象，可选 `version`；重复请求在已有任务时返回 409。旧 `force`、`skip_backup`、`auto_restart` 参数不能绕过安全检查、备份或重启。
- `get_progress()` 与 `get_version_info()['last_update']` 可查询重启后的最终结果和备份路径。监护中的状态为 `switching`、`awaiting_restart`、`awaiting_ws`；终态为 `confirmed`、`rolled_back`、`failed` 或 `rollback_failed`。

## 事务与拒绝条件

更新只获取 `https://github.com/xmxxc/QQBotForge.git` 的 `personal`，不使用部署目录的 `origin`、全局 Git 配置、HTTP 代理、重定向或第三方镜像；有本地 URL 重写或按 URL 设置的代理/重定向规则时拒绝更新。GitHub API 只直连 `api.github.com/repos/xmxxc/QQBotForge`，不读取访问令牌。

顺序为 fetch → 严格后续关系和 personal 归属校验 → CI 校验 → 冲突检查 → SQLite 在线备份 → 暂停插件监视 → `checkout --no-overwrite-ignore --detach` → 启动独立监护器并确认其就绪 → 停止旧应用 → 监护器启动目标版本。

CI 必须是目标 SHA 自己在 `personal` 上由 push 触发的 `.github/workflows/ci.yml`，最新 run 成功且该 run 的 GitHub Actions `check` 完成并成功。缺失、进行中、失败、API 不可用或响应无效一律拒绝。无 Git、不是项目 Git 根目录、同提交、倒退、分叉、目标不属于 personal、本地冲突、无法备份、应用未运行、监护器未就绪及并发更新也会拒绝。

保留不冲突的暂存、未暂存、未跟踪和忽略文件。路径有交集时保守拒绝，不执行 reset、clean、stash 或强制覆盖；即使未预检出的冲突也由 Git checkout 拒绝。非 Git、上传部署及缺失框架文件的启动脚本不再下载 ZIP 补全，请重新 Git 部署或拉取镜像。

## 备份与回滚

每次更新在 `data/backup/update-<随机标识>/` 创建私有目录，使用 SQLite backup API 备份框架及扩展目录内的 `.db`、`.sqlite`、`.sqlite3`，包括已提交的 WAL 内容；跳过 Git、虚拟环境、引导缓存及历史备份，拒绝数据库符号链接。备份目录必须解析到部署目录内部；失败清理先核对目录位置和本次创建标识。目录包含数据库相对路径清单和切换前复制的独立监护脚本。

监护脚本不导入目标应用代码。旧进程退出后启动目标应用，默认 90 秒内必须收到应用基于真实连接状态发出的 WS ready/replay_complete 确认；仅 HTTP 健康或 TCP socket 打开不能确认。目标启动即崩溃或 WS 超时，监护器先停止目标进程及其子进程，再切回原 SHA 并重启一次。成功确认前保留部署级更新锁，新进程确认 WS 后即释放（监护器已被外部停止时也不会遗留）；回滚被本地改动阻止时保留锁、原/目标 SHA 与备份路径供人工处理。

数据库备份保留用于人工恢复，回滚代码不会自动覆盖数据库，避免丢弃更新后收到的数据。当前实现不执行依赖安装或数据库迁移；上线候选应自行保证旧代码可读取其写入的数据。更新进程在切换之前意外被杀死，可能遗留 `data/update.lock`；确认没有更新/监护进程运行后，再人工检查 `update_pending.json` 和 HEAD，处理该锁。监护器本身被操作系统杀死、主机掉电和进程管理器同时争抢启动不在进程内回滚的保证范围内。

更新结果携带「重启后请确认群成员并使用『群状态 恢复』重建群状态」；监督启动会设置现有 `ELAINABOT_RESTART_RECOVERY=1`，供 #144 的重启恢复卡片复用。

## 上游同步

`sync-upstream.yml` 每周一 05:23 UTC 检查 ElainaCore/ElainaBot_v2 main。仓库与上游属于同一 fork 网络，因此直接用 `ElainaCore:main` 为 head 开 PR 到本仓库 main；已有 PR 则复用，无新提交则退出。不创建或推送同步分支，不合并 PR。

仅 proposal/report job 获得 `contents: read` 与 `pull-requests: write`，不执行上游代码；验证 job 只有 `contents: read`，检出 PR 合并提交，核对两个父 SHA，再叠加固定 CI 资产运行 Ruff/Mypy/Pytest。新增 action 引用固定完整 SHA，不使用 PAT、发布密钥或 `secrets: inherit`。仓库需允许 GitHub Actions 创建 PR。

GITHUB_TOKEN 创建的 PR 不触发普通 pull_request workflow，因此上述只读 job 显式跑 CI 并把日志链接和精确 SHA 写回 PR。该 run 绑定定时工作流的 main 版本，不冒充 PR 的必需检查；合并前主人仍需安排并核对必需检查。

## 前端维护

本仓库只提供编译后的 `web/dist`。旧 Update 模块不能显示 CI/差距且固定渲染镜像和上传控件，因此直接将 `web/dist/assets/Update.js` 改为可读 ES module，复用现有 Vue 运行时、请求客户端、CSS 和组件 scope ID；没有新增构建链或依赖。Node 渲染探针验证 pending CI 时禁用更新按钮及页面字段。

插件市场继续沿用自身历史镜像设置，传输代码已移至 `_market/`；旧 Python 镜像配置读写方法仅作市场兼容，不参与框架更新。
