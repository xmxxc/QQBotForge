"""Fixtures required by the self-contained send-check tests."""

import pytest


@pytest.fixture
def sample_config_dir(tmp_path):
    """Provide the minimal config tree used when resetting the global config."""
    (tmp_path / 'settings.yaml').write_text(
        """server:\n  host: 0.0.0.0\n  port: 15200\nweb:\n  admin_password: test_pass\nlogging:\n  dir: log\n  insert_interval: 2\n  batch_size: 0\n  retention_days: 5\n  wal_mode: true\npip:\n  auto_install: false\n  mirror: ''\n""",
        encoding='utf-8',
    )
    (tmp_path / 'bot.yaml').write_text(
        """bots:\n  - appid: '123456'\n    secret: test_secret_123\n    robot_qq: '987654321'\n    owner_ids: ['']\n    websocket:\n      enabled: false\n    message:\n      use_markdown: true\n    identity:\n      use_union_id_for_group: false\n      use_union_id_for_channel: false\n    welcome:\n      group_welcome: false\n      new_user_welcome: false\n      friend_add_message: false\n    maintenance:\n      enabled: false\n    dedup:\n      enabled: false\n    blacklist:\n      user_list: []\n      group_list: []\n    non_at_message:\n      enabled: false\n      group_whitelist: []\n      ignore_at_other_bot: false\n""",
        encoding='utf-8',
    )
    return str(tmp_path)
