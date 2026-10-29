Refs #100
Refs #59

框架更新将固定从 xmxxc/QQBotForge personal 直连 GitHub，以 Git 检出替代 ZIP 覆盖；手动后台及主人菜单共用 FrameworkUpdater 检查/更新接口，移除定时更新。

设计：FrameworkUpdater 负责固定源 fetch、严格后续关系和目标提交 CI 检查、冲突拒绝、SQLite 在线备份及更新状态；独立重启监护器在旧版本进程退出后启动目标版本，在限定时间未收到真实 WS 连接确认（包括启动即崩溃）时停止目标进程、切回原 SHA 并重启一次。备份保留 data 下的扩展 SQLite 库（含 WAL 已提交数据）及清单，不自动覆盖回写数据库。插件热加载在切换前停止。拒绝非 Git 部署、非 personal 历史/非后续提交、CI 未成功或无法核实、本地路径冲突、备份失败及并发更新。

上游同步：每周检查 ElainaCore/ElainaBot_v2 main，直接从同一 fork 网络的上游分支开 PR 到 main；无需创建本地同步分支，最小 contents: read + pull-requests: write，不用 PAT，不继承发布密钥，新增 action 引用固定 SHA。GITHUB_TOKEN 创建的 PR 不会自动触发普通 pull_request CI，故在工作流的独立只读 job 检出 PR 合并版本并显式执行相同 CI，再把结果链接写回 PR；不执行上游代码的写权限 job 与验证 job 分离。

当前为设计草案，实施与验证进行中。未部署、未合并。

🤖 Generated with [Codex](https://openai.com/codex/)
