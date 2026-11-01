/* 糯小喵设置 — Win11 风格。
 * 结构：左侧导航（分组 / 运行与状态）+ 右侧卡片；一次只显示一个分组。
 * 设计约束：纯静态、无构建、无外部依赖；配置 API 与保存语义保持不变。
 * 导出纯函数供 tests/test_config_ui.js 使用（UMD）。
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
  var PAGE_KEY = 'nxm-active-page';
  var CLUSTER_KEY = 'nxm-cluster-open';
  var LIST_KEY = 'nxm-list-open';
  var REVEAL_KEYS = ['debounce_dynamic_enabled', 'debounce_typing_enabled', 'merge_localize_images', 'proactive_silent_wake_allow_tools'];
  var LIST_ROWS = 6;
  var LIST_MAX_HEIGHT = 460;

  /* ============================ 分组 ============================ */
  var GROUP_META = [
    { id: 'overview', title: '总览', note: '插件总开关。关闭后所有功能暂停，设置保留。' },
    { id: 'debounce', title: '消息合并', note: '同一人连发的消息合并后回复。' },
    { id: 'human_reply', title: '回复节奏', note: '按打字速度分条发送回复。' },
    { id: 'interaction', title: '拦截与互动', note: '刷屏拦截、屏蔽名单、被戳与关键词回应。' },
    { id: 'activity', title: '聊天记录与参考', note: '记录近期聊天，供回忆、跨会话查看与记忆检索。' },
    { id: 'proactive', title: '主动消息', note: '只在允许名单内生效，有次数与冷却限制。' },
    { id: 'selfwake', title: '自醒', note: '群里安静时自动醒来思考，每天有次数上限。' },
    { id: 'qq_official', title: 'QQ 官方', note: 'QQ 官方机器人平台的专用适配：合成消息投递、纯文本、分条与窗口控制。' },
    { id: 'advanced', title: '高级', note: '工具提示词、能力说明与兼容参数，通常保持默认。' },
    { id: 'resident_state', title: '自主状态', note: '自主运行时的心情、关注点与表达风格（管理入口在“运行与状态”）。' },
    { id: 'shared_memory', title: '共享记忆与信箱', note: '四项进阶能力，默认关闭：主脑、未读信箱、话题概括、群聊识图。' },
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

  /* ============================ 功能块 ============================ */
  var CLUSTERS = [
    { id: 'debounce', title: '防抖合并', gates: ['debounce_enabled'],
      children: ['debounce_dynamic_enabled', 'debounce_group_enabled', 'debounce_private_enabled',
        'debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds',
        'debounce_typing_enabled', 'debounce_typing_hold_seconds', 'debounce_long_chars',
        'debounce_end_punctuation', 'debounce_max_parts', 'merge_idle_seconds',
        'merge_max_total_seconds', 'merge_join_separator', 'merge_localize_images', 'merge_localize_images_dir'],
      lists: ['debounce_user_whitelist', 'debounce_group_whitelist', 'debounce_group_blacklist'] },
    { id: 'human_reply', title: '分条回复', gates: ['human_reply_enabled'],
      children: ['human_reply_chars_per_minute', 'human_reply_interrupt_enabled', 'human_reply_show_input_status',
        'human_reply_max_wait_seconds', 'human_reply_max_length'],
      lists: ['human_reply_group_whitelist', 'human_reply_group_blacklist'] },
    { id: 'guard', title: '刷屏拦截', gates: ['include_group', 'include_private'],
      children: ['trigger_rounds', 'window_seconds', 'meme_tags', 'meme_cooldown_seconds'],
      lists: ['user_whitelist', 'user_blacklist', 'guard_group_whitelist', 'guard_group_blacklist'] },
    { id: 'poke', title: '被戳回应', gates: ['poke_reply_enabled'],
      children: ['poke_reply_prompt', 'poke_reply_cooldown_seconds'],
      lists: ['poke_group_whitelist', 'poke_group_blacklist'] },
    { id: 'keyword_wake', title: '关键词唤醒', gates: ['keyword_wake_enabled'],
      children: ['keyword_wake_list'],
      lists: ['keyword_wake_group_whitelist', 'keyword_wake_group_blacklist'] },
    { id: 'activity', title: '记录聊天', gates: ['activity_buffer_enabled'],
      children: ['activity_record_private', 'activity_record_bot_replies', 'activity_buffer_max_text_chars',
        'activity_retention_days', 'activity_require_admin', 'activity_tool_enabled', 'activity_hint_enabled'],
      lists: ['activity_group_whitelist', 'activity_group_blacklist'] },
    { id: 'lmem', title: '长期记忆', gates: ['lmem_gateway_enabled'],
      children: ['lmem_gateway_keywords', 'lmem_gateway_top_k', 'lmem_gateway_timeout_seconds', 'lmem_gateway_max_chars'],
      lists: [] },
    { id: 'proactive', title: '主动消息', gates: ['proactive_enabled'],
      children: ['proactive_hint_enabled', 'proactive_max_pending_per_person', 'proactive_daily_limit_per_person',
        'proactive_cooldown_minutes', 'proactive_context_other_sessions', 'proactive_context_other_private',
        'proactive_quiet_hours_start', 'proactive_quiet_hours_end'],
      lists: ['proactive_allow_groups', 'proactive_target_whitelist'] },
    { id: 'selfwake', title: '静默自醒', gates: ['proactive_silent_wake_enabled'],
      children: ['proactive_judge_provider_id', 'proactive_silent_wake_floor_minutes',
        'proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_ceil_minutes',
        'proactive_silent_wake_daily_budget', 'proactive_silent_wake_max_steps',
        'proactive_silent_wake_allow_tools', 'resident_extra_tools',
        'resident_run_token_limit', 'resident_daily_token_limit'],
      lists: [] },
    { id: 'shared_memory', title: '全局主脑', gates: ['shared_memory_enabled'],
      children: ['master_umo', 'master_auto_bind'], lists: [] },
    { id: 'inbox', title: '未读信箱', gates: ['unread_inbox_enabled'],
      children: [], lists: ['inbox_group_whitelist', 'inbox_group_blacklist'] },
    { id: 'mention_notice', title: '点名通知卡', gates: ['mention_notice_enabled'],
      children: ['mention_notice_limit'], lists: [] },
    { id: 'topic', title: '话题概括', gates: ['topic_summary_enabled'],
      children: ['topic_light_provider_id', 'topic_min_unread', 'topic_cooldown_seconds', 'topic_max_chars',
        'topic_line_max_chars', 'topic_timeout_seconds', 'topic_segment_msg_count', 'topic_segment_backfill_max'],
      lists: [] },
    { id: 'vision', title: '群聊识图', gates: ['vision_enabled'],
      children: ['vision_provider_ids', 'image_keep_days', 'vision_image_max', 'vision_brief_max_chars',
        'vision_read_max_chars', 'vision_timeout_seconds', 'vision_cache_ttl_days'],
      lists: [] },
    { id: 'qq_official', title: 'QQ 官方适配', gates: ['qq_official_enabled'],
      children: ['qq_official_content_mode', 'qq_official_max_bubbles', 'qq_official_passive_window_seconds',
        'qq_official_group_proactive', 'qq_official_strip_at_markup'],
      lists: [] },
    { id: 'tool_prompt', title: '自定义工具提示', gates: ['tool_call_prompt_enabled'],
      children: ['tool_call_prompt'], lists: [] },
  ];
  var VISIBLE_WHEN = {
    debounce_fast_idle_seconds: function (state) { return !!state.debounce_dynamic_enabled; },
    debounce_slow_idle_seconds: function (state) { return !!state.debounce_dynamic_enabled; },
    debounce_wake_idle_seconds: function (state) { return !!state.debounce_dynamic_enabled; },
    merge_idle_seconds: function (state) { return !state.debounce_dynamic_enabled; },
    debounce_typing_hold_seconds: function (state) { return !!state.debounce_typing_enabled; },
    merge_localize_images_dir: function (state) { return !!state.merge_localize_images; },
    activity_prefetch_keywords: function (state) { return referenceMode(state) === 'prefetch'; },
    resident_extra_tools: function (state) { return !!state.proactive_silent_wake_allow_tools; },
  };
  var clusterByChild = {}, clusterByGate = {};
  CLUSTERS.forEach(function (cluster) {
    cluster.children.concat(cluster.lists).forEach(function (key) { clusterByChild[key] = cluster; });
    cluster.gates.forEach(function (key) { clusterByGate[key] = cluster; });
  });
  function clusterFieldCount(cluster) { return cluster.children.length + cluster.lists.length; }
  function clusterGateOn(cluster, state) {
    return cluster.gates.some(function (key) { return !!state[key]; });
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
  /* 名单文本框的实时统计：条目、重复、空白，用于计数徽标与去重提示。 */
  function parseListStats(raw) {
    var parts = Array.isArray(raw) ? raw : String(raw == null ? '' : raw).split(/[\n,，、;；]+/);
    var seen = Object.create(null), items = [], duplicates = 0, blanks = 0;
    parts.forEach(function (part) {
      var text = String(part == null ? '' : part).trim();
      if (!text) { blanks += 1; return; }
      if (own(seen, text)) { duplicates += 1; return; }
      seen[text] = true;
      items.push(text);
    });
    return { items: items, count: items.length, duplicates: duplicates, blanks: blanks };
  }
  /* 「名单与范围」标题用：几个名单、共多少条目。 */
  function listSummary(state, keys) {
    var items = 0;
    (keys || []).forEach(function (key) { items += parseList(state && state[key]).length; });
    return { fields: (keys || []).length, items: items };
  }
  function valueText(value) {
    return value === '' ? '留空'
      : Array.isArray(value) ? value.length ? value.join('、') : '空名单'
      : value === true ? '开启' : value === false ? '关闭' : String(value);
  }
  function highlightParts(text, query) {
    var source = String(text == null ? '' : text);
    var needle = String(query == null ? '' : query).trim().toLowerCase();
    if (!needle) return [{ text: source, match: false }];
    var parts = [], lower = source.toLowerCase(), start = 0, index;
    while ((index = lower.indexOf(needle, start)) >= 0) {
      if (index > start) parts.push({ text: source.slice(start, index), match: false });
      parts.push({ text: source.slice(index, index + needle.length), match: true });
      start = index + needle.length;
    }
    if (!parts.length) return [{ text: source, match: false }];
    if (start < source.length) parts.push({ text: source.slice(start), match: false });
    return parts;
  }
  function pendingCount(group, keys) {
    var count = 0;
    group.fields.forEach(function (key) {
      if (key === '@reference') {
        if (REFERENCE_KEYS.some(function (name) { return keys.indexOf(name) >= 0; })) count += 1;
      } else if (keys.indexOf(key) >= 0) {
        count += 1;
      }
    });
    return count;
  }
  function referenceMode(value) {
    return value.capability_context_digest ? 'background' : value.activity_prefetch_enabled ? 'prefetch' : 'tools';
  }
  function referenceValues(mode) {
    return { activity_prefetch_enabled: mode === 'prefetch', capability_context_digest: mode === 'background' };
  }
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
  function matches(key, spec, query, groupTitle) {
    var text = [key, fieldTitle(spec, key), spec.description || '', spec.hint || '', groupTitle || ''].join(' ').toLowerCase();
    return text.indexOf(query.toLowerCase()) >= 0;
  }

  /* ============================ 页面 ============================ */
  function mount(win) {
    var doc = win.document;
    var schema = {}, state = {}, baseline = {}, defaults = {}, userDefault = null;
    var groups = [], providers = [], residentTools = [], diagnostics = [], timezone = '';
    var query = '', saving = false, loaded = false;
    var fieldErrors = {}, toastTimer = null, searchTimer = null, lastErrorSig = '';
    var clusterOpen = loadMap(CLUSTER_KEY);
    var listOpen = loadMap(LIST_KEY);
    var pageMap = loadMap(PAGE_KEY);
    var activePage = typeof pageMap.page === 'string' ? pageMap.page : 'overview';
    var lastView = '';

    function $(id) { return doc.getElementById(id); }
    function node(tag, cls, text) { var n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); return n; }
    function button(text, handler, cls) { var n = node('button', cls, text); n.type = 'button'; n.addEventListener('click', handler); return n; }
    function textNode(text) {
      if (typeof doc.createTextNode === 'function') return doc.createTextNode(text);
      var span = doc.createElement('span');
      span.textContent = text;
      return span;
    }
    function showToast(text) {
      var t = $('toast');
      if (!t) return;
      t.textContent = text;
      t.hidden = false;
      if (t.classList) { t.classList.remove('leave'); t.classList.add('show'); }
      clearTimeout(toastTimer);
      toastTimer = setTimeout(function () {
        if (!t.classList) { t.hidden = true; return; }
        t.classList.remove('show');
        t.classList.add('leave');
        toastTimer = setTimeout(function () { t.hidden = true; t.classList.remove('leave'); }, 240);
      }, 4200);
    }

    /* ---------- 动效 ---------- */
    /* 系统要求减少动效，或环境没有 rAF（测试里的迷你 DOM）时，全部退化为瞬时切换。 */
    function prefersReducedMotion() {
      try { return !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches); }
      catch (error) { return false; }
    }
    function canAnimate() {
      return typeof win.requestAnimationFrame === 'function' && !prefersReducedMotion();
    }
    function pop(el) {
      if (!el || !el.classList) return;
      el.classList.remove('pop');
      if (typeof el.offsetWidth === 'number') void el.offsetWidth;
      el.classList.add('pop');
      clearTimeout(el.__nxmPopTimer);
      el.__nxmPopTimer = setTimeout(function () { el.classList.remove('pop'); }, 320);
    }
    /* 折叠动画：hidden 仍是唯一真值；有动画能力时用 max-height 过渡，结束后再落 hidden。 */
    function setBodyOpen(el, open) {
      if (!el) return;
      if (el.__nxmOpen === open) return;
      el.__nxmOpen = open;
      if (el.__nxmTimer) { clearTimeout(el.__nxmTimer); el.__nxmTimer = null; }
      var style = el.style || {};
      if (!canAnimate() || typeof el.scrollHeight !== 'number') {
        el.hidden = !open;
        if (el.classList) el.classList.remove('is-animating');
        if (style.maxHeight !== undefined) { style.maxHeight = ''; style.opacity = ''; }
        return;
      }
      if (el.classList) el.classList.add('is-animating');
      if (open) {
        el.hidden = false;
        style.maxHeight = '0px';
        style.opacity = '0';
        void el.offsetHeight;
        style.maxHeight = el.scrollHeight + 'px';
        style.opacity = '1';
        el.__nxmTimer = setTimeout(function () {
          el.__nxmTimer = null;
          style.maxHeight = '';
          style.opacity = '';
          if (el.classList) el.classList.remove('is-animating');
        }, 280);
      } else {
        style.maxHeight = el.scrollHeight + 'px';
        style.opacity = '1';
        void el.offsetHeight;
        style.maxHeight = '0px';
        style.opacity = '0';
        el.__nxmTimer = setTimeout(function () {
          el.__nxmTimer = null;
          el.hidden = true;
          style.maxHeight = '';
          style.opacity = '';
          if (el.classList) el.classList.remove('is-animating');
        }, 240);
      }
    }
    /* 页面切换入场：重放 CSS 动画。 */
    function playPageIn() {
      var box = groupsEl();
      if (!box || !box.classList) return;
      box.classList.remove('page-enter');
      if (typeof box.offsetWidth === 'number') void box.offsetWidth;
      box.classList.add('page-enter');
    }
    function stagger(card, index) {
      if (card && card.style && typeof card.style.setProperty === 'function') card.style.setProperty('--i', String(index));
      return card;
    }
    function flashRow(el) {
      if (!el || !el.classList) return;
      el.classList.add('flash');
      setTimeout(function () { el.classList.remove('flash'); }, 1300);
    }
    function defaultOf(key) { return userDefault && own(userDefault, key) ? userDefault[key] : defaults[key]; }
    function loadMap(storageKey) {
      try {
        var raw = win.localStorage && win.localStorage.getItem(storageKey);
        var data = raw ? JSON.parse(raw) : null;
        return data && typeof data === 'object' ? data : {};
      } catch (error) { return {}; }
    }
    function saveMap(storageKey, map) {
      try { if (win.localStorage) win.localStorage.setItem(storageKey, JSON.stringify(map)); } catch (error) { /* 本地记忆失败不影响配置 */ }
    }
    function fillText(target, text) {
      var parts = highlightParts(text, query);
      if (parts.length === 1 && !parts[0].match) { target.textContent = parts[0].text; return target; }
      target.replaceChildren();
      parts.forEach(function (part) {
        if (!part.match) { target.appendChild(textNode(part.text)); return; }
        var mark = doc.createElement('mark');
        mark.textContent = part.text;
        target.appendChild(mark);
      });
      return target;
    }

    /* ---------- 图标 ---------- */
    function icon(name) {
      if (typeof doc.createElementNS !== 'function') return null;
      var NS = 'http://www.w3.org/2000/svg';
      var svg = doc.createElementNS(NS, 'svg');
      svg.setAttribute('class', 'nav-icon');
      svg.setAttribute('viewBox', '0 0 20 20');
      svg.setAttribute('aria-hidden', 'true');
      var use = doc.createElementNS(NS, 'use');
      use.setAttribute('href', '#i-' + name);
      svg.appendChild(use);
      return svg;
    }
    function chevronIcon() {
      var svg = icon('chevron');
      if (svg) { svg.setAttribute('class', 'chevron'); return svg; }
      return node('span', 'chevron-text', '▾');
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
    /* ---------- 名单编辑器 ---------- */
    function fitListInput(el) {
      if (!el || !el.style) return;
      if (typeof el.scrollHeight !== 'number' || !el.scrollHeight) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, LIST_MAX_HEIGHT) + 'px';
    }
    function listControl(key) {
      var spec = schema[key] || {};
      var title = fieldTitle(spec, key);
      var raw = Array.isArray(state[key]) ? state[key].join('\n') : state[key];
      var wrap = node('div', 'list-editor');
      wrap.id = 'list-editor-' + key;

      var top = node('div', 'list-editor-top');
      top.appendChild(node('span', 'list-editor-tip', '每行一项'));
      var count = node('span', 'list-count');
      count.id = 'list-count-' + key;
      count.setAttribute('aria-live', 'polite');
      top.appendChild(count);
      wrap.appendChild(top);

      var input = labelInput(node('textarea', 'list-input'), key);
      input.rows = LIST_ROWS;
      input.spellcheck = false;
      var rawStats = parseListStats(raw);
      input.value = rawStats.items.join('\n');
      input.placeholder = '还没有添加任何一项\n每行一项，也可粘贴逗号、顿号分隔的内容';
      wrap.appendChild(input);

      var foot = node('div', 'list-editor-foot');
      var meta = node('span', 'list-meta');
      meta.id = 'list-meta-' + key;
      foot.appendChild(meta);
      var actions = node('div', 'list-actions');
      actions.appendChild(button('整理', tidy, 'link-btn list-action'));
      var clearBtn = button('清空', clear, 'link-btn list-action danger');
      actions.appendChild(clearBtn);
      foot.appendChild(actions);
      var armed = false, armTimer = null;
      function disarm() {
        armed = false;
        clearTimeout(armTimer);
        clearBtn.textContent = '清空';
        if (clearBtn.classList) clearBtn.classList.remove('armed');
      }
      wrap.appendChild(foot);

      function sync(animateCount) {
        var stats = parseListStats(input.value);
        count.textContent = '共 ' + stats.count + ' 项';
        meta.textContent = stats.duplicates
          ? '已合并 ' + stats.duplicates + ' 项重复'
          : stats.count ? '重复项会自动合并' : '留空表示不限';
        if (wrap.classList) wrap.classList.toggle('is-empty', stats.count === 0);
        if (animateCount) pop(count);
        fitListInput(input);
        return stats;
      }
      function tidy() {
        var stats = parseListStats(input.value);
        input.value = stats.items.join('\n');
        sync(true);
        onChange(key, stats.items);
        showToast(stats.duplicates ? '已整理：合并了 ' + stats.duplicates + ' 项重复内容。' : '名单已经是整理好的状态。');
      }
      function clear() {
        /* 两段式确认：第一次点击只是「上膛」，3.2 秒内再点一次才真正清空，避免误触清掉整份名单。 */
        if (!armed) {
          armed = true;
          clearBtn.textContent = '确认清空？';
          if (clearBtn.classList) clearBtn.classList.add('armed');
          showToast('再点一次「确认清空？」即可清空这个名单。');
          clearTimeout(armTimer);
          armTimer = setTimeout(disarm, 3200);
          return;
        }
        disarm();
        input.value = '';
        sync(true);
        onChange(key, []);
        showToast('已清空「' + title + '」，保存后生效。');
      }

      input.addEventListener('input', function () {
        if (armed) disarm();
        onChange(key, sync(true).items);
      });
      sync(false);
      /* 存储值里本来就有重复：明确告诉用户显示值已经去过重，而不是笼统一句提示。 */
      if (rawStats.duplicates) meta.textContent = '已合并 ' + rawStats.duplicates + ' 项重复';
      if (typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(function () { fitListInput(input); });
      return wrap;
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
        var holder = node('div', 'stack-control'); holder.appendChild(input); holder.appendChild(custom); return holder;
      }
      if (spec.type === 'bool') {
        var label = node('label', 'switch'); input = labelInput(node('input'), key); input.type = 'checkbox'; input.checked = !!value;
        var text = node('span', 'switch-text', input.checked ? '开' : '关');
        input.addEventListener('change', function () { text.textContent = input.checked ? '开' : '关'; onChange(key, input.checked); });
        label.appendChild(input); label.appendChild(text); return label;
      }
      if (spec.type === 'list') return listControl(key);
      if (spec.type === 'text') { input = labelInput(node('textarea', 'prompt'), key); input.rows = 5; }
      else { input = labelInput(node('input'), key); input.type = TIME_KEYS.indexOf(key) >= 0 ? 'time' : spec.type === 'int' || spec.type === 'float' ? 'number' : 'text'; }
      input.value = value === null || value === undefined ? '' : value;
      if (input.type === 'number') { input.step = spec.type === 'int' ? 1 : 'any'; if (spec.slider) { input.min = spec.slider.min; input.max = spec.slider.max; } }
      input.addEventListener('input', function () { onChange(key, input.type === 'number' ? input.value === '' ? '' : Number(input.value) : input.value); });
      return input;
    }
    function referenceRow() {
      var wrap = node('div', 'row block'); wrap.id = 'field-@reference'; wrap.dataset.key = '@reference';
      var copy = node('div', 'row-copy');
      var label = node('strong', 'row-label'); fillText(label, '聊天参考方式'); copy.appendChild(label);
      var desc = node('p', 'row-desc'); fillText(desc, '不改变记录范围与查看权限。'); copy.appendChild(desc);
      var err = node('p', 'row-error'); err.id = 'error-@reference'; err.hidden = true; copy.appendChild(err);
      wrap.appendChild(copy);
      var control = node('div', 'row-control');
      control.appendChild(optionsControl('reference-mode', referenceMode(state), [
        { id: 'tools', title: '按需工具查询', note: '不自动附带记录，由模型需要时调用速览工具。' },
        { id: 'prefetch', title: '关键词预取', note: '问到别处聊天或翻记录时，自动附带近期记录。' },
        { id: 'background', title: '每轮背景', note: '带工具的回复每轮附带别处近况，不再同时关键词预取。' },
      ], function (mode) { Object.assign(state, referenceValues(mode)); REFERENCE_KEYS.forEach(function (key) { delete fieldErrors[key]; }); renderPage(); refresh(); }));
      wrap.appendChild(control);
      return wrap;
    }
    function row(key) {
      if (key === '@reference') return referenceRow();
      var spec = schema[key]; if (!spec) return null;
      var isBlock = spec.type === 'list' || spec.type === 'text' || key === 'merge_join_separator';
      var el = node('div', 'row' + (isBlock ? ' block' : ''));
      el.id = 'field-' + key; el.dataset.key = key;
      var copy = node('div', 'row-copy');
      var label = node('label', 'row-label'); label.id = 'label-' + key; fillText(label, fieldTitle(spec, key)); copy.appendChild(label);
      var desc = node('p', 'row-desc'); desc.id = 'hint-' + key; fillText(desc, spec.hint || ''); desc.hidden = !spec.hint; copy.appendChild(desc);
      var err = node('p', 'row-error'); err.id = 'error-' + key; err.hidden = true; copy.appendChild(err);
      el.appendChild(copy);
      var control = node('div', 'row-control'); control.appendChild(buildControl(key, spec)); el.appendChild(control);
      /* 标签与控件关联：有 id 的单个控件用 for；成组控件（工具多选等）用 aria-labelledby。 */
      var target = control.querySelector('#input-' + key);
      if (target) {
        label.htmlFor = 'input-' + key;
        target.setAttribute('aria-describedby', 'hint-' + key + ' error-' + key);
      } else {
        if (typeof label.removeAttribute === 'function') label.removeAttribute('for');
        var group = control.firstChild;
        if (group && typeof group.setAttribute === 'function') {
          group.setAttribute('role', 'group');
          group.setAttribute('aria-labelledby', 'label-' + key);
        }
      }
      return el;
    }

    /* ---------- 折叠 ---------- */
    function clusterShouldOpen(cluster) {
      if (query) return true;
      if (own(clusterOpen, cluster.id)) return !!clusterOpen[cluster.id];
      return clusterGateOn(cluster, state);
    }
    function listShouldOpen(cluster) {
      if (query) return true;
      if (own(listOpen, cluster.id)) return !!listOpen[cluster.id];
      /* 没记忆过时：功能已开启且名单里有内容，默认展开，省掉一次点击。 */
      if (!clusterGateOn(cluster, state)) return false;
      return cluster.lists.some(function (key) { return parseList(state[key]).length > 0; });
    }
    function toggleCluster(cluster) {
      clusterOpen[cluster.id] = !clusterShouldOpen(cluster);
      saveMap(CLUSTER_KEY, clusterOpen);
      applyClusterOpen(cluster);
    }
    function toggleList(cluster) {
      listOpen[cluster.id] = !listShouldOpen(cluster);
      saveMap(LIST_KEY, listOpen);
      applyListOpen(cluster);
    }
    function applyClusterOpen(cluster) {
      var body = $('cluster-body-' + cluster.id), toggle = $('cluster-toggle-' + cluster.id);
      var open = clusterShouldOpen(cluster);
      setBodyOpen(body, open);
      if (toggle) {
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        toggle.title = open ? '收起' : '展开';
      }
    }
    function applyListOpen(cluster) {
      var body = $('list-body-' + cluster.id), bar = $('list-bar-' + cluster.id);
      var open = listShouldOpen(cluster);
      setBodyOpen(body, open);
      if (bar) bar.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function fieldVisibleInCluster(key, cluster, group) {
      if (!schema[key]) return false;
      if (query) return matches(key, schema[key], query, group.title);
      var when = VISIBLE_WHEN[key];
      return when ? !!when(state) : true;
    }
    function visibleField(key, group) {
      if (key === '@reference') {
        var text = '聊天参考 别处聊天 其他会话 按需工具查询 关键词预取 每轮背景 activity_prefetch_enabled capability_context_digest';
        return !query || text.toLowerCase().indexOf(query) >= 0;
      }
      return !query || matches(key, schema[key] || {}, query, group.title);
    }
    function groupBlocks(group) {
      var blocks = [], seen = {};
      group.fields.forEach(function (key) {
        if (seen[key]) return;
        var cluster = clusterByGate[key];
        if (cluster) {
          blocks.push({ cluster: cluster });
          cluster.gates.concat(cluster.children, cluster.lists).forEach(function (child) { seen[child] = true; });
          return;
        }
        if (clusterByChild[key]) return;
        blocks.push({ key: key });
      });
      return blocks;
    }

    /* ---------- 页面渲染 ---------- */
    function pageHead(title, note, withReset) {
      var head = node('header', 'page-head');
      var copy = node('div');
      copy.appendChild(node('h1', 'page-title', title));
      if (note) copy.appendChild(node('p', 'page-note', note));
      head.appendChild(copy);
      if (withReset) {
        var reset = button('恢复本组', function () { resetGroup(groupById(activePage)); }, 'btn subtle');
        reset.id = 'group-reset'; reset.hidden = true;
        head.appendChild(reset);
      }
      return head;
    }
    function standaloneCard(title, rows) {
      var card = node('section', 'card');
      var head = node('div', 'card-head');
      head.appendChild(node('h2', 'card-title', title));
      card.appendChild(head);
      var body = node('div', 'card-body');
      rows.forEach(function (r) { body.appendChild(r); });
      card.appendChild(body);
      return card;
    }
    function listBlock(cluster, keys) {
      var wrap = node('div', 'subblock');
      wrap.id = 'list-' + cluster.id;
      var open = listShouldOpen(cluster);
      var head = button('', function () { toggleList(cluster); }, 'subblock-head');
      head.id = 'list-bar-' + cluster.id;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      head.setAttribute('aria-controls', 'list-body-' + cluster.id);
      head.appendChild(node('span', 'subblock-title', '名单与范围'));
      var summary = listSummary(state, cluster.lists);
      var summaryEl = node('span', 'subblock-count', summary.fields + ' 个名单 · 共 ' + summary.items + ' 项');
      summaryEl.id = 'list-summary-' + cluster.id;
      head.appendChild(summaryEl);
      var pending = node('span', 'nav-badge'); pending.id = 'list-pending-' + cluster.id; pending.hidden = true;
      head.appendChild(pending);
      head.appendChild(chevronIcon());
      wrap.appendChild(head);
      var body = node('div', 'subblock-body'); body.id = 'list-body-' + cluster.id; body.hidden = !open; body.__nxmOpen = open;
      keys.forEach(function (key) { var r = row(key); if (r) body.appendChild(r); });
      wrap.appendChild(body);
      return wrap;
    }
    function clusterCard(cluster, group) {
      var on = clusterGateOn(cluster, state);
      var open = clusterShouldOpen(cluster);
      var card = node('section', 'card'); card.id = 'cluster-' + cluster.id; card.dataset.cluster = cluster.id;
      var head = node('div', 'card-head');
      var title = node('h2', 'card-title');
      title.appendChild(node('span', '', cluster.title));
      var pill = node('span', 'state-pill' + (on ? '' : ' off'), on ? '' : '已关闭');
      pill.id = 'cluster-state-' + cluster.id;
      title.appendChild(pill);
      var pending = node('span', 'card-pending'); pending.id = 'cluster-pending-' + cluster.id; pending.hidden = true;
      title.appendChild(pending);
      head.appendChild(title);
      var actions = node('div', 'card-actions');
      if (cluster.gates.length === 1) {
        var gateKey = cluster.gates[0];
        var gateBox = node('div', 'gate-control');
        gateBox.id = 'field-' + gateKey; gateBox.dataset.key = gateKey;
        gateBox.appendChild(buildControl(gateKey, schema[gateKey]));
        actions.appendChild(gateBox);
      }
      var toggle = button('', function () { toggleCluster(cluster); }, 'icon-btn');
      toggle.id = 'cluster-toggle-' + cluster.id;
      toggle.title = open ? '收起' : '展开';
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-controls', 'cluster-body-' + cluster.id);
      toggle.setAttribute('aria-label', cluster.title + (open ? '：收起' : '：展开'));
      toggle.appendChild(chevronIcon());
      actions.appendChild(toggle);
      head.appendChild(actions);
      card.appendChild(head);
      var body = node('div', 'card-body'); body.id = 'cluster-body-' + cluster.id; body.hidden = !open; body.__nxmOpen = open;
      if (cluster.gates.length > 1) cluster.gates.forEach(function (key) { if (schema[key]) body.appendChild(row(key)); });
      cluster.children.forEach(function (key) {
        if (!fieldVisibleInCluster(key, cluster, group)) return;
        var r = row(key); if (r) body.appendChild(r);
      });
      var listKeys = cluster.lists.filter(function (key) { return fieldVisibleInCluster(key, cluster, group); });
      if (listKeys.length) body.appendChild(listBlock(cluster, listKeys));
      if (!body.children.length) body.appendChild(node('p', 'empty', '本功能没有额外设置。'));
      card.appendChild(body);
      return card;
    }
    function clusterSearchCard(cluster, group) {
      var gateMatch = cluster.gates.some(function (key) { return schema[key] && matches(key, schema[key], query, group.title); });
      var children = cluster.children.filter(function (key) { return schema[key] && matches(key, schema[key], query, group.title); });
      var lists = cluster.lists.filter(function (key) { return schema[key] && matches(key, schema[key], query, group.title); });
      if (!gateMatch && !children.length && !lists.length) return null;
      var card = node('section', 'card');
      var head = node('div', 'card-head');
      head.appendChild(node('h2', 'card-title', cluster.title));
      card.appendChild(head);
      var body = node('div', 'card-body');
      cluster.gates.forEach(function (key) { if (schema[key]) body.appendChild(row(key)); });
      children.forEach(function (key) { body.appendChild(row(key)); });
      lists.forEach(function (key) { body.appendChild(row(key)); });
      card.appendChild(body);
      card.dataset.count = String(cluster.gates.length + children.length + lists.length);
      return card;
    }
    function buildGroupCards(group) {
      var blocks = groupBlocks(group), cards = [], pendingRows = [];
      var plainTitle = group.id === 'overview' || group.id === 'resident_state' ? '常规' : '其他设置';
      function flush() {
        if (!pendingRows.length) return;
        cards.push(standaloneCard(plainTitle, pendingRows));
        pendingRows = [];
      }
      blocks.forEach(function (block) {
        if (block.cluster) {
          flush();
          cards.push(clusterCard(block.cluster, group));
          return;
        }
        var when = VISIBLE_WHEN[block.key];
        if (when && !when(state)) return;
        var r = row(block.key);
        if (r) pendingRows.push(r);
      });
      flush();
      return cards;
    }
    function renderConfigPage(group) {
      if (!group) return;
      groupsEl().replaceChildren();
      groupsEl().appendChild(pageHead(group.title, group.note, true));
      var cards = buildGroupCards(group);
      if (!cards.length) groupsEl().appendChild(node('p', 'empty', '这个分组没有可显示的设置。'));
      cards.forEach(function (card, index) { groupsEl().appendChild(stagger(card, index)); });
    }
    function renderSearch() {
      var sections = [], total = 0;
      groups.forEach(function (group) {
        var cards = [], rows = [];
        groupBlocks(group).forEach(function (block) {
          if (block.cluster) {
            var card = clusterSearchCard(block.cluster, group);
            if (card) { cards.push(card); total += Number(card.dataset.count || 0); }
            return;
          }
          if (!visibleField(block.key, group)) return;
          var r = row(block.key);
          if (r) { rows.push(r); total += 1; }
        });
        if (!cards.length && !rows.length) return;
        if (rows.length) cards.push(standaloneCard('其他设置', rows));
        var section = node('section', 'search-section');
        section.appendChild(node('h2', 'search-group-title', group.title));
        cards.forEach(function (card) { section.appendChild(card); });
        sections.push(section);
      });
      groupsEl().replaceChildren();
      groupsEl().appendChild(pageHead('搜索结果', total ? '共 ' + total + ' 项匹配：' + query : '没有匹配「' + query + '」的设置', false));
      if (!sections.length) groupsEl().appendChild(node('p', 'empty', '没有找到匹配项。可以搜索“私聊”“名单”“自醒”等词。'));
      sections.forEach(function (section, index) { groupsEl().appendChild(stagger(section, index)); });
    }
    function renderStatusPage() {
      var root = $('status-root'); if (!root) return;
      root.replaceChildren();
      root.appendChild(pageHead('运行与状态', '自主运行记录、当前状态与表达风格；长内容点击展开。', false));
      var host = node('div', 'resident-panel'); host.id = 'resident-root';
      root.appendChild(host);
      if (win.NuoxiaomiaoResidentPanel && typeof win.NuoxiaomiaoResidentPanel.mount === 'function') {
        win.NuoxiaomiaoResidentPanel.mount(host, win);
      } else {
        host.appendChild(node('p', 'empty', '状态面板未加载，请刷新页面重试。'));
      }
    }
    function groupsEl() { return $('groups'); }
    function groupById(id) {
      for (var i = 0; i < groups.length; i++) if (groups[i].id === id) return groups[i];
      return null;
    }
    function renderPage() {
      if (!loaded) return;
      var active = doc.activeElement;
      var keepId = active && active.id ? active.id : '';
      var keepY = typeof win.scrollY === 'number' ? win.scrollY : 0;
      updateNavActive();
      var statusMode = !query && activePage === 'status';
      var viewKey = statusMode ? 'status' : query ? 'search' : 'page:' + activePage;
      var groupsBox = groupsEl(), statusBox = $('status-root');
      if (groupsBox) groupsBox.hidden = statusMode;
      if (statusBox) statusBox.hidden = !statusMode;
      if (statusMode) renderStatusPage();
      else if (query) renderSearch();
      else renderConfigPage(groupById(activePage) || groups[0]);
      /* 只在真正切换视图时播放入场动画并回到顶部：搜索逐键重渲染不会闪、不会跳。 */
      if (viewKey !== lastView) {
        lastView = viewKey;
        playPageIn();
        if (typeof win.scrollTo === 'function') win.scrollTo(0, 0);
      } else if (keepId) {
        /* 同一视图内的重渲染（开关展开字段、保存返回、恢复默认）：把焦点和滚动位置还回去。 */
        var again = $(keepId);
        if (again && typeof again.focus === 'function') {
          try { again.focus({ preventScroll: true }); } catch (error) { again.focus(); }
          if (typeof win.scrollTo === 'function') win.scrollTo(0, keepY);
        }
      }
      var savebar = $('savebar');
      if (savebar) {
        /* 「运行与状态」页也保留保存条：有待保存修改时不隐藏，避免误以为已经保存。 */
        savebar.hidden = statusMode && !dirtyKeys(state, baseline, schema).length;
      }
    }

    /* ---------- 导航 ---------- */
    function renderNav() {
      var nav = $('nav'); if (!nav) return;
      nav.replaceChildren();
      groups.forEach(function (group) {
        var item = button('', function () { selectPage(group.id); }, 'nav-item');
        item.dataset.group = group.id;
        var ic = icon(group.id); if (ic) item.appendChild(ic);
        item.appendChild(node('span', 'nav-label', group.title));
        var badge = node('span', 'nav-badge'); badge.id = 'nav-pending-' + group.id; badge.hidden = true;
        item.appendChild(badge);
        nav.appendChild(item);
      });
      nav.appendChild(node('div', 'nav-sep'));
      var status = button('', function () { selectPage('status'); }, 'nav-item');
      status.dataset.page = 'status';
      var sic = icon('status'); if (sic) status.appendChild(sic);
      status.appendChild(node('span', 'nav-label', '运行与状态'));
      nav.appendChild(status);
    }
    function updateNavActive() {
      var items = doc.querySelectorAll('.nav-item');
      for (var i = 0; i < items.length; i += 1) {
        var item = items[i], on;
        if (query) on = false;
        else if (item.dataset.page === 'status') on = activePage === 'status';
        else on = item.dataset.group === activePage;
        item.classList.toggle('active', on);
        if (on) item.setAttribute('aria-current', 'page');
        else if (typeof item.removeAttribute === 'function') item.removeAttribute('aria-current');
        /* 窄屏是横向导航条：把当前页滚进可视区，否则它会被裁在屏幕外。 */
        if (on && typeof win.innerWidth === 'number' && win.innerWidth <= 900 && typeof item.scrollIntoView === 'function') {
          try { item.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (error) { /* 老浏览器忽略 */ }
        }
      }
    }
    function selectPage(id) {
      if (query) { query = ''; var s = $('search'); if (s) s.value = ''; }
      activePage = id; saveMap(PAGE_KEY, { page: id });
      renderPage(); refresh();
    }

    /* ---------- 刷新 ---------- */
    function refreshCluster(cluster, keys) {
      var count = cluster.children.concat(cluster.lists).filter(function (key) { return keys.indexOf(key) >= 0; }).length;
      var pending = $('cluster-pending-' + cluster.id);
      if (pending) { pending.textContent = count ? '待保存 ' + count + ' 项' : ''; pending.hidden = !count; }
      var listCount = cluster.lists.filter(function (key) { return keys.indexOf(key) >= 0; }).length;
      var listPending = $('list-pending-' + cluster.id);
      if (listPending) { listPending.textContent = listCount ? String(listCount) : ''; listPending.hidden = !listCount; }
      var on = clusterGateOn(cluster, state);
      var pill = $('cluster-state-' + cluster.id);
      if (pill) { pill.textContent = on ? '' : '已关闭'; pill.classList.toggle('off', !on); }
      var summaryEl = $('list-summary-' + cluster.id);
      if (summaryEl) {
        var summary = listSummary(state, cluster.lists);
        summaryEl.textContent = summary.fields + ' 个名单 · 共 ' + summary.items + ' 项';
      }
      applyClusterOpen(cluster);
      applyListOpen(cluster);
    }
    function refresh() {
      var keys = dirtyKeys(state, baseline, schema);
      var dirty = $('dirty'), detail = $('save-detail');
      if (dirty) dirty.textContent = saving ? '正在保存…' : keys.length ? '待保存 ' + keys.length + ' 项' : '没有待保存的修改';
      if (detail) detail.textContent = saving ? '保存期间继续编辑的内容会保留' : keys.length ? '点击保存后生效 · Ctrl / ⌘ + S' : loaded ? '当前修改均已保存' : '配置加载后可编辑';
      var saveBtn = $('save'), revertBtn = $('revert'), factoryBtn = $('restore-factory');
      if (saveBtn) saveBtn.disabled = !loaded || saving || !keys.length;
      if (revertBtn) revertBtn.disabled = !loaded || saving || !keys.length;
      if (factoryBtn) factoryBtn.disabled = !loaded || saving;
      var clear = $('search-clear'); if (clear) clear.hidden = !query;
      var savebar = $('savebar');
      if (savebar && savebar.classList) savebar.classList.toggle('is-dirty', !!loaded && keys.length > 0);
      groups.forEach(function (group) {
        var count = pendingCount(group, keys);
        var badge = $('nav-pending-' + group.id);
        if (badge) { badge.textContent = count ? String(count) : ''; badge.hidden = !count; }
      });
      var group = groupById(activePage);
      var reset = $('group-reset');
      if (reset) reset.hidden = !group || !pendingCount(group, keys);
      CLUSTERS.forEach(function (cluster) { refreshCluster(cluster, keys); });
      doc.querySelectorAll('[data-key]').forEach(function (el) {
        var key = el.dataset.key;
        var pending = key === '@reference' ? REFERENCE_KEYS.some(function (k) { return keys.indexOf(k) >= 0; }) : keys.indexOf(key) >= 0;
        el.classList.toggle('pending', pending);
        var error = key === '@reference' ? REFERENCE_KEYS.map(function (k) { return fieldErrors[k]; }).filter(Boolean).join('；') : fieldErrors[key];
        var errorNode = $('error-' + key);
        if (errorNode) { errorNode.textContent = Array.isArray(error) ? error.join('；') : error || ''; errorNode.hidden = !error; }
        el.querySelectorAll('input,textarea,select').forEach(function (input) { input.setAttribute('aria-invalid', error ? 'true' : 'false'); });
      });
      var banner = $('banner');
      if (banner) {
        banner.hidden = !!baseline.enabled && !!state.enabled;
        banner.textContent = !baseline.enabled ? '当前运行：插件已关闭。' + (state.enabled ? '草稿已开启，保存后生效。' : '可先编辑设置，开启总开关并保存后恢复功能。') : '草稿已关闭总开关；保存后插件功能暂停。';
      }
      renderErrors();
    }
    function renderDiagnostics() {
      var box = $('diagnostics'); if (!box) return;
      box.replaceChildren();
      if (!diagnostics.length) return;
      diagnostics.forEach(function (item) {
        var rowEl = node('div', 'diag ' + (item.severity === 'error' ? 'error' : item.severity === 'info' ? 'info' : 'warning'));
        rowEl.appendChild(node('span', '', item.message || item.code || ''));
        var links = node('span', 'links');
        (item.fields || []).filter(function (key) { return schema[key]; }).slice(0, 3).forEach(function (key) {
          links.appendChild(button('查看设置', function () { focusField(key); }, 'link-btn'));
        });
        rowEl.appendChild(links); box.appendChild(rowEl);
      });
    }
    function renderErrors() {
      var box = $('error-summary'); if (!box) return;
      var keys = Object.keys(fieldErrors);
      /* 同一组错误不重复重建，避免 aria-live 逐键反复播报。 */
      var sig = keys.join('|');
      if (sig === lastErrorSig) return;
      lastErrorSig = sig;
      box.replaceChildren(); box.hidden = !keys.length;
      if (!keys.length) return;
      box.appendChild(node('strong', '', '有 ' + keys.length + ' 项需要修正，尚未保存。'));
      keys.forEach(function (key) { box.appendChild(button(fieldTitle(schema[key], key), function () { focusField(key); }, 'link-btn')); });
    }
    function focusField(key) {
      var target = REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key;
      query = ''; var search = $('search'); if (search) search.value = '';
      var home = groupOf(key, groups);
      if (home) { activePage = home.id; saveMap(PAGE_KEY, { page: activePage }); }
      var cluster = clusterByChild[key] || clusterByGate[key];
      if (cluster) {
        clusterOpen[cluster.id] = true; saveMap(CLUSTER_KEY, clusterOpen);
        listOpen[cluster.id] = true; saveMap(LIST_KEY, listOpen);
      }
      renderPage(); refresh();
      var el = $('field-' + target); if (!el) return;
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      flashRow(el);
      var input = el.querySelector('input,textarea,select'); if (input) input.focus({ preventScroll: true });
    }
    function focusControl(key) {
      var el = $('input-' + key);
      if (!el || typeof el.focus !== 'function') return;
      try { el.focus({ preventScroll: true }); } catch (error) { el.focus(); }
    }
    function onChange(key, value) {
      state[key] = value; delete fieldErrors[key];
      if (WAKE_KEYS.indexOf(key) >= 0) WAKE_KEYS.forEach(function (k) { delete fieldErrors[k]; });
      /* 会引发整页重渲染的开关：重建后把焦点还给同一个控件，键盘操作不中断。 */
      if (REVEAL_KEYS.indexOf(key) >= 0) { renderPage(); focusControl(key); }
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
        renderDiagnostics(); renderPage(); refresh();
        var hasNewEdits = dirtyKeys(state, baseline, schema).length > 0;
        showToast(hasNewEdits ? '已保存提交的修改；保存期间的新修改仍待保存。' : '配置已保存并生效。');
      } catch (error) {
        fieldErrors = errorFields(error); renderPage(); refresh();
        showToast('保存失败：' + (error && error.message ? error.message : '请重试'));
        if (Object.keys(fieldErrors).length) focusField(Object.keys(fieldErrors)[0]);
      } finally { saving = false; refresh(); }
    }
    function revert() {
      if (!loaded || saving) return;
      state = clone(baseline); fieldErrors = {}; renderPage(); refresh(); showToast('已撤销待保存的修改。');
    }
    function restoreFactory() {
      if (!loaded || saving) return;
      Object.keys(schema).forEach(function (key) { state[key] = clone(defaults[key]); });
      fieldErrors = {}; renderPage(); refresh(); showToast('已恢复出厂默认，保存后生效。');
    }
    function resetGroup(group) {
      if (!group || !loaded || saving) return;
      group.fields.forEach(function (key) {
        if (key === '@reference') {
          REFERENCE_KEYS.forEach(function (name) { state[name] = clone(defaultOf(name)); delete fieldErrors[name]; });
          return;
        }
        state[key] = clone(defaultOf(key)); delete fieldErrors[key];
      });
      renderPage(); refresh();
      showToast('已恢复「' + group.title + '」的默认值，保存后生效。');
    }

    /* ---------- 初始化 ---------- */
    function wire(id, type, fn) { var el = $(id); if (el) el.addEventListener(type, fn); }
    function init() {
      wire('save', 'click', save);
      wire('revert', 'click', revert);
      wire('restore-factory', 'click', restoreFactory);
      wire('search', 'input', function () {
        var next = $('search').value.trim().toLowerCase();
        /* 真实浏览器里 120ms 防抖，避免逐键全量重渲染；无 rAF 的测试环境保持同步。 */
        if (typeof win.requestAnimationFrame !== 'function') { query = next; renderPage(); refresh(); return; }
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { query = next; renderPage(); refresh(); }, 120);
      });
      wire('search-clear', 'click', function () { $('search').value = ''; query = ''; renderPage(); refresh(); });
      doc.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); return; }
        if (event.key === 'Escape' && query && doc.activeElement === $('search')) { event.preventDefault(); $('search').value = ''; query = ''; renderPage(); refresh(); }
      });
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
        var tz = $('timezone'); if (tz) tz.textContent = timezone ? '机器人时区：' + timezone : '';
        groups = deriveGroups(schema);
        state = {}; Object.keys(schema).forEach(function (key) { state[key] = clone(own(data.config, key) ? data.config[key] : defaults[key]); });
        baseline = clone(state);
        if (activePage !== 'status' && !groupById(activePage)) activePage = groups.length ? groups[0].id : 'overview';
        loaded = true; fieldErrors = {};
        renderDiagnostics(); renderNav(); renderPage(); refresh();
        var boot = $('boot'), app = $('app');
        if (boot) boot.hidden = true;
        if (app) app.hidden = false;
      } catch (error) {
        var bootBox = $('boot');
        if (bootBox) {
          bootBox.replaceChildren();
          bootBox.appendChild(node('strong', '', '配置加载失败'));
          bootBox.appendChild(node('p', '', error.message || '无法读取配置。'));
          bootBox.appendChild(button('重新加载', load, 'btn primary'));
          bootBox.hidden = false;
        }
        var appBox = $('app'); if (appBox) appBox.hidden = true;
      }
    }
    async function start() {
      init();
      try {
        await new Promise(function (resolve, reject) {
          if (win.AstrBotPluginPage) return resolve();
          var started = Date.now();
          var timer = setInterval(function () {
            if (win.AstrBotPluginPage) { clearInterval(timer); resolve(); }
            else if (Date.now() - started > 6000) { clearInterval(timer); reject(new Error('请从 AstrBot 的插件页面打开设置页。')); }
          }, 50);
        });
        if (typeof win.AstrBotPluginPage.ready === 'function') await win.AstrBotPluginPage.ready();
        await load();
      } catch (error) {
        var bootBox = $('boot');
        if (bootBox) {
          bootBox.replaceChildren();
          bootBox.appendChild(node('strong', '', '无法连接 AstrBot'));
          bootBox.appendChild(node('p', '', error.message || '请重新打开插件页面。'));
        }
      }
    }
    start();
  }

  return {
    CORE_PLUGIN: CORE_PLUGIN, GROUP_META: GROUP_META, CLUSTERS: CLUSTERS, VISIBLE_WHEN: VISIBLE_WHEN,
    deriveGroups: deriveGroups, groupOf: groupOf, fieldTitle: fieldTitle, clusterFieldCount: clusterFieldCount,
    parseList: parseList, parseListStats: parseListStats, listSummary: listSummary,
    valueText: valueText, referenceMode: referenceMode, referenceValues: referenceValues,
    dirtyKeys: dirtyKeys, buildPatch: buildPatch, mergeSavedState: mergeSavedState,
    validateValues: validateValues, errorFields: errorFields, matches: matches,
    highlightParts: highlightParts, pendingCount: pendingCount,
    mount: mount,
  };
});
