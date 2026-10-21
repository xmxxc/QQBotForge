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
