# QQBotForge

[English](README.md) / **简体中文**

### 使用异步 Python 框架，构建并扩展你的 QQ 机器人。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Upstream](https://img.shields.io/badge/Upstream-ElainaBot_v2-5865f2)](https://github.com/ElainaCore/ElainaBot_v2)

[快速开始](#快速开始) · [Docker 部署](#docker-部署) · [配置说明](#配置说明) · [插件开发](#插件开发) · [参与贡献](#参与贡献)

QQBotForge 是基于 [ElainaBot v2](https://github.com/ElainaCore/ElainaBot_v2) 二次开发的 QQ 机器人框架，将多机器人连接、消息处理、插件扩展与浏览器管理面板整合在一个 Python 应用中。

本 Fork 延续上游架构与插件接口，以[英文首页](README.md)为默认入口，并提供完整中文说明。QQ 平台功能的实际可用范围取决于机器人获批的权限与接口能力。

## 功能特性

- **异步网络架构：** 基于 `aiohttp` 与 `websockets`，支持为每个机器人使用 WebSocket 连接或 Webhook 事件推送。
- **多机器人配置：** 各机器人独立配置凭据、主人、消息选项、维护模式与黑名单。
- **消息与事件 API：** 提供群聊、私聊、频道事件，以及文本、媒体、Markdown、按钮、模板与交互回调封装；接口适用条件见开发文档。
- **插件热重载：** 支持装饰器处理器、单文件与包式插件、生命周期钩子、插件配置及可选依赖安装。
- **Web 管理面板：** 管理机器人与配置，查看实时日志、系统统计，管理插件并浏览数据库。
- **插件市场：** 从上游 [Elaina-plugins](https://github.com/ElainaCore/Elaina-plugins) 目录浏览、安装和更新插件。
- **模块化服务：** 提供 SQLite 存储、定时任务，以及支持多种后端的图床模块。

## 快速开始

要求：**Python 3.11+**、**Git**；连接机器人还需要 QQ 机器人应用的 **APPID** 与 **Secret**。未填写凭据时也可以先启动 Web 面板。

```bash
git clone https://github.com/xmxxc/QQBotForge.git
cd QQBotForge
python -m venv .venv
```

激活虚拟环境：

```bash
# Linux / macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

安装依赖并启动：

```bash
python -m pip install -r requirements.txt
python main.py
```

打开 [Web 面板](http://localhost:5200/web/)。初始管理员密码为 `admin`；对外开放服务前，请修改 `config/settings.yaml` 中的 `web.admin_password`。通过面板配置编辑器填写 APPID 与 Secret，也可以编辑 `config/bot.yaml`。确认机器人连接成功后，再测试命令。

配置文件会在首次加载时从对应的 `*.example.yaml` 自动创建，也可以在首次启动前自行复制示例并修改。连接模式与设置详情见[配置说明](#配置说明)。

## Docker 部署

要求：Docker Engine 与 Docker Compose v2。以下命令使用 POSIX Shell，在已克隆的 QQBotForge 项目目录中执行。

### 构建 QQBotForge 源码

Docker 资源由上游 `ci-assets` 分支维护。下载 Dockerfile、构建忽略文件和入口脚本后，构建当前本地源码：

```bash
mkdir -p .ci
curl -fL https://raw.githubusercontent.com/ElainaCore/ElainaBot_v2/ci-assets/docker/Dockerfile -o .ci/Dockerfile
curl -fL https://raw.githubusercontent.com/ElainaCore/ElainaBot_v2/ci-assets/docker/Dockerfile.dockerignore -o .ci/Dockerfile.dockerignore
curl -fL https://raw.githubusercontent.com/ElainaCore/ElainaBot_v2/ci-assets/docker/entrypoint.sh -o .ci/entrypoint.sh
docker build -f .ci/Dockerfile -t qqbotforge:local .
docker run -d \
  --name qqbotforge \
  -p 127.0.0.1:5200:5200 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/plugins:/app/plugins" \
  -v "$(pwd)/modules:/app/modules" \
  -v "$(pwd)/data:/app/data" \
  --restart unless-stopped \
  qqbotforge:local
```

打开 [localhost:5200/web/](http://localhost:5200/web/)，按上文设置密码与机器人凭据。此端口映射仅绑定 Docker 宿主机的本地回环接口；远程访问与 Webhook 推送需要配置合适的反向代理或端口映射。

### 使用上游预构建镜像

如果需要运行上游 ElainaBot 发行版，可直接使用上游发布的 Compose 文件：

```bash
mkdir -p docker
curl -fL https://raw.githubusercontent.com/ElainaCore/ElainaBot_v2/ci-assets/docker/compose.yml -o docker/compose.yml
docker compose -f docker/compose.yml up -d
```

该方式运行由上游发布的 **`elainabot/elainabot:latest`**，不包含 QQBotForge 的专属改动。上游 Compose 文件会将 `5200` 端口发布到宿主机全部网络接口，部署前请设置管理员密码并调整网络访问范围。

### 数据持久化

两种示例均挂载下列宿主机目录。替换容器时请保留它们，升级前进行备份。

| 目录 | 内容 |
| --- | --- |
| `config/` | 机器人凭据、框架设置和消息模板 |
| `plugins/` | 系统插件、已安装插件及插件本地数据 |
| `modules/` | 模块及模块本地配置、数据 |
| `data/` | 数据库、日志、媒体与面板状态 |

## 配置说明

| 示例 → 本地文件 | 用途 |
| --- | --- |
| [`config/settings.example.yaml`](config/settings.example.yaml) → `config/settings.yaml` | 服务器、面板、日志、统计、依赖安装及自动更新 |
| [`config/bot.example.yaml`](config/bot.example.yaml) → `config/bot.yaml` | `bots` 列表：凭据、主人、连接模式与各机器人行为 |
| [`config/templates.example.yaml`](config/templates.example.yaml) → `config/templates.yaml` | 消息模板 |
| [`.env.example`](.env.example) → `.env` | 可选环境变量；YAML 值支持 `${VARIABLE:default}` 替换 |

- **面板：** `server.port` 默认为 `5200`，`web.framework_name` 控制展示名称。登录密码读取自 `settings.yaml` 的 `web.admin_password`。上游 `.env.example` 中的 `ADMIN_PASSWORD` / `ACCESS_TOKEN` 本身不会覆盖认证配置，请直接配置 YAML，或显式在 YAML 中引用环境变量。
- **WebSocket：** 每个机器人默认启用 `websocket.enabled`。填写 APPID 和 Secret 后自动获取网关，也可以通过 `websocket.custom_url` 指定地址。
- **Webhook：** 关闭对应机器人的 `websocket.enabled`，设置可达的回调地址 `https://<your-domain>/?appid=<APPID>`。事件接收端点位于服务器根路径，与 `/web/` 独立。配置公网 HTTPS 时需满足平台要求。
- **凭据：** 真实凭据保存在被忽略的本地配置或环境变量中。不要提交 `.env`、`config/bot.yaml`、会话文件或运行数据库。
- **更新：** 新配置默认启用 `auto_update.enabled: true`，每 600 秒检查 `ElainaCore/ElainaBot_v2`。更新器将覆盖范围内的文件备份至 `data/backup/`，替换符合更新规则的文件并重启应用。该流程不执行 Git 合并，上游文件可能直接覆盖 Fork 定制而不产生冲突标记；需要时从 Git 历史或备份手动恢复、协调改动，插件与模块请另行备份。已有部署可在 `config/settings.yaml` 中开启。

为保持 ElainaBot v2 兼容性，Python 分发名称 `elainabot`、`core` / `web` 导入路径、环境变量名、协议标识及上游集成端点继续沿用。已有配置保留原有展示名称，新配置默认显示 QQBotForge。

## 项目结构

```text
QQBotForge/
├── main.py          # 应用入口
├── config/          # 受版本控制的示例与本地运行配置
├── core/            # 网络、机器人、消息、插件、服务和存储
├── plugins/         # 系统与扩展插件
├── modules/         # 可选模块，包含图床
├── web/             # 面板后端及内置前端构建产物
└── docs/            # 插件与模块开发文档
```

## 插件开发

请先阅读[插件开发文档](docs/plugin-development.md)，了解目录结构、元数据、配置、生命周期、事件、消息 API、群管理和面板扩展。

创建 `plugins/hello/main.py`：

```python
from core.plugin.decorators import handler


@handler(r'^hello$', name='Hello', desc='回复一句问候')
async def say_hello(event, match):
    await event.reply('Hello from QQBotForge!')
```

加载器扫描 `plugins/` 下的一级目录，文件修改后会触发热重载。包式插件使用 `main.py` 作为入口，单文件插件可放在 `plugins/alone/` 中。更多示例见[内置示例插件](plugins/alone/示例插件.py)和[图床模块文档](docs/image-hosting.md)。

插件市场继续使用 [ElainaCore/Elaina-plugins](https://github.com/ElainaCore/Elaina-plugins)。插件收录请向该仓库提交，QQBotForge 集成问题请在本 Fork 反馈。

## 参与贡献

1. 在 [Issues](https://github.com/xmxxc/QQBotForge/issues) 描述问题或建议，尽可能附上复现步骤与脱敏日志。
2. 从 `main` 创建分支，保持改动聚焦。保留插件 API、导入路径和部署行为；需要迁移时先讨论方案。
3. 验证受影响的行为，保持中英文 README 同步，遵循 [`pyproject.toml`](pyproject.toml) 中的 Ruff 与 mypy 配置。
4. 提交 [Pull Request](https://github.com/xmxxc/QQBotForge/pulls)，说明改动目的与验证结果。

继承的 CI 运行 Ruff、mypy 与 pytest，测试资源维护在 `ci-assets` 分支而非 `main`。衍生作品请保留 MIT License 和现有版权声明。

## 上游致谢与许可证

QQBotForge Fork 自 **[ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)**。感谢原作者及贡献者提供框架、文档、内置资源和插件生态。上游交流群：[164178653](https://qm.qq.com/q/BDE32XRCbQ)。

英文主首页与顶部语言切换参考了 [NextChat](https://github.com/ChatGPTNextWeb/NextChat) 的布局方式。

项目采用 [MIT License](LICENSE)，完整保留原始版权声明：**Copyright (c) 2025 ElainaBot**。
