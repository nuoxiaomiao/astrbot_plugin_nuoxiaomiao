/* 糯小喵配置 — 从零重做版。
 * 设计约束：
 *  - 纯静态文件，无构建步骤、无外部依赖，由 AstrBot 直接服务。
 *  - 通过 window.AstrBotPluginPage 桥接（apiGet/apiPost/ready）。
 *  - 单页平铺：按 schema group 分组，所有字段常显，搜索即达，底部保存。
 *  - 分组从 _conf_schema.json 的 group 字段推导，保证 125 键全覆盖、不重复。
 *  - 导出纯函数供 tests/test_config_ui.js 使用（UMD）。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  /* ============================ 常量 ============================ */
  var CORE_PLUGIN = 'astrbot_plugin_nuoxiaomiao';
  var REFERENCE_KEYS = ['activity_prefetch_enabled', 'capability_context_digest'];
  var WAKE_KEYS = ['proactive_silent_wake_floor_minutes', 'proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_ceil_minutes'];
  var TIME_KEYS = ['proactive_quiet_hours_start', 'proactive_quiet_hours_end'];
  var HIDDEN_KEYS = ['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  var TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

  /* ============================ 分组推导（数据驱动） ============================ */
  /* GROUP_META 只提供稳定 id 与一句话说明；字段列表完全来自 schema 的 group。 */
  var GROUP_META = [
    { id: 'overview', title: '总览', note: '插件总开关。关闭后所有功能暂停，设置保留。' },
    { id: 'debounce', title: '消息合并', note: '把同一人连着发的多条消息拼成一条再回复，减少来回打断、省 token。' },
    { id: 'human_reply', title: '回复节奏', note: '让机器人像真人打字一样分条发送回复，而不是一次甩一大段。' },
    { id: 'interaction', title: '拦截与互动', note: '三件事互不干扰：刷屏拦截、完全屏蔽名单、被戳与关键词回应。' },
    { id: 'activity', title: '聊天记录与参考', note: '把最近的聊天存下来，让机器人能回忆、翻别的群，并按需要参考聊天或长期记忆。' },
    { id: 'proactive', title: '主动消息', note: '允许机器人主动找人说话（比如你让它「明天提醒我」）。只在允许名单内生效，有次数和冷却限制。' },
    { id: 'selfwake', title: '自醒', note: '群里安静时机器人自己醒来想想：要不要问候、查点什么、整理状态。每天有次数上限。' },
    { id: 'advanced', title: '高级', note: '通用工具提示词、能力说明与兼容参数，通常保持默认即可。' },
    { id: 'resident_state', title: '自主状态', note: '机器人自主运行时的心情、关注点与表达风格（管理入口在页面底部「自主运行与状态」）。' },
    { id: 'shared_memory', title: '共享记忆与信箱', note: '四项进阶能力，默认全部关闭：全局主脑、未读信箱、话题概括、群聊识图。' },
  ];
  function deriveGroups(schema) {
    var metaByTitle = {}, byTitle = {}, result = [];
    GROUP_META.forEach(function (meta) { metaByTitle[meta.title] = meta; });
    function ensure(title) {
      if (byTitle[title]) return byTitle[title];
      var meta = metaByTitle[title] || { id: 'group' + (result.length + 1), title: title, note: '' };
      var group = { id: meta.id, title: meta.title, note: meta.note || '', fields: [] };
      byTitle[title] = group; result.push(group); return group;
    }
    Object.keys(schema).forEach(function (key) {
      if (HIDDEN_KEYS.indexOf(key) >= 0) return;
      if (REFERENCE_KEYS.indexOf(key) >= 0 && key !== 'activity_prefetch_enabled') return;
      var spec = schema[key];
      if (!spec || typeof spec !== 'object') return;
      ensure(String(spec.group || '其他')).fields.push(key);
    });
    result.forEach(function (group) {
      var idx = group.fields.indexOf('activity_prefetch_enabled');
      if (idx >= 0) group.fields.splice(idx, 1, '@reference');
    });
    return result;
  }
  function groupOf(key, groups) {
    var target = REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key;
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].fields.indexOf(target) >= 0) return groups[i];
    }
    return groups[0];
  }

  /* ============================ 纯工具 ============================ */
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function fieldTitle(spec, key) { return String((spec || {}).description || key).replace(/^【[^】]+】\s*/, ''); }
  function parseList(value) {
    var list = Array.isArray(value) ? value : String(value || '').split(/[\n,，、;；]+/);
    return Array.from(new Set(list.map(function (x) { return String(x).trim(); }).filter(Boolean)));
  }
  function valueText(value) {
    return value === '' ? '留空'
      : Array.isArray(value) ? value.length ? value.join('、') : '空名单'
      : value === true ? '开启' : value === false ? '关闭' : String(value);
  }

  /* ============================ 聊天参考模式（覆盖两个键） ============================ */
  function referenceMode(value) {
    return value.capability_context_digest ? 'background' : value.activity_prefetch_enabled ? 'prefetch' : 'tools';
  }
  function referenceValues(mode) {
    return { activity_prefetch_enabled: mode === 'prefetch', capability_context_digest: mode === 'background' };
  }

  /* ============================ 草稿状态机 ============================ */
  function dirtyKeys(state, baseline, schema) {
    return Object.keys(schema).filter(function (key) { return HIDDEN_KEYS.indexOf(key) < 0 && !same(state[key], baseline[key]); });
  }
  function buildPatch(state, baseline, schema) {
    var result = {};
    dirtyKeys(state, baseline, schema).forEach(function (key) { result[key] = clone(state[key]); });
    if (REFERENCE_KEYS.some(function (key) { return own(result, key); })) {
      Object.assign(result, referenceValues(referenceMode(state)));
    }
    return result;
  }
  function mergeSavedState(current, baseline, snapshot, patch, server, schema) {
    var next = clone(current), nextBaseline = clone(baseline);
    Object.keys(patch).forEach(function (key) {
      if (!own(schema, key)) return;
      var value = own(server, key) ? server[key] : patch[key];
      nextBaseline[key] = clone(value);
      if (same(current[key], snapshot[key])) next[key] = clone(value);
    });
    return { state: next, baseline: nextBaseline };
  }
  function validateValues(state, patch, schema) {
    var errors = {};
    Object.keys(patch).forEach(function (key) {
      var spec = schema[key] || {}, value = patch[key];
      if (spec.type === 'int' || spec.type === 'float') {
        if (value === '' || value === null || typeof value !== 'number' || !Number.isFinite(value)) errors[key] = '请输入有效数字。';
        else if (spec.type === 'int' && !Number.isInteger(value)) errors[key] = '请输入整数。';
        else if (spec.slider && (value < spec.slider.min || value > spec.slider.max)) errors[key] = '请输入 ' + spec.slider.min + ' 到 ' + spec.slider.max + ' 之间的数值。';
      }
      if (TIME_KEYS.indexOf(key) >= 0 && !TIME_RE.test(String(value))) errors[key] = '请选择 00:00 到 23:59 的时间。';
    });
    if (WAKE_KEYS.every(function (key) { return own(state, key); })) {
      var floor = state[WAKE_KEYS[0]], mean = state[WAKE_KEYS[1]], ceil = state[WAKE_KEYS[2]];
      if (!(ceil >= floor + 2 && mean >= floor + 1 && mean <= ceil - 1)) WAKE_KEYS.forEach(function (key) { errors[key] = '上限至少比下限多 2 分钟；平均间隔须在下限 + 1 与上限 − 1 之间。'; });
    }
    return errors;
  }
  function errorFields(value) {
    if (!value || typeof value !== 'object') return {};
    if (value.field_errors && typeof value.field_errors === 'object') return value.field_errors;
    if (value.data && typeof value.data === 'object') return errorFields(value.data);
    if (value.response) return errorFields(value.response);
    return {};
  }

  /* ============================ 搜索 ============================ */
  function matches(key, spec, query, groupTitle) {
    var text = [key, fieldTitle(spec, key), spec.description || '', spec.hint || '', groupTitle || ''].join(' ').toLowerCase();
    return text.indexOf(query.toLowerCase()) >= 0;
  }

  /* ============================ 页面应用 ============================ */
  function mount(win) {
    var doc = win.document;
    var schema = {}, state = {}, baseline = {}, defaults = {}, userDefault = null;
    var groups = [], providers = [], residentTools = [], diagnostics = [], timezone = '';
    var query = '', saving = false, loaded = false;
    var fieldErrors = {}, toastTimer = null;
    var residentMounted = false;

    function $(id) { return doc.getElementById(id); }
    function node(tag, cls, text) { var n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); return n; }
    function button(text, handler, cls) { var n = node('button', cls, text); n.type = 'button'; n.addEventListener('click', handler); return n; }
    function showToast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(function () { $('toast').hidden = true; }, 5000); }
    function defaultOf(key) { return userDefault && own(userDefault, key) ? userDefault[key] : defaults[key]; }

    /* 某个字段当前为何不生效（功能开关关闭时的小灰字）。 */
    function reason(key) {
      if (key === 'enabled' || !state.enabled) return '';
      if (/^debounce_/.test(key) && key !== 'debounce_enabled' && !state.debounce_enabled) return '消息合并已关闭；可以先编辑，开启后生效。';
      if (key === 'merge_localize_images_dir' && !state.merge_localize_images) return '图片转存关闭，这个目录暂不使用。';
      if (/^human_reply_/.test(key) && key !== 'human_reply_enabled' && !state.human_reply_enabled) return '回复节奏已关闭；可以先编辑，开启后生效。';
      if (/^poke_/.test(key) && key !== 'poke_reply_enabled' && !state.poke_reply_enabled) return '被戳回应已关闭，这项暂不生效。';
      if (/^keyword_wake_/.test(key) && key !== 'keyword_wake_enabled' && !state.keyword_wake_enabled) return '关键词唤醒已关闭，这项暂不生效。';
      if (['trigger_rounds', 'window_seconds', 'user_whitelist', 'user_blacklist', 'meme_tags', 'meme_cooldown_seconds', 'guard_group_whitelist', 'guard_group_blacklist'].indexOf(key) >= 0 && !state.include_group && !state.include_private) return '群聊和私聊刷屏拦截均关闭，这项暂不生效。';
      if (/^activity_/.test(key) && key !== 'activity_buffer_enabled' && !state.activity_buffer_enabled) return '聊天记录已关闭，这项暂不生效。';
      if (key === 'activity_prefetch_keywords' && referenceMode(state) !== 'prefetch') return '仅在“关键词预取”模式下使用。';
      if (/^lmem_gateway_/.test(key) && key !== 'lmem_gateway_enabled' && !state.lmem_gateway_enabled) return '长期记忆检索已关闭，这项暂不生效。';
      if (/^proactive_/.test(key) && key !== 'proactive_enabled' && !state.proactive_enabled) return '主动消息已关闭，自醒也暂停。';
      if (/^proactive_silent_wake_/.test(key) && key !== 'proactive_silent_wake_enabled' && (!state.proactive_silent_wake_enabled || state.proactive_silent_wake_daily_budget === 0)) return '自醒已关闭或日预算为 0，这项暂不生效。';
      if (key === 'resident_extra_tools' && !state.proactive_silent_wake_allow_tools) return '自醒未允许调用工具，这项暂不使用。';
      if (key === 'tool_call_prompt' && !state.tool_call_prompt_enabled) return '自定义工具提示已关闭，这段文案暂不生效。';
      if (/^master_/.test(key) && !state.shared_memory_enabled) return '全局主脑共享记忆已关闭；可以先编辑，开启后生效。';
      if (/^inbox_group_/.test(key) && !state.unread_inbox_enabled) return '未读信箱分流已关闭，群名单暂不生效。';
      if (key === 'mention_notice_limit' && !state.mention_notice_enabled) return '点名通知卡已关闭，这项暂不生效。';
      if (/^topic_/.test(key) && key !== 'topic_summary_enabled' && !state.topic_summary_enabled) return '话题概括已关闭；可以先编辑，开启后生效。';
      if (/^vision_|^image_keep_days$/.test(key) && key !== 'vision_enabled' && !state.vision_enabled) return '群聊识图已关闭；可以先编辑，开启后生效。';
      return '';
    }

    /* ---------- 控件 ---------- */
    function labelInput(input, key) { input.id = 'input-' + key; input.setAttribute('aria-label', fieldTitle(schema[key], key)); return input; }
    function optionsControl(name, value, options, change) {
      var wrap = node('div', 'mode-options');
      options.forEach(function (item) {
        var label = node('label', 'mode-option'), input = node('input');
        input.type = 'radio'; input.name = name; input.value = item.id; input.checked = item.id === value;
        input.addEventListener('change', function () { if (input.checked) change(item.id); });
        var text = node('span'); text.appendChild(node('strong', '', item.title)); text.appendChild(node('small', '', item.note));
        label.appendChild(input); label.appendChild(text); wrap.appendChild(label);
      });
      return wrap;
    }
    function referenceControl() {
      var wrap = node('div', 'field block'); wrap.dataset.key = '@reference'; wrap.id = 'field-@reference';
      wrap.appendChild(node('strong', 'field-label', '别处聊天如何进入当前回复'));
      wrap.appendChild(node('p', 'field-hint', '聊天参考不改变记录范围和查看权限。每轮背景会增加上下文，也可能减少模型主动查工具的次数。'));
      wrap.appendChild(optionsControl('reference-mode', referenceMode(state), [
        { id: 'tools', title: '按需工具查询', note: '不自动附带记录，由模型需要时调用速览工具。' },
        { id: 'prefetch', title: '关键词预取', note: '问到别处聊天或翻记录时，自动附带近期记录。' },
        { id: 'background', title: '每轮背景', note: '带工具的回复每轮附带别处近况，不再同时关键词预取。' },
      ], function (mode) { Object.assign(state, referenceValues(mode)); REFERENCE_KEYS.forEach(function (key) { delete fieldErrors[key]; }); refresh(); }));
      var reasonNode = node('p', 'field-reason'); reasonNode.id = 'reason-@reference'; wrap.appendChild(reasonNode);
      var error = node('p', 'field-error'); error.id = 'error-@reference'; wrap.appendChild(error);
      var reset = button('恢复' + (userDefault ? '我的默认' : '出厂默认'), function () { REFERENCE_KEYS.forEach(function (key) { state[key] = clone(defaultOf(key)); }); renderGroups(); refresh(); }, 'restore-field');
      wrap.appendChild(reset); return wrap;
    }
    function residentToolsControl(key) {
      var wrap = node('div', 'tool-list'), names = parseList(state[key]), seen = [];
      residentTools.filter(function (item) { return !item.base; }).forEach(function (item) {
        seen.push(item.name);
        var label = node('label', 'tool-option'), input = node('input'); input.type = 'checkbox'; input.value = item.name; input.checked = names.indexOf(item.name) >= 0;
        input.disabled = (!item.active || !item.selectable) && !input.checked;
        input.addEventListener('change', function () { var next = parseList(state[key]).filter(function (name) { return name !== item.name; }); if (input.checked) next.push(item.name); onChange(key, next); });
        label.appendChild(input); label.appendChild(node('span', '', item.name + (item.reason ? ' · ' + item.reason : !item.active ? ' · 已停用' : ''))); wrap.appendChild(label);
      });
      names.filter(function (name) { return seen.indexOf(name) < 0; }).forEach(function (name) {
        var label = node('label', 'tool-option'), input = node('input'); input.type = 'checkbox'; input.checked = true;
        input.addEventListener('change', function () { onChange(key, parseList(state[key]).filter(function (x) { return x !== name; })); });
        label.appendChild(input); label.appendChild(node('span', '', name + ' · 当前不可用，可取消选择')); wrap.appendChild(label);
      });
      return wrap;
    }
    function providerControl(key) {
      var holder = node('div'), input = labelInput(node('select'), key);
      var auto = node('option', '', '使用全局默认模型'); auto.value = ''; input.appendChild(auto);
      providers.forEach(function (provider) { var o = node('option', '', provider.label || provider.id); o.value = provider.id; input.appendChild(o); });
      if (state[key] && !providers.some(function (provider) { return provider.id === state[key]; })) {
        var old = node('option', '', '当前不可用：' + state[key]); old.value = state[key]; input.appendChild(old);
        holder.appendChild(node('small', 'provider-warning', '原配置的模型当前不可用，已保留。请选择可用模型后保存。'));
      }
      input.value = state[key] || '';
      input.addEventListener('change', function () { onChange(key, input.value); var warning = holder.querySelector('.provider-warning'); if (warning) warning.remove(); });
      holder.prepend(input); return holder;
    }
    function buildControl(key, spec) {
      var value = state[key], input;
      if (key === 'resident_extra_tools') return residentToolsControl(key);
      if (/_provider_id$/.test(key) && spec.type === 'string') return providerControl(key);
      if (key === 'merge_join_separator') {
        input = labelInput(node('select'), key);
        [['\n', '换行（推荐）'], [' ', '空格'], ['@custom', '自定义']].forEach(function (item) { var o = node('option', '', item[1]); o.value = item[0]; input.appendChild(o); });
        input.value = value === '\n' || value === ' ' ? value : '@custom';
        var custom = node('textarea'); custom.rows = 2; custom.value = value || ''; custom.setAttribute('aria-label', '自定义合并分隔符'); custom.hidden = input.value !== '@custom';
        input.addEventListener('change', function () { custom.hidden = input.value !== '@custom'; onChange(key, custom.hidden ? input.value : custom.value); });
        custom.addEventListener('input', function () { onChange(key, custom.value); });
        var holder = node('div'); holder.appendChild(input); holder.appendChild(custom); return holder;
      }
      if (spec.type === 'bool') {
        var label = node('label', 'switch'); input = labelInput(node('input'), key); input.type = 'checkbox'; input.checked = !!value;
        var text = node('span', 'switch-text', input.checked ? '开' : '关');
        input.addEventListener('change', function () { text.textContent = input.checked ? '开' : '关'; onChange(key, input.checked); });
        label.appendChild(input); label.appendChild(text); return label;
      }
      if (spec.type === 'list') {
        var listHolder = node('div'); input = labelInput(node('textarea'), key); input.rows = 3; input.value = parseList(value).join('\n'); input.placeholder = '每行一项，也可用逗号分隔';
        var foot = node('div', 'list-footer'), count = node('span', '', parseList(value).length + ' 项');
        input.addEventListener('input', function () { var list = parseList(input.value); count.textContent = list.length + ' 项'; onChange(key, list); });
        foot.appendChild(count); foot.appendChild(button('清空名单', function () { input.value = ''; count.textContent = '0 项'; onChange(key, []); }));
        listHolder.appendChild(input); listHolder.appendChild(foot); return listHolder;
      }
      if (spec.type === 'text') { input = labelInput(node('textarea', 'prompt'), key); input.rows = 5; }
      else { input = labelInput(node('input'), key); input.type = TIME_KEYS.indexOf(key) >= 0 ? 'time' : spec.type === 'int' || spec.type === 'float' ? 'number' : 'text'; }
      input.value = value === null || value === undefined ? '' : value;
      if (input.type === 'number') { input.step = spec.type === 'int' ? 1 : 'any'; if (spec.slider) { input.min = spec.slider.min; input.max = spec.slider.max; } }
      input.addEventListener('input', function () { onChange(key, input.type === 'number' ? input.value === '' ? '' : Number(input.value) : input.value); });
      return input;
    }
    function field(key) {
      if (key === '@reference') return referenceControl();
      var spec = schema[key]; if (!spec) return null;
      var isBlock = spec.type === 'list' || spec.type === 'text' || key === 'merge_join_separator';
      var row = node('div', 'field' + (isBlock ? ' block' : '')); row.dataset.key = key; row.id = 'field-' + key;
      var copy = node('div', 'field-copy'), label = node('label', 'field-label', fieldTitle(spec, key)); label.htmlFor = 'input-' + key; copy.appendChild(label);
      var hint = node('p', 'field-hint', spec.hint || ''); hint.id = 'hint-' + key; copy.appendChild(hint);
      var reasonNode = node('p', 'field-reason'); reasonNode.id = 'reason-' + key; copy.appendChild(reasonNode);
      var errorNode = node('p', 'field-error'); errorNode.id = 'error-' + key; copy.appendChild(errorNode);
      var reset = button('恢复' + (userDefault && own(userDefault, key) ? '我的默认' : '出厂默认'), function () { state[key] = clone(defaultOf(key)); delete fieldErrors[key]; renderGroups(); refresh(); }, 'restore-field'); reset.title = valueText(defaultOf(key)); copy.appendChild(reset);
      row.appendChild(copy);
      var control = node('div', 'field-control'); control.appendChild(buildControl(key, spec)); row.appendChild(control);
      return row;
    }

    /* ---------- 渲染 ---------- */
    function visibleField(key, group) {
      if (key === '@reference') {
        var text = '聊天参考 别处聊天如何进入当前回复 按需工具查询 关键词预取 每轮背景 activity_prefetch_enabled capability_context_digest';
        return !query || text.toLowerCase().indexOf(query) >= 0;
      }
      return !query || matches(key, schema[key] || {}, query, group.title);
    }
    function renderGroup(group) {
      var keys = group.fields.filter(function (key) { return visibleField(key, group); });
      if (!keys.length) return null;
      var section = node('section', 'group'); section.id = 'group-' + group.id; section.dataset.group = group.id;
      var title = node('h2', 'group-title', group.title);
      var pending = node('span', 'pending'); pending.id = 'pending-' + group.id; title.appendChild(pending);
      section.appendChild(title);
      if (group.note && !query) section.appendChild(node('p', 'group-note', group.note));
      var body = node('div', 'group-fields');
      keys.forEach(function (key) { var row = field(key); if (row) body.appendChild(row); });
      section.appendChild(body);
      return section;
    }
    function renderGroups() {
      var groupsEl = $('groups'); groupsEl.replaceChildren();
      if (!Object.keys(schema).length) return;
      var count = 0;
      groups.forEach(function (group) { var section = renderGroup(group); if (section) { groupsEl.appendChild(section); count += 1; } });
      if (!count) groupsEl.appendChild(node('div', 'empty', '没有找到匹配项。可以搜索“私聊”“名单”“自醒”等词。'));
    }
    function renderDiagnostics() {
      var box = $('diagnostics'); box.replaceChildren();
      if (!diagnostics.length) return;
      diagnostics.forEach(function (item) {
        var row = node('div', 'diag ' + (item.severity === 'error' ? 'error' : item.severity === 'info' ? 'info' : 'warning'));
        row.appendChild(node('span', '', item.message || item.code || ''));
        var links = node('span', 'links');
        (item.fields || []).filter(function (key) { return schema[key]; }).slice(0, 3).forEach(function (key) { links.appendChild(button('查看设置', function () { focusField(key); })); });
        row.appendChild(links); box.appendChild(row);
      });
    }
    function renderErrors() {
      var box = $('error-summary'), keys = Object.keys(fieldErrors); box.replaceChildren(); box.hidden = !keys.length;
      if (keys.length) { box.appendChild(node('strong', '', '有 ' + keys.length + ' 项需要修正，尚未保存。')); keys.forEach(function (key) { box.appendChild(button(fieldTitle(schema[key], key), function () { focusField(key); })); }); }
    }
    function refresh() {
      var keys = dirtyKeys(state, baseline, schema);
      $('dirty').textContent = saving ? '正在保存…' : keys.length ? '待保存 ' + keys.length + ' 项' : '没有待保存的修改';
      $('save-detail').textContent = saving ? '保存期间继续编辑的内容会保留' : keys.length ? '点击保存后生效 · Ctrl / ⌘ + S' : loaded ? '当前修改均已保存' : '配置加载后可编辑';
      $('save').disabled = !loaded || saving || !keys.length;
      $('revert').disabled = !loaded || saving || !keys.length;
      $('restore-factory').disabled = !loaded || saving;
      $('search-clear').hidden = !query;
      groups.forEach(function (group) {
        var badge = $('pending-' + group.id);
        if (!badge) return;
        var count = group.fields.filter(function (key) { return key !== '@reference' && keys.indexOf(key) >= 0; }).length + (group.fields.indexOf('@reference') >= 0 && REFERENCE_KEYS.some(function (key) { return keys.indexOf(key) >= 0; }) ? 1 : 0);
        badge.textContent = count ? '待保存 ' + count + ' 项' : '';
        badge.hidden = !count;
      });
      doc.querySelectorAll('.field[data-key]').forEach(function (row) {
        var key = row.dataset.key;
        var pending = key === '@reference' ? REFERENCE_KEYS.some(function (k) { return keys.indexOf(k) >= 0; }) : keys.indexOf(key) >= 0;
        row.classList.toggle('is-pending', pending);
        var error = key === '@reference' ? REFERENCE_KEYS.map(function (k) { return fieldErrors[k]; }).filter(Boolean).join('；') : fieldErrors[key];
        var errorNode = $('error-' + key);
        if (errorNode) { errorNode.textContent = Array.isArray(error) ? error.join('；') : error || ''; errorNode.hidden = !error; }
        row.querySelectorAll('input,textarea,select').forEach(function (input) { input.setAttribute('aria-invalid', error ? 'true' : 'false'); });
        var reasonNode = $('reason-' + key), why = key === '@reference' ? reason('activity_prefetch_enabled') : reason(key);
        if (reasonNode) { reasonNode.textContent = why; reasonNode.hidden = !why; }
      });
      var banner = $('banner');
      banner.hidden = !!baseline.enabled && !!state.enabled;
      banner.textContent = !baseline.enabled ? '当前运行：插件已关闭。' + (state.enabled ? '草稿已开启，保存后生效。' : '可先编辑设置，开启总开关并保存后恢复功能。') : '草稿已关闭总开关；保存后插件功能暂停。';
      renderErrors();
    }
    function focusField(key) {
      var target = REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key;
      query = ''; $('search').value = ''; renderGroups(); refresh();
      var row = $('field-' + target); if (!row) return;
      row.scrollIntoView({ block: 'center', behavior: 'smooth' }); var input = row.querySelector('input,textarea,select'); if (input) input.focus({ preventScroll: true });
    }
    function onChange(key, value) {
      state[key] = value; delete fieldErrors[key];
      if (WAKE_KEYS.indexOf(key) >= 0) WAKE_KEYS.forEach(function (k) { delete fieldErrors[k]; });
      refresh();
    }

    /* ---------- 保存 ---------- */
    async function save() {
      if (!loaded || saving) return;
      var snapshot = clone(state), patch = buildPatch(snapshot, baseline, schema);
      if (!Object.keys(patch).length) { showToast('没有待保存的修改'); return; }
      fieldErrors = validateValues(snapshot, patch, schema);
      if (Object.keys(fieldErrors).length) { refresh(); focusField(Object.keys(fieldErrors)[0]); return; }
      saving = true; refresh();
      try {
        var response = await win.AstrBotPluginPage.apiPost('config', { config: clone(patch), return_validation_errors: true });
        if (response && response.status === 'error') throw response;
        var data = response && response.data && typeof response.data === 'object' ? response.data : response || {};
        if (data.validation_error) throw data;
        var merged = mergeSavedState(state, baseline, snapshot, patch, data.config || {}, schema);
        state = merged.state; baseline = merged.baseline; fieldErrors = {};
        if (Array.isArray(data.diagnostics)) diagnostics = data.diagnostics;
        renderDiagnostics(); renderGroups(); refresh();
        var hasNewEdits = dirtyKeys(state, baseline, schema).length > 0;
        showToast(hasNewEdits ? '已保存提交的修改；保存期间的新修改仍待保存。' : '配置已保存并生效。');
      } catch (error) {
        fieldErrors = errorFields(error); renderGroups(); refresh();
        showToast('保存失败：' + (error && error.message ? error.message : '请重试'));
        if (Object.keys(fieldErrors).length) focusField(Object.keys(fieldErrors)[0]);
      } finally { saving = false; refresh(); }
    }
    function revert() {
      if (!loaded || saving) return;
      state = clone(baseline); fieldErrors = {}; renderGroups(); refresh(); showToast('已撤销待保存的修改。');
    }
    function restoreFactory() {
      if (!loaded || saving) return;
      Object.keys(schema).forEach(function (key) { state[key] = clone(defaults[key]); });
      fieldErrors = {}; renderGroups(); refresh(); showToast('已恢复出厂默认，保存后生效。');
    }

    /* ---------- 初始化 ---------- */
    function wire(id, type, fn) { var el = $(id); if (el) el.addEventListener(type, fn); }
    function init() {
      wire('save', 'click', save);
      wire('revert', 'click', revert);
      wire('restore-factory', 'click', restoreFactory);
      wire('search', 'input', function () { query = $('search').value.trim().toLowerCase(); renderGroups(); refresh(); });
      wire('search-clear', 'click', function () { $('search').value = ''; query = ''; renderGroups(); refresh(); });
      var resident = $('resident');
      if (resident) resident.addEventListener('toggle', function () {
        if (resident.open && !residentMounted && win.NuoxiaomiaoResidentPanel) { residentMounted = true; win.NuoxiaomiaoResidentPanel.mount($('resident-root'), win); }
      });
      doc.addEventListener('keydown', function (event) { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); } });
      win.addEventListener('beforeunload', function (event) { if (!dirtyKeys(state, baseline, schema).length) return; event.preventDefault(); event.returnValue = ''; });
    }
    async function load() {
      try {
        var response = await win.AstrBotPluginPage.apiGet('config');
        if (response && response.status === 'error') throw response;
        var data = response && response.data && typeof response.data === 'object' ? response.data : response || {};
        schema = data.schema || {}; if (!Object.keys(schema).length) throw new Error('没有取得配置字段，请重试或重新加载插件。');
        defaults = data.defaults || {}; userDefault = data.user_default || null;
        if (Array.isArray(data.providers)) providers = data.providers;
        if (Array.isArray(data.resident_tools)) residentTools = data.resident_tools;
        if (Array.isArray(data.diagnostics)) diagnostics = data.diagnostics;
        if (typeof data.timezone === 'string') timezone = data.timezone;
        $('timezone').textContent = timezone ? '机器人时区：' + timezone : '';
        groups = deriveGroups(schema);
        state = {}; Object.keys(schema).forEach(function (key) { state[key] = clone(own(data.config, key) ? data.config[key] : defaults[key]); });
        baseline = clone(state);
        loaded = true; fieldErrors = {}; renderDiagnostics(); renderGroups(); refresh();
        $('boot').hidden = true; $('app').hidden = false;
      } catch (error) {
        $('boot').replaceChildren(); $('boot').appendChild(node('strong', '', '配置加载失败')); $('boot').appendChild(node('p', '', error.message || '无法读取配置。')); $('boot').appendChild(button('重新加载', load, 'btn primary'));
        $('boot').hidden = false; $('app').hidden = true;
      }
    }
    async function start() {
      init();
      try {
        await new Promise(function (resolve, reject) { if (win.AstrBotPluginPage) return resolve(); var start = Date.now(); var timer = setInterval(function () { if (win.AstrBotPluginPage) { clearInterval(timer); resolve(); } else if (Date.now() - start > 6000) { clearInterval(timer); reject(new Error('请从 AstrBot 的插件页面打开配置页。')); } }, 50); });
        if (typeof win.AstrBotPluginPage.ready === 'function') await win.AstrBotPluginPage.ready();
        await load();
      } catch (error) {
        $('boot').replaceChildren(); $('boot').appendChild(node('strong', '', '无法连接 AstrBot')); $('boot').appendChild(node('p', '', error.message || '请重新打开插件页面。'));
      }
    }
    start();
  }

  return {
    CORE_PLUGIN: CORE_PLUGIN, GROUP_META: GROUP_META, deriveGroups: deriveGroups, groupOf: groupOf, fieldTitle: fieldTitle,
    parseList: parseList, valueText: valueText, referenceMode: referenceMode, referenceValues: referenceValues,
    dirtyKeys: dirtyKeys, buildPatch: buildPatch, mergeSavedState: mergeSavedState,
    validateValues: validateValues, errorFields: errorFields, matches: matches,
    mount: mount,
  };
});
