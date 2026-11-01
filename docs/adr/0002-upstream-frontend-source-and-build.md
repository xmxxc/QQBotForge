# ADR 0002：前端上游源码与构建来源

- 状态：已决定；后端已发布，前端源码/构建路线等待许可证反馈
- 日期：2026-10-31
- 关联：#158

## 背景

QQBotForge 当前随仓库同步 `web/dist`，但三态连接显示和更新页维护需要可读的 Vue 源码。公开上游源码为 `ElainaCore/ElainaBot_v2_web`（Vue 3 + Vite，固定观察提交 `06ce2b8e4fd4ac0c9e131fb6fb554395f702ca2f`）。该仓库当前没有 license 文件；主人已确认发布问题 issue，但前端 fork、源码 PR 和构建分发仍需等待许可证/授权反馈。

## 决定

1. 三态前端功能从上游源码维护；QQBotForge 不直接手改 `web/dist`。
2. 当前前端沟通仅为问题 issue，由上游决定三态如何实现。fork 路线 A 保留为候选方案，收到许可证回复后，再由主人决定是否启用；许可证答复本身不代表继续 fork 或前端代码 PR 的授权。若启用，建议 fork 名为 `xmxxc/ElainaBot_v2_web`，`main` 跟踪上游 `main`，自有修改使用 feature 分支并以 PR 合并。
3. 若主人决定启用该方案，上游三态改动与 QQBotForge 专属更新页源码分开提交。更新页源码保留在 fork，是因为上游同步会整体覆盖 `web/dist`，而 #100/#171 的更新页行为必须在每次重建后可重现、可审查。
4. 若方案启用，从已审查的源码 commit 和 `package-lock.json` 执行 `npm ci`、`npm run build`，将构建来源写入 manifest，再把产物同步到 QQBotForge。

## 同步边界

当前路线为后端 issue/PR 与前端问题 issue，三者已发布。收到前端许可证回复后，由主人重新决定是否启用 fork 路线；前端实现方式由上游决定。若启用 fork 路线，源码同步、保留更新页、固定源码构建 `dist`、QQBotForge 同步 PR 按顺序推进，具体前端代码提交安排需在该决定中明确。

## 后果

每次产物都有源码 commit、锁文件和构建器证据；构建失败或来源不匹配时停止同步并回滚到上一个已记录产物。前端仓库的 license/授权确认仍是源码 PR、fork 和构建分发的前置。
