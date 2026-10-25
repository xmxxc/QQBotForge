# Fix #102 rich-message `send_check` contract

## Scope

基于 `1b524d4`（工作树 `/opt/qqbot/qqbf-101-ci-assets`）修复 docs/plugin-development.md:809 的契约缺口。通用 `reply()`、`send_to_group()`、`send_to_user()`、`send_to_channel()` 和 `post_json()` 在传入媒体、Ark、卡片消息载荷（以及 stream endpoint）并同时提供 `send_check` 时，现在在业务请求前抛出 `TypeError`；不会触发业务 HTTP 请求。专用 `reply_image()`/`reply_ark()`/`reply_card()`/stream API 的既有无 `send_check` 形态未改动。

无 `send_check` 的 rich payload 仍按原路径发送。未引入 #74/#75 业务代码或 CI fixture 修改。

## Changes

- `core/message/_http.py`: 增加统一 rich-payload 判定和拒绝检查；覆盖 `post_json`、`_post_message` 公共边界，并识别 `/stream_messages`。
- `core/message/sender.py`: generic `reply` 与 group/user push 在构建/钩子前拒绝 rich 参数；channel payload 使用统一边界检查。
- `tests/test_send_check.py`: 使用真实 `MessageSender` 入口回归媒体、Ark/card `msg_type`/kwargs、stream endpoint；断言零业务请求；另验证无检查媒体发送兼容。

## Verification

- `uv run pytest -q tests/test_send_check.py` — **30 passed**
- `uv run ruff check core/message/_http.py core/message/sender.py tests/test_send_check.py` — **passed**
- `uv run mypy core/message/_http.py core/message/sender.py` — **passed**（仅既有 untyped-body notes）
- `git diff --check` — **passed**
- `uv run pytest -q` — collection blocked by existing CI-assets checkout issue: `ModuleNotFoundError: No module named 'tests.helpers'` from `tests/test_api_modules_runtime.py`; no failure came from this fix.

## Result

修复提交后 head/tree 信息见父任务回报。
