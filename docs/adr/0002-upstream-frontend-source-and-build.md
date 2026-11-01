# ADR 0002：前端上游源码与构建来源

- 状态：已决定；后端已发布，前端源码/构建路线等待许可证反馈
- 日期：2026-10-31
- 关联：#158

## 背景

QQBotForge 当前随仓库同步 `web/dist`，但三态连接显示和更新页维护需要可读的 Vue 源码。公开上游源码为 `ElainaCore/ElainaBot_v2_web`（Vue 3 + Vite，固定观察提交 `06ce2b8e4fd4ac0c9e131fb6fb554395f702ca2f`）。该仓库当前没有 license 文件；主人已确认发布问题 issue，但前端 fork、源码 PR 和构建分发仍需等待许可证/授权反馈。

## 决定

1. 三态前端功能从上游源码维护；QQBotForge 不直接手改 `web/dist`。
2. 采用 fork 路线 A：建议 fork 名为 `xmxxc/ElainaBot_v2_web`。fork 的 `main` 跟踪上游 `main`，自有修改使用 feature 分支并以 PR 合并。
3. 上游三态改动与 QQBotForge 专属更新页源码分开提交。更新页源码保留在 fork，是因为上游同步会整体覆盖 `web/dist`，而 #100/#171 的更新页行为必须在每次重建后可重现、可审查。
4. 从已审查的源码 commit 和 `package-lock.json` 执行 `npm ci`、`npm run build`，将构建来源写入 manifest，再把产物同步到 QQBotForge。

## 同步边界

顺序固定为：上游后端 PR → 上游前端三态 PR → fork 同步并保留更新页源码 → 固定源码构建 `dist` → QQBotForge 同步 PR。后端 issue/PR 与前端问题 issue 已发布；前端 fork、源码 PR 和构建分发仍等待许可证/授权反馈。

## 后果

每次产物都有源码 commit、锁文件和构建器证据；构建失败或来源不匹配时停止同步并回滚到上一个已记录产物。前端仓库的 license/授权确认仍是源码 PR、fork 和构建分发的前置。
