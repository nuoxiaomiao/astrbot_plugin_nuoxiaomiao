/* The schema owns defaults and native field text; this page owns layout and labels. */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  var CORE_PLUGIN = 'astrbot_plugin_nuoxiaomiao';
  var REFERENCE_KEYS = ['activity_prefetch_enabled', 'capability_context_digest'];
  var WAKE_KEYS = ['proactive_silent_wake_floor_minutes', 'proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_ceil_minutes'];
  var TIME_KEYS = ['proactive_quiet_hours_start', 'proactive_quiet_hours_end'];
  var OMITTED_KEYS = ['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  var FIELD_TITLES = {
    debounce_enabled: '启用消息合并',
    debounce_group_enabled: '合并群聊中的连续消息',
    debounce_private_enabled: '合并私聊中的连续消息',
    debounce_dynamic_enabled: '按消息内容决定等待时间',
    debounce_typing_enabled: '对方还在输入时继续等待',
    human_reply_enabled: '启用分条回复',
    activity_tool_enabled: '允许模型查询聊天记录',
    lmem_gateway_enabled: '启用长期记忆检索',
    human_reply_max_length: '超过多少字时不分条发送',
    human_reply_max_wait_seconds: '每条回复最多等多久（秒）',
    trigger_rounds: '连续对话几轮后拦截',
    window_seconds: '多久没新触发就重新计数（秒）',
    user_whitelist: '这些用户免于刷屏拦截',
    user_blacklist: '只检测这些用户（留空检测所有人）',
    proactive_silent_wake_floor_minutes: '最早多久醒来（分钟）',
    proactive_silent_wake_ceil_minutes: '最晚多久醒来（分钟）',
    proactive_silent_wake_target_mean_minutes: '平均多久醒来（分钟）',
    proactive_silent_wake_max_steps: '每次自醒最多执行几步',
    resident_extra_tools: '自醒可以使用的额外工具'
  };
  function fieldTitle(spec, key) { return FIELD_TITLES[key] || String((spec || {}).description || key).replace(/^【[^】]+】\s*/, ''); }
  function parseList(value) {
    var list = Array.isArray(value) ? value : String(value || '').split(/[\n,，、;；]+/);
    return Array.from(new Set(list.map(function (x) { return String(x).trim(); }).filter(Boolean)));
  }
  function referenceMode(value) {
    return value.capability_context_digest ? 'background' : value.activity_prefetch_enabled ? 'prefetch' : 'tools';
  }
  function referenceValues(mode) {
    return { activity_prefetch_enabled: mode === 'prefetch', capability_context_digest: mode === 'background' };
  }
  function toolMode(value) {
    var names = parseList(value);
    return names.length === 0 ? 'all' : names.every(function (name) { return name === CORE_PLUGIN; }) ? 'core' : 'selected';
  }
  function toolValues(mode, names) {
    if (mode === 'all') return [];
    if (mode === 'core') return [CORE_PLUGIN];
    return [CORE_PLUGIN].concat(parseList(names).filter(function (name) { return name !== CORE_PLUGIN && name !== '（内置工具）'; }));
  }
  function dirtyKeys(state, baseline, schema) {
    return Object.keys(schema).filter(function (key) { return OMITTED_KEYS.indexOf(key) < 0 && !same(state[key], baseline[key]); });
  }
  function buildPatch(state, baseline, schema) {
    var result = {};
    dirtyKeys(state, baseline, schema).forEach(function (key) { result[key] = clone(state[key]); });
    // Normalize a legacy both-on configuration only when the reference mode is edited.
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
      if (TIME_KEYS.indexOf(key) >= 0 && !/^([01]\d|2[0-3]):[0-5]\d$/.test(String(value))) errors[key] = '请选择 00:00 到 23:59 的时间。';
    });
    // The server validates the merged config, even for an empty default snapshot.
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
  function section(id, title, note, fields, advanced, children) { return { id: id, title: title, note: note, fields: fields || [], advanced: !!advanced, children: children || [] }; }
  function feature(id, title, note, fields, children, enabledKeys) {
    return Object.assign(section(id, title, note, fields, false, children), { feature: true, enabledKeys: enabledKeys || [] });
  }
  function walkSections(sections, visit, ancestors) {
    (sections || []).forEach(function (s) { var path = (ancestors || []).concat(s); visit(s, path); walkSections(s.children, visit, path); });
  }
  function sectionFields(s) {
    var fields = [];
    walkSections([s], function (item) { fields = fields.concat(item.fields); });
    return fields;
  }
  function configKeys(key) { return key === '@reference' ? REFERENCE_KEYS : [key]; }
  var GROUPS = [
    { id: 'overview', title: '总览', note: '查看各功能的设置、运行检查和插件总开关。', sections: [feature('global', '插件总开关', '', ['enabled'])] },
    { id: 'debounce', title: '消息合并', note: '把连续发送的几条消息合起来，再统一回复。', sections: [
      feature('merge', '合并连续消息', '群聊只合并已经唤醒机器人的消息，指令直接放行。', ['debounce_enabled'], [
        section('scenes', '在哪些聊天里启用', '', ['debounce_group_enabled', 'debounce_private_enabled']),
        section('waiting', '等多久再回复', '按内容等待时，完整句、短句和只叫名字分别计时。', ['debounce_dynamic_enabled', 'debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds', 'merge_idle_seconds']),
        section('typing', '对方还在输入时', '只有支持输入状态通知的私聊平台才生效。', ['debounce_typing_enabled', 'debounce_typing_hold_seconds']),
        section('advanced', '高级设置', '限制范围、判断规则和合并细节。', [], true, [
          section('scope', '只对哪些人和群生效', '名单留空表示不限制；群排除名单优先。', ['debounce_user_whitelist', 'debounce_group_whitelist', 'debounce_group_blacklist']),
          section('rules', '怎样判断已经说完', '', ['debounce_long_chars', 'debounce_end_punctuation', 'force_wake_prefix']),
          section('limits', '合并数量与最长等待', '最长等待从本批第一条消息开始计算。', ['debounce_max_parts', 'merge_max_total_seconds', 'merge_join_separator']),
          section('images', '合并图片', '目录留空使用插件默认位置。', ['merge_localize_images', 'merge_localize_images_dir'])
        ])
      ], ['debounce_enabled'])
    ] },
    { id: 'human_reply', title: '回复节奏', note: '让回复按打字速度逐条发送。', sections: [
      feature('reply', '按打字速度分条回复', '仅处理非流式纯文本；OutputPro 先清洗和处理引用，再按这里的节奏发送。', ['human_reply_enabled'], [
        section('speed', '打字速度', '', ['human_reply_chars_per_minute']),
        section('interrupt', '用户插话时', '已经发出第一条回复后，本轮会说完，新消息排到下一轮。', ['human_reply_interrupt_enabled']),
        section('typing', '向对方显示输入状态', '', ['human_reply_show_input_status']),
        section('advanced', '高级设置', '发送限制和生效群范围。', [], true, [
          section('limits', '长度与等待限制', '', ['human_reply_max_length', 'human_reply_max_wait_seconds']),
          section('scope', '在哪些群生效', '群名单同时限制插话打断和分条发送，私聊不受群名单影响。', ['human_reply_group_whitelist', 'human_reply_group_blacklist'])
        ])
      ], ['human_reply_enabled'])
    ] },
    { id: 'interaction', title: '拦截与互动', note: '刷屏拦截、屏蔽用户、被戳回应和关键词唤醒分别设置，互不代替。', sections: [
      feature('guard', '刷屏拦截', '统计与机器人对话的批次；合并后的几条消息算一轮。', ['include_group', 'include_private'], [
        section('rules', '什么时候拦截', '', ['trigger_rounds', 'window_seconds']),
        section('advanced', '高级设置', '检测名单、生效群和表情回应。', [], true, [
          section('users', '检测哪些用户', '仅检测名单留空时检测所有非免检用户；免检名单优先。', ['user_whitelist', 'user_blacklist']),
          section('scope', '在哪些群拦截', '', ['guard_group_whitelist', 'guard_group_blacklist']),
          section('response', '拦截后发送的表情', '需要安装 meme_manager。', ['meme_tags', 'meme_cooldown_seconds'])
        ])
      ], ['include_group', 'include_private']),
      feature('blocked', '屏蔽用户', '名单里的用户发来指令或聊天都不处理；优先于刷屏免检名单。', ['blocked_users']),
      feature('poke', '被戳回应', '被戳时让模型回一句，群聊有冷却，私聊没有冷却。', ['poke_reply_enabled'], [
        section('cooldown', '多久可以再回应', '', ['poke_reply_cooldown_seconds']),
        section('advanced', '高级设置', '回应提示词和生效群范围。', [], true, [
          section('prompt', '让模型怎么回应', '', ['poke_reply_prompt']),
          section('scope', '在哪些群回应', '', ['poke_group_whitelist', 'poke_group_blacklist'])
        ])
      ], ['poke_reply_enabled']),
      feature('keyword', '关键词唤醒', '群消息没有 @ 机器人时，提到关键词也能唤醒。', ['keyword_wake_enabled'], [
        section('words', '哪些词会唤醒', '', ['keyword_wake_list']),
        section('advanced', '高级设置', '限制生效群范围。', ['keyword_wake_group_whitelist', 'keyword_wake_group_blacklist'], true)
      ], ['keyword_wake_enabled'])
    ] },
    { id: 'activity', title: '聊天记录与参考', note: '记录近期聊天、查看其它会话，并按需要参考聊天或长期记忆。', sections: [
      feature('recording', '聊天记录', '停止记录会暂停会话速览，已有记录仍然保留。', ['activity_buffer_enabled'], [
        section('sources', '记录哪些消息', '', ['activity_record_private', 'activity_record_bot_replies', 'resident_inbox_enabled']),
        section('retention', '保留多久', '', ['activity_retention_days']),
        section('advanced', '高级设置', '记录群范围、单条长度和保存位置。', [], true, [
          section('scope', '记录哪些群', '名单改变不会删除已经保存的记录。', ['activity_group_whitelist', 'activity_group_blacklist']),
          section('storage', '记录长度与保存位置', '数据库路径留空使用插件默认位置。', ['activity_buffer_max_text_chars', 'activity_db_path'])
        ])
      ], ['activity_buffer_enabled']),
      feature('permissions', '查看权限', '允许模型通过会话速览工具查询聊天记录。', ['activity_tool_enabled'], [
        section('access', '谁可以查看其它会话', '', ['activity_require_admin']),
        section('hint', '提醒模型可以查询', '', ['activity_hint_enabled'])
      ], ['activity_tool_enabled']),
      feature('reference', '聊天参考', '选择别处聊天怎样进入当前回复，仍遵守记录范围和查看权限。', ['@reference'], [
        section('keywords', '什么时候自动参考', '', ['activity_prefetch_keywords'])
      ]),
      feature('memory', '长期记忆', '命中触发词后从 LivingMemory 检索，未安装时跳过。', ['lmem_gateway_enabled'], [
        section('keywords', '什么时候查记忆', '', ['lmem_gateway_keywords']),
        section('advanced', '高级设置', '检索数量、等待时间和兼容参数。', [], true, [
          section('limits', '检索限制', '', ['lmem_gateway_top_k', 'lmem_gateway_timeout_seconds', 'lmem_gateway_max_chars']),
          section('integration', 'LivingMemory 连接设置', '通常保持默认即可。', ['lmem_gateway_plugin_name', 'lmem_gateway_recall_tool_name'])
        ])
      ], ['lmem_gateway_enabled'])
    ] },
    { id: 'proactive', title: '主动消息', note: '允许模型主动联系指定的群和用户。', sections: [
      feature('send', '主动联系', '模型通过 send_message 发起消息；静默时段内顺延到时段结束。', ['proactive_enabled'], [
        section('targets', '可以联系谁', '允许群留空表示不向任何群发送；主人和管理员始终允许被主动私聊。', ['proactive_allow_groups', 'proactive_target_whitelist']),
        section('frequency', '多久联系一次', '', ['proactive_daily_limit_per_person', 'proactive_cooldown_minutes']),
        section('quiet', '什么时候保持安静', '开始与结束相同表示关闭静默时段，按页面显示的机器人时区运行。', ['proactive_quiet_hours_start', 'proactive_quiet_hours_end']),
        section('advanced', '高级设置', '待发数量、模型提醒和生成时参考的会话。', [], true, [
          section('pending', '最多积攒多少待发消息', '', ['proactive_max_pending_per_person']),
          section('context', '生成时参考哪些会话', '也会影响自醒；普通回复的参考方式在“聊天记录与参考”中设置。', ['proactive_context_other_sessions', 'proactive_context_other_private']),
          section('hint', '提醒模型可以主动联系', '', ['proactive_hint_enabled'])
        ])
      ], ['proactive_enabled'])
    ] },
    { id: 'selfwake', title: '自醒', note: '没有新消息时定期醒来，决定要不要行动。', sections: [
      feature('wake', '安静时自己醒来', '真人消息会重新开始安静计时；静默时段内不自醒。', ['proactive_silent_wake_enabled'], [
        section('model', '使用哪个模型', '', ['proactive_judge_provider_id']),
        section('schedule', '多久醒来、每天几次', '每日自醒次数与主动消息发送次数分别限制。', ['proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_daily_budget']),
        section('tools', '醒来后可以做什么', '', ['proactive_silent_wake_allow_tools', 'resident_extra_tools']),
        section('advanced', '高级设置', '间隔边界、执行步骤和模型用量限制。', [], true, [
          section('intervals', '醒来间隔的上下限', '上下限至少相差 2 分钟，平均间隔距离两端至少 1 分钟。', ['proactive_silent_wake_floor_minutes', 'proactive_silent_wake_ceil_minutes']),
          section('steps', '最多执行几步', '', ['proactive_silent_wake_max_steps']),
          section('tokens', '模型用量限制', '0 表示不额外限制。', ['resident_run_token_limit', 'resident_daily_token_limit'])
        ])
      ], ['proactive_silent_wake_enabled'])
    ] },
    { id: 'advanced', title: '高级', note: '通用工具提示词与能力文案；各功能的详细参数在对应功能内设置。', sections: [
      feature('prompts', '自定义工具提示词', '为工具调用补充通用提示，通常保持默认即可。', ['tool_call_prompt_enabled'], [
        section('advanced', '高级设置', '发送给模型的通用工具提示。', ['tool_call_prompt'], true)
      ], ['tool_call_prompt_enabled']),
      feature('capability', '自定义能力说明', '自定义文案会覆盖内置提示，请与实际功能开关保持一致。', [], [
        section('advanced', '高级设置', '留空使用内置能力说明。', ['capability_hint_text'], true)
      ])
    ] }
  ];
  GROUPS.push({ id: 'resident', title: '自主运行与状态', note: '查看真实运行回执、能力联动、信箱与风格提案。下方管理操作即时生效，配置开关仍需保存。', sections: [feature('state', '自主状态', '', ['resident_state_enabled'])] });
  function fieldPath(key) {
    var target = REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key, found = null;
    GROUPS.some(function (group) {
      walkSections(group.sections, function (s, path) { if (s.fields.indexOf(target) >= 0) found = { group: group, path: path }; });
      return !!found;
    });
    return found;
  }
  function groupOf(key) {
    var home = fieldPath(key);
    return home ? home.group : GROUPS.find(function (group) { return group.id === 'advanced'; });
  }
  function matches(key, spec, query) {
    var home = fieldPath(key), path = home ? home.path.map(function (s) { return s.title + ' ' + s.note; }).join(' ') : '';
    return [key, fieldTitle(spec, key), spec.description || '', spec.hint || '', groupOf(key).title, path].join(' ').toLowerCase().indexOf(query.toLowerCase()) >= 0;
  }
  function overviewStatus(groupId, state) {
    if (!state.enabled) return { active: false, label: '总开关关闭' };
    if (groupId === 'debounce') return !state.debounce_enabled ? { active: false, label: '已关闭' } : !state.debounce_group_enabled && !state.debounce_private_enabled ? { active: false, label: '未开启场景' } : { active: true, label: '按场景生效' };
    if (groupId === 'human_reply') return { active: !!state.human_reply_enabled, label: state.human_reply_enabled ? '纯文本生效' : '已关闭' };
    if (groupId === 'interaction') { var active = state.include_group || state.include_private || state.poke_reply_enabled || (state.keyword_wake_enabled && parseList(state.keyword_wake_list).length > 0) || parseList(state.blocked_users).length > 0; return { active: !!active, label: active ? '按功能生效' : '已关闭' }; }
    if (groupId === 'activity') { var active = state.activity_buffer_enabled || state.lmem_gateway_enabled; return { active: !!active, label: active ? '按来源生效' : '已关闭' }; }
    if (groupId === 'proactive') return { active: !!state.proactive_enabled, label: state.proactive_enabled ? '按目标生效' : '已关闭' };
    if (groupId === 'selfwake') return !state.proactive_enabled ? { active: false, label: '主动消息关闭' } : !state.proactive_silent_wake_enabled ? { active: false, label: '已关闭' } : !state.proactive_silent_wake_daily_budget ? { active: false, label: '日预算为 0' } : { active: true, label: '安静时生效' };
    return null;
  }

  function mount(win) {
    var doc = win.document;
    var schema = {}, state = {}, baseline = {}, defaults = {}, userDefault = null;
    var providers = [], catalog = [], residentTools = [], diagnostics = [], timezone = '';
    var activeGroup = 'overview', query = '', pendingOnly = false, saving = false, loaded = false;
    var fieldErrors = {}, toastTimer = null, selectedToolMode = null;
    var expanded = {}, editingDisabled = {}, revealedFields = {};
    var featureViews = [], sectionViews = [];
    function $(id) { return doc.getElementById(id); }
    function node(tag, cls, text) { var n = doc.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = String(text); return n; }
    function button(text, handler, cls) { var n = node('button', cls, text); n.type = 'button'; n.addEventListener('click', handler); return n; }
    function valueText(value) { return value === '' ? '留空' : Array.isArray(value) ? value.length ? value.join('、') : '空名单' : value === true ? '开启' : value === false ? '关闭' : String(value); }
    function defaultOf(key, factory) { return !factory && userDefault && own(userDefault, key) ? userDefault[key] : defaults[key]; }
    function reason(key) {
      if (key === 'enabled') return '';
      if (!state.enabled) return ''; // The page banner explains the global switch once.
      if (/^debounce_|^merge_|^force_wake_prefix$/.test(key) && key !== 'debounce_enabled' && !state.debounce_enabled) return '防抖合并关闭；可以先编辑，开启后生效。';
      if (['debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds', 'debounce_long_chars', 'debounce_end_punctuation'].indexOf(key) >= 0 && !state.debounce_dynamic_enabled) return '当前使用固定等待，这项暂不参与判定。';
      if (key === 'merge_idle_seconds' && state.debounce_dynamic_enabled) return '当前按内容动态等待，这项暂不使用。';
      if (/^debounce_typing_/.test(key) && (!state.debounce_private_enabled || (key !== 'debounce_typing_enabled' && !state.debounce_typing_enabled))) return '仅在私聊防抖和输入状态等待开启时生效。';
      if (key === 'merge_localize_images_dir' && !state.merge_localize_images) return '图片转存关闭，这个目录暂不使用。';
      if (/^human_reply_/.test(key) && key !== 'human_reply_enabled' && !state.human_reply_enabled) return '拟人回复关闭；可以先编辑，开启后生效。';
      if (/^poke_/.test(key) && key !== 'poke_reply_enabled' && !state.poke_reply_enabled) return '被戳回应关闭，这项暂不生效。';
      if (/^keyword_wake_/.test(key) && key !== 'keyword_wake_enabled' && !state.keyword_wake_enabled) return '关键词唤醒关闭，这项暂不生效。';
      if (['trigger_rounds', 'window_seconds', 'user_whitelist', 'user_blacklist', 'meme_tags', 'meme_cooldown_seconds', 'guard_group_whitelist', 'guard_group_blacklist'].indexOf(key) >= 0 && !state.include_group && !state.include_private) return '群聊和私聊刷屏拦截均关闭，这项暂不生效。';
      if (/^activity_/.test(key) && key !== 'activity_buffer_enabled' && !state.activity_buffer_enabled) return '聊天记录关闭，速览工具和预取暂不生效。';
      if (key === 'activity_prefetch_keywords' && referenceMode(state) !== 'prefetch') return '仅在“关键词预取”模式下使用。';
      if (key === 'activity_hint_enabled' && !state.activity_tool_enabled) return '速览工具关闭，模型工具提示暂不生效。';
      if (/^lmem_gateway_/.test(key) && key !== 'lmem_gateway_enabled' && !state.lmem_gateway_enabled) return '长期记忆检索关闭，这项暂不生效。';
      if (/^proactive_/.test(key) && key !== 'proactive_enabled' && !state.proactive_enabled) return '主动消息关闭，静默自醒也暂停。';
      if (/^proactive_silent_wake_|^proactive_judge_provider_id$/.test(key) && key !== 'proactive_silent_wake_enabled' && (!state.proactive_silent_wake_enabled || state.proactive_silent_wake_daily_budget === 0)) return '静默自醒关闭或日预算为 0，这项暂不生效。';
      if (['resident_extra_tools', 'proactive_silent_wake_tool_plugins', 'proactive_silent_wake_max_steps'].indexOf(key) >= 0 && !state.proactive_silent_wake_allow_tools) return '自醒未允许调用工具，这项暂不使用。';
      if (key === 'tool_call_prompt' && !state.tool_call_prompt_enabled) return '自定义工具提示关闭，这段文案暂不生效。';
      return '';
    }
    function showToast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(function () { $('toast').hidden = true; }, 5000); }
    function onChange(key, value) {
      state[key] = value; delete fieldErrors[key];
      if (WAKE_KEYS.indexOf(key) >= 0) WAKE_KEYS.forEach(function (k) { delete fieldErrors[k]; });
      GROUPS.forEach(function (group) {
        walkSections(group.sections, function (s, path) {
          if (!s.feature || s.enabledKeys.indexOf(key) < 0) return;
          delete editingDisabled[group.id + ':' + path.map(function (item) { return item.id; }).join(':')];
          sectionFields(s).forEach(function (fieldKey) { delete revealedFields[fieldKey]; });
        });
      });
      refresh();
    }
    function labelInput(input, key) { input.id = 'input-' + key; input.setAttribute('aria-label', fieldTitle(schema[key], key)); input.setAttribute('aria-describedby', 'hint-' + key + ' reason-' + key + ' error-' + key); return input; }
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
        { id: 'background', title: '每轮背景', note: '带工具的回复每轮附带别处近况，不再同时关键词预取。' }
      ], function (mode) { Object.assign(state, referenceValues(mode)); REFERENCE_KEYS.forEach(function (key) { delete fieldErrors[key]; }); refresh(); }));
      var hint = node('p', 'field-reason'); hint.id = 'reason-@reference'; wrap.appendChild(hint);
      var error = node('p', 'field-error'); error.id = 'error-@reference'; wrap.appendChild(error);
      var reset = button('恢复' + (userDefault ? '我的默认' : '出厂默认'), function () { REFERENCE_KEYS.forEach(function (key) { state[key] = clone(defaultOf(key)); }); renderPane(); refresh(); }, 'restore-field');
      wrap.appendChild(reset); return wrap;
    }
    function residentToolsControl(key) {
      var wrap = node('div', 'tool-list'), names = parseList(state[key]), seen = [];
      wrap.appendChild(node('p', 'field-hint', '基础记录、记忆读取、计划、状态和发信工具固定保留，但仍受功能开关与会话权限限制。以下选择额外工具。'));
      residentTools.filter(function (item) { return !item.base; }).forEach(function (item) {
        seen.push(item.name);
        var label = node('label', 'tool-option'), input = node('input'); input.type = 'checkbox'; input.value = item.name; input.checked = names.indexOf(item.name) >= 0;
        input.disabled = (!item.active || !item.selectable) && !input.checked;
        input.addEventListener('change', function () { var next = parseList(state[key]).filter(function (name) { return name !== item.name; }); if (input.checked) next.push(item.name); onChange(key, next); });
        label.appendChild(input); label.appendChild(node('span', '', item.name + ' · ' + (item.plugin || 'AstrBot') + (item.reason ? ' · ' + item.reason : !item.active ? ' · 已停用' : ''))); wrap.appendChild(label);
      });
      names.filter(function (name) { return seen.indexOf(name) < 0; }).forEach(function (name) {
        var label = node('label', 'tool-option'), input = node('input'); input.type = 'checkbox'; input.checked = true;
        input.addEventListener('change', function () { onChange(key, parseList(state[key]).filter(function (x) { return x !== name; })); });
        label.appendChild(input); label.appendChild(node('span', '', name + ' · 当前不可用，可取消选择')); wrap.appendChild(label);
      });
      return wrap;
    }
    function toolsControl(key) {
      var wrap = node('div'), mode = selectedToolMode || toolMode(state[key]);
      wrap.appendChild(optionsControl('tool-mode', mode, [
        { id: 'all', title: '全部工具', note: '使用全部可用工具，配置简单，上下文较多。' },
        { id: 'core', title: '仅核心工具', note: '只看会话、读消息和发送主动消息。' },
        { id: 'selected', title: '选择插件', note: '核心工具始终保留，再添加指定插件的工具。' }
      ], function (next) { selectedToolMode = next; onChange(key, toolValues(next, state[key])); renderPane(); refresh(); }));
      wrap.appendChild(node('p', 'tool-fixed', '固定保留：list_conversations · read_conversation_messages · send_message'));
      if (mode !== 'selected') return wrap;
      var external = catalog.filter(function (item) { return item.name !== CORE_PLUGIN && !item.builtin && item.name !== '（内置工具）'; });
      var missing = parseList(state[key]).filter(function (name) { return name !== CORE_PLUGIN && !external.some(function (item) { return item.name === name; }); });
      if (!external.length) wrap.appendChild(node('p', 'field-hint', '当前没有可选择的外部插件工具。'));
      var list = node('div', 'tool-list');
      external.concat(missing.map(function (name) { return { name: name, missing: true }; })).forEach(function (item) {
        var label = node('label', 'tool-item' + (item.missing ? ' tool-missing' : '')), checkbox = node('input');
        checkbox.type = 'checkbox'; checkbox.checked = parseList(state[key]).indexOf(item.name) >= 0;
        checkbox.addEventListener('change', function () { var names = parseList(state[key]).filter(function (name) { return name !== item.name; }); if (checkbox.checked) names.push(item.name); onChange(key, toolValues('selected', names)); count.textContent = '已选择 ' + parseList(state[key]).filter(function (name) { return name !== CORE_PLUGIN; }).length + ' 个外部插件'; });
        var text = node('span'); text.appendChild(node('strong', '', item.name)); text.appendChild(node('small', '', item.missing ? '当前不可用；取消勾选可从配置移除。' : (item.count || 0) + ' 个工具 · ' + (item.tools || []).join('、')));
        label.appendChild(checkbox); label.appendChild(text); list.appendChild(label);
      });
      wrap.appendChild(list);
      var count = node('p', 'field-hint', '已选择 ' + parseList(state[key]).filter(function (name) { return name !== CORE_PLUGIN; }).length + ' 个外部插件'); wrap.appendChild(count);
      wrap.appendChild(node('p', 'field-hint', '内置工具不参加插件筛选。清空外部选择后仅保留核心工具。'));
      return wrap;
    }
    function buildControl(key, spec) {
      var value = state[key], input;
      if (key === 'resident_extra_tools') return residentToolsControl(key);
      if (key === 'proactive_silent_wake_tool_plugins') return toolsControl(key);
      if (spec.type === 'bool') {
        var label = node('label', 'switch'); input = labelInput(node('input'), key); input.type = 'checkbox'; input.checked = !!value;
        var text = node('span', 'switch-text', input.checked ? '开' : '关');
        input.addEventListener('change', function () { text.textContent = input.checked ? '开' : '关'; onChange(key, input.checked); });
        label.appendChild(input); label.appendChild(text); return label;
      }
      if (key === 'proactive_judge_provider_id') {
        var holder = node('div'); input = labelInput(node('select'), key); var auto = node('option', '', '使用全局默认模型'); auto.value = ''; input.appendChild(auto);
        providers.forEach(function (provider) { var o = node('option', '', provider.label || provider.id); o.value = provider.id; input.appendChild(o); });
        if (value && !providers.some(function (provider) { return provider.id === value; })) { var old = node('option', '', '当前不可用：' + value); old.value = value; input.appendChild(old); holder.appendChild(node('small', 'provider-warning', '原配置的模型当前不可用，已保留。请选择可用模型后保存。')); }
        input.value = value || ''; input.addEventListener('change', function () { onChange(key, input.value); var warning = holder.querySelector('.provider-warning'); if (warning) warning.remove(); }); holder.prepend(input); return holder;
      }
      if (spec.type === 'list') {
        var listHolder = node('div'); input = labelInput(node('textarea'), key); input.rows = 3; input.value = parseList(value).join('\n'); input.placeholder = '每行一项，也可用逗号分隔';
        var foot = node('div', 'list-footer'), count = node('span', '', parseList(value).length + ' 项');
        input.addEventListener('input', function () { var list = parseList(input.value); count.textContent = list.length + ' 项'; onChange(key, list); });
        foot.appendChild(count); foot.appendChild(button('清空名单', function () { input.value = ''; count.textContent = '0 项'; onChange(key, []); })); listHolder.appendChild(input); listHolder.appendChild(foot); return listHolder;
      }
      if (key === 'merge_join_separator') {
        var separator = node('div'); input = labelInput(node('select'), key);
        [['\n', '换行（推荐）'], [' ', '空格'], ['@custom', '自定义']].forEach(function (item) { var o = node('option', '', item[1]); o.value = item[0]; input.appendChild(o); });
        input.value = value === '\n' || value === ' ' ? value : '@custom';
        var custom = node('textarea'); custom.rows = 2; custom.value = value || ''; custom.setAttribute('aria-label', '自定义合并分隔符'); custom.hidden = input.value !== '@custom'; custom.style.marginTop = '10px';
        input.addEventListener('change', function () { custom.hidden = input.value !== '@custom'; onChange(key, custom.hidden ? input.value : custom.value); }); custom.addEventListener('input', function () { onChange(key, custom.value); }); separator.appendChild(input); separator.appendChild(custom); return separator;
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
      var top = node('div', 'field-top'), copy = node('div', 'field-copy'), label = node('label', 'field-label', fieldTitle(spec, key)); label.htmlFor = 'input-' + key; copy.appendChild(label);
      var hint = node('p', 'field-hint', spec.hint || ''); hint.id = 'hint-' + key; copy.appendChild(hint);
      var actions = node('div', 'field-actions');
      var reset = button('恢复' + (userDefault && own(userDefault, key) ? '我的默认' : '出厂默认'), function () { state[key] = clone(defaultOf(key)); delete fieldErrors[key]; if (key === 'proactive_silent_wake_tool_plugins') selectedToolMode = null; renderPane(); refresh(); }, 'restore-field'); reset.title = valueText(defaultOf(key)); actions.appendChild(reset); copy.appendChild(actions);
      top.appendChild(copy); var control = node('div', 'field-control'); control.appendChild(buildControl(key, spec));
      if (isBlock) { row.appendChild(top); row.appendChild(control); } else { top.appendChild(control); row.appendChild(top); }
      var reasonNode = node('p', 'field-reason'); reasonNode.id = 'reason-' + key; row.appendChild(reasonNode);
      var errorNode = node('p', 'field-error'); errorNode.id = 'error-' + key; row.appendChild(errorNode); return row;
    }
    function visibleKey(key, s) {
      if (key === '@reference') return (!pendingOnly || REFERENCE_KEYS.some(function (k) { return !same(state[k], baseline[k]); })) && (!query || REFERENCE_KEYS.some(function (k) { return matches(k, schema[k] || {}, query); }) || ('聊天参考 按需工具查询 关键词预取 每轮背景 ' + s.note).indexOf(query) >= 0);
      if (!schema[key] || OMITTED_KEYS.indexOf(key) >= 0 || (pendingOnly && same(state[key], baseline[key]))) return false;
      return !query || matches(key, schema[key], query) || (s.title + ' ' + s.note).toLowerCase().indexOf(query) >= 0;
    }
    function hasFieldError(key) { return configKeys(key).some(function (k) { return own(fieldErrors, k); }); }
    function forceField(key) { return !!query || pendingOnly || !!revealedFields[key] || hasFieldError(key); }
    function conditionVisible(key) {
      if (['debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds', 'debounce_long_chars', 'debounce_end_punctuation'].indexOf(key) >= 0) return !!state.debounce_dynamic_enabled;
      if (key === 'merge_idle_seconds') return !state.debounce_dynamic_enabled;
      if (key === 'debounce_typing_hold_seconds') return !!state.debounce_private_enabled && !!state.debounce_typing_enabled;
      if (key === 'merge_localize_images_dir') return !!state.merge_localize_images;
      if (key === 'activity_prefetch_keywords') return referenceMode(state) === 'prefetch';
      if (key === 'resident_extra_tools' || key === 'proactive_silent_wake_max_steps') return !!state.proactive_silent_wake_allow_tools;
      return true;
    }
    function featureReason(s) {
      if (s.enabledKeys.length && !s.enabledKeys.some(function (key) { return !!state[key]; })) return '这项功能已关闭。参数保留，开启并保存后生效。';
      if (s.id === 'permissions' && !state.activity_buffer_enabled) return '聊天记录已关闭，会话速览暂不生效；可以先设置查看权限。';
      if (s.id === 'reference' && !state.activity_buffer_enabled) return '聊天记录已关闭，聊天参考暂不生效；可以先选择参考方式。';
      if (s.id === 'wake' && !state.proactive_enabled) return '主动消息已关闭，自醒暂不运行；可以先编辑自醒设置。';
      if (s.id === 'wake' && !state.proactive_silent_wake_daily_budget) return '每天自醒次数为 0，暂不自醒；增加次数并保存后生效。';
      return '';
    }
    function renderSection(s, group, filter, ancestors) {
      var path = (ancestors || []).concat(s.id), id = group.id + ':' + path.join(':');
      var keys = s.fields.filter(function (key) { return visibleKey(key, s); });
      var children = s.children.map(function (child) { return renderSection(child, group, filter, path); }).filter(Boolean);
      if (!keys.length && !children.length) return null;
      // Keep the function's controls as context when searching its parameters.
      if (s.feature) keys = s.fields.filter(function (key) { return key === '@reference' || (schema[key] && OMITTED_KEYS.indexOf(key) < 0); });
      var el = node(s.advanced ? 'details' : 'section', s.feature ? 'feature-card' : s.advanced ? 'section advanced-section' : 'subsection');
      el.dataset.section = id;
      var body, view = { el: el, spec: s, keys: sectionFields(s), forced: false };
      if (s.feature) {
        el.id = 'feature-' + group.id + '-' + s.id;
        var heading = node('div', 'feature-heading'), copy = node('div'), title = node('h2', 'feature-title', s.title);
        title.id = el.id + '-title'; el.setAttribute('aria-labelledby', title.id);
        if (filter) copy.appendChild(node('p', 'subsection-note', group.title));
        copy.appendChild(title);
        if (s.note) copy.appendChild(node('p', 'feature-note', s.note));
        heading.appendChild(copy);
        var pending = node('span', 'feature-pending'); pending.hidden = true; heading.appendChild(pending); el.appendChild(heading);
        var switches = node('div', 'feature-switches'); keys.forEach(function (key) { var row = field(key); if (row) switches.appendChild(row); });
        if (keys.length) el.appendChild(switches);
        var notice = node('p', 'feature-notice'); notice.hidden = true; el.appendChild(notice);
        body = node('div', 'feature-parameters'); body.id = el.id + '-parameters';
        var edit = button('编辑关闭功能的设置', function () {
          editingDisabled[id] = body.hidden;
          view.keys.forEach(function (key) { delete revealedFields[key]; });
          refresh();
        }, 'feature-edit');
        edit.id = 'edit-' + group.id + '-' + s.id; edit.setAttribute('aria-controls', body.id); edit.hidden = true;
        el.appendChild(edit); el.appendChild(body);
        featureViews.push({ spec: s, id: id, card: el, parameters: body, edit: edit, notice: notice, pending: pending, keys: view.keys });
      } else if (s.advanced) {
        el.open = !!filter || !!expanded[id];
        var summary = node('summary'), text = node('span'); text.appendChild(node('span', 'section-title', s.title));
        if (s.note) text.appendChild(node('span', 'section-description', s.note)); summary.appendChild(text); el.appendChild(summary);
        body = node('div', 'section-body'); el.appendChild(body);
        view.lastOpen = el.open;
        el.addEventListener('toggle', function () {
          // Native toggle events are queued, including events from programmatic opens.
          if (sectionViews.indexOf(view) < 0 || view.lastOpen === el.open) return;
          view.lastOpen = el.open;
          if (filter || view.forced) return;
          expanded[id] = el.open;
          try { win.localStorage.setItem('nxm_config_expanded', JSON.stringify(expanded)); } catch (_) {}
        });
      } else {
        el.appendChild(node('h3', 'subsection-title', s.title));
        if (s.note) el.appendChild(node('p', 'subsection-note', s.note));
        body = node('div', 'subsection-body'); el.appendChild(body);
      }
      if (!s.feature) keys.forEach(function (key) { var row = field(key); if (row) body.appendChild(row); });
      children.forEach(function (child) { body.appendChild(child); });
      sectionViews.push(view); return el;
    }
    function refreshVisibility(dirty) {
      doc.querySelectorAll('.field[data-key]').forEach(function (row) {
        row.hidden = !forceField(row.dataset.key) && !conditionVisible(row.dataset.key);
      });
      // Children are registered before their parents so empty subgroups disappear too.
      sectionViews.forEach(function (view) {
        if (view.spec.feature) return;
        view.el.hidden = !Array.from(view.el.querySelectorAll('.field')).some(function (row) { return !row.hidden; });
        if (view.spec.advanced) {
          var forced = view.keys.some(forceField);
          if (forced && !view.el.open) { view.el.open = true; view.lastOpen = true; }
          view.forced = forced;
        }
      });
      featureViews.forEach(function (view) {
        var disabled = view.spec.enabledKeys.length && !view.spec.enabledKeys.some(function (key) { return !!state[key]; });
        var forced = view.keys.some(forceField), show = !disabled || !!editingDisabled[view.id] || forced;
        view.parameters.hidden = !show;
        view.edit.hidden = !disabled || !view.spec.children.length || !!query || pendingOnly || view.keys.some(hasFieldError);
        view.edit.textContent = show ? '收起关闭功能的设置' : '编辑关闭功能的设置';
        view.edit.setAttribute('aria-expanded', show ? 'true' : 'false');
        // Clicking "收起" also ends a temporary reveal from a diagnostic jump.
        view.notice.textContent = featureReason(view.spec); view.notice.hidden = !view.notice.textContent;
        var count = view.keys.flatMap(configKeys).filter(function (key) { return dirty.indexOf(key) >= 0; }).length;
        view.pending.textContent = '待保存 ' + count + ' 项'; view.pending.hidden = !count;
      });
    }
    function extraSections() {
      var grouped = GROUPS.flatMap(function (group) { return group.sections.flatMap(sectionFields); }).concat(REFERENCE_KEYS, OMITTED_KEYS);
      var extras = Object.keys(schema).filter(function (key) { return grouped.indexOf(key) < 0; });
      return extras.length ? [feature('extra', '其他配置', '新增配置项仍可编辑和保存。', [], [section('advanced', '高级设置', '', extras, true)])] : [];
    }
    function summaryFor(group) {
      var s = state;
      switch (group.id) {
        case 'debounce': return [s.debounce_enabled, s.debounce_dynamic_enabled ? '按内容等待 · 完整句 ' + valueText(s.debounce_fast_idle_seconds) + ' 秒 · 短句 ' + valueText(s.debounce_slow_idle_seconds) + ' 秒' : '固定等待 ' + valueText(s.merge_idle_seconds) + ' 秒'];
        case 'human_reply': return [s.human_reply_enabled, valueText(s.human_reply_chars_per_minute) + ' 字/分钟 · ' + (s.human_reply_max_length === 0 ? '接管字数不限制' : '超过 ' + valueText(s.human_reply_max_length) + ' 字交回原流程')];
        case 'interaction': return [s.include_group || s.include_private || s.poke_reply_enabled || s.keyword_wake_enabled || parseList(s.blocked_users).length, '刷屏拦截 ' + (s.include_group || s.include_private ? '开启' : '关闭') + ' · 被戳 ' + (s.poke_reply_enabled ? '回应' : '忽略') + ' · 屏蔽 ' + parseList(s.blocked_users).length + ' 人'];
        case 'activity': return [s.activity_buffer_enabled || s.lmem_gateway_enabled, '聊天参考：' + { tools: '按需工具查询', prefetch: '关键词预取', background: '每轮背景' }[referenceMode(s)] + ' · 记录保留 ' + valueText(s.activity_retention_days) + ' 天'];
        case 'proactive': return [s.proactive_enabled, '允许 ' + parseList(s.proactive_allow_groups).length + ' 个群 · 每目标每天 ' + valueText(s.proactive_daily_limit_per_person) + ' 次'];
        case 'selfwake': return [s.proactive_enabled && s.proactive_silent_wake_enabled && s.proactive_silent_wake_daily_budget > 0, '平均间隔 ' + valueText(s.proactive_silent_wake_target_mean_minutes) + ' 分钟 · 每天最多 ' + valueText(s.proactive_silent_wake_daily_budget) + ' 次判断'];
        default: return [true, '通用工具提示词和自定义能力文案'];
      }
    }
    function renderOverviewStatus() {
      var grid = $('overview-grid'); if (!grid) return; grid.replaceChildren();
      var switches = {
        debounce: [['debounce_enabled', '合并连续消息']],
        human_reply: [['human_reply_enabled', '逐条拟人发送']],
        interaction: [['include_group', '群聊刷屏拦截'], ['include_private', '私聊刷屏拦截'], ['poke_reply_enabled', '被戳时回应'], ['keyword_wake_enabled', '关键词唤醒']],
        activity: [['activity_buffer_enabled', '记录聊天消息'], ['activity_tool_enabled', '允许速览查询'], ['lmem_gateway_enabled', '检索长期记忆']],
        proactive: [['proactive_enabled', '允许主动消息']],
        selfwake: [['proactive_silent_wake_enabled', '启用静默自醒']]
      };
      var conditions = {
        debounce: '需开启群聊或私聊场景；群消息需先唤醒机器人，名单继续限制范围。',
        human_reply: '仅处理非流式纯文本；群名单同时限制发送与请求打断。',
        interaction: '各项独立生效；关键词需有词条；屏蔽名单优先于刷屏免检名单。',
        activity: '速览查询需开启聊天记录；长期记忆检索需安装 LivingMemory。',
        proactive: '仅向允许目标发送；群名单为空时只允许主人、管理员等获准私聊目标。',
        selfwake: '需开启主动消息且日预算大于 0；静默时段内暂停。'
      };
      GROUPS.slice(1, 7).forEach(function (group) {
        var card = node('section', 'overview-card'); card.dataset.group = group.id;
        var heading = node('div', 'overview-card-heading'); heading.appendChild(node('strong', '', group.title)); heading.appendChild(node('span', 'status-chip')); card.appendChild(heading);
        card.appendChild(node('p', 'overview-summary', summaryFor(group)[1]));
        var settings = node('div', 'overview-settings');
        switches[group.id].forEach(function (entry) {
          var key = entry[0]; if (!schema[key]) return;
          var row = node('div', 'field overview-setting'); row.dataset.key = key; row.id = 'field-' + key;
          var top = node('div', 'overview-setting-top'), label = node('label', 'field-label', entry[1]); label.htmlFor = 'input-' + key;
          top.appendChild(label); top.appendChild(buildControl(key, schema[key])); row.appendChild(top);
          var why = node('p', 'field-reason'); why.id = 'reason-' + key; row.appendChild(why);
          var error = node('p', 'field-error'); error.id = 'error-' + key; row.appendChild(error); settings.appendChild(row);
        });
        card.appendChild(settings); card.appendChild(node('p', 'overview-condition', conditions[group.id]));
        card.appendChild(button('查看设置 →', function () { navigate(group.id); }, 'overview-link')); grid.appendChild(card);
      });
      grid.appendChild(button('高级：通用工具提示词和自定义能力文案 →', function () { navigate('advanced'); }, 'overview-advanced'));
      refreshOverviewIndicators();
    }
    function refreshOverviewIndicators() {
      doc.querySelectorAll('.overview-card').forEach(function (card) {
        var id = card.dataset.group, status = overviewStatus(id, state); if (!status) return;
        var chip = card.querySelector('.status-chip'); chip.textContent = status.label; chip.classList.toggle('on', status.active);
        card.querySelector('.overview-summary').textContent = summaryFor(groupOfTitle(id))[1];
      });
    }
    function renderPane() {
      var pane = $('pane'); pane.replaceChildren();
      featureViews = []; sectionViews = [];
      var filtering = !!query || pendingOnly;
      $('page-title').textContent = filtering ? pendingOnly ? '待保存的修改' : '搜索结果' : groupOfTitle(activeGroup).title;
      $('page-description').textContent = filtering ? (query ? '搜索“' + query + '”；' : '') + '显示全部分组中符合条件的配置项。' : groupOfTitle(activeGroup).note;
      if (!filtering && activeGroup === 'overview') {
        var lead = node('div', 'overview-lead'); if (schema.enabled) lead.appendChild(field('enabled')); pane.appendChild(lead);
        pane.appendChild(node('p', 'field-hint', '以下摘要展示当前草稿。待保存的修改不会立即改变运行行为。'));
        var grid = node('div', 'overview-grid'); grid.id = 'overview-grid'; pane.appendChild(grid); renderOverviewStatus(); return;
      }
      var count = 0;
      GROUPS.filter(function (group) { return filtering || group.id === activeGroup; }).forEach(function (group) {
        var sections = group.sections.concat(group.id === 'advanced' ? extraSections() : []);
        sections.forEach(function (s) { var card = renderSection(s, group, filtering); if (card) { pane.appendChild(card); count += 1; } });
      });
      if (!filtering && activeGroup === 'resident' && win.NuoxiaomiaoResidentPanel) { var panel = node('div', 'resident-panel'); pane.appendChild(panel); win.NuoxiaomiaoResidentPanel.mount(panel, win); count += 1; }
      if (!count) pane.appendChild(node('div', 'empty', pendingOnly ? '没有符合条件的待保存修改。' : '没有找到匹配项。可以搜索“私聊”“名单”“自醒”等词。'));
    }
    function groupOfTitle(id) { return GROUPS.find(function (group) { return group.id === id; }) || GROUPS[0]; }
    function navigate(id) { activeGroup = id; query = ''; pendingOnly = false; revealedFields = {}; $('search').value = ''; $('pending-only').checked = false; closeSidebar(); renderPane(); refresh(); win.scrollTo({ top: 0, behavior: 'auto' }); }
    function focusField(key) {
      var target = REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key;
      navigate(groupOf(key).id); revealedFields[target] = true; refresh();
      var row = $('field-' + target); if (!row) return;
      row.scrollIntoView({ block: 'center', behavior: 'smooth' }); var input = row.querySelector('input,textarea,select'); if (input) input.focus({ preventScroll: true });
    }
    function renderDiagnostics() {
      var box = $('diagnostics'); box.replaceChildren();
      if (!diagnostics.length) return;
      box.appendChild(node('p', 'field-hint', '运行检查基于最近一次保存的配置。修改后保存以更新检查结果。'));
      diagnostics.forEach(function (item) { var severity = item.severity === 'error' ? 'error' : item.severity === 'info' ? 'info' : 'warning'; var row = node('div', 'notice diagnostic ' + severity); row.appendChild(node('span', '', item.message || item.code || '')); var links = node('span', 'links'); (item.fields || []).filter(function (key) { return schema[key]; }).slice(0, 3).forEach(function (key) { links.appendChild(button('查看设置', function () { focusField(key); })); }); row.appendChild(links); box.appendChild(row); });
    }
    function renderErrors() {
      var box = $('error-summary'), keys = Object.keys(fieldErrors); box.replaceChildren(); box.hidden = !keys.length;
      if (keys.length) { box.appendChild(node('strong', '', '有 ' + keys.length + ' 项需要修正，尚未保存。')); keys.forEach(function (key) { box.appendChild(button(fieldTitle(schema[key], key), function () { focusField(key); })); }); }
    }
    function refresh() {
      var keys = dirtyKeys(state, baseline, schema), filtered = !!query || pendingOnly;
      $('dirty').textContent = saving ? '正在保存…' : keys.length ? '待保存 ' + keys.length + ' 项' : '没有待保存的修改';
      $('save-detail').textContent = saving ? '保存期间继续编辑的内容会保留' : keys.length ? '点击保存后生效 · Ctrl / ⌘ + S' : loaded ? '当前修改均已保存' : '配置加载后可编辑';
      $('save').disabled = !loaded || saving || !keys.length; $('save-default').disabled = !loaded || saving; $('clear-default').disabled = !loaded || saving || !userDefault;
      $('restore-default').disabled = !loaded || !userDefault || saving; $('restore-factory').disabled = !loaded || saving; $('revert').disabled = !loaded || saving || !keys.length;
      $('search-clear').hidden = !query;
      doc.querySelectorAll('.nav-button').forEach(function (tab) { var group = groupOfTitle(tab.dataset.group); tab.classList.toggle('active', !filtered && group.id === activeGroup); tab.setAttribute('aria-current', !filtered && group.id === activeGroup ? 'page' : 'false'); var count = keys.filter(function (key) { return groupOf(key).id === group.id; }).length; var badge = tab.querySelector('.nav-count'); badge.textContent = count; badge.hidden = !count; });
      doc.querySelectorAll('.field[data-key]').forEach(function (row) {
        var key = row.dataset.key, reference = key === '@reference'; var pending = reference ? REFERENCE_KEYS.some(function (k) { return keys.indexOf(k) >= 0; }) : keys.indexOf(key) >= 0;
        row.classList.toggle('is-pending', pending); var error = reference ? REFERENCE_KEYS.map(function (k) { return fieldErrors[k]; }).filter(Boolean).join('；') : fieldErrors[key]; row.classList.toggle('has-error', !!error);
        var errorNode = $('error-' + key); if (errorNode) { errorNode.textContent = Array.isArray(error) ? error.join('；') : error || ''; errorNode.hidden = !error; }
        row.querySelectorAll('input,textarea,select').forEach(function (input) { input.setAttribute('aria-invalid', error ? 'true' : 'false'); });
        var reasonNode = $('reason-' + key), why = reference ? reason('activity_prefetch_enabled') : reason(key);
        var card = row.closest('.feature-card'), featureView = featureViews.find(function (view) { return view.card === card; });
        if (featureView && featureReason(featureView.spec)) why = '';
        if (reasonNode) { reasonNode.textContent = why; reasonNode.hidden = !why; }
        var reset = row.querySelector('.restore-field'); if (reset) reset.hidden = reference ? REFERENCE_KEYS.every(function (k) { return same(state[k], defaultOf(k)); }) : same(state[key], defaultOf(key));
      });
      refreshVisibility(keys);
      var banner = $('banner'); banner.hidden = !!baseline.enabled && !!state.enabled;
      banner.textContent = !baseline.enabled ? '当前运行：插件已关闭。' + (state.enabled ? '草稿已开启，保存后生效。' : '可先编辑设置，开启总开关并保存后恢复功能。') : '草稿已关闭总开关；保存后插件功能暂停。';
      renderErrors();
      refreshOverviewIndicators();
    }
    function unwrap(response) { return response && response.data && typeof response.data === 'object' ? response.data : response || {}; }
    function updateMetadata(data) { if (Array.isArray(data.providers)) providers = data.providers; if (Array.isArray(data.tool_plugins)) catalog = data.tool_plugins; if (Array.isArray(data.resident_tools)) residentTools = data.resident_tools; if (Array.isArray(data.diagnostics)) diagnostics = data.diagnostics; if (typeof data.timezone === 'string') timezone = data.timezone; $('timezone').textContent = timezone ? '机器人时区：' + timezone : '机器人时区：未提供'; }
    async function save(asDefault) {
      if (!loaded || saving) return;
      var snapshot = clone(state), patch = buildPatch(snapshot, baseline, schema);
      if (!asDefault && !Object.keys(patch).length) { showToast('没有待保存的修改'); return; }
      fieldErrors = validateValues(snapshot, asDefault ? snapshot : patch, schema);
      if (Object.keys(fieldErrors).length) { renderPane(); refresh(); focusField(Object.keys(fieldErrors)[0]); return; }
      saving = true; refresh(); closeDefaultMenu();
      try {
        var response = await win.AstrBotPluginPage.apiPost(asDefault ? 'config/default' : 'config', { config: clone(patch), return_validation_errors: true });
        if (response && response.status === 'error') throw response;
        var data = unwrap(response);
        // The host bridge strips failed response details. This opt-in success
        // envelope carries validation errors without committing any config.
        if (data.validation_error) throw data;
        var merged = mergeSavedState(state, baseline, snapshot, patch, data.config || {}, schema);
        state = merged.state; baseline = merged.baseline; fieldErrors = {};
        if (asDefault) userDefault = data.user_default || null;
        updateMetadata(data); renderDiagnostics(); renderPane();
        var hasNewEdits = dirtyKeys(state, baseline, schema).length > 0;
        showToast(asDefault ? '已保存并设为我的默认。' + (hasNewEdits ? '保存期间的新修改仍待保存。' : '') : hasNewEdits ? '已保存提交的修改；保存期间的新修改仍待保存。' : '配置已保存并生效。');
      } catch (error) {
        fieldErrors = errorFields(error); renderPane(); renderErrors();
        var detail = error && error.message ? error.message : '请求失败，请重试';
        showToast('保存失败：' + detail); if (Object.keys(fieldErrors).length) focusField(Object.keys(fieldErrors)[0]);
      } finally { saving = false; refresh(); }
    }
    function restoreDefaults(factory) {
      if (!loaded || saving || (!factory && !userDefault)) return;
      Object.keys(schema).forEach(function (key) { state[key] = clone(defaultOf(key, factory)); });
      selectedToolMode = null; fieldErrors = {}; closeDefaultMenu(); renderPane(); refresh(); showToast('已将' + (factory ? '出厂默认' : '我的默认') + '填入草稿，保存后生效。');
    }
    async function clearDefault() {
      if (!loaded || saving || !userDefault) return;
      saving = true; refresh(); closeDefaultMenu();
      try { var response = await win.AstrBotPluginPage.apiPost('config/default', { clear: true }); if (response && response.status === 'error') throw response; userDefault = null; renderPane(); showToast('已删除我的默认快照；当前运行配置和草稿不变。'); }
      catch (error) { showToast('删除默认快照失败：' + (error.message || '请重试')); }
      finally { saving = false; refresh(); }
    }
    function closeDefaultMenu() { $('default-menu').hidden = true; $('default-menu-btn').setAttribute('aria-expanded', 'false'); }
    function closeSidebar() { $('sidebar').classList.remove('open'); $('drawer-backdrop').hidden = true; }
    function init() {
      try { var theme = win.localStorage.getItem('nxm_config_theme'); if (theme) doc.documentElement.dataset.theme = theme; expanded = JSON.parse(win.localStorage.getItem('nxm_config_expanded') || '{}') || {}; } catch (_) {}
      var nav = $('tabs'); GROUPS.forEach(function (group, index) { var tab = button('', function () { navigate(group.id); }, 'nav-button'); tab.dataset.group = group.id; tab.appendChild(node('span', 'nav-number', String(index + 1).padStart(2, '0'))); tab.appendChild(node('span', '', group.title)); var count = node('span', 'nav-count'); count.hidden = true; tab.appendChild(count); nav.appendChild(tab); });
      $('save').addEventListener('click', function () { save(false); }); $('save-default').addEventListener('click', function () { save(true); });
      $('revert').addEventListener('click', function () { if (saving) return; state = clone(baseline); fieldErrors = {}; selectedToolMode = null; renderPane(); refresh(); showToast('已撤销待保存的修改。'); });
      $('restore-default').addEventListener('click', function () { restoreDefaults(false); }); $('restore-factory').addEventListener('click', function () { restoreDefaults(true); }); $('clear-default').addEventListener('click', clearDefault);
      $('default-menu-btn').addEventListener('click', function () { var menu = $('default-menu'); menu.hidden = !menu.hidden; $('default-menu-btn').setAttribute('aria-expanded', String(!menu.hidden)); });
      doc.addEventListener('click', function (event) { if (!event.target.closest('.dropdown')) closeDefaultMenu(); });
      $('drawer-toggle').addEventListener('click', function () { $('sidebar').classList.add('open'); $('drawer-backdrop').hidden = false; }); $('drawer-close').addEventListener('click', closeSidebar); $('drawer-backdrop').addEventListener('click', closeSidebar);
      $('theme-toggle').addEventListener('click', function () { var current = doc.documentElement.dataset.theme || (win.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); var next = current === 'dark' ? 'light' : 'dark'; doc.documentElement.dataset.theme = next; try { win.localStorage.setItem('nxm_config_theme', next); } catch (_) {} });
      $('search').addEventListener('input', function () { query = $('search').value.trim().toLowerCase(); renderPane(); refresh(); }); $('search-clear').addEventListener('click', function () { $('search').value = ''; query = ''; renderPane(); refresh(); });
      $('pending-only').addEventListener('change', function () { pendingOnly = $('pending-only').checked; renderPane(); refresh(); });
      doc.addEventListener('keydown', function (event) { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(false); } if (event.key === 'Escape') { closeSidebar(); closeDefaultMenu(); } });
      win.addEventListener('beforeunload', function (event) { if (!dirtyKeys(state, baseline, schema).length) return; event.preventDefault(); event.returnValue = ''; });
    }
    async function load() {
      try {
        var response = await win.AstrBotPluginPage.apiGet('config'); if (response && response.status === 'error') throw response;
        var data = unwrap(response); schema = data.schema || {}; if (!Object.keys(schema).length) throw new Error('没有取得配置字段，请重试或重新加载插件。');
        defaults = data.defaults || {}; userDefault = data.user_default || null;
        state = {}; Object.keys(schema).forEach(function (key) { state[key] = clone(own(data.config, key) ? data.config[key] : defaults[key]); }); baseline = clone(state);
        updateMetadata(data); loaded = true; fieldErrors = {}; renderDiagnostics(); renderPane(); refresh(); $('boot').hidden = true; $('app').hidden = false;
      } catch (error) {
        $('boot').replaceChildren(); $('boot').appendChild(node('strong', '', '配置加载失败')); $('boot').appendChild(node('p', '', error.message || '无法读取配置。')); $('boot').appendChild(button('重新加载', load, 'button primary')); $('boot').hidden = false; $('app').hidden = true;
      }
    }
    async function start() {
      init();
      try {
        await new Promise(function (resolve, reject) { if (win.AstrBotPluginPage) return resolve(); var start = Date.now(); var timer = setInterval(function () { if (win.AstrBotPluginPage) { clearInterval(timer); resolve(); } else if (Date.now() - start > 6000) { clearInterval(timer); reject(new Error('请从 AstrBot 的插件页面打开配置页。')); } }, 50); });
        if (typeof win.AstrBotPluginPage.ready === 'function') await win.AstrBotPluginPage.ready(); await load();
      } catch (error) { $('boot').replaceChildren(); $('boot').appendChild(node('strong', '', '无法连接 AstrBot')); $('boot').appendChild(node('p', '', error.message || '请重新打开插件页面。')); }
    }
    start();
  }
  return { CORE_PLUGIN: CORE_PLUGIN, GROUPS: GROUPS, sectionFields: sectionFields, groupOf: groupOf, fieldTitle: fieldTitle, overviewStatus: overviewStatus, parseList: parseList, referenceMode: referenceMode, referenceValues: referenceValues, toolMode: toolMode, toolValues: toolValues, dirtyKeys: dirtyKeys, buildPatch: buildPatch, mergeSavedState: mergeSavedState, validateValues: validateValues, errorFields: errorFields, matches: matches, mount: mount };
});
