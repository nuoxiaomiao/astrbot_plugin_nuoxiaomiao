/* 糯小喵 · 自主运行与状态面板（从零重做，极简样式）。
 * 保持与宿主一致的桥接契约（win.AstrBotPluginPage.apiGet/apiPost）。
 * 全部文本用 textContent 写入，防止 XSS。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.NuoxiaomiaoResidentPanel = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  var labels = {
    pending: '等待开始', running: '运行中', paused: '已暂停', stopped: '已停止',
    succeeded: '已完成', failed: '失败', unknown: '结果待核实', interrupted: '已中断',
    skipped: '已跳过', limited: '达到限制', sent: '发送成功', sending: '发送中',
    spoke: '已发言', quiet: '安静结束', approved: '已批准', rejected: '已拒绝',
    ready: '可用', unavailable: '不可用', out_of_scope: '当前范围不可用'
  };
  function unwrap(value) { return value && value.data ? value.data : value || {}; }

  function mount(root, win) {
    var doc = win.document, data = null, umo = '', before = 0, sequence = 0, busy = false;

    function node(tag, text, cls) {
      var n = doc.createElement(tag);
      if (text !== undefined) n.textContent = String(text);
      if (cls) n.className = cls;
      return n;
    }
    function button(text, fn) {
      var n = node('button', text, 'btn');
      n.type = 'button';
      n.addEventListener('click', fn);
      return n;
    }
    function input(title, value, textarea) {
      var label = node('label', title, 'field-inline');
      var el = node(textarea ? 'textarea' : 'input');
      el.value = value == null ? '' : value;
      el.setAttribute('aria-label', title);
      if (textarea) el.rows = 4;
      label.appendChild(el);
      return { label: label, el: el };
    }
    function card(title) {
      var el = node('section', undefined, 'resident-card');
      el.appendChild(node('h3', title));
      root.appendChild(el);
      return el;
    }
    function time(value) { return value ? new Date(value * 1000).toLocaleString() : '—'; }
    function error(message) {
      var box = root.querySelector('[role="alert"]');
      if (!box) { box = node('p', '', 'notice error'); box.setAttribute('role', 'alert'); root.prepend(box); }
      box.textContent = message;
    }

    async function load() {
      var seq = ++sequence;
      try {
        var response = unwrap(await win.AstrBotPluginPage.apiGet('resident', { umo: umo, before: before }));
        if (seq !== sequence) return;
        if (response.operation_error) throw new Error(response.operation_error);
        data = response; umo = data.umo || ''; render();
      } catch (exc) { error('读取失败：' + (exc.message || '请重试')); }
    }
    async function act(payload) {
      if (busy) return;
      busy = true;
      root.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
      try {
        var response = unwrap(await win.AstrBotPluginPage.apiPost('resident', Object.assign({ umo: umo }, payload)));
        if (response.operation_error) throw new Error(response.operation_error);
        data = response; before = 0; render();
      } catch (exc) { error('操作未完成：' + (exc.message || '请重试')); }
      finally { busy = false; root.querySelectorAll('button').forEach(function (b) { b.disabled = false; }); }
    }

    function render() {
      root.replaceChildren();

      var head = card('会话与信箱'), picker = node('select');
      picker.setAttribute('aria-label', '查看会话');
      (data.sessions || []).forEach(function (s) {
        var opt = node('option', (s.name || s.conversation_id) + ' · 未读 ' + (s.unread || 0) + ' / 提及 ' + (s.unread_mentions || 0));
        opt.value = s.umo;
        picker.appendChild(opt);
      });
      picker.value = umo;
      picker.addEventListener('change', function () { umo = picker.value; before = 0; load(); });
      head.appendChild(picker);
      head.appendChild(button('刷新运行数据', load));
      if (!data.sessions.length) head.appendChild(node('p', '还没有可查看的会话记录。', 'muted'));
      if (data.migration) head.appendChild(node('p', data.migration, 'muted'));
      var budget = data.budget || {};
      head.appendChild(node('p', '今日自醒 ' + (budget.used || 0) + ' 次 · 未缓存输入与输出 ' + (budget.tokens || 0) + ' token' + (budget.unknown ? ' · 部分用量未知' : ''), 'muted'));

      var capabilities = card('能力联动');
      (data.capabilities || []).forEach(function (c) {
        var item = node('div', undefined, 'resident-capability');
        item.appendChild(node('span', c.title + ' · ' + (labels[c.state] || c.state), 'badge badge-' + (c.state === 'ready' ? 'ok' : c.state === 'unavailable' || c.state === 'out_of_scope' ? 'off' : 'warn')));
        item.appendChild(node('p', c.source + '：' + c.reason, 'muted'));
        item.appendChild(node('small', c.tools.join('、'), 'muted'));
        if (c.url) {
          var link = node('a', '打开 ' + c.source + ' 管理页面');
          link.href = c.url; link.target = '_blank'; link.rel = 'noopener';
          item.appendChild(link);
        }
        capabilities.appendChild(item);
      });

      if (data.state) {
        var state = card('当前状态 · ' + data.persona), mood = node('select');
        mood.setAttribute('aria-label', '共享心情');
        data.moods.forEach(function (name) { mood.appendChild(node('option', name)); });
        mood.value = data.state.mood ? data.state.mood.mood : data.moods[0];
        state.appendChild(mood);
        var strength = input('心情强度（0–100）', data.state.mood ? data.state.mood.intensity : 50);
        var focus = input('仅本会话的关注点', data.state.focus, true);
        strength.el.type = 'number'; strength.el.min = 0; strength.el.max = 100;
        state.appendChild(strength.label); state.appendChild(focus.label);
        state.appendChild(button('保存当前状态', function () {
          act({ action: 'state', mood: mood.value, intensity: Number(strength.el.value), focus: focus.el.value });
        }));

        var style = card('表达风格 · 版本 ' + data.style.version);
        style.appendChild(node('p', '基础人设由 AstrBot 管理。这里仅维护经批准的风格补充。', 'muted'));
        style.appendChild(node('pre', data.style.body || '尚无风格补充'));
        (data.proposals || []).forEach(function (p) {
          var item = node('details', undefined, 'resident-detail');
          item.appendChild(node('summary', '#' + p.id + ' · ' + (labels[p.status] || p.status) + ' · 基准版本 ' + p.base_version));
          item.appendChild(node('p', p.reason));
          item.appendChild(node('strong', '当前版本'));
          item.appendChild(node('pre', data.style.body || '（空）'));
          item.appendChild(node('strong', '提案版本'));
          item.appendChild(node('pre', p.body));
          if (p.diff) { item.appendChild(node('strong', '与当前版本的差异')); item.appendChild(node('pre', p.diff)); }
          if (p.status === 'pending') {
            item.appendChild(button('批准此提案', function () { act({ action: 'approve', proposal_id: p.id, expected_version: data.style.version }); }));
            item.appendChild(button('拒绝此提案', function () { act({ action: 'reject', proposal_id: p.id, expected_version: data.style.version }); }));
          }
          style.appendChild(item);
        });
        (data.versions || []).filter(function (v) { return v.version !== data.style.version; }).forEach(function (v) {
          var item = node('details', undefined, 'resident-detail');
          item.appendChild(node('summary', '历史版本 ' + v.version));
          item.appendChild(node('pre', v.body || '（空）'));
          item.appendChild(button('回滚到此版本', function () { act({ action: 'rollback', version: v.version, expected_version: data.style.version }); }));
          style.appendChild(item);
        });
      }

      var runs = card('自主运行记录');
      (data.runs || []).forEach(function (run) {
        var item = node('details', undefined, 'resident-detail');
        item.appendChild(node('summary', '#' + run.id + ' · ' + (labels[run.status] || run.status) + ' · ' + (labels[run.outcome] || run.outcome) + ' · ' + time(run.started)));
        item.appendChild(node('p', run.umo + ' · 输入 ' + run.input_other + ' / 缓存 ' + run.input_cached + ' / 输出 ' + run.output + (run.usage_unknown ? ' · 用量不完整' : ''), 'muted'));
        item.appendChild(node('pre', run.summary));
        if (run.status === 'running' || run.status === 'paused') {
          item.appendChild(button(run.status === 'paused' ? '恢复' : '暂停', function () { act({ action: run.status === 'paused' ? 'resume' : 'pause', run_id: run.id }); }));
          item.appendChild(button('停止本轮', function () { act({ action: 'stop', run_id: run.id }); }));
        }
        if ((run.tool_requests || []).length) {
          var requests = node('details', undefined, 'resident-detail');
          requests.appendChild(node('summary', '模型请求的工具 · ' + run.tool_requests.length + ' 次'));
          run.tool_requests.forEach(function (request) {
            requests.appendChild(node('p', request.tool));
            requests.appendChild(node('pre', request.args));
          });
          item.appendChild(requests);
        }
        (run.deferred || []).forEach(function (pending) {
          item.appendChild(node('p', '静默顺延 #' + pending.id + ' · ' + (labels[pending.status] || pending.status) + ' · ' + pending.target_umo + ' · ' + time(pending.fire_ts), 'muted'));
        });
        if ((run.deferred || []).some(function (p) { return p.status === 'pending'; }) && run.status !== 'running' && run.status !== 'paused') {
          item.appendChild(button('取消本轮待发送消息', function () { act({ action: 'stop', run_id: run.id }); }));
        }
        (run.sends || []).forEach(function (send) {
          var detail = node('details', undefined, 'resident-detail');
          detail.appendChild(node('summary', '实际发送 · ' + (labels[send.status] || send.status) + ' · ' + send.umo));
          detail.appendChild(node('pre', send.body));
          detail.appendChild(node('p', send.detail, 'muted'));
          if (send.status === 'unknown') {
            var note = input('发送核实依据', '', true);
            detail.appendChild(note.label);
            ['sent', 'failed'].forEach(function (conclusion) {
              detail.appendChild(button('核实为' + labels[conclusion], function () { act({ action: 'review_resident_send', send_id: send.id, conclusion: conclusion, note: note.el.value }); }));
            });
          }
          item.appendChild(detail);
        });
        (run.actions || []).forEach(function (a) {
          var detail = node('details', undefined, 'resident-detail');
          detail.appendChild(node('summary', a.tool + ' · ' + (labels[a.status] || a.status)));
          detail.appendChild(node('pre', a.args));
          detail.appendChild(node('pre', a.result));
          if (a.status === 'unknown') {
            var note = input('核实依据', '', true);
            detail.appendChild(note.label);
            ['succeeded', 'failed'].forEach(function (conclusion) {
              detail.appendChild(button('核实为' + labels[conclusion], function () { act({ action: 'review', action_id: a.id, conclusion: conclusion, note: note.el.value }); }));
            });
          }
          item.appendChild(detail);
        });
        runs.appendChild(item);
      });
      if (!data.runs.length) runs.appendChild(node('p', '还没有自主运行记录。', 'muted'));
      if (data.next_before) runs.appendChild(button('查看更早的运行', function () { before = data.next_before; load(); }));

      (data.unknown_sends || []).forEach(function (row) {
        var item = card('待核实的发送 #' + row.id);
        item.appendChild(node('p', row.target_umo, 'muted'));
        item.appendChild(node('pre', row.note));
        ['fired', 'failed'].forEach(function (conclusion) {
          item.appendChild(button(conclusion === 'fired' ? '已核实发送成功' : '已核实未发送', function () { act({ action: 'review_send', send_id: row.id, conclusion: conclusion }); }));
        });
      });
    }

    root.appendChild(node('p', '正在读取自主运行数据…', 'muted'));
    load();
    return { reload: load };
  }

  return { mount: mount, labels: labels };
});
