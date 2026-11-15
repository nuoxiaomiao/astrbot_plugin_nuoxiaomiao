/* 糯小喵设置 — Schema 分组、脏值追踪、校验与纯工具（浏览器 + Node UMD）。 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.NuoxiaomiaoConfigState = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  var CORE_PLUGIN = 'astrbot_plugin_nuoxiaomiao';
  var REFERENCE_KEYS = ['activity_prefetch_enabled', 'capability_context_digest'];
  var WAKE_KEYS = [
    'proactive_silent_wake_floor_minutes',
    'proactive_silent_wake_target_mean_minutes',
    'proactive_silent_wake_ceil_minutes',
  ];
  var TIME_KEYS = ['proactive_quiet_hours_start', 'proactive_quiet_hours_end'];
  var HIDDEN_KEYS = ['proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  var TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  var PAGE_KEY = 'nxm-active-page';
  var CLUSTER_KEY = 'nxm-cluster-open';
  var LIST_KEY = 'nxm-list-open';
  var REVEAL_KEYS = [
    'debounce_dynamic_enabled',
    'debounce_typing_enabled',
    'merge_localize_images',
    'proactive_silent_wake_allow_tools',
  ];
  var LIST_ROWS = 6;
  var LIST_MAX_HEIGHT = 460;

  var GROUP_META = [
    { id: 'overview', title: '总览', note: '插件总开关。关闭后所有功能暂停，设置保留。' },
    { id: 'debounce', title: '消息合并', note: '同一人连发的消息合并后回复。' },
    { id: 'interaction', title: '拦截与互动', note: '刷屏拦截、屏蔽名单、被戳与关键词回应。' },
    { id: 'activity', title: '聊天记录与参考', note: '记录近期聊天，供跨会话查看与未读/已读统计。' },
    { id: 'proactive', title: '主动消息', note: '只在允许名单内生效，有次数与冷却限制。' },
    { id: 'selfwake', title: '自醒', note: '群里安静时自动醒来思考，每天有次数上限。' },
    { id: 'qq_official', title: 'QQ 官方', note: 'QQ 官方机器人平台的专用适配：合成消息投递、纯文本、转发展开与唤醒控制。' },
    { id: 'advanced', title: '高级', note: '工具提示词、近况附带与记录库路径，通常保持默认。' },
    { id: 'resident_state', title: '自主状态', note: '自主运行与聊天时的心情与关注点（管理入口在“运行与状态”）。' },
  ];

  var CLUSTERS = [
    {
      id: 'debounce', title: '防抖合并', gates: ['debounce_enabled'],
      children: [
        'debounce_dynamic_enabled', 'debounce_group_enabled', 'debounce_private_enabled',
        'debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds',
        'debounce_typing_enabled', 'debounce_typing_hold_seconds', 'debounce_long_chars',
        'debounce_end_punctuation', 'debounce_max_parts', 'merge_idle_seconds',
        'merge_max_total_seconds', 'merge_join_separator', 'merge_localize_images', 'merge_localize_images_dir',
      ],
      lists: ['debounce_user_whitelist', 'debounce_group_whitelist', 'debounce_group_blacklist'],
    },
    {
      id: 'guard', title: '刷屏拦截', gates: ['include_group', 'include_private'],
      children: ['trigger_rounds', 'window_seconds', 'meme_tags', 'meme_cooldown_seconds'],
      lists: ['user_whitelist', 'user_blacklist', 'guard_group_whitelist', 'guard_group_blacklist'],
    },
    {
      id: 'poke', title: '被戳回应', gates: ['poke_reply_enabled'],
      children: ['poke_reply_prompt', 'poke_reply_cooldown_seconds'],
      lists: ['poke_group_whitelist', 'poke_group_blacklist'],
    },
    {
      id: 'keyword_wake', title: '关键词唤醒', gates: ['keyword_wake_enabled'],
      children: ['keyword_wake_list'],
      lists: ['keyword_wake_group_whitelist', 'keyword_wake_group_blacklist'],
    },
    {
      id: 'activity', title: '记录聊天', gates: ['activity_buffer_enabled'],
      children: [
        'activity_record_private', 'activity_record_bot_replies', 'activity_buffer_max_text_chars',
        'activity_retention_days', 'activity_require_admin', 'activity_tool_enabled', 'activity_hint_enabled',
      ],
      lists: ['activity_group_whitelist', 'activity_group_blacklist'],
    },
    {
      id: 'proactive', title: '主动消息', gates: ['proactive_enabled'],
      children: [
        'proactive_hint_enabled', 'proactive_max_pending_per_person', 'proactive_daily_limit_per_person',
        'proactive_cooldown_minutes', 'proactive_context_other_sessions', 'proactive_context_other_private',
        'proactive_quiet_hours_start', 'proactive_quiet_hours_end',
      ],
      lists: ['proactive_allow_groups', 'proactive_target_whitelist'],
    },
    {
      id: 'selfwake', title: '静默自醒', gates: ['proactive_silent_wake_enabled'],
      children: [
        'proactive_judge_provider_id', 'proactive_silent_wake_floor_minutes',
        'proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_ceil_minutes',
        'proactive_silent_wake_daily_budget', 'proactive_silent_wake_max_steps',
        'proactive_silent_wake_allow_tools', 'resident_extra_tools',
        'resident_run_token_limit', 'resident_daily_token_limit',
      ],
      lists: [],
    },
    {
      id: 'qq_official', title: 'QQ 官方适配', gates: ['qq_official_enabled'],
      children: [
        'qq_official_content_mode', 'qq_official_group_proactive', 'qq_official_strip_at_markup',
        'qq_official_quote_trigger', 'qq_official_text_chars', 'qq_official_flatten_forward',
      ],
      lists: ['qq_official_admin_openids'],
    },
    { id: 'tool_prompt', title: '自定义工具提示', gates: ['tool_call_prompt_enabled'], children: ['tool_call_prompt'], lists: [] },
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

  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function fieldTitle(spec, key) { return String((spec || {}).description || key).replace(/^【[^】]+】\s*/, ''); }
  function clusterFieldCount(cluster) { return cluster.children.length + cluster.lists.length; }
  function parseBool(value) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'string') {
      var low = value.trim().toLowerCase();
      if (low === '0' || low === 'false' || low === 'no' || low === 'off' || low === '禁用' || low === '否') return false;
      if (low === '1' || low === 'true' || low === 'yes' || low === 'on' || low === '启用' || low === '是') return true;
    }
    return !!value;
  }
  function clusterGateOn(cluster, state) {
    return cluster.gates.some(function (key) { return parseBool(state[key]); });
  }
  function isHiddenKey(key, spec) {
    return HIDDEN_KEYS.indexOf(key) >= 0 || !!(spec && spec.invisible);
  }
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
      var spec = schema[key];
      if (!spec || typeof spec !== 'object' || isHiddenKey(key, spec)) return;
      if (REFERENCE_KEYS.indexOf(key) >= 0 && key !== 'activity_prefetch_enabled') return;
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
  function parseList(value) {
    var list = Array.isArray(value) ? value : String(value || '').split(/[\n,，、;；]+/);
    return Array.from(new Set(list.map(function (x) { return String(x).trim(); }).filter(Boolean)));
  }
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
    return parseBool(value.capability_context_digest) ? 'background' : parseBool(value.activity_prefetch_enabled) ? 'prefetch' : 'tools';
  }
  function referenceValues(mode) {
    return { activity_prefetch_enabled: mode === 'prefetch', capability_context_digest: mode === 'background' };
  }
  function dirtyKeys(state, baseline, schema) {
    return Object.keys(schema).filter(function (key) {
      return !isHiddenKey(key, schema[key]) && !same(state[key], baseline[key]);
    });
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
      if (
        typeof floor === 'number' && Number.isFinite(floor)
        && typeof mean === 'number' && Number.isFinite(mean)
        && typeof ceil === 'number' && Number.isFinite(ceil)
        && !(ceil >= floor + 2 && mean >= floor + 1 && mean <= ceil - 1)
      ) {
        WAKE_KEYS.forEach(function (key) { errors[key] = '上限至少比下限多 2 分钟；平均间隔须在下限 + 1 与上限 − 1 之间。'; });
      }
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

  return {
    CORE_PLUGIN: CORE_PLUGIN,
    REFERENCE_KEYS: REFERENCE_KEYS,
    WAKE_KEYS: WAKE_KEYS,
    TIME_KEYS: TIME_KEYS,
    HIDDEN_KEYS: HIDDEN_KEYS,
    TIME_RE: TIME_RE,
    PAGE_KEY: PAGE_KEY,
    CLUSTER_KEY: CLUSTER_KEY,
    LIST_KEY: LIST_KEY,
    REVEAL_KEYS: REVEAL_KEYS,
    LIST_ROWS: LIST_ROWS,
    LIST_MAX_HEIGHT: LIST_MAX_HEIGHT,
    GROUP_META: GROUP_META,
    CLUSTERS: CLUSTERS,
    VISIBLE_WHEN: VISIBLE_WHEN,
    clusterByChild: clusterByChild,
    clusterByGate: clusterByGate,
    clone: clone,
    same: same,
    own: own,
    parseBool: parseBool,
    isHiddenKey: isHiddenKey,
    fieldTitle: fieldTitle,
    clusterFieldCount: clusterFieldCount,
    clusterGateOn: clusterGateOn,
    deriveGroups: deriveGroups,
    groupOf: groupOf,
    parseList: parseList,
    parseListStats: parseListStats,
    listSummary: listSummary,
    valueText: valueText,
    highlightParts: highlightParts,
    pendingCount: pendingCount,
    referenceMode: referenceMode,
    referenceValues: referenceValues,
    dirtyKeys: dirtyKeys,
    buildPatch: buildPatch,
    mergeSavedState: mergeSavedState,
    validateValues: validateValues,
    errorFields: errorFields,
    matches: matches,
  };
});
