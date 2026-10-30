'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ui = require('../pages/config/app.js');
const schema = JSON.parse(fs.readFileSync(path.join(__dirname, '../_conf_schema.json'), 'utf8').replace(/^\uFEFF/, ''));
const defaults = Object.fromEntries(Object.entries(schema).map(([key, spec]) => [key, spec.default]));
const copy = value => JSON.parse(JSON.stringify(value));

test('all active schema fields have one navigation home; reserved delay is omitted', () => {
  assert.equal(ui.GROUPS.length, 9);
  assert.deepEqual(ui.GROUPS.map(group => group.title), ['总览', '消息合并', '回复节奏', '拦截与互动', '聊天记录与参考', '主动消息', '自醒', '高级', '自主运行与状态']);
  const collect = section => (section.fields || []).concat((section.children || []).flatMap(collect));
  const grouped = ui.GROUPS.flatMap(group => group.sections.flatMap(section => {
    const fields = collect(section);
    assert.deepEqual(ui.sectionFields(section), fields, `${group.id}:${section.id}`);
    for (const key of fields.filter(key => !key.startsWith('@'))) assert.equal(ui.groupOf(key).id, group.id, key);
    return fields;
  }));
  grouped.push('activity_prefetch_enabled', 'capability_context_digest');
  for (const key of Object.keys(schema)) {
    if (['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'].includes(key)) continue;
    assert.equal(grouped.filter(item => item === key).length, 1, key);
  }
  assert.ok(!grouped.includes('proactive_min_delay_minutes'));
  assert.equal(ui.groupOf('activity_prefetch_enabled').id, 'activity');
  assert.equal(ui.groupOf('capability_context_digest').id, 'activity');
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

test('reference choices map to exactly one automatic context source', () => {
  assert.deepEqual(ui.referenceValues('tools'), {activity_prefetch_enabled: false, capability_context_digest: false});
  assert.deepEqual(ui.referenceValues('prefetch'), {activity_prefetch_enabled: true, capability_context_digest: false});
  assert.deepEqual(ui.referenceValues('background'), {activity_prefetch_enabled: false, capability_context_digest: true});
});

test('tools have explicit all, core, and selected semantics', () => {
  assert.equal(ui.toolMode([]), 'all');
  assert.equal(ui.toolMode([ui.CORE_PLUGIN]), 'core');
  assert.deepEqual(ui.toolValues('all', ['memory']), []);
  assert.deepEqual(ui.toolValues('core', ['memory']), [ui.CORE_PLUGIN]);
  assert.deepEqual(ui.toolValues('selected', ['memory', ui.CORE_PLUGIN, 'memory', '（内置工具）']), [ui.CORE_PLUGIN, 'memory']);
  assert.equal(ui.toolMode(ui.toolValues('selected', [])), 'core');
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
});

// A deliberately small DOM implements the actual page's event and state paths.
// No browser globals, package installation, real API, or config file writes.
class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.children = [];
    this.dataset = {}; this.attributes = {}; this.events = {}; this.style = {};
    this.className = ''; this.textContent = ''; this.value = ''; this.hidden = false;
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
  const ids = ['app','sidebar','tabs','pane','page-title','page-description','timezone','banner','diagnostics','error-summary','boot','toast','dirty','save-detail','save','save-default','clear-default','restore-default','restore-factory','revert','search-clear','default-menu','default-menu-btn','drawer-toggle','drawer-close','drawer-backdrop','theme-toggle','search','pending-only'];
  for (const id of ids) { const element = document.createElement(id === 'search' || id === 'pending-only' ? 'input' : 'div'); element.id = id; document.documentElement.appendChild(element); }
  const calls = [], storage = new Map();
  const window = {document, events: {}, addEventListener(type, listener) { (this.events[type] ||= []).push(listener); }, scrollTo() {}, matchMedia() { return {matches: false}; }, localStorage: {getItem(key) { return storage.has(key) ? storage.get(key) : null; }, setItem(key, value) { storage.set(key, String(value)); }}, AstrBotPluginPage: {
    async ready() {},
    async apiGet() { return {status: 'ok', data: {schema, defaults, config: {...copy(defaults), ...config}, timezone: 'Asia/Shanghai', providers: [{id: 'available', label: '可用模型'}], diagnostics: [{code: 'sample', severity: 'info', message: '检查提示', fields: ['trigger_rounds']}], tool_plugins: [{name: ui.CORE_PLUGIN, count: 3}, {name: '（内置工具）', builtin: true, count: 2}, {name: 'memory', count: 1, tools: ['recall']}], ...metadata}}; },
    async apiPost(route, body) { calls.push({route, body: copy(body)}); if (customPost) return customPost(route, body); return {status: 'ok', data: {config: {...copy(defaults), ...config, ...body.config}}}; }
  }};
  ui.mount(window);
  return {document, window, calls, storage, get: id => document.getElementById(id), navigate(id) { const tab = document.querySelectorAll('.nav-button').find(node => node.dataset.group === id); tab.dispatch('click'); }};
}
const settled = () => new Promise(resolve => setImmediate(resolve));
function visible(element) {
  if (!element) return false;
  for (let current = element; current; current = current.parentElement) {
    if (current.hidden || current.tagName === 'DETAILS' && !current.open) return false;
  }
  return true;
}
function check(page, key, value) { const input = page.get('input-' + key); input.checked = value; input.dispatch('change'); }
function search(page, text) { const input = page.get('search'); input.value = text; input.dispatch('input'); }
function selectReference(page, value) {
  const radios = page.get('field-@reference').querySelectorAll('input');
  radios.forEach(input => { input.checked = input.value === value; });
  radios.find(input => input.value === value).dispatch('change');
}
function openAdvanced(page, key) {
  const details = page.get('field-' + key).closest('details');
  assert.ok(details, `${key} has advanced settings`);
  details.open = true; details.dispatch('toggle');
  return details;
}

test('page renders nine navigation groups, visible primary switches, time and provider controls', async () => {
  const page = fixture({proactive_judge_provider_id: 'removed'}); await settled();
  assert.equal(page.document.querySelectorAll('.nav-button').length, 9);
  assert.equal(page.get('timezone').textContent, '机器人时区：Asia/Shanghai');
  assert.ok(page.get('diagnostics').children.length);
  page.navigate('debounce');
  const merge = page.get('field-debounce_enabled').closest('.feature-card');
  assert.equal(merge.id, 'feature-debounce-merge');
  assert.equal(merge.tagName, 'SECTION');
  assert.equal(page.get('field-debounce_enabled').closest('details'), null);
  assert.ok(visible(page.get('input-debounce_enabled')));
  assert.equal(page.get('field-debounce_long_chars').closest('details').open, false);
  page.navigate('proactive'); assert.equal(page.get('input-proactive_quiet_hours_start').type, 'time');
  page.navigate('selfwake');
  const provider = page.get('input-proactive_judge_provider_id');
  assert.equal(provider.tagName, 'SELECT'); assert.equal(provider.value, 'removed');
  assert.ok(provider.children.some(option => option.value === 'removed'));
  assert.ok(provider.children.some(option => option.value === 'available'));
});

test('each interaction and record function contains its own related parameters', async () => {
  const page = fixture(); await settled(); page.navigate('interaction');
  for (const [feature, fields] of [
    ['guard', ['include_group', 'include_private', 'trigger_rounds', 'user_whitelist', 'user_blacklist', 'guard_group_whitelist', 'guard_group_blacklist', 'meme_tags', 'meme_cooldown_seconds']],
    ['blocked', ['blocked_users']],
    ['poke', ['poke_reply_enabled', 'poke_reply_prompt', 'poke_reply_cooldown_seconds', 'poke_group_whitelist', 'poke_group_blacklist']],
    ['keyword', ['keyword_wake_enabled', 'keyword_wake_list', 'keyword_wake_group_whitelist', 'keyword_wake_group_blacklist']],
  ]) {
    const card = page.get('feature-interaction-' + feature);
    assert.equal(card.tagName, 'SECTION');
    assert.equal(card.closest('details'), null);
    for (const key of fields) assert.equal(page.get('field-' + key).closest('.feature-card'), card, key);
  }
  page.navigate('activity');
  for (const [feature, fields] of [
    ['recording', ['activity_buffer_enabled', 'activity_record_private', 'activity_record_bot_replies', 'activity_retention_days', 'activity_group_whitelist', 'activity_group_blacklist', 'activity_buffer_max_text_chars', 'activity_db_path', 'resident_inbox_enabled']],
    ['permissions', ['activity_tool_enabled', 'activity_require_admin', 'activity_hint_enabled']],
    ['reference', ['@reference', 'activity_prefetch_keywords']],
    ['memory', ['lmem_gateway_enabled', 'lmem_gateway_keywords', 'lmem_gateway_top_k', 'lmem_gateway_timeout_seconds', 'lmem_gateway_max_chars', 'lmem_gateway_plugin_name', 'lmem_gateway_recall_tool_name']],
  ]) {
    const card = page.get('feature-activity-' + feature);
    for (const key of fields) assert.equal(page.get('field-' + key).closest('.feature-card'), card, key);
  }
});

test('a disabled function can be edited temporarily without enabling it on save', async () => {
  const page = fixture({human_reply_enabled: false}); await settled(); page.navigate('human_reply');
  const card = page.get('feature-human_reply-reply');
  assert.ok(visible(page.get('input-human_reply_enabled')));
  assert.equal(card.querySelector('.feature-parameters').hidden, true);
  assert.equal(visible(page.get('input-human_reply_chars_per_minute')), false);
  const input = page.get('input-human_reply_chars_per_minute');
  page.get('edit-human_reply-reply').dispatch('click');
  assert.equal(card.querySelector('.feature-parameters').hidden, false);
  assert.ok(visible(input));
  assert.equal(page.get('input-human_reply_chars_per_minute'), input);
  assert.equal(page.get('field-human_reply_group_whitelist').closest('details').open, false);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  input.value = '280'; input.dispatch('input'); page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {human_reply_chars_per_minute: 280});
  assert.equal(page.get('input-human_reply_enabled').checked, false);
});

test('temporary editing can be collapsed without discarding the draft or enabling the function', async () => {
  const page = fixture({human_reply_enabled: false}); await settled(); page.navigate('human_reply');
  const edit = page.get('edit-human_reply-reply'), input = page.get('input-human_reply_chars_per_minute');
  assert.equal(edit.getAttribute('aria-expanded'), 'false');
  edit.dispatch('click'); assert.ok(visible(input)); assert.equal(edit.getAttribute('aria-expanded'), 'true');
  input.value = '300'; input.dispatch('input');
  edit.dispatch('click'); assert.equal(visible(input), false); assert.equal(edit.getAttribute('aria-expanded'), 'false');
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  edit.dispatch('click'); assert.ok(visible(input)); assert.equal(input.value, '300');
  edit.dispatch('click'); page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {human_reply_chars_per_minute: 300});
  assert.equal(page.get('input-human_reply_enabled').checked, false);
});

test('enabling then disabling a function ends temporary editing while preserving its inputs', async () => {
  const page = fixture({human_reply_enabled: false}); await settled(); page.navigate('human_reply');
  const edit = page.get('edit-human_reply-reply'), input = page.get('input-human_reply_chars_per_minute');
  edit.dispatch('click'); input.value = '300'; input.dispatch('input');
  assert.ok(visible(input));
  check(page, 'human_reply_enabled', true); assert.ok(visible(input));
  check(page, 'human_reply_enabled', false);
  assert.equal(visible(input), false);
  assert.equal(edit.getAttribute('aria-expanded'), 'false');
  assert.equal(page.get('input-human_reply_chars_per_minute'), input);
  assert.equal(input.value, '300');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  edit.dispatch('click'); assert.ok(visible(input));
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.calls.length, 0);
});

test('switching a function on and off from overview clears its earlier temporary edit state', async () => {
  const page = fixture({human_reply_enabled: false}); await settled(); page.navigate('human_reply');
  page.get('edit-human_reply-reply').dispatch('click');
  const input = page.get('input-human_reply_chars_per_minute'); input.value = '300'; input.dispatch('input');
  page.navigate('overview');
  check(page, 'human_reply_enabled', true); check(page, 'human_reply_enabled', false);
  page.navigate('human_reply');
  assert.equal(visible(page.get('input-human_reply_chars_per_minute')), false);
  assert.equal(page.get('edit-human_reply-reply').getAttribute('aria-expanded'), 'false');
  assert.equal(page.get('input-human_reply_chars_per_minute').value, 300);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  assert.equal(page.calls.length, 0);
});

test('global and cross-function dependencies keep their settings editable', async () => {
  for (const scenario of [
    {config: {enabled: false}, group: 'debounce', key: 'debounce_slow_idle_seconds', value: 1.6, dependency: 'enabled'},
    {config: {proactive_enabled: false}, group: 'selfwake', key: 'proactive_silent_wake_target_mean_minutes', value: 35, dependency: 'proactive_enabled'},
    {config: {activity_buffer_enabled: false}, group: 'activity', key: 'activity_require_admin', value: false, dependency: 'activity_buffer_enabled'},
  ]) {
    const page = fixture(scenario.config); await settled(); page.navigate(scenario.group);
    const input = page.get('input-' + scenario.key);
    assert.ok(visible(input), scenario.key);
    if (typeof scenario.value === 'boolean') { input.checked = scenario.value; input.dispatch('change'); }
    else { input.value = String(scenario.value); input.dispatch('input'); }
    assert.equal(page.get('input-' + scenario.key), input);
    page.get('save').dispatch('click'); await settled();
    assert.deepEqual(page.calls[0].body.config, {[scenario.key]: scenario.value});
    page.navigate(scenario.dependency === 'enabled' ? 'overview' : scenario.dependency === 'proactive_enabled' ? 'proactive' : 'activity');
    assert.equal(page.get('input-' + scenario.dependency).checked, false);
  }
});

test('an unrelated save retains hidden function and conditional parameter values', async () => {
  const page = fixture({human_reply_enabled: false, human_reply_chars_per_minute: 480, debounce_dynamic_enabled: true, merge_idle_seconds: 9});
  await settled(); page.navigate('interaction');
  const rounds = page.get('input-trigger_rounds'); rounds.value = '4'; rounds.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
  page.navigate('debounce');
  assert.equal(visible(page.get('input-merge_idle_seconds')), false);
  check(page, 'debounce_dynamic_enabled', false);
  assert.ok(visible(page.get('input-merge_idle_seconds')));
  assert.equal(page.get('input-merge_idle_seconds').value, 9);
  page.navigate('human_reply');
  assert.equal(visible(page.get('input-human_reply_chars_per_minute')), false);
  page.get('edit-human_reply-reply').dispatch('click');
  assert.ok(visible(page.get('input-human_reply_chars_per_minute')));
  assert.equal(page.get('input-human_reply_chars_per_minute').value, 480);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
});

test('guard parameters stay expanded when either the group or private switch is enabled', async () => {
  const page = fixture({include_group: false, include_private: false}); await settled(); page.navigate('interaction');
  const input = page.get('input-trigger_rounds');
  assert.equal(visible(input), false);
  check(page, 'include_group', true); assert.ok(visible(input));
  check(page, 'include_private', true); check(page, 'include_group', false); assert.ok(visible(input));
  check(page, 'include_private', false); assert.equal(visible(input), false);
  assert.equal(page.get('input-trigger_rounds'), input);
  page.get('edit-interaction-guard').dispatch('click'); assert.ok(visible(input));
  assert.equal(page.get('input-include_group').checked, false);
  assert.equal(page.get('input-include_private').checked, false);
  assert.equal(page.calls.length, 0);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
});

test('dynamic and fixed waiting controls switch visibility while keeping their inputs', async () => {
  const page = fixture({debounce_enabled: true, debounce_dynamic_enabled: true}); await settled(); page.navigate('debounce');
  const fast = page.get('input-debounce_fast_idle_seconds'), slow = page.get('input-debounce_slow_idle_seconds'), fixed = page.get('input-merge_idle_seconds');
  assert.ok(visible(fast)); assert.ok(visible(slow)); assert.equal(visible(fixed), false);
  check(page, 'debounce_dynamic_enabled', false);
  assert.equal(visible(fast), false); assert.equal(visible(slow), false); assert.ok(visible(fixed));
  check(page, 'debounce_dynamic_enabled', true);
  assert.ok(visible(fast)); assert.ok(visible(slow)); assert.equal(visible(fixed), false);
  assert.equal(page.get('input-debounce_fast_idle_seconds'), fast);
  assert.equal(page.get('input-debounce_slow_idle_seconds'), slow);
  assert.equal(page.get('input-merge_idle_seconds'), fixed);
});

test('input-status details follow their switch and retain their draft value', async () => {
  const page = fixture({debounce_enabled: true, debounce_private_enabled: true, debounce_typing_enabled: true}); await settled(); page.navigate('debounce');
  const hold = page.get('input-debounce_typing_hold_seconds');
  assert.ok(visible(hold)); hold.value = '6'; hold.dispatch('input');
  check(page, 'debounce_typing_enabled', false); assert.equal(visible(hold), false);
  check(page, 'debounce_typing_enabled', true); assert.ok(visible(hold));
  assert.equal(page.get('input-debounce_typing_hold_seconds'), hold);
  assert.equal(hold.value, '6');
});

test('prefetch keywords are only shown in prefetch mode without replacing the input', async () => {
  const page = fixture({activity_prefetch_enabled: false, capability_context_digest: false}); await settled(); page.navigate('activity');
  const keywords = page.get('input-activity_prefetch_keywords');
  assert.equal(visible(keywords), false);
  selectReference(page, 'prefetch'); assert.ok(visible(keywords));
  keywords.value = '最近聊什么\n翻记录'; keywords.dispatch('input');
  selectReference(page, 'background'); assert.equal(visible(keywords), false);
  selectReference(page, 'prefetch'); assert.ok(visible(keywords));
  assert.equal(page.get('input-activity_prefetch_keywords'), keywords);
  assert.equal(keywords.value, '最近聊什么\n翻记录');
  selectReference(page, 'tools'); assert.equal(visible(keywords), false);
});

test('autonomous tool choices and step limits follow the tool switch', async () => {
  const page = fixture({proactive_silent_wake_allow_tools: false, resident_extra_tools: ['safe_read']}, undefined, {resident_tools: [
    {name: 'safe_read', active: true, selectable: true},
  ]}); await settled(); page.navigate('selfwake');
  openAdvanced(page, 'proactive_silent_wake_max_steps');
  const tools = page.get('field-resident_extra_tools'), steps = page.get('input-proactive_silent_wake_max_steps');
  assert.equal(visible(tools), false); assert.equal(visible(steps), false);
  check(page, 'proactive_silent_wake_allow_tools', true);
  assert.ok(visible(tools)); assert.ok(visible(steps)); assert.equal(tools.querySelector('input').checked, true);
  check(page, 'proactive_silent_wake_allow_tools', false);
  assert.equal(visible(tools), false); assert.equal(visible(steps), false);
  assert.equal(page.get('field-resident_extra_tools'), tools);
  assert.equal(page.get('input-proactive_silent_wake_max_steps'), steps);
});

test('search reveals a disabled function and advanced ancestors only while filtering', async () => {
  const page = fixture({human_reply_enabled: false}); await settled(); page.navigate('human_reply');
  assert.equal(visible(page.get('input-human_reply_group_whitelist')), false);
  const stored = [...page.storage];
  search(page, 'human_reply_group_whitelist');
  const row = page.get('field-human_reply_group_whitelist');
  assert.equal(row.closest('.feature-card').id, 'feature-human_reply-reply');
  assert.ok(visible(row)); assert.equal(row.closest('details').open, true);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.deepEqual([...page.storage], stored);
  page.get('search-clear').dispatch('click');
  assert.equal(visible(page.get('input-human_reply_group_whitelist')), false);
  assert.equal(page.get('field-human_reply_group_whitelist').closest('details').open, false);
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
});

test('clearing search restores a manually expanded advanced section', async () => {
  const page = fixture({human_reply_enabled: true}); await settled(); page.navigate('human_reply');
  openAdvanced(page, 'human_reply_group_whitelist');
  const stored = [...page.storage];
  search(page, 'human_reply_group_blacklist');
  assert.ok(visible(page.get('input-human_reply_group_blacklist')));
  page.get('search-clear').dispatch('click');
  assert.equal(page.get('field-human_reply_group_whitelist').closest('details').open, true);
  assert.ok(visible(page.get('input-human_reply_group_whitelist')));
  assert.deepEqual([...page.storage], stored);
});

test('pending-only reveals changes under a disabled function without changing its switch', async () => {
  const personal = {...copy(defaults), human_reply_enabled: false, human_reply_chars_per_minute: 280};
  const page = fixture({human_reply_enabled: false}, undefined, {user_default: personal}); await settled(); page.navigate('human_reply');
  page.get('restore-default').dispatch('click');
  assert.equal(visible(page.get('input-human_reply_chars_per_minute')), false);
  page.get('pending-only').checked = true; page.get('pending-only').dispatch('change');
  const input = page.get('input-human_reply_chars_per_minute');
  assert.ok(visible(input)); assert.equal(input.value, 280);
  assert.equal(input.closest('.feature-card').id, 'feature-human_reply-reply');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {human_reply_chars_per_minute: 280});
});

test('deep field errors reveal and focus advanced parameters of a disabled function', async () => {
  const page = fixture({human_reply_enabled: false}, async () => ({status: 'error', message: '配置校验失败', field_errors: {human_reply_group_whitelist: '名单校验错误'}}));
  await settled(); page.navigate('interaction');
  const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input');
  page.get('save').dispatch('click'); await settled();
  const invalid = page.get('input-human_reply_group_whitelist');
  assert.ok(visible(invalid));
  assert.equal(invalid.closest('.feature-card').id, 'feature-human_reply-reply');
  assert.equal(invalid.closest('details').open, true);
  assert.equal(page.document.activeElement, invalid);
  assert.equal(page.get('error-human_reply_group_whitelist').textContent, '名单校验错误');
  assert.equal(page.get('input-human_reply_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
});

test('diagnostic links reveal conditionally hidden advanced parameters without editing settings', async () => {
  const page = fixture({debounce_dynamic_enabled: false}, undefined, {diagnostics: [{code: 'waiting', severity: 'warning', message: '检查完整句判断', fields: ['debounce_end_punctuation']}]});
  await settled(); page.get('diagnostics').querySelector('button').dispatch('click');
  const input = page.get('input-debounce_end_punctuation');
  assert.ok(visible(input));
  assert.equal(input.closest('.feature-card').id, 'feature-debounce-merge');
  assert.equal(input.closest('details').open, true);
  assert.equal(page.document.activeElement, input);
  assert.equal(page.get('input-debounce_dynamic_enabled').checked, false);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  assert.equal(page.calls.length, 0);
});

test('ordinary typing and related switch changes retain the focused input node', async () => {
  const page = fixture({debounce_enabled: true, debounce_dynamic_enabled: true}); await settled(); page.navigate('debounce');
  const input = page.get('input-debounce_slow_idle_seconds'); input.focus();
  for (const value of ['2', '2.4']) {
    input.value = value; input.dispatch('input');
    assert.equal(page.get('input-debounce_slow_idle_seconds'), input);
    assert.equal(page.document.activeElement, input);
    assert.equal(input.value, value);
  }
  check(page, 'debounce_typing_enabled', false);
  assert.equal(page.get('input-debounce_slow_idle_seconds'), input);
  assert.equal(page.document.activeElement, input);
  assert.ok(visible(input));
});

test('page save submits only edited fields, keeps in-flight input pending, and exposes errors', async () => {
  let resolveSave;
  const page = fixture({}, () => new Promise(resolve => { resolveSave = resolve; })); await settled(); page.navigate('interaction');
  let input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input'); page.get('save').dispatch('click');
  assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
  input.value = '6'; input.dispatch('input');
  resolveSave({status: 'ok', data: {config: {...defaults, trigger_rounds: 4}}}); await settled();
  assert.equal(page.get('input-trigger_rounds').value, 6);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  page.get('pending-only').checked = true; page.get('pending-only').dispatch('change');
  assert.ok(page.get('field-trigger_rounds')); assert.equal(page.get('field-window_seconds'), null);
});

test('server field errors stay attached to the correct editable field', async () => {
  const page = fixture({}, async () => ({status: 'error', message: '配置校验失败', field_errors: {trigger_rounds: '测试错误'}})); await settled(); page.navigate('interaction');
  const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input'); page.get('save').dispatch('click'); await settled();
  assert.equal(page.get('error-trigger_rounds').textContent, '测试错误');
  assert.equal(page.get('input-trigger_rounds').getAttribute('aria-invalid'), 'true');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
});

test('autonomous choices persist concrete names and never implicitly select new tools', async () => {
  const page = fixture({resident_extra_tools: ['safe_read']}, undefined, {resident_tools: [
    {name: 'list_conversations', base: true, active: true, selectable: true},
    {name: 'safe_read', active: true, selectable: true},
    {name: 'new_tool', active: true, selectable: true},
    {name: 'task', active: true, selectable: false},
  ]}); await settled(); page.navigate('selfwake');
  const tools = page.get('field-resident_extra_tools');
  const items = tools.querySelectorAll('input'); assert.equal(items.length, 3);
  assert.equal(items[0].checked, true); assert.equal(items[1].checked, false); assert.equal(items[2].disabled, true);
  items[0].checked = false; items[0].dispatch('change');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config.resident_extra_tools, []);
});

test('reselecting legacy background mode disables prefetch without changing unrelated fields', async () => {
  const page = fixture({activity_prefetch_enabled: true, capability_context_digest: true}); await settled(); page.navigate('activity');
  const reference = page.get('field-@reference');
  const background = reference.querySelectorAll('input').find(input => input.value === 'background');
  assert.equal(background.checked, true);
  background.dispatch('change');
  page.get('save').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0].body.config, {activity_prefetch_enabled: false, capability_context_digest: true});
});

test('applying defaults edits the draft; deleting the snapshot preserves unsaved input', async () => {
  const page = fixture({}, undefined, {user_default: {...copy(defaults), trigger_rounds: 5}}); await settled(); page.navigate('interaction');
  page.get('restore-default').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, 5);
  assert.equal(page.calls.length, 0);
  page.get('clear-default').dispatch('click'); await settled();
  assert.deepEqual(page.calls[0], {route: 'config/default', body: {clear: true}});
  assert.equal(page.get('input-trigger_rounds').value, 5);
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  assert.equal(page.get('restore-default').disabled, true);
  page.get('restore-factory').dispatch('click');
  assert.equal(page.get('input-trigger_rounds').value, defaults.trigger_rounds);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
});

test('wake validation rejects fractional values too close to the bounds', () => {
  const state = {...defaults, proactive_silent_wake_floor_minutes: 5, proactive_silent_wake_target_mean_minutes: 5.5, proactive_silent_wake_ceil_minutes: 6};
  assert.ok(ui.validateValues(state, {proactive_silent_wake_target_mean_minutes: 5.5}, schema).proactive_silent_wake_target_mean_minutes);
});

test('both save routes request bridge-safe field errors and leave baseline unchanged on validation failure', async () => {
  for (const asDefault of [false, true]) {
    const page = fixture({}, async () => {
      // AstrBot resolves successful API responses to their data, while error
      // responses become Error(message) and lose field_errors.
      const response = {status: 'ok', data: {validation_error: true, message: '配置校验失败', field_errors: {trigger_rounds: '宿主桥校验错误'}, errors: ['宿主桥校验错误']}};
      if (response.status !== 'ok') throw new Error(response.message);
      return response.data;
    });
    await settled(); page.navigate('interaction');
    const input = page.get('input-trigger_rounds'); input.value = '4'; input.dispatch('input');
    page.get(asDefault ? 'save-default' : 'save').dispatch('click'); await settled();
    assert.equal(page.calls[0].route, asDefault ? 'config/default' : 'config');
    assert.equal(page.calls[0].body.return_validation_errors, true);
    assert.deepEqual(page.calls[0].body.config, {trigger_rounds: 4});
    assert.equal(page.get('error-trigger_rounds').textContent, '宿主桥校验错误');
    assert.equal(page.get('dirty').textContent, '待保存 1 项');
    page.get('revert').dispatch('click');
    assert.equal(page.get('input-trigger_rounds').value, defaults.trigger_rounds);
  }
});

test('saving an unchanged configuration as default still validates existing wake relationships', async () => {
  const page = fixture({proactive_silent_wake_floor_minutes: 30, proactive_silent_wake_target_mean_minutes: 6, proactive_silent_wake_ceil_minutes: 10});
  await settled(); page.get('save-default').dispatch('click'); await settled();
  assert.equal(page.calls.length, 0);
  assert.ok(page.get('error-proactive_silent_wake_floor_minutes').textContent);
  assert.equal(page.get('dirty').textContent, '没有待保存的修改');
  assert.ok(ui.validateValues({...defaults, proactive_silent_wake_floor_minutes: 30, proactive_silent_wake_target_mean_minutes: 6, proactive_silent_wake_ceil_minutes: 10}, {}, schema).proactive_silent_wake_floor_minutes);
});

test('dedicated-page titles remove native category prefixes without editing schema', () => {
  const spec = {description: '【消息合并】群聊启用'};
  assert.equal(ui.fieldTitle(spec, 'sample'), '群聊启用');
  assert.equal(spec.description, '【消息合并】群聊启用');
});

test('overview offers keyboard-accessible switches and links instead of nested buttons', async () => {
  const page = fixture(); await settled();
  const cards = page.document.querySelectorAll('.overview-card'); assert.equal(cards.length, 6);
  for (const card of cards) { assert.equal(card.tagName, 'SECTION'); assert.ok(card.querySelector('input')); assert.ok(card.querySelector('.overview-link')); assert.ok(card.querySelector('.overview-condition')); }
  assert.ok(page.get('input-debounce_enabled')); assert.ok(page.get('input-human_reply_enabled'));
  assert.ok(page.get('input-include_group')); assert.ok(page.get('input-include_private'));
  assert.ok(page.get('input-poke_reply_enabled')); assert.ok(page.get('input-keyword_wake_enabled'));
  assert.ok(page.get('input-activity_buffer_enabled')); assert.ok(page.get('input-lmem_gateway_enabled'));
  assert.ok(page.get('input-proactive_enabled')); assert.ok(page.get('input-proactive_silent_wake_enabled'));
  const proactive = page.get('input-proactive_enabled'); proactive.checked = false; proactive.dispatch('change');
  assert.equal(cards.find(card => card.dataset.group === 'selfwake').querySelector('.status-chip').textContent, '主动消息关闭');
  assert.equal(page.get('dirty').textContent, '待保存 1 项');
  assert.equal(page.calls.length, 0);
});

test('overview derives inactive states from feature dependencies', () => {
  assert.equal(ui.overviewStatus('debounce', {...defaults, debounce_group_enabled: false, debounce_private_enabled: false}).active, false);
  assert.equal(ui.overviewStatus('selfwake', {...defaults, proactive_silent_wake_daily_budget: 0}).label, '日预算为 0');
  assert.equal(ui.overviewStatus('selfwake', {...defaults, proactive_enabled: false}).active, false);
  assert.equal(ui.overviewStatus('advanced', defaults), null);
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
