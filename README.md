# QQBotForge CI 资源

这是一个独立的孤儿分支，用于在应用源代码分支之外保存测试套件和 Docker
部署资源。

- `tests/`：CI 使用的测试套件。
- `docker/`：Dockerfile、启动脚本和 Compose 配置。

工作流定义在 `main` 和 `personal` 的 `.github/workflows/` 中：`ci.yml` 与
`docker-publish.yml` 先检出触发运行的准确源代码提交，再从本分支的固定提交
（`CI_ASSETS_SHA`）取出 `tests/` 或 `docker/`。修改本分支后，需要在这两个工作流里
更新 `CI_ASSETS_SHA` 才会生效。请保持分支名称 `ci-assets` 稳定。

镜像发布到 `ghcr.io/xmxxc/qqbotforge`。使用已发布镜像部署：

```bash
docker compose -f docker/compose.yml up -d
```

如需在本地构建源代码镜像，请先克隆 `main`，再将 `docker/` 中的四个文件复制到该
检出目录的 `.ci/` 文件夹，最后运行：

```bash
docker compose -f .ci/compose.build.yml up -d --build
```
