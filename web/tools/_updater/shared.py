"""固定框架更新源，以及旧插件市场设置读取接口。"""

import logging

from web.tools._market.mirrors import load_github_mirror  # noqa: F401  # legacy import compatibility

log = logging.getLogger('ElainaBot.web.updater')
GITHUB_REPO = 'xmxxc/QQBotForge'
GITHUB_BRANCH = 'personal'
GITHUB_API_URL = f'https://api.github.com/repos/{GITHUB_REPO}'
GITHUB_URL = f'https://github.com/{GITHUB_REPO}'
