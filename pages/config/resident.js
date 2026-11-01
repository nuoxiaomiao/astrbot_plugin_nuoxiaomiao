/* 糯小喵 · 运行与状态（Win11 风格重做）。
 * 原则：先摘要后细节；运行记录默认收起、一次展开一条；长文本点开再看。
 * 保持与宿主一致的桥接契约（win.AstrBotPluginPage.apiGet/apiPost）。
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
    succeeded: '已完成', failed: '失败', unknown: '待核实', interrupted: '已中断',
    skipped: '已跳过', limited: '达到限制', sent: '发送成功', sending: '发送中',
    spoke: '已发言', quiet: '安静结束', approved: '已批准', rejected: '已拒绝',
    ready: '可用', unavailable: '不可用', out_of_scope: '当前范围不可用',
    fired: '已发送', 'succeeded-action': '已完成'
  };
  function unwrap(value) { return value && value.data ? value.data : value || {}; }

  function mount(host, win) {
    var doc = win.document;
    var data = null, umo = '', before = 0, sequence = 0, busy = false;
    var filter = 'all', expandedRun = null;

    function node(tag, cls, text) {
      var n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = String(text);
      return n;
    }
    function button(text, fn, cls) {
      var n = node('button', cls, text);
      n.type = 'button';
      n.addEventListener('click', fn);
      return n;
    }
    function card(title) {
      var c = node('section', 'card');
      var head = node('div', 'card-head');
      head.appendChild(node('h2', 'card-title', title));
      c.appendChild(head);
      return c;
    }
    function body(c) {
      var b = node('div', 'card-body');
      c.appendChild(b);
      return b;
    }
    function row(label, desc, control) {
      var r = node('div', 'row');
      var copy = node('div', 'row-copy');
      copy.appendChild(node('label', 'row-label', label));
      if (desc) copy.appendChild(node('p', 'row-desc', desc));
      r.appendChild(copy);
      var ctl = node('div', 'row-control');
      ctl.appendChild(control);
      r.appendChild(ctl);
      return r;
    }
    function blockRow(label, desc, control) {
      var r = node('div', 'row block');
      var copy = node('div', 'row-copy');
      copy.appendChild(node('label', 'row-label', label));
      if (desc) copy.appendChild(node('p', 'row-desc', desc));
      r.appendChild(copy);
      var ctl = node('div', 'row-control');
      ctl.appendChild(control);
      r.appendChild(ctl);
      return r;
    }
    function fmt(n) { return Number(n || 0).toLocaleString(); }
    function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }
    function fmtDuration(sec) {
      if (!sec || sec < 0) return '—';
      var s = Math.round(sec);
      if (s < 60) return s + ' 秒';
      var m = Math.floor(s / 60);
      if (m < 60) return m + ' 分 ' + (s % 60) + ' 秒';
      return Math.floor(m / 60) + ' 时 ' + (m % 60) + ' 分';
    }
    function sessionName(u) {
      var list = (data && data.sessions) || [];
      for (var i = 0; i < list.length; i += 1) if (list[i].umo === u) return list[i].name || list[i].conversation_id || u;
      return u || '—';
    }
    function label(value) { return labels[value] || value || '—'; }
    function statusClass(status) {
      if (status === 'running' || status === 'paused') return 'running';
      if (status === 'failed') return 'failed';
      if (status === 'unknown') return 'unknown';
      if (status === 'succeeded' || status === 'sent' || status === 'spoke') return 'succeeded';
      return '';
    }
    function badgeClass(status) {
      if (status === 'failed') return 'err';
      if (status === 'unknown') return 'warn';
      if (status === 'succeeded' || status === 'sent' || status === 'spoke') return 'ok';
      return '';
    }
    function tokens(run) { return (run.input_other || 0) + (run.input_cached || 0) + (run.output || 0); }
    function error(message) {
      var box = node('div', 'infobar danger');
      box.setAttribute('role', 'alert');
      box.textContent = message;
      host.prepend(box);
    }
    function rawBlock(title, text) {
      var det = node('details', 'raw');
      det.appendChild(node('summary', '', title));
      det.appendChild(node('pre', '', text || '（空）'));
      return det;
    }

    async function load() {
      var seq = ++sequence;
      try {
        var response = unwrap(await win.AstrBotPluginPage.apiGet('resident', { umo: umo, before: before }));
        if (seq !== sequence) return;
        if (response.operation_error) throw new Error(response.operation_error);
        data = response; umo = data.umo || ''; render();
      } catch (exc) {
        error('读取失败：' + (exc.message || '请重试'));
      }
    }
    async function act(payload) {
      if (busy) return;
      busy = true;
      try {
        var response = unwrap(await win.AstrBotPluginPage.apiPost('resident', Object.assign({ umo: umo }, payload)));
        if (response.operation_error) throw new Error(response.operation_error);
        data = response; before = 0; render();
      } catch (exc) {
        error('操作未完成：' + (exc.message || '请重试'));
      } finally {
        busy = false;
      }
    }

    function render() {
      host.replaceChildren();
      if (!data) { host.appendChild(node('p', 'empty', '正在读取自主运行数据…')); return; }
      var sessions = data.sessions || [];
      var budget = data.budget || {};
      var runs = data.runs || [];
      var proposals = data.proposals || [];
      var pendingProposals = proposals.filter(function (p) { return p.status === 'pending'; });
      var unknownSends = data.unknown_sends || [];
      var unknownRunSends = [], unknownActions = [];
      runs.forEach(function (run) {
        (run.sends || []).forEach(function (send) { if (send.status === 'unknown') unknownRunSends.push({ run: run, send: send }); });
        (run.actions || []).forEach(function (action) { if (action.status === 'unknown') unknownActions.push({ run: run, action: action }); });
      });
      var pendingTotal = unknownSends.length + unknownRunSends.length + unknownActions.length + pendingProposals.length;

      /* 工具栏 */
      var bar = node('div', 'toolbar');
      var picker = node('select');
      picker.id = 'resident-session';
      picker.setAttribute('aria-label', '查看会话');
      sessions.forEach(function (s) {
        var opt = node('option', '', (s.name || s.conversation_id) + ' · 未读 ' + (s.unread || 0) + ' / 提及 ' + (s.unread_mentions || 0));
        opt.value = s.umo;
        picker.appendChild(opt);
      });
      picker.value = umo;
      picker.addEventListener('change', function () { umo = picker.value; before = 0; load(); });
      bar.appendChild(picker);
      bar.appendChild(button('刷新', load, 'btn'));
      bar.appendChild(node('span', 'muted', '今日自醒 ' + (budget.used || 0) + ' 次 · token ' + fmt(budget.tokens || 0) + (budget.unknown ? ' · 部分用量未知' : '')));
      host.appendChild(bar);

      /* 统计块 */
      var tiles = node('div', 'tiles');
      tiles.appendChild(tile(String(budget.used || 0), '今日自醒', 'tile-wake'));
      tiles.appendChild(tile(fmt(budget.tokens || 0), '今日 token', 'tile-token'));
      tiles.appendChild(tile(String(pendingTotal), '待核实 / 待处理', 'tile-pending'));
      host.appendChild(tiles);

      if (pendingTotal) host.appendChild(pendingCard(unknownSends, unknownRunSends, unknownActions, pendingProposals));
      host.appendChild(runsCard(runs));
      host.appendChild(stateCard());
      host.appendChild(styleCard(proposals));
      host.appendChild(capabilitiesCard());
    }

    function tile(value, labelText, id) {
      var t = node('div', 'tile');
      var v = node('strong', 'tile-value', value); v.id = id;
      t.appendChild(v);
      t.appendChild(node('span', 'tile-label', labelText));
      return t;
    }

    function pendingCard(unknownSends, unknownRunSends, unknownActions, proposals) {
      var c = card('需要处理');
      var b = body(c);
      unknownSends.forEach(function (item) {
        var r = node('div', 'mini-row');
        r.appendChild(node('span', 'mini-title', '待核实的主动发送 · ' + sessionName(item.target_umo)));
        r.appendChild(node('p', 'mini-meta', item.note || '平台未返回回执'));
        var acts = node('div');
        acts.appendChild(button('已核实发送成功', function () { act({ action: 'review_send', send_id: item.id, conclusion: 'fired' }); }, 'btn'));
        acts.appendChild(node('span', '', ' '));
        acts.appendChild(button('已核实未发送', function () { act({ action: 'review_send', send_id: item.id, conclusion: 'failed' }); }, 'btn'));
        r.appendChild(acts);
        b.appendChild(r);
      });
      unknownRunSends.forEach(function (pair) {
        var r = node('div', 'mini-row');
        r.appendChild(node('span', 'mini-title', '待核实的回复发送 · ' + sessionName(pair.send.umo)));
        r.appendChild(node('p', 'mini-meta', pair.send.body || ''));
        var note = node('input'); note.type = 'text'; note.placeholder = '核实依据（可选）'; note.setAttribute('aria-label', '核实依据');
        var acts = node('div');
        acts.appendChild(note);
        acts.appendChild(button('已发送', function () { act({ action: 'review_resident_send', send_id: pair.send.id, conclusion: 'sent', note: note.value }); }, 'btn'));
        acts.appendChild(node('span', '', ' '));
        acts.appendChild(button('未发送', function () { act({ action: 'review_resident_send', send_id: pair.send.id, conclusion: 'failed', note: note.value }); }, 'btn'));
        r.appendChild(acts);
        b.appendChild(r);
      });
      unknownActions.forEach(function (pair) {
        var r = node('div', 'mini-row');
        r.appendChild(node('span', 'mini-title', '待核实的工具动作 · ' + pair.action.tool));
        r.appendChild(node('p', 'mini-meta', pair.action.args || ''));
        var note = node('input'); note.type = 'text'; note.placeholder = '核实依据（可选）'; note.setAttribute('aria-label', '核实依据');
        var acts = node('div');
        acts.appendChild(note);
        acts.appendChild(button('已完成', function () { act({ action: 'review', action_id: pair.action.id, conclusion: 'succeeded', note: note.value }); }, 'btn'));
        acts.appendChild(node('span', '', ' '));
        acts.appendChild(button('失败', function () { act({ action: 'review', action_id: pair.action.id, conclusion: 'failed', note: note.value }); }, 'btn'));
        r.appendChild(acts);
        b.appendChild(r);
      });
      proposals.forEach(function (p) {
        var r = node('div', 'mini-row');
        r.appendChild(node('span', 'mini-title', '待批风格提案 #' + p.id));
        r.appendChild(node('p', 'mini-meta', p.reason || ''));
        if (p.diff) r.appendChild(rawBlock('查看差异', p.diff));
        var acts = node('div');
        acts.appendChild(button('批准', function () { act({ action: 'approve', proposal_id: p.id, expected_version: data.style.version }); }, 'btn primary'));
        acts.appendChild(node('span', '', ' '));
        acts.appendChild(button('拒绝', function () { act({ action: 'reject', proposal_id: p.id, expected_version: data.style.version }); }, 'btn'));
        r.appendChild(acts);
        b.appendChild(r);
      });
      return c;
    }

    function visibleRuns(runs) {
      return runs.filter(function (run) {
        if (filter === 'running') return run.status === 'running' || run.status === 'paused' || run.status === 'pending';
        if (filter === 'failed') return run.status === 'failed';
        if (filter === 'unknown') {
          return run.status === 'unknown'
            || (run.sends || []).some(function (s) { return s.status === 'unknown'; })
            || (run.actions || []).some(function (a) { return a.status === 'unknown'; });
        }
        return true;
      });
    }

    function runsCard(runs) {
      var c = card('自主运行记录');
      var chips = node('div', 'chips');
      [['all', '全部'], ['running', '运行中'], ['unknown', '待核实'], ['failed', '失败']].forEach(function (pair) {
        var chip = button(pair[1], function () { filter = pair[0]; render(); }, 'chip');
        chip.id = 'filter-' + pair[0];
        chip.dataset.filter = pair[0];
        chip.classList.toggle('active', filter === pair[0]);
        chips.appendChild(chip);
      });
      c.appendChild(chips);
      var list = node('div', 'run-list');
      var shown = visibleRuns(runs);
      shown.forEach(function (run) {
        list.appendChild(runRow(run));
        if (expandedRun === run.id) list.appendChild(runDetail(run));
      });
      if (!shown.length) list.appendChild(node('p', 'empty', '没有符合筛选的运行记录。'));
      c.appendChild(list);
      if (data.next_before) {
        var more = button('加载更早的记录', function () { before = data.next_before; load(); }, 'btn');
        more.id = 'load-more';
        var holder = node('div', 'card-body');
        holder.appendChild(more);
        c.appendChild(holder);
      }
      return c;
    }

    function runRow(run) {
      var r = button('', function () { expandedRun = expandedRun === run.id ? null : run.id; render(); }, 'run-row');
      r.id = 'run-row-' + run.id;
      r.appendChild(node('span', 'status-dot ' + statusClass(run.status)));
      var main = node('div', 'run-main');
      var title = node('div', 'run-title');
      title.appendChild(node('span', '', '#' + run.id));
      title.appendChild(node('span', 'badge ' + badgeClass(run.status), label(run.status)));
      if (run.outcome) title.appendChild(node('span', 'badge', label(run.outcome)));
      main.appendChild(title);
      var meta = [fmtTime(run.started)];
      if (run.ended) meta.push('耗时 ' + fmtDuration(run.ended - run.started));
      meta.push(sessionName(run.umo));
      meta.push('token ' + fmt(tokens(run)));
      meta.push('工具 ' + (run.tool_requests || []).length);
      meta.push('发送 ' + (run.sends || []).length);
      if ((run.deferred || []).length) meta.push('顺延 ' + run.deferred.length);
      main.appendChild(node('div', 'run-meta', meta.join(' · ')));
      r.appendChild(main);
      r.appendChild(node('span', 'chevron-text', expandedRun === run.id ? '▴' : '▾'));
      return r;
    }

    function runDetail(run) {
      var d = node('div', 'run-detail');
      d.id = 'run-detail-' + run.id;
      d.appendChild(node('p', 'run-summary', run.summary || '无摘要'));
      var kv = node('div', 'kv');
      kv.appendChild(node('span', '', '输入 ' + fmt(run.input_other || 0)));
      kv.appendChild(node('span', '', '缓存 ' + fmt(run.input_cached || 0)));
      kv.appendChild(node('span', '', '输出 ' + fmt(run.output || 0)));
      kv.appendChild(node('span', '', '合计 ' + fmt(tokens(run))));
      if (run.usage_unknown) kv.appendChild(node('span', 'badge warn', '用量不完整'));
      d.appendChild(kv);

      if ((run.tool_requests || []).length) {
        d.appendChild(node('h4', '', '工具调用 · ' + run.tool_requests.length));
        run.tool_requests.forEach(function (req) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', req.tool));
          item.appendChild(rawBlock('参数', req.args));
          d.appendChild(item);
        });
      }
      if ((run.sends || []).length) {
        d.appendChild(node('h4', '', '实际发送 · ' + run.sends.length));
        run.sends.forEach(function (send) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', label(send.status) + ' · ' + sessionName(send.umo)));
          if (send.body) item.appendChild(rawBlock('内容', send.body));
          if (send.detail) item.appendChild(node('p', 'mini-meta', send.detail));
          d.appendChild(item);
        });
      }
      if ((run.deferred || []).length) {
        d.appendChild(node('h4', '', '静默顺延 · ' + run.deferred.length));
        run.deferred.forEach(function (pending) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', label(pending.status) + ' · ' + sessionName(pending.target_umo)));
          item.appendChild(node('p', 'mini-meta', '计划发送：' + fmtTime(pending.fire_ts)));
          d.appendChild(item);
        });
      }
      if ((run.actions || []).length) {
        d.appendChild(node('h4', '', '工具动作 · ' + run.actions.length));
        run.actions.forEach(function (action) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', action.tool + ' · ' + label(action.status)));
          if (action.args) item.appendChild(rawBlock('参数', action.args));
          if (action.result) item.appendChild(rawBlock('结果', action.result));
          d.appendChild(item);
        });
      }
      if (run.status === 'running' || run.status === 'paused') {
        var acts = node('div');
        if (run.status === 'paused') acts.appendChild(button('恢复', function () { act({ action: 'resume', run_id: run.id }); }, 'btn'));
        else acts.appendChild(button('暂停', function () { act({ action: 'pause', run_id: run.id }); }, 'btn'));
        acts.appendChild(node('span', '', ' '));
        acts.appendChild(button('停止本轮', function () { act({ action: 'stop', run_id: run.id }); }, 'btn'));
        d.appendChild(acts);
      }
      return d;
    }

    function stateCard() {
      var c = card('当前状态' + (data.persona ? ' · ' + data.persona : ''));
      var b = body(c);
      if (!data.state) {
        b.appendChild(node('p', 'empty', '当前会话没有可编辑的自主状态。'));
        return c;
      }
      var mood = node('select');
      mood.id = 'state-mood';
      mood.setAttribute('aria-label', '共享心情');
      (data.moods || []).forEach(function (name) { mood.appendChild(node('option', '', name)); });
      mood.value = data.state.mood ? data.state.mood.mood : (data.moods || [])[0];
      b.appendChild(row('共享心情', '同人设共享心情标签。', mood));

      var intensity = node('input');
      intensity.type = 'range'; intensity.min = 0; intensity.max = 100; intensity.step = 1;
      intensity.id = 'state-intensity';
      intensity.value = data.state.mood ? data.state.mood.intensity : 50;
      var intensityText = node('span', 'muted', String(intensity.value));
      intensity.addEventListener('input', function () { intensityText.textContent = intensity.value; });
      var holder = node('div');
      holder.appendChild(intensity);
      holder.appendChild(intensityText);
      b.appendChild(row('心情强度', '0–100，越大越明显。', holder));

      var focus = node('textarea');
      focus.id = 'state-focus';
      focus.rows = 3;
      focus.value = data.state.focus || '';
      b.appendChild(blockRow('仅本会话的关注点', '只影响当前会话，不影响其他人设。', focus));

      var actions = node('div');
      actions.appendChild(button('保存当前状态', function () {
        act({ action: 'state', mood: mood.value, intensity: Number(intensity.value), focus: focus.value });
      }, 'btn primary'));
      b.appendChild(actions);
      return c;
    }

    function styleCard(proposals) {
      var c = card('表达风格 · 版本 ' + (data.style ? data.style.version : 0));
      var b = body(c);
      b.appendChild(node('p', 'mini-meta', '基础人设由 AstrBot 管理，这里只维护经批准的风格补充。'));
      b.appendChild(rawBlock('查看当前风格', data.style ? data.style.body : ''));
      var pending = proposals.filter(function (p) { return p.status === 'pending'; });
      if (pending.length) {
        b.appendChild(node('h4', '', '待批提案 · ' + pending.length));
        pending.forEach(function (p) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', '#' + p.id + ' · 基准版本 ' + p.base_version));
          item.appendChild(node('p', 'mini-meta', p.reason || ''));
          if (p.diff) item.appendChild(rawBlock('查看差异', p.diff));
          var acts = node('div');
          acts.appendChild(button('批准此提案', function () { act({ action: 'approve', proposal_id: p.id, expected_version: data.style.version }); }, 'btn primary'));
          acts.appendChild(node('span', '', ' '));
          acts.appendChild(button('拒绝此提案', function () { act({ action: 'reject', proposal_id: p.id, expected_version: data.style.version }); }, 'btn'));
          item.appendChild(acts);
          b.appendChild(item);
        });
      }
      var versions = (data.versions || []).filter(function (v) { return v.version !== data.style.version; });
      if (versions.length) {
        var det = node('details', 'raw');
        det.appendChild(node('summary', '', '历史版本 · ' + versions.length));
        versions.forEach(function (v) {
          var item = node('div', 'mini-row');
          item.appendChild(node('span', 'mini-title', '版本 ' + v.version));
          item.appendChild(rawBlock('内容', v.body));
          item.appendChild(button('回滚到此版本', function () { act({ action: 'rollback', version: v.version, expected_version: data.style.version }); }, 'btn'));
          det.appendChild(item);
        });
        b.appendChild(det);
      }
      return c;
    }

    function capabilitiesCard() {
      var c = card('能力联动');
      var b = body(c);
      var caps = data.capabilities || [];
      if (!caps.length) { b.appendChild(node('p', 'empty', '没有检测到可联动的能力。')); return c; }
      caps.forEach(function (cap) {
        var item = node('div', 'mini-row');
        var title = node('span', 'mini-title', cap.title + ' · ' + label(cap.state));
        title.className = 'mini-title';
        item.appendChild(title);
        item.appendChild(node('p', 'mini-meta', cap.source + '：' + cap.reason));
        if (cap.tools && cap.tools.length) item.appendChild(node('p', 'mini-meta', '工具：' + cap.tools.join('、')));
        if (cap.url) {
          var link = node('a', '', '打开 ' + cap.source + ' 管理页');
          link.href = cap.url; link.target = '_blank'; link.rel = 'noopener';
          item.appendChild(link);
        }
        b.appendChild(item);
      });
      return c;
    }

    host.appendChild(node('p', 'empty', '正在读取自主运行数据…'));
    load();
    return { reload: load };
  }

  return { mount: mount, labels: labels };
});
