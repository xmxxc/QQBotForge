# 前端构建来源与同步流程

## 固定来源

- 上游后端：`ElainaCore/ElainaBot_v2`，观察基线 `53e6f74e112c56f35f85c49343a2d8ec3b2cfe65`。
- 上游前端：`ElainaCore/ElainaBot_v2_web`，观察基线 `06ce2b8e4fd4ac0c9e131fb6fb554395f702ca2f`。
- 建议 fork：`xmxxc/ElainaBot_v2_web`（尚未创建）；fork `main` 跟踪上游 `main`，自有改动使用 `feature/*` 分支和 PR。
- 前端为 Vue 3 + Vite，入口脚本为 `npm run build`，仓库有 `package-lock.json`，当前未发现 license 文件。公开 fork、issue、PR 和构建分发前必须由主人确认 license/授权；本文不表示已获授权。

## 可重建构建

在固定源码 commit 的干净工作树执行：

```sh
node --version
npm --version
npm ci
npm run build
```

Node/npm 版本必须记录在构建 manifest；依赖以该 commit 的 `package-lock.json` 为准，禁止用 `npm install` 改写锁文件。构建输出先进入临时目录，验证通过后才同步 QQBotForge 的 `web/dist`。

每次产物随同 manifest 记录：

```yaml
source_repo: ElainaCore/ElainaBot_v2_web # 或已获确认的 fork
source_commit: <完整 SHA>
builder: <Node 版本、npm 版本、构建器身份>
built_at_utc: <UTC 时间>
qqbotforge_target_commit: <同步到的 QQBotForge SHA>
lockfile_sha256: <package-lock.json 摘要>
artifact_files: <文件清单或清单摘要>
```

## 同步顺序与维护

1. 先在 `ElainaCore/ElainaBot_v2` 提交后端三态 issue/PR。
2. 再在 `ElainaCore/ElainaBot_v2_web` 提交不含 QQBotForge 专属更新页的前端三态 issue/PR。
3. 上游确认并完成授权后，将 fork `main` 同步上游 `main`；在 fork feature 分支保留 #100/#171 更新页源码，并通过 PR 合并。
4. 从 fork 的固定源码 commit 执行上述构建，核对 manifest、产物清单和必要的静态检查。
5. 在 QQBotForge feature 分支更新 `web/dist` 和来源记录，运行 CI 校验后提交同步 PR。

上游同步冲突、更新页源码迁移和三态源码必须保持可区分的提交，便于回滚和审查。

## QQBotForge CI 校验

CI 应拒绝缺少来源 manifest、源码 commit、锁摘要或构建器版本的产物；校验产物清单与 manifest 一致，并在可用时用相同 Node/npm 执行 `npm ci`/`npm run build` 对比。CI 不向上游推送、不创建 fork，也不把 dist 的手工修改视为有效来源。

## 回滚边界

构建失败、产物清单不一致、上游同步产生未审查冲突或 license/授权未确认时，停止发布并恢复到上一个已记录的 `qqbotforge_target_commit`。回滚只替换 QQBotForge 已同步的构建产物和来源记录；不删除上游或 fork 的源码分支，不发布外部 issue/PR。
