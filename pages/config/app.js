/* 糯小喵配置中心 — 极简版（schema 驱动，草稿/保存/基线状态机）。
 * 设计约束：
 *  - 纯静态文件，无构建步骤、无外部依赖，由 AstrBot 直接服务。
 *  - 通过 window.AstrBotPluginPage 桥接（apiGet/apiPost/ready）。
 *  - 导出纯函数供 tests/test_config_ui.js 使用（UMD）。
 *  - 视觉极简、功能收敛：分组导航 + 字段卡片流 + 搜索 + 待保存 + 保存/默认快照。
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
  var OMITTED_KEYS = ['proactive_min_delay_minutes', 'proactive_silent_wake_tool_plugins', 'resident_tools_migrated'];
  var TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
  var FIELD_TITLES = {
    debounce_enabled: '启用消息合并',
    debounce_group_enabled: '合并群聊中的连续消息',
    debounce_private_enabled: '合并私聊中的连续消息',
    debounce_dynamic_enabled: '按消息长短决定等多久',
    debounce_typing_enabled: '对方还在输入时继续等待',
    debounce_fast_idle_seconds: '一句话说完后等多久（秒）',
    debounce_slow_idle_seconds: '话没说完时等多久（秒）',
    debounce_wake_idle_seconds: '只叫了名字时多等多久（秒）',
    debounce_typing_hold_seconds: '收到「正在输入」后多等多久（秒）',
    debounce_long_chars: '多少字就算一句话说完',
    debounce_end_punctuation: '哪些标点算一句话说完',
    debounce_max_parts: '一批最多合并几句',
    merge_idle_seconds: '固定等待时长（秒）',
    merge_max_total_seconds: '一批消息最长等多久（秒）',
    merge_join_separator: '合并时多句话之间用什么隔开',
    merge_localize_images: '图片先存到本地再合并',
    merge_localize_images_dir: '合并图片的保存目录',
    human_reply_enabled: '启用分条回复',
    human_reply_chars_per_minute: '打字速度（字/分钟）',
    human_reply_interrupt_enabled: '用户插话时中止未开口的回复',
    human_reply_show_input_status: '打字时显示「正在输入」',
    human_reply_max_length: '超过多少字时不分条发送',
    human_reply_max_wait_seconds: '每条回复最多等多久（秒）',
    trigger_rounds: '连续对话几轮后拦截',
    window_seconds: '多久没新触发就重新计数（秒）',
    include_group: '群聊刷屏拦截',
    include_private: '私聊刷屏拦截',
    poke_reply_enabled: '被戳时回应',
    poke_reply_prompt: '被戳时的回应提示词',
    poke_reply_cooldown_seconds: '群聊被戳后冷却（秒）',
    blocked_users: '完全屏蔽这些用户',
    user_whitelist: '这些用户免于刷屏拦截',
    user_blacklist: '只检测这些用户（留空检测所有人）',
    meme_tags: '刷屏时回的表情分类',
    meme_cooldown_seconds: '两次表情间隔（秒）',
    keyword_wake_enabled: '关键词唤醒',
    keyword_wake_list: '哪些词会唤醒机器人',
    activity_buffer_enabled: '记录聊天消息',
    activity_record_private: '也记录私聊消息',
    activity_record_bot_replies: '也记录机器人自己的回复',
    activity_buffer_max_text_chars: '单条记录最多记多少字',
    activity_retention_days: '聊天记录保留几天',
    activity_require_admin: '仅主人/管理员能翻记录',
    activity_tool_enabled: '允许模型查询聊天记录',
    activity_hint_enabled: '提醒机器人可以翻记录',
    activity_prefetch_enabled: '提到别处聊天时自动带近况',
    activity_prefetch_keywords: '哪些话会触发自动带近况',
    capability_context_digest: '每轮回复都附带其他群近况',
    lmem_gateway_enabled: '启用长期记忆检索',
    lmem_gateway_keywords: '哪些话会触发翻长期记忆',
    lmem_gateway_top_k: '一次最多取几条记忆',
    lmem_gateway_timeout_seconds: '记忆检索超时（秒）',
    lmem_gateway_max_chars: '记忆最多带多少字',
    lmem_gateway_plugin_name: '记忆插件名（一般不用改）',
    lmem_gateway_recall_tool_name: '记忆检索工具名（一般不用改）',
    proactive_enabled: '允许机器人主动发消息',
    proactive_hint_enabled: '提醒机器人可以主动找人',
    proactive_allow_groups: '允许发主动消息的群',
    proactive_target_whitelist: '允许被主动私聊的人',
    proactive_max_pending_per_person: '同一目标最多排几条待发',
    proactive_daily_limit_per_person: '同一目标每天最多发几次',
    proactive_cooldown_minutes: '两次主动消息间隔（分钟）',
    proactive_context_other_sessions: '发消息前参考其他群近况',
    proactive_context_other_private: '参考时是否包含私聊',
    proactive_quiet_hours_start: '静默时段开始',
    proactive_quiet_hours_end: '静默时段结束',
    proactive_silent_wake_enabled: '启用静默自醒',
    proactive_judge_provider_id: '自醒用的模型',
    proactive_silent_wake_target_mean_minutes: '平均多久醒来（分钟）',
    proactive_silent_wake_floor_minutes: '最早多久醒来（分钟）',
    proactive_silent_wake_ceil_minutes: '最晚多久醒来（分钟）',
    proactive_silent_wake_daily_budget: '每天最多自醒几次',
    proactive_silent_wake_max_steps: '每次自醒最多执行几步',
    proactive_silent_wake_allow_tools: '自醒时允许调用工具',
    resident_run_token_limit: '单轮 token 上限（0=不限）',
    resident_daily_token_limit: '每日 token 上限（0=不限）',
    resident_extra_tools: '自醒可以使用的额外工具',
    tool_call_prompt_enabled: '启用自定义工具提示词',
    tool_call_prompt: '工具调用提示词内容',
    capability_hint_text: '能力提示文案（留空用内置）',
    force_wake_prefix: '合成消息唤醒前缀（一般留空）',
    activity_db_path: '聊天记录库位置',
    shared_memory_enabled: '启用全局主脑共享记忆',
    master_umo: '主脑会话编号',
    master_auto_bind: '首次管理员私聊自动绑定主脑',
    unread_inbox_enabled: '启用未读信箱分流',
    inbox_group_whitelist: '启用信箱的群（白名单）',
    inbox_group_blacklist: '不启用信箱的群（黑名单）',
    mention_notice_enabled: '启用点名通知卡',
    mention_notice_limit: '通知卡最多带几条未读',
    topic_summary_enabled: '启用话题概括',
    topic_light_provider_id: '话题概括用的模型',
    topic_min_unread: '至少几条未读才概括',
    topic_cooldown_seconds: '两次概括间隔（秒）',
    topic_max_chars: '话题一句话最多多少字',
    topic_line_max_chars: '喂给模型的消息截断字数',
    topic_timeout_seconds: '概括超时（秒）',
    topic_segment_msg_count: '攒几条未读算一段',
    topic_segment_backfill_max: '概括时补几段旧消息',
    vision_enabled: '启用群聊识图',
    vision_provider_ids: '识图用哪些模型（按顺序回退）',
    image_keep_days: '图片保留几天',
    vision_image_max: '一条消息最多留底几张',
    vision_brief_max_chars: '自动描述最长字数',
    vision_read_max_chars: '回答关于图的问题最长字数',
    vision_timeout_seconds: '识图超时（秒）',
    vision_cache_ttl_days: '描述缓存保留天数',
    resident_inbox_enabled: '启用未读信箱（未读概况）',
    resident_state_enabled: '启用自主状态/心情/风格'
  };

  /* ============================ utils ============================ */
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function fieldTitle(spec, key) { return FIELD_TITLES[key] || String((spec || {}).description || key).replace(/^【[^】]+】\s*/, ''); }
  function parseList(value) {
    var list = Array.isArray(value) ? value : String(value || '').split(/[\n,，、;；]+/);
    return Array.from(new Set(list.map(function (x) { return String(x).trim(); }).filter(Boolean)));
  }
  function valueText(value) {
    return value === '' ? '留空'
      : Array.isArray(value) ? value.length ? value.join('、') : '空名单'
      : value === true ? '开启' : value === false ? '关闭' : String(value);
  }

  /* ============================ 引用/工具模式（纯函数） ============================ */
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

  /* ============================ 草稿状态机（纯函数） ============================ */
  function dirtyKeys(state, baseline, schema) {
    return Object.keys(schema).filter(function (key) { return OMITTED_KEYS.indexOf(key) < 0 && !same(state[key], baseline[key]); });
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

  /* ============================ 导入/导出/diff（纯函数，无 UI） ============================ */
  function exportConfig(state, schema) {
    var out = {};
    Object.keys(schema).forEach(function (key) {
      if (own(state, key)) out[key] = clone(state[key]);
    });
    return JSON.stringify(out, null, 2);
  }
  function importConfig(text, schema, defaults) {
    var errors = {}, values = {};
    var parsed;
    try {
      parsed = JSON.parse(String(text || ''));
    } catch (exc) {
      return { values: {}, errors: { _global: 'JSON 无法解析：' + exc.message } };
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { values: {}, errors: { _global: '需要一段 JSON 对象（以 { 开头）。' } };
    }
    Object.keys(parsed).forEach(function (key) {
      if (!own(schema, key)) return;
      var spec = schema[key], raw = parsed[key];
      if (spec.type === 'bool') {
        if (raw === true || raw === 'true' || raw === 1 || raw === '1' || raw === 'on') values[key] = true;
        else if (raw === false || raw === 'false' || raw === 0 || raw === '0' || raw === 'off') values[key] = false;
        else errors[key] = '需要 true 或 false。';
      } else if (spec.type === 'int' || spec.type === 'float') {
        var num = Number(raw);
        if (raw !== '' && raw !== null && typeof raw !== 'boolean' && Number.isFinite(num)) {
          values[key] = spec.type === 'int' ? Math.round(num) : num;
          if (spec.slider && (num < spec.slider.min || num > spec.slider.max)) errors[key] = '应在 ' + spec.slider.min + ' 到 ' + spec.slider.max + ' 之间。';
        } else if (raw === '' || raw === null) {
          values[key] = own(defaults, key) ? clone(defaults[key]) : (spec.type === 'int' ? 0 : 0);
        } else errors[key] = '需要数字。';
      } else if (spec.type === 'list') {
        values[key] = parseList(Array.isArray(raw) ? raw : String(raw));
      } else if (spec.type === 'string' || spec.type === 'text') {
        values[key] = raw === null || raw === undefined ? '' : String(raw);
      } else if (spec.type === 'time') {
        var t = String(raw);
        values[key] = TIME_RE.test(t) ? t : errors[key] = '需要 00:00–23:59 的时间。';
      } else {
        values[key] = clone(raw);
      }
    });
    return { values: values, errors: errors };
  }
  function buildDiffList(state, baseline, schema) {
    return dirtyKeys(state, baseline, schema).map(function (key) {
      var spec = schema[key] || {}, oldValue = clone(baseline[key]), newValue = clone(state[key]);
      return {
        key: key,
        title: fieldTitle(spec, key),
        old: valueText(oldValue),
        new: valueText(newValue),
        listChanged: spec.type === 'list' && !same(oldValue, newValue)
      };
    });
  }

  /* ============================ 分组定义 ============================ */
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
    { id: 'debounce', title: '消息合并', note: '把同一人连着发的好几条消息拼成一条再回复，避免来回打断、省 token。适合群里爱连发短句的人。', sections: [
      feature('merge', '合并连续消息', '只合并「叫了机器人」的消息：比如连着发「小喵」「在吗」会拼成一条再回；发指令（/开头）不合并、立即执行。', ['debounce_enabled'], [
        section('scenes', '在哪些聊天里启用', '群聊和私聊可以分开开关。', ['debounce_group_enabled', 'debounce_private_enabled']),
        section('waiting', '等多久再回复', '话已经说完的等短一点；话还没说完（短句）多等一会；只叫了名字（没说事）再等等，方便你接下一句。', ['debounce_dynamic_enabled', 'debounce_fast_idle_seconds', 'debounce_slow_idle_seconds', 'debounce_wake_idle_seconds', 'merge_idle_seconds']),
        section('typing', '对方还在输入时', '对方显示「正在输入」就继续等，把没打完的话一起收进来。仅私聊平台支持。', ['debounce_typing_enabled', 'debounce_typing_hold_seconds']),
        section('advanced', '高级设置', '限制范围、判断规则和合并细节。', [], true, [
          section('scope', '只对哪些人和群生效', '名单留空表示不限制；群排除名单优先。', ['debounce_user_whitelist', 'debounce_group_whitelist', 'debounce_group_blacklist']),
          section('rules', '怎样判断「话已说完」', '达到字数、以句号叹号等标点结尾，就算一句说完了。', ['debounce_long_chars', 'debounce_end_punctuation', 'force_wake_prefix']),
          section('limits', '合并数量与最长等待', '攒到上限或等满时间就立刻回复，避免拖太久。', ['debounce_max_parts', 'merge_max_total_seconds', 'merge_join_separator']),
          section('images', '合并图片', '合并含图片的消息时先存到本地，防止等待期间图裂。', ['merge_localize_images', 'merge_localize_images_dir'])
        ])
      ], ['debounce_enabled'])
    ] },
    { id: 'human_reply', title: '回复节奏', note: '让机器人像真人打字一样，一条一条慢慢发，而不是一次甩一大段。纯文字回复才生效。', sections: [
      feature('reply', '按打字速度分条回复', '只对纯文字回复生效；图片、转发、流式回复不受影响。想关掉就直接关上面的开关。', ['human_reply_enabled'], [
        section('speed', '打字速度', '默认 240 字/分钟；想更像真人慢速输入可以调到 60～90。', ['human_reply_chars_per_minute']),
        section('interrupt', '用户插话时', '第一条还没发出前插话 → 打断重来；已经发出第一条 → 这轮说完，新消息排到下一轮。', ['human_reply_interrupt_enabled']),
        section('typing', '向对方显示输入状态', '打字期间让对方看到「正在输入…」。仅私聊平台支持，失败不影响回复。', ['human_reply_show_input_status']),
        section('advanced', '高级设置', '发送限制和生效群范围。', [], true, [
          section('limits', '长度与等待限制', '太长的回复不分条（直接一次发），每条气泡也有最长等待。', ['human_reply_max_length', 'human_reply_max_wait_seconds']),
          section('scope', '在哪些群生效', '名单留空表示所有群；排除名单优先。私聊不受群名单影响。', ['human_reply_group_whitelist', 'human_reply_group_blacklist'])
        ])
      ], ['human_reply_enabled'])
    ] },
    { id: 'interaction', title: '拦截与互动', note: '管三件事，互不干扰：① 连续刷屏会被拦下；② 名单里的人被完全屏蔽；③ 被戳、被提关键词时机器人会回应。', sections: [
      feature('guard', '刷屏拦截', '防同一个人连续刷屏：短时间连着触发 N 轮就拦下这条，并（可选）回个表情。只统计「对机器人说话」，普通聊天不影响。', ['include_group', 'include_private'], [
        section('rules', '什么时候拦截', '连续对话几轮 + 窗口内没新动静就重新计数。', ['trigger_rounds', 'window_seconds']),
        section('advanced', '高级设置', '检测名单、生效群和表情回应。', [], true, [
          section('users', '检测哪些用户', '默认检测所有人；也可以只检测名单里的用户，或让某些人豁免。', ['user_whitelist', 'user_blacklist']),
          section('scope', '在哪些群拦截', '名单留空表示所有群；排除名单优先。', ['guard_group_whitelist', 'guard_group_blacklist']),
          section('response', '拦截后发送的表情', '需要安装 meme_manager 插件。', ['meme_tags', 'meme_cooldown_seconds'])
        ])
      ], ['include_group', 'include_private']),
      feature('blocked', '屏蔽用户', '名单里的人发什么（包括指令）都不处理。优先级最高，被屏蔽的人不参与刷屏检测。', ['blocked_users']),
      feature('poke', '被戳回应', '有人戳机器人时，让它自然回一句（比如「别戳啦，喵！」）。群聊里隔一段时间才回应一次，私聊不限。', ['poke_reply_enabled'], [
        section('cooldown', '多久可以再回应', '', ['poke_reply_cooldown_seconds']),
        section('advanced', '高级设置', '回应提示词和生效群范围。', [], true, [
          section('prompt', '让模型怎么回应', '{username} 会自动替换成戳你的人的名字。', ['poke_reply_prompt']),
          section('scope', '在哪些群回应', '', ['poke_group_whitelist', 'poke_group_blacklist'])
        ])
      ], ['poke_reply_enabled']),
      feature('keyword', '关键词唤醒', '群里没人 @ 机器人时，只要提到你设的词（比如「小喵」），机器人也会回应。适合不想打 @ 的场景。', ['keyword_wake_enabled'], [
        section('words', '哪些词会唤醒', '消息里出现任一关键词就会回应，如：小喵、江楠、楠酱。', ['keyword_wake_list']),
        section('advanced', '高级设置', '限制生效群范围。', ['keyword_wake_group_whitelist', 'keyword_wake_group_blacklist'], true)
      ], ['keyword_wake_enabled'])
    ] },
    { id: 'activity', title: '聊天记录与参考', note: '把最近的聊天存下来，让机器人能回忆、翻别的群/私聊，并按需要参考聊天或长期记忆。', sections: [
      feature('recording', '聊天记录', '把最近的聊天存到本插件记录库，供回忆和跨会话查看。关闭后停止记录但不清空已存内容。', ['activity_buffer_enabled'], [
        section('sources', '记录哪些消息', '群聊默认记录；私聊和机器人自己的回复可以单独开关。', ['activity_record_private', 'activity_record_bot_replies', 'resident_inbox_enabled']),
        section('retention', '保留多久', '默认保留 7 天，超期自动清理。', ['activity_retention_days']),
        section('advanced', '高级设置', '记录群范围、单条长度和保存位置。', [], true, [
          section('scope', '记录哪些群', '名单改变不会删除已经保存的记录。', ['activity_group_whitelist', 'activity_group_blacklist']),
          section('storage', '记录长度与保存位置', '数据库路径留空使用插件默认位置。', ['activity_buffer_max_text_chars', 'activity_db_path'])
        ])
      ], ['activity_buffer_enabled']),
      feature('permissions', '查看权限', '决定谁能让机器人翻聊天记录：默认只有主人/管理员能查别的群和私聊。', ['activity_tool_enabled'], [
        section('access', '谁可以查看其它会话', '', ['activity_require_admin']),
        section('hint', '提醒模型可以查询', '', ['activity_hint_enabled'])
      ], ['activity_tool_enabled']),
      feature('reference', '聊天参考', '选一种方式让机器人参考「其他群/私聊最近聊了什么」：① 需要时自己查；② 你提到别处时自动带；③ 每轮都带一份。', ['@reference'], [
        section('keywords', '什么时候自动参考', '在「关键词预取」模式下生效：你问别的群/翻记录时，自动附带近期记录。', ['activity_prefetch_keywords'])
      ]),
      feature('memory', '长期记忆', '配合长期记忆插件：当你问「上次我们说好的事」这类话时，机器人去翻长期记忆。没装插件就自动跳过。', ['lmem_gateway_enabled'], [
        section('keywords', '什么时候查记忆', '', ['lmem_gateway_keywords']),
        section('advanced', '高级设置', '检索数量、等待时间和兼容参数。', [], true, [
          section('limits', '检索限制', '', ['lmem_gateway_top_k', 'lmem_gateway_timeout_seconds', 'lmem_gateway_max_chars']),
          section('integration', 'LivingMemory 连接设置', '通常保持默认即可。', ['lmem_gateway_plugin_name', 'lmem_gateway_recall_tool_name'])
        ])
      ], ['lmem_gateway_enabled'])
    ] },
    { id: 'proactive', title: '主动消息', note: '允许机器人主动找人说话（比如你让它「明天提醒我」，或它想主动问候你）。只在允许名单内生效，有次数和冷却限制。', sections: [
      feature('send', '主动联系', '机器人通过「发消息」工具主动联系允许的目标；深夜静默时段内发出的会顺延到时段结束再发。', ['proactive_enabled'], [
        section('targets', '可以联系谁', '允许的群留空 = 不向任何群主动发；主人和管理员始终允许被主动私聊。', ['proactive_allow_groups', 'proactive_target_whitelist']),
        section('frequency', '多久联系一次', '同一目标每天最多几次、两次之间至少隔多久。', ['proactive_daily_limit_per_person', 'proactive_cooldown_minutes']),
        section('quiet', '什么时候保持安静', '开始与结束相同 = 不设静默时段。按页面显示的机器人时区运行。', ['proactive_quiet_hours_start', 'proactive_quiet_hours_end']),
        section('advanced', '高级设置', '待发数量、模型提醒和生成时参考的会话。', [], true, [
          section('pending', '最多积攒多少待发消息', '静默时段内同一目标最多排队几条，超出拒绝新增。', ['proactive_max_pending_per_person']),
          section('context', '生成时参考哪些会话', '发消息前参考其他群/私聊的近况，话说得更自然；也会影响自醒。', ['proactive_context_other_sessions', 'proactive_context_other_private']),
          section('hint', '提醒模型可以主动联系', '', ['proactive_hint_enabled'])
        ])
      ], ['proactive_enabled'])
    ] },
    { id: 'selfwake', title: '自醒', note: '群里没人说话时，机器人会自己醒来想想：要不要主动问候、查点什么、整理下状态。像「偶尔关心你一下」，每天有次数上限。', sections: [
      feature('wake', '安静时自己醒来', '安静一段时间后醒来做一轮完整思考（可查记录、想说话就发）。有人说话就重新计时；静默时段内不醒。', ['proactive_silent_wake_enabled'], [
        section('model', '使用哪个模型', '留空用全局默认模型；指定模型不可用时本轮失败，不自动换。', ['proactive_judge_provider_id']),
        section('schedule', '多久醒来、每天几次', '决定安静多久后醒来的概率，不是固定闹钟；自醒次数与主动消息发送次数分别限制。', ['proactive_silent_wake_target_mean_minutes', 'proactive_silent_wake_daily_budget']),
        section('tools', '醒来后可以做什么', '允许调用工具时能做更多事（查记录、发消息等）；关闭则只做简短判断。', ['proactive_silent_wake_allow_tools', 'resident_extra_tools']),
        section('advanced', '高级设置', '间隔边界、执行步骤和模型用量限制。', [], true, [
          section('intervals', '醒来间隔的上下限', '上下限至少相差 2 分钟，平均间隔距离两端至少 1 分钟。', ['proactive_silent_wake_floor_minutes', 'proactive_silent_wake_ceil_minutes']),
          section('steps', '最多执行几步', '单轮最多走几步 = 单次成本上限。调小省钱但能做的事变少。', ['proactive_silent_wake_max_steps']),
          section('tokens', '模型用量限制', '0 = 不额外限制。', ['resident_run_token_limit', 'resident_daily_token_limit'])
        ])
      ], ['proactive_silent_wake_enabled'])
    ] },
    { id: 'advanced', title: '高级', note: '通用工具提示词与能力说明，通常保持默认即可；各功能的详细参数在对应功能内设置。', sections: [
      feature('prompts', '自定义工具提示词', '给机器人的工具调用补充通用提示，一般不用改。', ['tool_call_prompt_enabled'], [
        section('advanced', '高级设置', '发送给模型的通用工具提示。', ['tool_call_prompt'], true)
      ], ['tool_call_prompt_enabled']),
      feature('capability', '自定义能力说明', '自定义文案会覆盖内置提示，请与实际功能开关保持一致。', [], [
        section('advanced', '高级设置', '留空使用内置能力说明。', ['capability_hint_text'], true)
      ])
    ] }
  ];
  GROUPS.push({ id: 'resident', title: '自主运行与状态', note: '看机器人自主运行的真实记录（调过什么工具、发过什么话、有没有失败），以及心情/状态、表达风格提案。这里的操作即时生效；页面顶部的开关仍要保存才生效。', sections: [feature('state', '自主状态', '心情、关注点和风格提案都从这里看和管理。', ['resident_state_enabled'])] });
  GROUPS.push({ id: 'shared_memory', title: '共享记忆与信箱', note: '四项进阶能力，全部默认关闭：① 主脑=让所有群/私聊共享同一段记忆；② 信箱=群聊没被点名时不打扰、消息先收起来；③ 话题概括=把一段聊天压成一句话；④ 群聊识图=记住群里的图并能回答关于图的问题。', sections: [
    feature('master', '全局主脑共享记忆', '让机器人跨群、跨私聊记得同一件事：在 A 群说的事，B 群里也能接上。适合想要「同一个猫」的感觉；多群同时聊天时会排队。默认关闭。', ['shared_memory_enabled'], [
      section('target', '主脑会话', '显式填写优先；留空且开启自动绑定时，你与管理员第一次私聊会自动绑定。', ['master_umo', 'master_auto_bind'])
    ], ['shared_memory_enabled']),
    feature('inbox', '未读信箱分流', '群里没 @ 你、没提关键词的普通消息，机器人先不回复、收进「未读信箱」；等有人点名或提关键词再一次性回应。省得每句话都搭话。默认关闭。', ['unread_inbox_enabled'], [
      section('scope', '在哪些群启用', '名单留空表示不限制；群排除名单优先。', ['inbox_group_whitelist', 'inbox_group_blacklist'])
    ], ['unread_inbox_enabled']),
    feature('notice', '点名通知卡', '有人点名你时，机器人在回复里带一句「你不在时有 N 条未读、最近在聊 XX」，让它知道前面错过了什么。默认关闭。', ['mention_notice_enabled'], [
      section('detail', '通知卡内容', '', ['mention_notice_limit'])
    ], ['mention_notice_enabled']),
    feature('topic', '话题概括', '用便宜的模型把一段没看的聊天总结成一句话（比如「在聊怎么养猫」），机器人回看时一目了然。默认关闭。', ['topic_summary_enabled'], [
      section('model', '使用哪个模型', '留空使用会话默认模型。', ['topic_light_provider_id']),
      section('trigger', '什么时候概括', '未读够多才概括，且两次之间要有间隔，避免高频群反复烧模型。', ['topic_min_unread', 'topic_cooldown_seconds']),
      section('advanced', '高级设置', '话题长度、分段与超时。', [], true, [
        section('length', '话题长度', '', ['topic_max_chars', 'topic_line_max_chars']),
        section('segments', '分段与超时', '', ['topic_segment_msg_count', 'topic_segment_backfill_max', 'topic_timeout_seconds'])
      ])
    ], ['topic_summary_enabled']),
    feature('vision', '群聊识图', '群友发的图片会被存下来并自动转成文字描述，之后你问「刚才那张图里是什么」，机器人能回答。图片默认保留 3 天。默认关闭。', ['vision_enabled'], [
      section('model', '使用哪些模型', '每行一个模型 ID，按顺序回退；留空使用 AstrBot 默认识图模型。', ['vision_provider_ids']),
      section('retention', '留底与缓存', '', ['image_keep_days', 'vision_cache_ttl_days']),
      section('advanced', '高级设置', '留底张数与描述长度。', [], true, [
        section('limits', '数量与长度', '', ['vision_image_max', 'vision_brief_max_chars', 'vision_read_max_chars']),
        section('timeout', '超时', '', ['vision_timeout_seconds'])
      ])
    ], ['vision_enabled'])
  ] });

  /* ============================ 字段归属 / 搜索 ============================ */
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

  /* ============================ 页面应用 ============================ */
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
    function showToast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(function () { $('toast').hidden = true; }, 5000); }
    function defaultOf(key, factory) { return !factory && userDefault && own(userDefault, key) ? userDefault[key] : defaults[key]; }

    function reason(key) {
      if (key === 'enabled') return '';
      if (!state.enabled) return '';
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
      if (/^master_/.test(key) && key !== 'shared_memory_enabled' && !state.shared_memory_enabled) return '全局主脑共享记忆关闭；可以先编辑，开启后生效。';
      if (/^inbox_group_/.test(key) && !state.unread_inbox_enabled) return '未读信箱分流关闭，群名单暂不生效。';
      if (key === 'mention_notice_limit' && !state.mention_notice_enabled) return '点名通知卡关闭，这项暂不生效。';
      if (/^topic_/.test(key) && key !== 'topic_summary_enabled' && !state.topic_summary_enabled) return '话题概括关闭；可以先编辑，开启后生效。';
      if (/^vision_|^image_keep_days$/.test(key) && key !== 'vision_enabled' && !state.vision_enabled) return '群聊识图关闭；可以先编辑，开启后生效。';
      return '';
    }

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

    /* ---------- 控件构建 ---------- */
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

    /* ---------- 渲染 ---------- */
    function renderSection(s, group, filter, ancestors) {
      var path = (ancestors || []).concat(s.id), id = group.id + ':' + path.join(':');
      var keys = s.fields.filter(function (key) { return visibleKey(key, s); });
      var children = s.children.map(function (child) { return renderSection(child, group, filter, path); }).filter(Boolean);
      if (!keys.length && !children.length) return null;
      if (s.feature) keys = s.fields.filter(function (key) { return key === '@reference' || (schema[key] && OMITTED_KEYS.indexOf(key) < 0); });
      var el = node(s.advanced ? 'details' : 'section', s.feature ? 'feature-card' : s.advanced ? 'section advanced-section' : 'subsection');
      el.dataset.section = id;
      var body, view = { el: el, spec: s, keys: sectionFields(s), forced: false };
      if (s.feature) {
        el.id = 'feature-' + group.id + '-' + s.id;
        var heading = node('div', 'feature-heading'), copyEl = node('div'), title = node('h2', 'feature-title', s.title);
        title.id = el.id + '-title'; el.setAttribute('aria-labelledby', title.id);
        if (filter) copyEl.appendChild(node('p', 'subsection-note', group.title + ' › ' + s.title));
        copyEl.appendChild(title);
        if (s.note) copyEl.appendChild(node('p', 'feature-note', s.note));
        heading.appendChild(copyEl);
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
        debounce: '需开启群聊或私聊场景；群消息要先「叫了机器人」才会合并，发指令（/开头）不合并。',
        human_reply: '只对纯文字回复生效；图片、转发、流式回复不受影响。',
        interaction: '三件事互不干扰：刷屏拦截管连刷、屏蔽名单管拉黑、被戳和关键词管回应。屏蔽名单优先，被屏蔽的人发什么都不回。',
        activity: '机器人要能翻记录，需先开启「聊天记录」；要翻长期记忆，需安装 LivingMemory 插件。',
        proactive: '只发给允许名单里的目标；群名单为空时只允许主人/管理员等获准私聊的人。',
        selfwake: '需同时开启「主动消息」且每日自醒次数大于 0；静默时段内不自醒。'
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

    /* ---------- 保存流程 ---------- */
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

    /* ---------- 初始化 ---------- */
    function wire(id, type, fn) { var el = $(id); if (el) el.addEventListener(type, fn); }
    function closeDefaultMenu() { $('default-menu').hidden = true; $('default-menu-btn').setAttribute('aria-expanded', 'false'); }
    function closeSidebar() { $('sidebar').classList.remove('open'); $('drawer-backdrop').hidden = true; }
    function init() {
      try { var theme = win.localStorage.getItem('nxm_config_theme'); if (theme) doc.documentElement.dataset.theme = theme; expanded = JSON.parse(win.localStorage.getItem('nxm_config_expanded') || '{}') || {}; } catch (_) {}
      var nav = $('tabs'); GROUPS.forEach(function (group, index) {
        var tab = button('', function () { navigate(group.id); }, 'nav-button');
        tab.dataset.group = group.id;
        tab.appendChild(node('span', 'nav-number', String(index + 1).padStart(2, '0')));
        tab.appendChild(node('span', 'nav-title', group.title));
        var count = node('span', 'nav-count'); count.hidden = true; tab.appendChild(count);
        nav.appendChild(tab);
      });
      wire('save', 'click', function () { save(false); });
      wire('save-default', 'click', function () { save(true); });
      wire('revert', 'click', function () { if (saving) return; state = clone(baseline); fieldErrors = {}; selectedToolMode = null; renderPane(); refresh(); showToast('已撤销待保存的修改。'); });
      wire('restore-default', 'click', function () { restoreDefaults(false); });
      wire('restore-factory', 'click', function () { restoreDefaults(true); });
      wire('clear-default', 'click', clearDefault);
      wire('default-menu-btn', 'click', function () { var menu = $('default-menu'); menu.hidden = !menu.hidden; $('default-menu-btn').setAttribute('aria-expanded', String(!menu.hidden)); });
      doc.addEventListener('click', function (event) { if (!event.target.closest('.dropdown')) closeDefaultMenu(); });
      wire('drawer-toggle', 'click', function () { $('sidebar').classList.add('open'); $('drawer-backdrop').hidden = false; });
      wire('drawer-close', 'click', closeSidebar);
      wire('drawer-backdrop', 'click', closeSidebar);
      wire('theme-toggle', 'click', function () { var current = doc.documentElement.dataset.theme || (win.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); var next = current === 'dark' ? 'light' : 'dark'; doc.documentElement.dataset.theme = next; try { win.localStorage.setItem('nxm_config_theme', next); } catch (_) {} });
      wire('search', 'input', function () { query = $('search').value.trim().toLowerCase(); renderPane(); refresh(); });
      wire('search-clear', 'click', function () { $('search').value = ''; query = ''; renderPane(); refresh(); });
      wire('pending-only', 'change', function () { pendingOnly = $('pending-only').checked; renderPane(); refresh(); });
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
        $('boot').replaceChildren(); $('boot').appendChild(node('strong', '', '配置加载失败')); $('boot').appendChild(node('p', '', error.message || '无法读取配置。')); $('boot').appendChild(button('重新加载', load, 'btn primary')); $('boot').hidden = false; $('app').hidden = true;
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

  return {
    CORE_PLUGIN: CORE_PLUGIN, GROUPS: GROUPS, sectionFields: sectionFields, groupOf: groupOf, fieldTitle: fieldTitle,
    overviewStatus: overviewStatus, parseList: parseList, referenceMode: referenceMode, referenceValues: referenceValues,
    toolMode: toolMode, toolValues: toolValues, dirtyKeys: dirtyKeys, buildPatch: buildPatch, mergeSavedState: mergeSavedState,
    validateValues: validateValues, errorFields: errorFields, matches: matches,
    exportConfig: exportConfig, importConfig: importConfig, buildDiffList: buildDiffList,
    mount: mount
  };
});
