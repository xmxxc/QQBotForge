// Shipped ES module maintained directly: this repository contains no frontend build sources.
import {aA as h, r as ref, o as onMounted, ag as onUnmounted} from './vendor.js';
import {r as unwrap, i as api} from './index.js';

const Update = {
  __name: 'Update',
  __scopeId: 'data-v-0f90cad8',
  setup() {
    const version = ref({}), check = ref({}), changes = ref([]), progress = ref({});
    const busy = ref(false), error = ref('');
    let disposed = false;
    const reminder = '重启后请确认群成员并使用「群状态 恢复」重建群状态。';
    async function checkUpdate() {
      error.value = '';
      try {
        check.value = unwrap(await api.get('/api/update/check')) || {};
        error.value = check.value.error || '';
      } catch (e) { error.value = e.normalizedMessage || e.message; }
    }
    async function refreshProgress() {
      try { progress.value = unwrap(await api.get('/api/update/progress')) || {}; }
      catch (e) { if (!busy.value) error.value = e.normalizedMessage || e.message; }
    }
    async function pollUpdate() {
      busy.value = true;
      try {
        do {
          await new Promise(resolve => setTimeout(resolve, 1000));
          if (disposed) return;
          await refreshProgress();
        } while (progress.value.is_updating || !['completed', 'failed'].includes(progress.value.stage));
        version.value = unwrap(await api.get('/api/update/version')) || {};
        await checkUpdate();
      } catch (e) { error.value = e.normalizedMessage || e.message; }
      finally { busy.value = false; }
    }
    async function startUpdate() {
      if (busy.value || !check.value.can_update) return;
      busy.value = true;
      error.value = '';
      try {
        await api.post('/api/update/start', {version: check.value.latest_version});
        await pollUpdate();
      } catch (e) { error.value = e.normalizedMessage || e.message; busy.value = false; }
    }
    onMounted(async () => {
      await Promise.all([
        checkUpdate(), refreshProgress(),
        (async () => { try { version.value = unwrap(await api.get('/api/update/version')) || {}; } catch (e) { error.value = e.message; } })(),
        (async () => { try { changes.value = unwrap(await api.get('/api/update/changelog')) || []; } catch (e) { error.value = e.message; } })()
      ]);
      if (progress.value.is_updating) pollUpdate();
    });
    onUnmounted(() => { disposed = true; });
    const stat = (label, value) => h('div', {class: 'ui-stat c-blue'}, [
      h('div', {class: 'ui-stat-label'}, label), h('div', {class: 'ui-stat-val', style: 'overflow-wrap:anywhere;font-size:15px'}, String(value))
    ]);
    return () => h('div', {class: 'upd-page'}, [
      h('div', {class: 'ui-stat-grid upd-stats'}, [
        stat('更新源', 'xmxxc/QQBotForge · personal'),
        stat('当前提交', check.value.current_version || version.value.version || 'unknown'),
        stat('personal 提交', check.value.latest_version || '—'),
        stat('CI 状态', check.value.ci_status || 'unknown'),
        stat('提交差距', check.value.gap == null ? '无法比较' : `${check.value.gap} 个提交`)
      ]),
      h('div', {class: 'upd-card'}, [
        h('div', {class: 'upd-card-header'}, '手动更新'),
        h('div', {class: 'upd-card-actions'}, [
          h('button', {class: 'btn', onClick: checkUpdate, disabled: busy.value || !!progress.value.is_updating}, '检查更新'),
          h('button', {class: 'btn btn-primary', onClick: startUpdate,
            disabled: busy.value || !!progress.value.is_updating || !check.value.can_update}, '更新到最新')
        ]),
        h('p', null, '更新前自动备份 SQLite 数据库，切换后自动重启；WS 验证失败会回滚。'),
        h('p', null, check.value.relation === 'diverged' ? '当前提交与 personal 无后续关系，无法更新。' : ''),
        h('p', null, reminder),
        error.value ? h('div', {class: 'upd-error'}, error.value) : null,
        progress.value.message ? h('div', {class: 'upd-progress-msg'}, progress.value.message) : null,
        progress.value.backup_path ? h('p', null, `数据库备份：${progress.value.backup_path}`) : null
      ]),
      h('div', {class: 'upd-card'}, [
        h('div', {class: 'upd-card-header'}, [
          h('span', {class: 'upd-title'}, 'personal 更新日志'),
          h('a', {href: 'https://github.com/xmxxc/QQBotForge/tree/personal', target: '_blank', rel: 'noopener noreferrer'}, 'GitHub')
        ]),
        h('div', {class: 'upd-log-wrap'}, [
          h('div', {class: 'upd-log-list'}, changes.value.map(commit => h('div', {class: 'upd-log-item', key: commit.full_sha || commit.sha}, [
          h('code', null, commit.sha), h('div', {class: 'upd-log-msg'}, commit.message),
          h('div', {class: 'upd-log-date'}, commit.date)
          ])))
        ])
      ])
    ]);
  }
};
export default Update;
