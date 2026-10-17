# QQBotForge

**English** / [简体中文](README_CN.md)

### Build and extend QQ bots with an asynchronous Python framework.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Upstream](https://img.shields.io/badge/Upstream-ElainaBot_v2-5865f2)](https://github.com/ElainaCore/ElainaBot_v2)

[Quick Start](#quick-start) · [Docker](#docker-installation) · [Configuration](#configuration) · [Plugin Development](#plugin-development) · [Contributing](#contributing)

QQBotForge is a development fork of [ElainaBot v2](https://github.com/ElainaCore/ElainaBot_v2) for building bots on the QQ bot platform. It combines multi-bot connections, message handling, extensible plugins and a browser-based management panel in one Python application.

The fork retains the upstream architecture and plugin interfaces, with English as the default documentation language and a complete [Simplified Chinese README](README_CN.md). Platform features depend on the permissions and capabilities granted to your QQ bot.

## Features

- **Asynchronous networking:** `aiohttp` and `websockets`, with per-bot WebSocket connections or Webhook event delivery.
- **Multi-bot configuration:** independent credentials, owners, message options, maintenance mode and blacklists.
- **Message and event APIs:** group, private and channel events; text, media, Markdown, buttons, templates and interaction callbacks. See the development guide for interface-specific requirements.
- **Hot-reloadable plugins:** decorated handlers, standalone or package-style plugins, lifecycle hooks, configuration and optional dependency installation.
- **Web management:** bot/configuration management, live logs, system statistics, plugin management and a database browser.
- **Plugin marketplace:** browse, install and update plugins from the upstream [Elaina-plugins](https://github.com/ElainaCore/Elaina-plugins) catalog.
- **Modular services:** SQLite-backed storage, scheduled tasks and an image-hosting module with multiple backends.

## Quick Start

Requirements: **Python 3.11+**, **Git**, and a QQ bot application's **APPID** and **Secret** to connect a bot. The Web panel can start before credentials are configured.

```bash
git clone https://github.com/xmxxc/QQBotForge.git
cd QQBotForge
python -m venv .venv
```

Activate the virtual environment:

```bash
# Linux / macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install dependencies and start:

```bash
python -m pip install -r requirements.txt
python main.py
```

Open the [Web panel](http://localhost:5200/web/). The initial administrator password is `admin`; change `web.admin_password` in `config/settings.yaml` before exposing the service. Add APPID and Secret through the panel's configuration editor, or edit `config/bot.yaml`. Confirm the bot connects before testing commands.

Configuration files are created from the corresponding `*.example.yaml` files when first loaded. You may copy and edit the examples before the first start. See [Configuration](#configuration) for settings and connection modes.

## Docker Installation

Requirements: Docker Engine and Docker Compose v2. Run these POSIX shell commands from the cloned QQBotForge directory.

### Build the QQBotForge checkout

Docker assets are maintained on the upstream `ci-assets` branch. Download the Dockerfile, its ignore file and entrypoint, then build the local source:

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

Visit [localhost:5200/web/](http://localhost:5200/web/) and configure the password and bot credentials as above. This port mapping binds to the Docker host's loopback interface; remote access and Webhook delivery require a suitable reverse proxy or port mapping.

### Use the upstream prebuilt image

To run the upstream ElainaBot distribution instead of building this fork:

```bash
mkdir -p docker
curl -fL https://raw.githubusercontent.com/ElainaCore/ElainaBot_v2/ci-assets/docker/compose.yml -o docker/compose.yml
docker compose -f docker/compose.yml up -d
```

This runs **`elainabot/elainabot:latest`**, published by upstream, without QQBotForge-specific changes. The upstream Compose file publishes port `5200` on all host interfaces. Set the administrator password and adjust network access before deployment.

### Persistent data

Both examples mount these host directories. Keep them when replacing containers and back them up before upgrades.

| Directory | Contents |
| --- | --- |
| `config/` | Bot credentials, framework settings and message templates |
| `plugins/` | System/installed plugins and plugin-local data |
| `modules/` | Modules and module-local configuration/data |
| `data/` | Databases, logs, media and panel state |

## Configuration

| Example → local file | Purpose |
| --- | --- |
| [`config/settings.example.yaml`](config/settings.example.yaml) → `config/settings.yaml` | Server, panel, logging, statistics, dependency installation and auto-update settings |
| [`config/bot.example.yaml`](config/bot.example.yaml) → `config/bot.yaml` | The `bots` list: credentials, owners, connection mode and per-bot behavior |
| [`config/templates.example.yaml`](config/templates.example.yaml) → `config/templates.yaml` | Message templates |
| [`.env.example`](.env.example) → `.env` | Optional environment variables; YAML values support `${VARIABLE:default}` substitution |

- **Panel:** `server.port` defaults to `5200`; `web.framework_name` controls the display name. Login reads `web.admin_password` from `settings.yaml`. The `ADMIN_PASSWORD` / `ACCESS_TOKEN` entries in the upstream `.env.example` do not override authentication by themselves; configure YAML directly or use an explicit YAML environment placeholder.
- **WebSocket:** enabled by default per bot via `websocket.enabled`. Provide APPID and Secret; the framework obtains the gateway unless `websocket.custom_url` is set.
- **Webhook:** disable that bot's `websocket.enabled` and configure its reachable callback URL as `https://<your-domain>/?appid=<APPID>`. The event receiver is at the server root, separate from `/web/`. Set up public HTTPS according to platform requirements.
- **Secrets:** keep credentials in ignored local configuration or environment variables. Never commit `.env`, `config/bot.yaml`, session files or runtime databases.
- **Updates:** new configurations enable `auto_update.enabled: true` and check `ElainaCore/ElainaBot_v2` every 600 seconds. The updater backs up covered files to `data/backup/`, replaces eligible files and restarts the application. It does not perform a Git merge; upstream files may overwrite fork customizations without conflict markers. Restore or reconcile changes manually from your Git history or backups when needed, and separately back up plugins/modules. Existing installations can enable this in `config/settings.yaml`.

The Python distribution name `elainabot`, `core`/`web` imports, environment variable names, protocol identifiers and upstream integration endpoints remain compatible with ElainaBot v2. Existing configurations keep their selected display name; new configurations use QQBotForge.

## Project Structure

```text
QQBotForge/
├── main.py          # Application entrypoint
├── config/          # Versioned examples; local runtime configuration
├── core/            # Networking, bots, messages, plugins, services and storage
├── plugins/         # System and extension plugins
├── modules/         # Optional modules, including image hosting
├── web/             # Panel backend and bundled frontend build
└── docs/            # Plugin and module development guides
```

## Plugin Development

Start with the [plugin development guide](docs/plugin-development.md) (currently in Chinese). It covers layout, metadata, configuration, lifecycle hooks, events, message APIs, group management and panel extensions.

Create `plugins/hello/main.py`:

```python
from core.plugin.decorators import handler


@handler(r'^hello$', name='Hello', desc='Reply with a greeting')
async def say_hello(event, match):
    await event.reply('Hello from QQBotForge!')
```

The loader scans first-level directories under `plugins/` and reloads changed plugin files. Package plugins use `main.py`; standalone plugins can be placed in `plugins/alone/`. See the [bundled example](plugins/alone/示例插件.py) and [image-hosting guide](docs/image-hosting.md) for more examples.

The marketplace continues to use [ElainaCore/Elaina-plugins](https://github.com/ElainaCore/Elaina-plugins). Submit catalog entries there; report QQBotForge integration issues in this fork.

## Contributing

1. Open an [issue](https://github.com/xmxxc/QQBotForge/issues) with the problem or proposal, reproduction steps and redacted logs where relevant.
2. Create a branch from `main` and keep changes focused. Preserve plugin APIs, import paths and deployment behavior unless a migration is explicitly discussed.
3. Verify affected behavior and keep both READMEs consistent. Follow the Ruff and mypy settings in [`pyproject.toml`](pyproject.toml).
4. Submit a [pull request](https://github.com/xmxxc/QQBotForge/pulls) describing the purpose and validation results.

Inherited CI runs Ruff, mypy and pytest using test assets maintained on `ci-assets` rather than `main`. Retain the MIT license and existing copyright notices in derived work.

## Acknowledgements and License

QQBotForge is forked from **[ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)**. Thanks to its original authors and contributors for the framework, documentation, bundled assets and plugin ecosystem. Upstream community group: [164178653](https://qm.qq.com/q/BDE32XRCbQ).

The English-first README and top-of-page language switch follow the layout approach used by [NextChat](https://github.com/ChatGPTNextWeb/NextChat).

Licensed under the [MIT License](LICENSE). The original notice, **Copyright (c) 2025 ElainaBot**, is retained unchanged.
