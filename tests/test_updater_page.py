"""Render the shipped ES module with Vue stubs and verify its visible contract."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node unavailable')
def test_update_page_shows_commit_ci_gap_and_manual_action(tmp_path):
    asset = Path(__file__).parents[1] / 'web/dist/assets/Update.js'
    source = asset.read_text()

    source = re.sub(r'import[^;]+;', '', source)
    module = tmp_path / 'Update.mjs'
    module.write_text(
        """
const callbacks = [];
const ref = value => ({value});
const onMounted = cb => callbacks.push(cb);
const onUnmounted = () => {};
const h = (tag, props, children) => ({tag, props, children});
const unwrap = response => response.data;
const api = {get: async path => ({data: path.endsWith('/check') ? {
    source: {repository: 'xmxxc/QQBotForge', branch: 'personal'}, current_version: 'abc12345',
    latest_version: 'def67890', gap: 3, ci_status: 'pending', can_update: false, has_update: true
} : path.endsWith('/version') ? {version: 'abc12345'} : path.endsWith('/changelog') ? Array.from({length: 20}, (_, i) => ({sha: 'commit' + i, message: 'Log entry ' + i, date: '2026-10-31'})) : []})};
"""
        + source
        + """
const render = Update.setup();
await Promise.all(callbacks.map(cb => cb()));
const tree = render();
const text = JSON.stringify(tree);
for (const expected of ['personal', 'def67890', 'pending', '差距', '3', '检查更新', '更新到最新']) {
    if (!text.includes(expected)) throw new Error('missing ' + expected);
}
if (text.includes('镜像选择') || text.includes('上传更新包') || text.includes('跳过备份')) throw new Error('obsolete controls');
const logCard = tree.children.find(node => node && node.props && node.props.class === 'upd-card' && JSON.stringify(node).includes('personal 更新日志'));
const logWrap = logCard && logCard.children.find(node => node && node.props && node.props.class === 'upd-log-wrap');
if (!logWrap || !logWrap.children || logWrap.children[0].props.class !== 'upd-log-list') throw new Error('log list is not scoped by wrapper');
if (logWrap.children[0].children.length !== 20 || !text.includes('Log entry 19')) throw new Error('missing changelog entries');
function visit(node) {
    if (!node || typeof node !== 'object') return;
    if (node.tag === 'button' && node.children === '更新到最新' && node.props.disabled !== true) throw new Error('unsafe enabled update');
    for (const value of Object.values(node)) {
        if (Array.isArray(value)) value.forEach(visit); else visit(value);
    }
}
visit(tree);
"""
    )
    result = subprocess.run(['node', str(module)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_update_log_has_desktop_height_and_contained_scrolling():
    css = (Path(__file__).parents[1] / 'web/dist/assets/Update.css').read_text()
    # Desktop must reserve space even when the log card is not a flex container.
    desktop = css.split('@media')[0]
    wrap = re.search(r'\.upd-log-wrap\[data-v-0f90cad8\]\{([^}]+)\}', desktop).group(1)
    height = re.search(r'min-height:(\d+)px', wrap)
    assert height and int(height.group(1)) >= 320, 'desktop changelog needs a usable minimum height'
    assert 'position:relative' in wrap
    log = re.search(r'\.upd-log-list\[data-v-0f90cad8\]\{([^}]+)\}', desktop).group(1)
    assert 'position:absolute' in log and 'inset:0' in log and 'overflow-y:auto' in log
