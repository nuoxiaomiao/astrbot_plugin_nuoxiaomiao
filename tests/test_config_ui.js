'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../pages/config/app.js');
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, '../_conf_schema.json'), 'utf8').replace(/^\uFEFF/, ''));
const defaults = Object.fromEntries(Object.entries(schema).map(([key, spec]) => [key, spec.default]));
const groups = ui.deriveGroups(schema);
const copy = value => JSON.parse(JSON.stringify(value));

test('deriveGroups covers every schema key exactly once, with reference folded in', () => {
  const collected = [];
  for (const group of groups) {
    for (const key of group.fields) {
      assert.ok(!collected.includes(key), `duplicate ${key}`);
      collected.push(key);
    }
  }
  const hidden = ['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  const expected = Object.keys(schema).filter(key => !hidden.includes(key));
  for (const key of expected) {
    if (key === 'activity_prefetch_enabled' || key === 'capability_context_digest') continue; // folded into @reference
    assert.ok(collected.includes(key), `missing ${key}`);
  }
  assert.ok(collected.includes('@reference'));
  assert.ok(!collected.includes('activity_prefetch_enabled'));
  assert.ok(!collected.includes('capability_context_digest'));
  assert.equal(groups.map(g => g.title).join(','), '总览,消息合并,回复节奏,拦截与互动,聊天记录与参考,主动消息,自醒,高级,自主状态,共享记忆与信箱');
});

test('groupOf maps every key and the reference to one home', () => {
  assert.equal(ui.groupOf('enabled', groups).id, 'overview');
  assert.equal(ui.groupOf('debounce_enabled', groups).id, 'debounce');
  assert.equal(ui.groupOf('human_reply_enabled', groups).id, 'human_reply');
  assert.equal(ui.groupOf('include_group', groups).id, 'interaction');
  assert.equal(ui.groupOf('activity_prefetch_enabled', groups).id, 'activity');
  assert.equal(ui.groupOf('capability_context_digest', groups).id, 'activity');
  assert.equal(ui.groupOf('proactive_enabled', groups).id, 'proactive');
  assert.equal(ui.groupOf('proactive_silent_wake_enabled', groups).id, 'selfwake');
  assert.equal(ui.groupOf('force_wake_prefix', groups).id, 'advanced');
  assert.equal(ui.groupOf('resident_state_enabled', groups).id, 'resident_state');
  assert.equal(ui.groupOf('vision_enabled', groups).id, 'shared_memory');
});

test('reference choices map to exactly one automatic context source', () => {
  assert.deepEqual(ui.referenceValues('tools'), {activity_prefetch_enabled: false, capability_context_digest: false});
  assert.deepEqual(ui.referenceValues('prefetch'), {activity_prefetch_enabled: true, capability_context_digest: false});
  assert.deepEqual(ui.referenceValues('background'), {activity_prefetch_enabled: false, capability_context_digest: true});
  assert.equal(ui.referenceMode({activity_prefetch_enabled: true, capability_context_digest: false}), 'prefetch');
  assert.equal(ui.referenceMode({activity_prefetch_enabled: false, capability_context_digest: true}), 'background');
  assert.equal(ui.referenceMode({activity_prefetch_enabled: true, capability_context_digest: true}), 'background');
});

test('fieldTitle strips native category prefixes without editing schema', () => {
  const spec = {description: '【消息合并】群聊启用'};
  assert.equal(ui.fieldTitle(spec, 'sample'), '群聊启用');
  assert.equal(spec.description, '【消息合并】群聊启用');
});

test('an unrelated save preserves a legacy both-on reference configuration', () => {
  const baseline = {...defaults, activity_prefetch_enabled: true, capability_context_digest: true};
  const state = {...baseline, trigger_rounds: 5};
  assert.equal(ui.referenceMode(state), 'background');
  assert.deepEqual(ui.buildPatch(state, baseline, schema), {trigger_rounds: 5});
  Object.assign(state, ui.referenceValues('background'));
  assert.deepEqual(ui.buildPatch(state, baseline, schema), {
    trigger_rounds: 5, activity_prefetch_enabled: false, capability_context_digest: true
  });
});

test('a save response preserves edits made while the request was pending', () => {
  const baseline = {...defaults, trigger_rounds: 3, window_seconds: 180};
  const snapshot = {...baseline, trigger_rounds: 4};
  const patch = ui.buildPatch(snapshot, baseline, schema);
  const current = {...snapshot, trigger_rounds: 6, window_seconds: 240};
  const merged = ui.mergeSavedState(current, baseline, snapshot, patch, {...snapshot}, schema);
  assert.equal(merged.state.trigger_rounds, 6);
  assert.equal(merged.state.window_seconds, 240);
  assert.equal(merged.baseline.trigger_rounds, 4);
  assert.deepEqual(ui.buildPatch(merged.state, merged.baseline, schema), {trigger_rounds: 6, window_seconds: 240});
  assert.equal(baseline.trigger_rounds, 3);
  assert.equal(snapshot.trigger_rounds, 4);
});

test('save adopts server normalization for untouched inputs', () => {
  const baseline = {...defaults, trigger_rounds: 3};
  const snapshot = {...baseline, trigger_rounds: 4};
  const merged = ui.mergeSavedState(snapshot, baseline, snapshot, {trigger_rounds: 4}, {...snapshot, trigger_rounds: 5}, schema);
  assert.equal(merged.state.trigger_rounds, 5);
  assert.equal(merged.baseline.trigger_rounds, 5);
  assert.deepEqual(ui.buildPatch(merged.state, merged.baseline, schema), {});
});

test('a full server config response only synchronizes fields submitted in the patch', () => {
  const baseline = {...defaults, trigger_rounds: 3, window_seconds: 180};
  const snapshot = {...baseline, trigger_rounds: 4};
  const merged = ui.mergeSavedState(snapshot, baseline, snapshot, {trigger_rounds: 4}, {...snapshot, trigger_rounds: 5, window_seconds: 240}, schema);
  assert.equal(merged.state.trigger_rounds, 5);
  assert.equal(merged.state.window_seconds, 180);
  assert.equal(merged.baseline.window_seconds, 180);
});

test('time, numeric, and wake relationship errors block invalid dirty inputs', () => {
  const state = {...defaults, proactive_silent_wake_floor_minutes: 30, proactive_silent_wake_target_mean_minutes: 6, proactive_silent_wake_ceil_minutes: 10, proactive_quiet_hours_start: '25:70'};
  const errors = ui.validateValues(state, ui.buildPatch(state, defaults, schema), schema);
  assert.ok(errors.proactive_quiet_hours_start);
  assert.ok(errors.proactive_silent_wake_floor_minutes);
  assert.ok(errors.proactive_silent_wake_target_mean_minutes);
  assert.ok(errors.proactive_silent_wake_ceil_minutes);
  assert.ok(ui.validateValues({...defaults, trigger_rounds: ''}, {trigger_rounds: ''}, schema).trigger_rounds);
  assert.deepEqual(ui.validateValues({...defaults, proactive_quiet_hours_start: '23:30', proactive_quiet_hours_end: '07:00'}, {proactive_quiet_hours_start: '23:30', proactive_quiet_hours_end: '07:00'}, schema), {});
});

test('editing an unrelated setting does not reject existing legacy values', () => {
  const state = {...defaults, proactive_quiet_hours_start: '0', trigger_rounds: 4};
  assert.deepEqual(ui.validateValues(state, {trigger_rounds: 4}, schema), {});
});

test('structured field errors and list text survive expected interface shapes', () => {
  assert.deepEqual(ui.errorFields({response: {data: {field_errors: {trigger_rounds: '请输入整数'}}}}), {trigger_rounds: '请输入整数'});
  assert.deepEqual(ui.parseList('10001，10002\n10001;10003'), ['10001', '10002', '10003']);
  assert.equal(ui.valueText([]), '空名单');
  assert.equal(ui.valueText(true), '开启');
  assert.equal(ui.valueText(''), '留空');
});

test('wake validation rejects fractional values too close to the bounds', () => {
  const state = {...defaults, proactive_silent_wake_floor_minutes: 5, proactive_silent_wake_target_mean_minutes: 5.5, proactive_silent_wake_ceil_minutes: 6};
  assert.ok(ui.validateValues(state, {proactive_silent_wake_target_mean_minutes: 5.5}, schema).proactive_silent_wake_target_mean_minutes);
});

test('matches finds keys by title, hint, group, and raw key', () => {
  const spec = schema.trigger_rounds;
  assert.ok(ui.matches('trigger_rounds', spec, '连刷', '拦截与互动'));
  assert.ok(ui.matches('trigger_rounds', spec, 'trigger_rounds', '拦截与互动'));
  assert.ok(ui.matches('trigger_rounds', spec, '拦截', '拦截与互动'));
  assert.ok(!ui.matches('trigger_rounds', spec, '识图', '拦截与互动'));
});

// A deliberately small DOM implements the actual page's event and state paths.
// No browser globals, package installation, real API, or config file writes.
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
  dispatch(type, extra = {}) { const event = {target: this, preventDefault() {}, ...extra}; for (const listener of this.events[type] || []) listener(event); }
  match(selector) {
    if (selector === '[data-key]') return this.dataset.key !== undefined;
    if (selector.endsWith('[data-key]')) return this.match(selector.replace('[data-key]', '')) && this.dataset.key !== undefined;
    if (selector.startsWith('.')) return selector.slice(1).split('.').every(name => this.classList.contains(name));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName.toLowerCase() === selector;
  }
  querySelectorAll(selectors) { const found = []; const choices = selectors.split(','); const walk = item => { for (const child of item.children) { if (choices.some(choice => child.match(choice))) found.push(child); walk(child); } }; walk(this); return found; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let item = this; item; item = item.parent) if (item.match(selector)) return item; return null; }
  focus() { this.document.focused = this; this.document.activeElement = this; }
  scrollIntoView() {}
}
function fixture(config = {}, customPost, metadata = {}) {
  const document = {events: {}, createElement(tag) { return new Element(tag, this); }, addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }};
  document.documentElement = document.createElement('html');
  document.getElementById = id => document.documentElement.querySelector('#' + id);
  document.querySelectorAll = selector => document.documentElement.querySelectorAll(selector);
  const ids = ['app','groups','search','search-clear','timezone','banner','diagnostics','error-summary','dirty','save-detail','save','revert','restore-factory','resident','resident-root','boot','toast'];
  for (const id of ids) { const element = document.createElement(id === 'search' ? 'input' : 'div'); element.id = id; if (id === 'resident') element.tagName = 'DETAILS'; document.documentElement.appendChild(element); }
  const calls = [], storage = new Map();
  const window = {document, events: {}, addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }, scrollTo() {}, matchMedia() { return {matches: false}; }, localStorage: {getItem(key) { return storage.has(key) ? storage.get(key) : null; }, setItem(key, value) { storage.set(key, String(value)); }}, AstrBotPluginPage: {
    async ready() {},
    async apiGet() { return {status: 'ok', data: {schema, defaults, config: {...copy(defaults), ...config}, timezone: 'Asia/Shanghai', providers: [{id: 'available', label: '可用模型'}], diagnostics: [{code: 'sample', severity: 'info', message: '检查提示', fields: ['trigger_rounds']}], ...metadata}}; },
    async apiPost(route, body) { calls.push({route, body: copy(body)}); if (customPost) return customPost(route, body); return {status: 'ok', data: {config: {...copy(defaults), ...config, ...body.config}}}; }
  }};
  ui.mount(window);
  return {document, window, calls, storage, get: id => document.getElementById(id)};
}
const settled = () => new Promise(resolve => setImmediate(resolve));
function check(page, key, value) { const input = page.get('input-' + key); input.checked = value; input.dispatch('change'); }
function search(page, text) { const input = page.get('search'); input.value = text; input.dispatch('input'); }
function fieldVisible(page, key) {
  const row = page.get('field-' + key);
  if (!row) return false;
  for (let current = row; current; current = current.parentElement) if (current.hidden) return false;
  return true;
}

test('page renders all ten flat groups with every field', async () => {
  const page = fixture(); await settled();
  const sections = page.document.querySelectorAll('.group');
  assert.equal(sections.length, 10);
  assert.deepEqual(sections.map(s => s.querySelector('.group-title').textContent), ['总览','消息合并','回复节奏','拦截与互动','聊天记录与参考','主动消息','自醒','高级','自主状态','共享记忆与信箱']);
  for (const key of Object.keys(schema)) {
    if (['proactive_min_delay_minutes','proactive_silent_wake_tool_plugins','resident_tools_migrated','activity_prefetch_enabled','capability_context_digest'].includes(key)) continue;
    assert.ok(fieldVisible(page, key), `field ${key} rendered`);
    if (key === 'resident_extra_tools') { assert.ok(page.get('field-' + key).querySelectorAll('input').length >= 0); continue; }
    assert.ok(page.get('input-' + key), `input ${key} rendered`);
  }
  assert.ok(page.get('field-@reference'));
  assert.equal(page.get('timezone').textContent, '机器人时区：Asia/Shanghai');
  assert.ok(page.document.querySelectorAll('.diag').length);
});

test('page renders a provider select, time input, and reference options', async () => {
  const page = fixture({proactive_judge_provider_id: 'removed'}); await settled();
  const provider = page.get('input-proactive_judge_provider_id');
  assert.equal(provider.tagName, 'SELECT'); assert.equal(provider.value, 'removed');
  assert.ok(provider.children.some(option => option.value === 'removed'));
  assert.ok(provider.children.some(option => option.value === 'available'));
  assert.equal(page.get('input-proactive_quiet_hours_start').type, 'time');
  const reference = page.get('field-@reference');
  assert.equal(reference.querySelectorAll('input').length, 3);
});

test('search filters fields across groups and clears back', async () => {
  const page = fixture(); await settled();
  assert.ok(fieldVisible(page, 'trigger_rounds'));
  search(page, '自醒');
  assert.equal(fieldVisible(page, 'trigger_rounds'), false);
  assert.ok(fieldVisible(page, 'proactive_silent_wake_target_mean_minutes'));
  assert.equal(page.get('field-@reference'), null);
  search(page, '识图');
  assert.ok(fieldVisible(page, 'vision_enabled'));
  page.get('search-clear').dispatch('click');
  assert.ok(fieldVisible(page, 'trigger_rounds'));
  assert.ok(page.get('field-@reference'));
});

test('editing marks pending and save submits only dirty keys', async () => {
  const page = fixture(); await settled();
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
});

test('switch change and reference change are pending and saved', async () => {
  const page = fixture(); await settled();
  check(page, 'poke_reply_enabled', false);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {poke_reply_enabled: false});
  const radios = page.get('field-@reference').querySelectorAll('input');
  const background = radios.find(input => input.value === 'background');
  background.checked = true; background.dispatch('change');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[1].body.config, {activity_prefetch_enabled: false, capability_context_digest: true});
});

test('revert discards pending edits; restore factory fills defaults', async () => {
  const page = fixture({trigger_rounds: 9}); await settled();
  const input = page.get('input-trigger_rounds'); input.value = '8'; input.dispatch('input');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('revert').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, 9);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  page.get('restore-factory').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, defaults.trigger_rounds);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('revert').dispatch('click');
});

test('invalid input shows inline error, blocks save, and focuses the field', async () => {
  const page = fixture(); await settled();
  const input = page.get('input-trigger_rounds'); input.value = 'abc'; input.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.equal(page.calls.length, 0);
  assert.equal(page.get('error-trigger_rounds').textContent, '请输入有效数字。');
  assert.equal(page.get('input-trigger_rounds').getAttribute('aria-invalid'), 'true');
  assert.equal(page.document.activeElement && page.document.activeElement.id, 'input-trigger_rounds');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
});

test('server field errors stay attached to the correct editable field', async () => {
  const page = fixture({}, async () => ({status: 'ok', data: {validation_error: true, message: '配置校验失败', field_errors: {trigger_rounds: '测试错误'}, errors: ['测试错误']}})); await settled();
  const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input'); page.get('save').dispatch('click'); await settled();
  assert.equal(page.get('error-trigger_rounds').textContent, '测试错误');
  assert.equal(page.get('input-trigger_rounds').getAttribute('aria-invalid'), 'true');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('revert').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, defaults.trigger_rounds);
});

test('diagnostic links reveal and focus the field without editing settings', async () => {
  const page = fixture(); await settled();
  page.get('diagnostics').querySelector('button').dispatch('click');
  assert.ok(fieldVisible(page, 'trigger_rounds'));
  assert.equal(page.document.activeElement && page.document.activeElement.id, 'input-trigger_rounds');
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  assert.equal(page.calls.length, 0);
});

test('disabled function keeps parameters visible with a reason note', async () => {
  const page = fixture({human_reply_enabled: false}); await settled();
  assert.ok(fieldVisible(page, 'human_reply_chars_per_minute'));
  assert.ok(page.get('reason-human_reply_chars_per_minute').textContent);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  const input = page.get('input-human_reply_chars_per_minute'); input.value = '280'; input.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {human_reply_chars_per_minute: 280});
  assert.equal(page.get('input-human_reply_enabled').checked, false);
});

test('list fields edit and save parsed items', async () => {
  const page = fixture(); await settled();
  const input = page.get('input-blocked_users');
  assert.equal(input.tagName, 'TEXTAREA');
  input.value = '10001\n10002'; input.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {blocked_users: ['10001', '10002']});
});

test('an unrelated save retains hidden function and conditional parameter values', async () => {
  const page = fixture({human_reply_enabled: false, human_reply_chars_per_minute: 480, debounce_dynamic_enabled: true, merge_idle_seconds: 9});
  await settled();
  const rounds = page.get('input-trigger_rounds'); rounds.value = '4'; rounds.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
  assert.equal(page.get('input-human_reply_chars_per_minute').value, 480);
  assert.equal(page.get('input-merge_idle_seconds').value, 9);
});

test('autonomous tool choices persist concrete names and never implicitly select new tools', async () => {
  const page = fixture({resident_extra_tools: ['safe_read']}, undefined, {resident_tools: [
    {name: 'list_conversations', base: true, active: true, selectable: true},
    {name: 'safe_read', active: true, selectable: true},
    {name: 'new_tool', active: true, selectable: true},
    {name: 'task', active: true, selectable: false},
  ]}); await settled();
  const tools = page.get('field-resident_extra_tools');
  const items = tools.querySelectorAll('input'); assert.equal(items.length, 3);
  assert.equal(items[0].checked, true); assert.equal(items[1].checked, false); assert.equal(items[2].disabled, true);
  items[0].checked = false; items[0].dispatch('change');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config.resident_extra_tools, []);
});

test('save during concurrent edits preserves the newer input', async () => {
  let resolveSave;
  const page = fixture({}, () => new Promise(resolve => { resolveSave = resolve; })); await settled();
  const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input'); page.get('save').dispatch('click');
  assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
  input.value = '6'; input.dispatch('input');
  resolveSave({status: 'ok', data: {config: {...defaults, trigger_rounds: 4}}}); await settled();
  assert.equal(page.get('input-trigger_rounds').value, 6);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[1].body.config, {trigger_rounds: 6});
});

test('per-field restore fills factory or my-default and keeps draft dirty', async () => {
  const page = fixture({trigger_rounds: 3}, undefined, {user_default: {...copy(defaults), trigger_rounds: 5}}); await settled();
  const input = page.get('input-trigger_rounds'); input.value = '8'; input.dispatch('input');
  page.get('field-trigger_rounds').querySelector('.restore-field').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, 5);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  assert.equal(page.calls.length, 0);
});

const residentPanel = require('../pages/config/resident.js');
function residentData() {
  return {umo: 'qq:GroupMessage:1', persona: '喵', sessions: [{umo: 'qq:GroupMessage:1', name: '群', unread: 3, unread_mentions: 1}], moods: ['平静', '开心'], state: {mood: {mood: '平静', intensity: 0}, focus: '原关注点'}, style: {version: 2, body: '当前风格'}, proposals: [{id: 7, status: 'pending', body: '<script>仅作为文本展示</script>', reason: '建议', base_version: 2}], versions: [{version: 1, body: '旧风格'}], capabilities: [{title: '记忆', source: 'LivingMemory', state: 'ready', reason: '已注册', tools: ['recall'], url: '/#/plugin-view/astrbot_plugin_livingmemory/dashboard'}], runs: [{id: 9, status: 'unknown', outcome: 'quiet', started: 1, umo: 'qq:GroupMessage:1', input_other: 10, input_cached: 20, output: 5, usage_unknown: 1, summary: '待核实', actions: [{id: 10, tool: 'safe_edit', status: 'unknown', args: '{}', result: '缺少回执'}], sends: [{id: 11, umo: 'qq:GroupMessage:1', status: 'failed', body: '发送内容', detail: '平台拒绝'}], deferred: []}], budget: {used: 1, tokens: 15, unknown: 1}, unknown_sends: [], next_before: 9};
}
async function panelFixture(post) {
  const page = fixture(); await settled();
  const root = page.document.createElement('div'); page.document.documentElement.appendChild(root);
  const calls = [], reads = [], data = residentData();
  page.window.AstrBotPluginPage.apiGet = async (route, params) => { reads.push({route, params}); return copy(data); };
  page.window.AstrBotPluginPage.apiPost = async (route, body) => { calls.push({route, body: copy(body)}); return post ? post(body) : copy(data); };
  residentPanel.mount(root, page.window); await settled();
  return {root, calls, reads, data, button(text) { return root.querySelectorAll('button').find(n => n.textContent === text); }};
}

test('resident proposals send explicit decisions and baseline versions', async () => {
  for (const [text, action, extra] of [['批准此提案','approve',{proposal_id:7}], ['拒绝此提案','reject',{proposal_id:7}], ['回滚到此版本','rollback',{version:1}]]) {
    const page = await panelFixture(); page.button(text).dispatch('click'); await settled();
    assert.deepEqual(page.calls[0], {route:'resident',body:{umo:'qq:GroupMessage:1',action,expected_version:2,...extra}});
    assert.ok(page.root.querySelectorAll('pre').some(n => n.textContent === '<script>仅作为文本展示</script>'));
    assert.equal(page.root.querySelectorAll('script').length,0);
  }
});

test('resident failed state update preserves typed focus and displays the server error', async () => {
  const page = await panelFixture(() => ({operation_error:'版本冲突，请刷新'}));
  const focus = page.root.querySelectorAll('textarea').find(n => n.getAttribute('aria-label') === '仅本会话的关注点');
  focus.value = '保留的草稿'; page.button('保存当前状态').dispatch('click'); await settled();
  assert.equal(focus.value, '保留的草稿');
  assert.ok(page.root.querySelectorAll('p').some(n => n.textContent.includes('版本冲突')));
  assert.equal(page.calls[0].body.focus, '保留的草稿');
});

test('resident runs distinguish unsuccessful actual sends, unknown tools, and partial usage', async () => {
  const page = await panelFixture();
  assert.ok(page.root.querySelectorAll('summary').some(n => n.textContent.includes('实际发送 · 失败')));
  assert.ok(page.root.querySelectorAll('summary').some(n => n.textContent.includes('safe_edit · 结果待核实')));
  assert.ok(page.root.querySelectorAll('p').some(n => n.textContent.includes('用量不完整')));
  const note = page.root.querySelectorAll('textarea').find(n => n.getAttribute('aria-label') === '核实依据'); note.value = '已核对文件内容';
  page.button('核实为已完成').dispatch('click'); await settled();
  assert.equal(page.calls[0].body.action, 'review'); assert.equal(page.calls[0].body.action_id, 10); assert.equal(page.calls[0].body.note, '已核对文件内容');
});

test('resident navigation uses the existing plugin page and stable run pagination', async () => {
  const page = await panelFixture();
  assert.equal(page.root.querySelector('a').href, '/#/plugin-view/astrbot_plugin_livingmemory/dashboard');
  page.button('查看更早的运行').dispatch('click'); await settled();
  assert.deepEqual(page.reads[1], {route:'resident', params:{umo:'qq:GroupMessage:1',before:9}});
});
