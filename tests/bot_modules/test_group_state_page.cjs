const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {setImmediate: tick} = require('node:timers/promises');

// Run the shipped page script, replacing only the DOM and network boundaries.
async function page() {
    const html = fs.readFileSync(path.join(__dirname, '../../modules/group_state/page.html'), 'utf8');
    const nodes = {};
    for (const [, id] of html.matchAll(/id="([^"]+)"/g)) {
        nodes[id] = {
            value: id === 'gs-group' ? 'group-a' : '', textContent: '', disabled: false, options: [],
            append(option) {
                this.options.push(option);
                if (!this.value) this.value = option.value;
            },
        };
    }
    const requests = [];
    vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {
        document: {getElementById: id => nodes[id], createElement: () => ({})}, URLSearchParams,
        fetch: (url, options) => url === '/api/bots'
            ? Promise.resolve({json: async () => ({data: {bots: [{appid: 'bot-a'}, {appid: 'bot-b'}]}})})
            : new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
    });
    await tick();
    return {
        nodes, requests,
        output: () => nodes['gs-output'].textContent,
        async reply(request, data) {
            request.resolve({ok: true, json: async () => ({success: true, data})});
            await tick();
        },
    };
}

test('an earlier GET cannot restore verified facts after a failed refresh', async () => {
    const host = await page();
    const earlier = host.requests[0];
    host.nodes['gs-refresh'].onclick();
    const latest = {group: 'group-a', version: 4, reliable: false, status: 'historical', reason: 'permission_denied'};
    await host.reply(host.requests.at(-1), latest);
    assert.deepEqual(JSON.parse(host.output()), latest);
    await host.reply(earlier, {bot: 'bot-a', version: 3, groups: [{group: 'group-a', reliable: true}]});
    assert.deepEqual(JSON.parse(host.output()), latest);
});

test('changing Bot clears old facts and loads only the selected Bot', async () => {
    const host = await page();
    await host.reply(host.requests[0], {bot: 'bot-a', groups: [{reliable: true}]});
    host.nodes['gs-load'].onclick();
    const earlier = host.requests.at(-1);
    host.nodes['gs-bot'].value = 'bot-b';
    host.nodes['gs-bot'].onchange?.();
    assert.equal(host.output(), '');
    const selected = host.requests.at(-1);
    assert.match(selected.url, /bot=bot-b$/);
    await host.reply(earlier, {bot: 'bot-a', groups: [{reliable: true}]});
    assert.equal(host.output(), '');
    const latest = {bot: 'bot-b', groups: []};
    await host.reply(selected, latest);
    assert.deepEqual(JSON.parse(host.output()), latest);
});

test('returning to a Bot cannot revive a request from the earlier selection', async () => {
    const host = await page();
    const earlierA = host.requests[0];
    host.nodes['gs-bot'].value = 'bot-b';
    host.nodes['gs-bot'].onchange();
    const earlierB = host.requests.at(-1);
    host.nodes['gs-bot'].value = 'bot-a';
    host.nodes['gs-bot'].onchange();
    const latest = {bot: 'bot-a', version: 8, groups: [{reliable: false}]};
    await host.reply(host.requests.at(-1), latest);
    await host.reply(earlierA, {bot: 'bot-a', version: 3, groups: [{reliable: true}]});
    await host.reply(earlierB, {bot: 'bot-b', groups: []});
    assert.deepEqual(JSON.parse(host.output()), latest);
});

for (const failure of ['network', 'http', 'json']) {
    test(`a late ${failure} error cannot clear a newer successful result`, async () => {
        const host = await page();
        const earlier = host.requests[0];
        host.nodes['gs-load'].onclick();
        const latest = {bot: 'bot-a', version: 8, groups: []};
        await host.reply(host.requests.at(-1), latest);
        const status = host.nodes['gs-status'].textContent;
        if (failure === 'network') earlier.reject(new Error('old network failure'));
        else earlier.resolve({
            ok: false,
            json: async () => {
                if (failure === 'json') throw new Error('old invalid JSON');
                return {success: false, error: 'old HTTP error'};
            },
        });
        await tick();
        assert.deepEqual(JSON.parse(host.output()), latest);
        assert.equal(host.nodes['gs-status'].textContent, status);
    });
}

test('starting a request clears old verified facts and current errors remain visible', async () => {
    const host = await page();
    await host.reply(host.requests[0], {bot: 'bot-a', groups: [{reliable: true}]});
    host.nodes['gs-refresh'].onclick();
    assert.equal(host.output(), '');
    assert.equal(host.nodes['gs-status'].textContent, '正在读取…');
    host.requests.at(-1).reject(new Error('current network failure'));
    await tick();
    assert.equal(host.output(), '');
    assert.equal(host.nodes['gs-status'].textContent, 'current network failure');
});

test('a late complete roster cannot overwrite a newer failed member verification', async () => {
    const host = await page();
    host.nodes['gs-roster'].onclick();
    const earlier = host.requests.at(-1);
    host.nodes['gs-verify'].onclick();
    const latest = {group: 'group-a', reliable: false, state: 'unknown', reason: 'permission_denied'};
    await host.reply(host.requests.at(-1), latest);
    await host.reply(earlier, {group: 'group-a', complete: true, version: 3});
    assert.deepEqual(JSON.parse(host.output()), latest);
});

test('a late member page from an earlier Bot cannot enable the next page', async () => {
    const host = await page();
    host.nodes['gs-members'].onclick();
    const earlier = host.requests.at(-1);
    host.nodes['gs-bot'].value = 'bot-b';
    host.nodes['gs-bot'].onchange();
    await host.reply(host.requests.at(-1), {bot: 'bot-b', groups: []});
    await host.reply(earlier, {group: 'group-a', version: 2, generation: 'g', members: [{member: 'old'}], next_cursor: '1.key'});
    assert.equal(host.nodes['gs-next'].disabled, true);
    assert.deepEqual(JSON.parse(host.output()), {bot: 'bot-b', groups: []});
});

test('editing the group retires the earlier member page and its late response', async () => {
    const host = await page();
    host.nodes['gs-members'].onclick();
    await host.reply(host.requests.at(-1), {group: 'group-a', version: 2, generation: 'g', members: [], next_cursor: '1.key'});
    assert.equal(host.nodes['gs-next'].disabled, false);
    host.nodes['gs-members'].onclick();
    const earlier = host.requests.at(-1);
    host.nodes['gs-group'].value = 'group-b';
    host.nodes['gs-group'].oninput();
    assert.equal(host.nodes['gs-next'].disabled, true);
    await host.reply(earlier, {group: 'group-a', version: 2, generation: 'g', members: [], next_cursor: '1.key'});
    assert.equal(host.nodes['gs-next'].disabled, true);
    assert.equal(host.output(), '');
});

test('the next page carries the cursor, version and generation of the current group page', async () => {
    const host = await page();
    host.nodes['gs-members'].onclick();
    await host.reply(host.requests.at(-1), {group: 'group-a', version: 2, generation: 'g', members: [], next_cursor: '1.key'});
    host.nodes['gs-next'].onclick();
    const query = new URL(host.requests.at(-1).url, 'http://host').searchParams;
    assert.deepEqual(Object.fromEntries(query), {bot: 'bot-a', group: 'group-a', limit: '30', generation: 'g', version: '2', cursor: '1.key'});
});

test('editing a group clears already displayed results immediately', async () => {
    const host = await page();
    host.nodes['gs-members'].onclick();
    await host.reply(host.requests.at(-1), {group: 'group-a', members: [{member: 'old'}], next_cursor: '1.key'});
    host.nodes['gs-group'].value = 'group-b';
    host.nodes['gs-group'].oninput();
    assert.equal(host.output(), '');
    assert.equal(host.nodes['gs-next'].disabled, true);
});

test('returning to a group cannot restore a request from its earlier selection', async () => {
    const host = await page();
    host.nodes['gs-members'].onclick();
    const earlier = host.requests.at(-1);
    host.nodes['gs-group'].value = 'group-b';
    host.nodes['gs-group'].oninput();
    host.nodes['gs-group'].value = 'group-a';
    host.nodes['gs-group'].oninput();
    await host.reply(earlier, {group: 'group-a', members: [{member: 'old'}], next_cursor: '1.key'});
    assert.equal(host.output(), '');
    assert.equal(host.nodes['gs-next'].disabled, true);
});

test('the status line names complete, in-progress, unknown and historical facts in Chinese', async () => {
    const host = await page();
    await host.reply(host.requests[0], {bot: 'bot-a', recovery: {ready: true}, groups: [
        {group: 'g1', reliable: true, status: 'verified', reason: '', roster: {complete: true, reason: ''}},
        {group: 'g2', reliable: false, status: 'pending', reason: 'refreshing', roster: {complete: false, reason: 'roster_refreshing'}},
        {group: 'g3', reliable: false, status: 'historical', reason: 'process_restart', roster: {complete: false, reason: 'not_verified'}},
    ]});
    const status = host.nodes['gs-status'].textContent;
    assert.match(status, /g1 可靠（已核验），名册完整/);
    assert.match(status, /g2 处理中（正在刷新），名册处理中（正在核验名册）/);
    assert.match(status, /g3 历史（重启或重载后需重新核验），名册未知（尚未核验）/);
    assert.doesNotMatch(status, /process_restart|not_verified/);
    host.nodes['gs-roster'].onclick();
    await host.reply(host.requests.at(-1), {complete: false, reason: 'timeout', members: []});
    assert.equal(host.nodes['gs-status'].textContent, '已更新：名册核验：不完整，原因：查询超时');
    host.nodes['gs-verify'].onclick();
    await host.reply(host.requests.at(-1), {member: 'm1', state: 'unknown', reliable: false, status: 'pending', reason: 'permission_denied'});
    assert.equal(host.nodes['gs-status'].textContent, '已更新：单人核验：m1：无权限（平台无权限），角色未知，是否 Bot 未知');
});

test('summaries keep unknown fields unknown and never report a pending recovery as success', async () => {
    const host = await page();
    await host.reply(host.requests[0], {bot: 'bot-a', recovery: {ready: false, reason: 'incomplete_event'}, groups: [
        {group: 'g1', reliable: false, status: 'verified', reason: '', continuity_reason: 'recovery_pending', checked_at: null,
            bot_state: {member_role: null}, roster: {complete: false, reason: 'recovery_pending'}},
        {group: 'g2', reliable: false, status: 'historical', reason: 'persistence_failed', roster: {complete: false, reason: 'persistence_failed'}},
        {group: 'g3', reliable: false, status: 'historical', reason: 'future_code', roster: {complete: false, reason: 'state_changed'}},
    ]});
    const text = host.nodes['gs-summary'].textContent;
    assert.match(text, /恢复基线：未就绪（需重建，请点击“重建恢复基线”；原因：事件字段不完整）/);
    assert.match(text, /g1：暂停使用（恢复基线未完成）；历史记录：Bot 在群（角色未知），主动消息未知；最近查询 未查询/);
    assert.match(text, /g2：失败（保存失败）/);
    assert.match(text, /g3：历史（其他原因（future_code））；Bot 在群状态未知/);
    assert.doesNotMatch(text, /可靠|已就绪/);
    // Owner details keep native IDs and raw fields.
    assert.equal(JSON.parse(host.output()).groups[2].reason, 'future_code');
    host.nodes['gs-rebuild'].onclick();
    await host.reply(host.requests.at(-1), {ready: false});
    assert.match(host.nodes['gs-status'].textContent, /恢复基线重建：未完成/);
});

test('a late response cannot replace the readable summary of the current Bot', async () => {
    const host = await page();
    const earlier = host.requests[0];
    host.nodes['gs-bot'].value = 'bot-b';
    host.nodes['gs-bot'].onchange();
    await host.reply(host.requests.at(-1), {bot: 'bot-b', recovery: {ready: true}, groups: [{group: 'gb', reliable: false, reason: 'timeout', state: 'unknown', roster: {}}]});
    await host.reply(earlier, {bot: 'bot-a', recovery: {ready: true}, groups: [{group: 'ga', reliable: true, roster: {complete: true}}]});
    assert.match(host.nodes['gs-summary'].textContent, /gb：失败（查询超时）/);
    assert.doesNotMatch(host.nodes['gs-summary'].textContent, /ga/);
    host.nodes['gs-group'].value = 'group-b';
    host.nodes['gs-group'].oninput();
    assert.equal(host.nodes['gs-summary'].textContent, '');
});

test('the status line labels the shared cases exactly like the QQ replies', async () => {
    const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'group_state_labels.json'), 'utf8'));
    const host = await page();
    await host.reply(host.requests[0], {bot: 'bot-a', recovery: {ready: false, phase: 'processing'},
        groups: cases.map(([fact], index) => ({...fact, group: 'g' + index, roster: {}}))});
    const status = host.nodes['gs-status'].textContent;
    cases.forEach(([, expected], index) => assert.ok(status.includes('g' + index + ' ' + expected + '，名册'), expected));
    assert.match(host.nodes['gs-summary'].textContent, /恢复基线：未就绪（处理中，请稍后再查看）/);
});
