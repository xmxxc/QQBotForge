const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {setImmediate: tick} = require('node:timers/promises');

// Run the shipped page script, replacing only the DOM and network boundaries.
async function page() {
    const nodes = {};
    for (const id of ['gs-bot', 'gs-status', 'gs-output', 'gs-group', 'gs-load', 'gs-refresh']) {
        nodes[id] = {
            value: id === 'gs-group' ? 'group-a' : '', textContent: '', options: [],
            append(option) {
                this.options.push(option);
                if (!this.value) this.value = option.value;
            },
        };
    }
    const requests = [];
    const html = fs.readFileSync(path.join(__dirname, '../../modules/group_state/page.html'), 'utf8');
    vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {
        document: {getElementById: id => nodes[id], createElement: () => ({})},
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
