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


@pytest.mark.skipif(not shutil.which('node'), reason='Node unavailable')
@pytest.mark.parametrize('scenario', ['completed', 'transient', 'idle', 'failed', 'refresh-retry', 'unmount', 'reentry', 'outage', 'available', 'inflight-unmount', 'check-error'])
def test_update_page_restart_refreshes_without_manual_reload(tmp_path, scenario):
    """171-page-restart: render actual module through its API/lifecycle boundary."""
    source = (Path(__file__).parents[1] / 'web/dist/assets/Update.js').read_text()
    source = re.sub(r'import[^;]+;', '', source)
    harness = r"""
const mounted = [], unmounted = [], timers = [];
const ref = value => ({value});
const onMounted = cb => mounted.push(cb);
const onUnmounted = cb => unmounted.push(cb);
const h = (tag, props, children) => ({tag, props, children});
const unwrap = response => response.data;
const setTimeout = cb => timers.push(cb);
const reminder = '重启后请确认群成员并使用「群状态 恢复」重建群状态。';
let phase = 'before', frames = [], statusFailures = 0, holdProgress = false, releaseProgress;
let available = false, dataError = false;
const api = {
  post: async () => { phase = 'after'; return {}; },
  get: async path => {
    if (path.endsWith('/progress')) {
      if (holdProgress) return new Promise(resolve => { releaseProgress = resolve; });
      const frame = frames.length ? frames.shift() : {stage: 'idle', is_updating: false};
      if (frame === 'offline') throw new Error('temporary offline');
      return {data: frame};
    }
    if (phase === 'after' && statusFailures && /\/(version|check)$/.test(path)) {
      statusFailures--; throw new Error('status unavailable');
    }
    if (phase === 'after' && path.endsWith('/check') && dataError) return {data: {error: 'mock fetch failure', can_update: false}};
    return {data: path.endsWith('/check') ? {
      current_version: phase === 'before' ? 'old-sha' : 'new-sha',
      latest_version: 'new-sha', gap: phase === 'before' ? 6 : available ? 2 : 0,
      can_update: phase === 'before' || available, ci_status: 'success'
    } : path.endsWith('/version') ? {version: phase === 'before' ? 'old-sha' : 'new-sha'} : []};
  }
};
"""
    assertions = r"""
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
function buttons(tree) { return tree.children[1].children[1].children; }
function assert(ok, message) { if (!ok) throw new Error(message); }
for (const scenario of [SCENARIO]) {
  phase = 'before'; frames = []; statusFailures = 0; timers.length = 0;
  available = scenario === 'available'; dataError = scenario === 'check-error';
  mounted.length = 0; unmounted.length = 0;
  const render = Update.setup(); await mounted[0]();
  assert(JSON.stringify(render()).includes('old-sha'), 'control: old version visible');
  assert(!buttons(render())[1].props.disabled, 'control: update available');
  const terminal = {stage: scenario === 'failed' ? 'failed' : 'completed', is_updating: false,
    message: (scenario === 'failed' ? 'WS verification failed ' : 'confirmed ') + reminder,
    backup_path: '/mock/backup'};
  frames = scenario === 'idle' ? [{stage: 'idle', is_updating: false}]
    : scenario === 'outage' ? Array(65).fill('offline')
    : [...(scenario === 'transient' ? [...Array(10).fill('offline'), {stage: 'restarting', is_updating: true}, 'offline'] : []), terminal];
  if (scenario === 'refresh-retry') statusFailures = 2;
  holdProgress = scenario === 'inflight-unmount';
  const operation = buttons(render())[1].props.onClick(); await flush();
  if (scenario === 'inflight-unmount') {
    timers.shift()(); await flush();
    assert(!!releaseProgress, 'progress request not in flight');
    unmounted[0]();
    const snapshot = JSON.stringify(render());
    releaseProgress({data: terminal}); await flush();
    assert(JSON.stringify(render()) === snapshot, 'in-flight result mutated unmounted page');
    continue;
  }
  if (scenario === 'unmount') {
    unmounted[0]();
    const snapshot = JSON.stringify(render());
    for (let i = 0; i < 70 && timers.length; i++) { timers.shift()(); await flush(); }
    assert(JSON.stringify(render()) === snapshot, 'unmounted page mutated');
    continue;
  }
  for (let i = 0; i < 70 && timers.length; i++) { timers.shift()(); await flush(); }
  const tree = render(), text = JSON.stringify(tree);
  assert(!buttons(tree)[0].props.disabled, scenario + ': check remains busy');
  if (scenario === 'outage') {
    assert(buttons(tree)[1].props.disabled, 'outage must invalidate update capability');
    assert(text.includes('temporary offline'), 'outage error hidden');
    frames = [{stage: 'idle', is_updating: false}];
    await buttons(tree)[0].props.onClick(); await flush();
    assert(JSON.stringify(render()).includes('new-sha'), 'manual recovery failed');
    continue;
  }
  if (scenario === 'check-error') {
    assert(text.includes('new-sha') && !text.includes('old-sha') && text.includes('无法比较'), 'check error hides current version');
    assert(text.includes('mock fetch failure') && buttons(tree)[1].props.disabled, 'check error capability/message');
    dataError = false; await buttons(tree)[0].props.onClick();
    assert(JSON.stringify(render()).includes('0 个提交'), 'data error recovery failed');
    continue;
  }
  assert(text.includes('new-sha') && text.includes(scenario === 'available' ? '2 个提交' : '0 个提交') && !text.includes('old-sha'), scenario + ': stale version/gap');
  assert(buttons(tree)[1].props.disabled === !available, scenario + ': ignores latest capability');
  if (scenario !== 'idle') {
    assert(text.split(reminder).length - 1 === 1, scenario + ': duplicate reminder');
    assert(text.includes('/mock/backup'), 'backup information lost');
    assert(text.includes(scenario === 'failed' ? 'WS verification failed' : 'confirmed'), 'server message lost');
  } else assert(!text.includes('confirmed'), 'idle must not invent success');
  await operation;
  if (scenario === 'reentry') {
    unmounted[0](); mounted.length = 0;
    const next = Update.setup(); await mounted[0]();
    assert(JSON.stringify(next()).includes('new-sha'), 'reentry stale');
  }
}
"""
    module = tmp_path / 'restart.mjs'
    module.write_text(harness + source + assertions.replace('SCENARIO', repr(scenario)))
    result = subprocess.run(['node', str(module)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
