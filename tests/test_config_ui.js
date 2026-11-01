'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../pages/config/app.js');
const residentPanel = require('../pages/config/resident.js');
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, '../_conf_schema.json'), 'utf8').replace(/^\uFEFF/, ''));
const defaults = Object.fromEntries(Object.entries(schema).map(([key, spec]) => [key, spec.default]));
const groups = ui.deriveGroups(schema);
const copy = value => value === undefined ? null : JSON.parse(JSON.stringify(value));

/* ============================ 纯函数 ============================ */

test('deriveGroups covers every schema key exactly once', () => {
  const collected = [];
  for (const group of groups) {
    for (const key of group.fields) {
      assert.ok(!collected.includes(key), 'duplicate ' + key);
      collected.push(key);
    }
  }
  const hidden = ['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  for (const key of Object.keys(schema)) {
    if (hidden.includes(key)) continue;
    if (key === 'activity_prefetch_enabled' || key === 'capability_context_digest') continue;
    assert.ok(collected.includes(key), 'missing ' + key);
  }
  assert.ok(collected.includes('@reference'));
  assert.equal(groups.map(g => g.title).join(','), '总览,消息合并,回复节奏,拦截与互动,聊天记录与参考,主动消息,自醒,高级,自主状态,共享记忆与信箱');
});

test('groupOf maps keys to their page', () => {
  assert.equal(ui.groupOf('enabled', groups).id, 'overview');
  assert.equal(ui.groupOf('debounce_enabled', groups).id, 'debounce');
  assert.equal(ui.groupOf('include_group', groups).id, 'interaction');
  assert.equal(ui.groupOf('activity_prefetch_enabled', groups).id, 'activity');
  assert.equal(ui.groupOf('proactive_silent_wake_enabled', groups).id, 'selfwake');
  assert.equal(ui.groupOf('vision_enabled', groups).id, 'shared_memory');
});

test('reference choices map to exactly one automatic context source', () => {
  assert.deepEqual(ui.referenceValues('tools'), { activity_prefetch_enabled: false, capability_context_digest: false });
  assert.deepEqual(ui.referenceValues('prefetch'), { activity_prefetch_enabled: true, capability_context_digest: false });
  assert.deepEqual(ui.referenceValues('background'), { activity_prefetch_enabled: false, capability_context_digest: true });
  assert.equal(ui.referenceMode({ activity_prefetch_enabled: true, capability_context_digest: false }), 'prefetch');
  assert.equal(ui.referenceMode({ activity_prefetch_enabled: false, capability_context_digest: true }), 'background');
});

test('fieldTitle strips native category prefixes', () => {
  const spec = { description: '【消息合并】群聊防抖' };
  assert.equal(ui.fieldTitle(spec, 'sample'), '群聊防抖');
  assert.equal(spec.description, '【消息合并】群聊防抖');
});

test('save patches only dirty keys and preserves concurrent edits', () => {
  const baseline = { ...defaults, trigger_rounds: 3, window_seconds: 180 };
  const snapshot = { ...baseline, trigger_rounds: 4 };
  assert.deepEqual(ui.buildPatch(snapshot, baseline, schema), { trigger_rounds: 4 });
  const merged = ui.mergeSavedState({ ...snapshot, trigger_rounds: 6 }, baseline, snapshot, { trigger_rounds: 4 }, { ...snapshot }, schema);
  assert.equal(merged.state.trigger_rounds, 6);
  assert.equal(merged.baseline.trigger_rounds, 4);
  assert.deepEqual(ui.buildPatch(merged.state, merged.baseline, schema), { trigger_rounds: 6 });
});

test('validation blocks invalid numbers, times and wake relationships', () => {
  const state = { ...defaults, proactive_silent_wake_floor_minutes: 30, proactive_silent_wake_target_mean_minutes: 6, proactive_silent_wake_ceil_minutes: 10, proactive_quiet_hours_start: '25:70' };
  const errors = ui.validateValues(state, ui.buildPatch(state, defaults, schema), schema);
  assert.ok(errors.proactive_quiet_hours_start);
  assert.ok(errors.proactive_silent_wake_floor_minutes);
  assert.ok(ui.validateValues({ ...defaults, trigger_rounds: '' }, { trigger_rounds: '' }, schema).trigger_rounds);
  assert.deepEqual(ui.validateValues({ ...defaults, proactive_quiet_hours_start: '23:30' }, { proactive_quiet_hours_start: '23:30' }, schema), {});
});

test('list parsing and structured errors survive interface shapes', () => {
  assert.deepEqual(ui.parseList('10001，10002\n10001;10003'), ['10001', '10002', '10003']);
  assert.equal(ui.valueText([]), '空名单');
  assert.deepEqual(ui.errorFields({ response: { data: { field_errors: { trigger_rounds: '请输入整数' } } } }), { trigger_rounds: '请输入整数' });
});

test('matches searches title, hint, group and raw key', () => {
  const spec = schema.trigger_rounds;
  assert.ok(ui.matches('trigger_rounds', spec, '连刷', '拦截与互动'));
  assert.ok(ui.matches('trigger_rounds', spec, 'trigger_rounds', '拦截与互动'));
  assert.ok(!ui.matches('trigger_rounds', spec, '识图', '拦截与互动'));
});

test('highlightParts marks every match', () => {
  assert.deepEqual(ui.highlightParts('聊天记录与参考', '记录'), [
    { text: '聊天', match: false }, { text: '记录', match: true }, { text: '与参考', match: false }
  ]);
  assert.deepEqual(ui.highlightParts('aaa', 'a'), [
    { text: 'a', match: true }, { text: 'a', match: true }, { text: 'a', match: true }
  ]);
  assert.deepEqual(ui.highlightParts('abc', ''), [{ text: 'abc', match: false }]);
});

test('pendingCount folds the reference pair into one item', () => {
  const activity = groups.find(g => g.id === 'activity');
  assert.equal(ui.pendingCount(activity, ['activity_prefetch_enabled']), 1);
  assert.equal(ui.pendingCount(activity, ['activity_prefetch_enabled', 'capability_context_digest']), 1);
  assert.equal(ui.pendingCount(groups.find(g => g.id === 'interaction'), ['trigger_rounds', 'window_seconds']), 2);
});

test('every cluster key exists in the schema and appears once', () => {
  const seen = new Set();
  for (const cluster of ui.CLUSTERS) {
    for (const key of cluster.gates.concat(cluster.children, cluster.lists)) {
      assert.ok(schema[key], 'missing schema key ' + key);
      assert.ok(!seen.has(key), 'duplicate cluster key ' + key);
      seen.add(key);
    }
  }
});

/* ============================ 迷你 DOM ============================ */

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.children = [];
    this.dataset = {}; this.attributes = {}; this.events = {}; this.style = {};
    this.className = ''; this.textContent = ''; this.value = ''; this.hidden = false; this.open = false;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { if (!this.classList.contains(name)) this.className += ' ' + name; },
      remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(' '); },
      toggle: (name, on) => { if (on === undefined) on = !this.classList.contains(name); this.classList[on ? 'add' : 'remove'](name); }
    };
  }
  get parentElement() { return this.parent || null; }
  appendChild(child) { child.remove(); child.parent = this; this.children.push(child); return child; }
  prepend(child) { child.remove(); child.parent = this; this.children.unshift(child); }
  replaceChildren(...children) { this.children.forEach(child => { child.parent = null; }); this.children = []; children.forEach(child => this.appendChild(child)); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }
  dispatch(type, extra = {}) { const event = { target: this, preventDefault() {}, ...extra }; for (const listener of this.events[type] || []) listener(event); }
  match(selector) {
    if (selector === '[data-key]') return this.dataset.key !== undefined;
    if (selector.endsWith('[data-key]')) return this.match(selector.replace('[data-key]', '')) && this.dataset.key !== undefined;
    if (selector.startsWith('.')) return selector.slice(1).split('.').every(name => this.classList.contains(name));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName.toLowerCase() === selector;
  }
  querySelectorAll(selectors) {
    const found = [], choices = selectors.split(',');
    const walk = item => { for (const child of item.children) { if (choices.some(choice => child.match(choice.trim()))) found.push(child); walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.document.focused = this; this.document.activeElement = this; }
  scrollIntoView() {}
}

function fixture(config = {}, customPost, metadata = {}, residentData = null) {
  const document = { events: {}, createElement(tag) { return new Element(tag, this); }, addEventListener(type, listener) { (this.events[type] ||= []).push(listener); } };
  document.documentElement = document.createElement('html');
  document.getElementById = id => document.documentElement.querySelector('#' + id);
  document.querySelectorAll = selector => document.documentElement.querySelectorAll(selector);
  const ids = ['app', 'groups', 'status-root', 'nav', 'search', 'search-clear', 'timezone', 'banner', 'diagnostics', 'error-summary', 'dirty', 'save-detail', 'save', 'revert', 'restore-factory', 'savebar', 'boot', 'toast'];
  for (const id of ids) { const el = document.createElement(id === 'search' ? 'input' : 'div'); el.id = id; document.documentElement.appendChild(el); }
  const calls = [], reads = [], storage = new Map();
  const window = {
    document,
    events: {},
    addEventListener(type, listener) { (this.events[type] ||= []).push(listener); },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); }
    },
    AstrBotPluginPage: {
      async ready() {},
      async apiGet(endpoint, params) {
        reads.push({ endpoint, params: copy(params) });
        if (endpoint === 'resident') {
          return { status: 'ok', data: residentData ? copy(residentData) : { sessions: [], runs: [], proposals: [], versions: [], capabilities: [], moods: [], budget: {}, unknown_sends: [] } };
        }
        return { status: 'ok', data: {
          schema, defaults, config: { ...copy(defaults), ...config }, user_default: null,
          providers: [{ id: 'available', label: '可用模型' }],
          resident_tools: [{ name: 'safe_read', active: true, selectable: true }],
          timezone: 'Asia/Shanghai',
          diagnostics: [{ code: 'sample', severity: 'info', message: '检查提示', fields: ['trigger_rounds'] }],
          ...metadata
        } };
      },
      async apiPost(endpoint, body) {
        calls.push({ endpoint, body: copy(body) });
        if (customPost) return customPost(endpoint, body);
        return { status: 'ok', data: { config: { ...copy(defaults), ...config, ...(body && body.config || {}) } } };
      }
    }
  };
  window.NuoxiaomiaoResidentPanel = residentPanel;
  ui.mount(window);
  return { document, window, calls, reads, storage, get: id => document.getElementById(id) };
}

const settled = () => new Promise(resolve => setImmediate(resolve));
function navItem(page, id) { return page.get('nav').querySelectorAll('.nav-item').find(item => item.dataset.group === id); }
function statusNav(page) { return page.get('nav').querySelectorAll('.nav-item').find(item => item.dataset.page === 'status'); }
function check(page, key, value) { const input = page.get('input-' + key); input.checked = value; input.dispatch('change'); }
function search(page, text) { const input = page.get('search'); input.value = text; input.dispatch('input'); }
function fieldVisible(page, key) {
  const row = page.get('field-' + key);
  if (!row) return false;
  for (let current = row; current; current = current.parentElement) if (current.hidden) return false;
  return true;
}
function pageTitle(page) { const el = page.get('groups').querySelector('.page-title'); return el ? el.textContent : ''; }
function findButton(root, text) { return root.querySelectorAll('button').find(btn => btn.textContent === text); }

/* ============================ 配置页 ============================ */

test('renders only the active group with Win11 cards', async () => {
  const page = fixture(); await settled();
  assert.equal(pageTitle(page), '总览');
  assert.equal(page.get('app').hidden, false);
  assert.equal(page.get('status-root').hidden, true);
  const navLabels = page.get('nav').querySelectorAll('.nav-label').map(el => el.textContent);
  assert.equal(navLabels.length, 11);
  assert.equal(navLabels[10], '运行与状态');
  assert.ok(page.get('groups').querySelectorAll('.card').length >= 1);
  assert.ok(page.get('groups').querySelectorAll('.page-title').length === 1);
});

test('nav switches pages and remembers the selection', async () => {
  const page = fixture(); await settled();
  navItem(page, 'interaction').dispatch('click');
  assert.equal(pageTitle(page), '拦截与互动');
  assert.ok(navItem(page, 'interaction').classList.contains('active'));
  assert.equal(JSON.parse(page.storage.get('nxm-active-page')).page, 'interaction');
  navItem(page, 'debounce').dispatch('click');
  assert.equal(pageTitle(page), '消息合并');
});

test('search shows grouped results and restores the previous page', async () => {
  const page = fixture(); await settled();
  navItem(page, 'interaction').dispatch('click');
  search(page, '白名单');
  assert.equal(pageTitle(page), '搜索结果');
  assert.ok(page.get('groups').querySelectorAll('.search-group-title').length >= 2);
  assert.ok(page.get('groups').querySelectorAll('.card').length >= 1);
  assert.equal(page.get('search-clear').hidden, false);
  page.get('search-clear').dispatch('click');
  assert.equal(pageTitle(page), '拦截与互动');
});

test('editing marks nav badge and save submits only dirty keys', async () => {
  const page = fixture(); await settled();
  navItem(page, 'interaction').dispatch('click');
  const input = page.get('input-trigger_rounds'); input.value = '5'; input.dispatch('input');
  assert.equal(page.get('nav-pending-interaction').textContent, '1');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, { trigger_rounds: 5 });
  assert.equal(page.get('nav-pending-interaction').hidden, true);
});

test('feature card folds its settings and lists under the toggle', async () => {
  const page = fixture(); await settled();
  navItem(page, 'debounce').dispatch('click');
  assert.equal(page.get('cluster-body-debounce').hidden, false);
  assert.equal(page.get('cluster-state-debounce').textContent, '');
  assert.equal(page.get('list-body-debounce').hidden, true);
  page.get('list-bar-debounce').dispatch('click');
  assert.equal(page.get('list-body-debounce').hidden, false);
  assert.deepEqual(JSON.parse(page.storage.get('nxm-list-open')), { debounce: true });
  check(page, 'debounce_enabled', false);
  assert.equal(page.get('cluster-body-debounce').hidden, true);
  assert.equal(page.get('cluster-state-debounce').textContent, '已关闭');
  assert.equal(page.get('reason-debounce_slow_idle_seconds'), null);
});

test('conditional fields swap with their switch', async () => {
  const page = fixture(); await settled();
  navItem(page, 'debounce').dispatch('click');
  assert.equal(page.get('field-merge_idle_seconds'), null);
  check(page, 'debounce_dynamic_enabled', false);
  assert.ok(page.get('field-merge_idle_seconds'));
  assert.equal(page.get('field-debounce_slow_idle_seconds'), null);
});

test('group reset restores only that group', async () => {
  const page = fixture(); await settled();
  navItem(page, 'interaction').dispatch('click');
  const input = page.get('input-trigger_rounds'); input.value = '5'; input.dispatch('input');
  assert.equal(page.get('group-reset').hidden, false);
  page.get('group-reset').dispatch('click'); await settled();
  assert.equal(page.get('input-trigger_rounds').value, defaults.trigger_rounds);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
});

test('invalid input shows error summary and focuses the field', async () => {
  const page = fixture(); await settled();
  navItem(page, 'interaction').dispatch('click');
  const input = page.get('input-trigger_rounds'); input.value = 'abc'; input.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.equal(page.calls.length, 0);
  assert.equal(page.get('error-trigger_rounds').textContent, '请输入有效数字。');
  assert.equal(page.get('error-summary').hidden, false);
  assert.equal(page.document.activeElement && page.document.activeElement.id, 'input-trigger_rounds');
});

test('status page hides the save bar and mounts the resident panel', async () => {
  const page = fixture({}, undefined, {}, residentFixtureData()); await settled();
  statusNav(page).dispatch('click'); await settled();
  assert.equal(page.get('status-root').hidden, false);
  assert.equal(page.get('groups').hidden, true);
  assert.equal(page.get('savebar').hidden, true);
  assert.equal(page.get('status-root').querySelector('.page-title').textContent, '运行与状态');
  assert.ok(page.get('resident-root'));
  assert.equal(page.get('tile-wake').textContent, '1');
  navItem(page, 'overview').dispatch('click');
  assert.equal(page.get('savebar').hidden, false);
  assert.equal(pageTitle(page), '总览');
});

/* ============================ 运行与状态 ============================ */

function residentFixtureData() {
  return {
    umo: 'qq:GroupMessage:1',
    persona: '喵',
    sessions: [{ umo: 'qq:GroupMessage:1', name: '测试群', conversation_id: '1', unread: 3, unread_mentions: 1 }],
    moods: ['平静', '开心'],
    state: { mood: { mood: '平静', intensity: 40 }, focus: '原关注点' },
    style: { version: 2, body: '当前风格' },
    proposals: [
      { id: 7, status: 'pending', body: '新风格', reason: '建议更活泼', base_version: 2, diff: '--- 当前\n+++ 建议\n+新风格' },
      { id: 6, status: 'approved', body: '旧', reason: '旧提案', base_version: 1, diff: '' }
    ],
    versions: [{ version: 1, body: '旧风格' }],
    capabilities: [{ title: '记忆', source: 'LivingMemory', state: 'ready', reason: '已注册', tools: ['recall'], url: '/#/plugin-view/astrbot_plugin_livingmemory/dashboard' }],
    runs: [
      { id: 9, status: 'succeeded', outcome: 'spoke', started: 100, ended: 280, umo: 'qq:GroupMessage:1', input_other: 10, input_cached: 20, output: 5, usage_unknown: 0, summary: '问候了一句', tool_requests: [{ tool: 'safe_read', args: '{"path":"a"}' }], sends: [{ id: 11, umo: 'qq:GroupMessage:1', status: 'failed', body: '发送内容', detail: '平台拒绝' }], deferred: [], actions: [{ id: 10, tool: 'safe_edit', status: 'unknown', args: '{}', result: '缺少回执' }] },
      { id: 8, status: 'failed', outcome: '', started: 50, ended: 60, umo: 'qq:GroupMessage:1', input_other: 1, input_cached: 0, output: 0, usage_unknown: 1, summary: '', tool_requests: [], sends: [], deferred: [], actions: [] }
    ],
    budget: { used: 1, tokens: 15, unknown: 1 },
    unknown_sends: [{ id: 20, target_umo: 'qq:GroupMessage:1', note: '平台未回执' }],
    next_before: 8
  };
}

async function residentFixture(post) {
  const page = fixture({}, undefined, {}, residentFixtureData()); await settled();
  const root = page.document.createElement('div');
  page.document.documentElement.appendChild(root);
  const calls = [], reads = [];
  page.window.AstrBotPluginPage.apiGet = async (endpoint, params) => { reads.push({ endpoint, params: copy(params) }); return copy(residentFixtureData()); };
  page.window.AstrBotPluginPage.apiPost = async (endpoint, body) => { calls.push({ endpoint, body: copy(body) }); return post ? post(body) : copy(residentFixtureData()); };
  residentPanel.mount(root, page.window);
  await settled();
  return { root, calls, reads, button: text => findButton(root, text) };
}

test('resident summary tiles show budget and pending work', async () => {
  const panel = await residentFixture();
  assert.equal(panel.root.querySelector('#tile-wake').textContent, '1');
  assert.equal(panel.root.querySelector('#tile-token').textContent, '15');
  assert.equal(panel.root.querySelector('#tile-pending').textContent, '3');
});

test('runs are collapsed by default and expand on click', async () => {
  const panel = await residentFixture();
  const row = panel.root.querySelector('#run-row-9');
  assert.ok(row);
  const title = row.querySelector('.run-title');
  assert.ok(title.querySelectorAll('span').some(span => span.textContent === '#9'));
  assert.equal(panel.root.querySelector('#run-detail-9'), null);
  row.dispatch('click');
  const detail = panel.root.querySelector('#run-detail-9');
  assert.ok(detail);
  assert.equal(detail.querySelector('.run-summary').textContent, '问候了一句');
  assert.ok(detail.querySelectorAll('h4').some(h => h.textContent.includes('工具调用')));
});

test('run filter chips narrow the list', async () => {
  const panel = await residentFixture();
  panel.root.querySelector('#filter-failed').dispatch('click');
  assert.equal(panel.root.querySelector('#run-row-9'), null);
  assert.ok(panel.root.querySelector('#run-row-8'));
  panel.root.querySelector('#filter-all').dispatch('click');
  assert.ok(panel.root.querySelector('#run-row-9'));
});

test('unknown sends and actions verify with explicit payloads', async () => {
  const panel = await residentFixture();
  panel.button('已核实发送成功').dispatch('click'); await settled();
  assert.deepEqual(panel.calls[0].body, { umo: 'qq:GroupMessage:1', action: 'review_send', send_id: 20, conclusion: 'fired' });
  panel.button('已完成').dispatch('click'); await settled();
  assert.deepEqual(panel.calls[1].body, { umo: 'qq:GroupMessage:1', action: 'review', action_id: 10, conclusion: 'succeeded', note: '' });
});

test('style proposals approve, reject and rollback with baseline versions', async () => {
  const panel = await residentFixture();
  panel.button('批准此提案').dispatch('click'); await settled();
  assert.deepEqual(panel.calls[0].body, { umo: 'qq:GroupMessage:1', action: 'approve', proposal_id: 7, expected_version: 2 });
  panel.button('回滚到此版本').dispatch('click'); await settled();
  assert.deepEqual(panel.calls[1].body, { umo: 'qq:GroupMessage:1', action: 'rollback', version: 1, expected_version: 2 });
});

test('failed state save keeps typed focus and shows the error', async () => {
  const panel = await residentFixture(() => ({ operation_error: '版本冲突，请刷新' }));
  const focus = panel.root.querySelector('#state-focus');
  focus.value = '保留的草稿';
  panel.button('保存当前状态').dispatch('click'); await settled();
  assert.equal(focus.value, '保留的草稿');
  const alert = panel.root.querySelector('.infobar.danger');
  assert.ok(alert && alert.textContent.includes('版本冲突'));
  assert.equal(panel.calls[0].body.focus, '保留的草稿');
});

test('load more uses the stable before cursor', async () => {
  const panel = await residentFixture();
  panel.button('加载更早的记录').dispatch('click'); await settled();
  assert.deepEqual(panel.reads[1].params, { umo: 'qq:GroupMessage:1', before: 8 });
});

test('capabilities link points at the management page', async () => {
  const panel = await residentFixture();
  const link = panel.root.querySelector('a');
  assert.equal(link.href, '/#/plugin-view/astrbot_plugin_livingmemory/dashboard');
});

test('session switch reloads that conversation', async () => {
  const panel = await residentFixture();
  const picker = panel.root.querySelector('#resident-session');
  picker.value = 'qq:GroupMessage:1';
  picker.dispatch('change'); await settled();
  assert.equal(panel.reads[1].params.umo, 'qq:GroupMessage:1');
});
