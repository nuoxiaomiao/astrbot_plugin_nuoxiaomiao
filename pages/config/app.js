/* 糯小喵设置 — Win11 风格主入口（浏览器 + Node UMD）。 */
(function (root, factory) {
  'use strict';
  var S = root && root.NuoxiaomiaoConfigState ? root.NuoxiaomiaoConfigState : (typeof require === 'function' ? require('./schema_state.js') : null);
  var C = root && root.NuoxiaomiaoConfigControls ? root.NuoxiaomiaoConfigControls : (typeof require === 'function' ? require('./controls.js') : null);
  var api = factory(S, C);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root);
})(typeof window !== 'undefined' ? window : null, function (S, C) {
  'use strict';

  function mount(win) {
    var doc = win.document;
    var dom = C.createDomHelpers(doc);
    var node = dom.node, button = dom.button, icon = dom.icon, chevronIcon = dom.chevronIcon;

    var schema = {}, state = {}, baseline = {}, defaults = {}, userDefault = null;
    var groups = [], providers = [], residentTools = [], diagnostics = [], timezone = '';
    var query = '', saving = false, loaded = false;
    var fieldErrors = {}, toastTimer = null, searchTimer = null, lastErrorSig = '';
    var residentHandle = null;

    function loadMap(storageKey) {
      try {
        var raw = win.localStorage && win.localStorage.getItem(storageKey);
        var data = raw ? JSON.parse(raw) : null;
        return data && typeof data === 'object' ? data : {};
      } catch (error) { return {}; }
    }
    function saveMap(storageKey, map) {
      try { if (win.localStorage) win.localStorage.setItem(storageKey, JSON.stringify(map)); } catch (error) { /* ignore sandbox */ }
    }

    var clusterOpen = loadMap(S.CLUSTER_KEY);
    var listOpen = loadMap(S.LIST_KEY);
    var pageMap = loadMap(S.PAGE_KEY);
    var activePage = typeof pageMap.page === 'string' ? pageMap.page : 'overview';
    var lastView = '';

    function $(id) { return doc.getElementById(id); }
    function groupsEl() { return $('groups'); }
    function groupById(id) {
      for (var i = 0; i < groups.length; i++) if (groups[i].id === id) return groups[i];
      return null;
    }

    function syncThemeFromContext(ctx) {
      var theme = '';
      if (ctx && typeof ctx.theme === 'string') theme = ctx.theme;
      else if (win.location && typeof win.location.search === 'string') {
        var match = win.location.search.match(/[?&]theme=(light|dark)/i);
        if (match) theme = match[1].toLowerCase();
      }
      if (theme === 'light' || theme === 'dark') {
        try { doc.documentElement.dataset.theme = theme; } catch (error) { /* ignore */ }
      }
    }
    syncThemeFromContext();
    if (win.AstrBotPluginPage && typeof win.AstrBotPluginPage.onContext === 'function') {
      try { win.AstrBotPluginPage.onContext(syncThemeFromContext); } catch (error) { /* ignore */ }
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
    function defaultOf(key) { return userDefault && S.own(userDefault, key) ? userDefault[key] : defaults[key]; }

    var controls = C.createControlFactory({
      win: win,
      doc: doc,
      S: S,
      dom: dom,
      getQuery: function () { return query; },
      getSchema: function () { return schema; },
      getState: function () { return state; },
      getProviders: function () { return providers; },
      getResidentTools: function () { return residentTools; },
      pop: pop,
      showToast: showToast,
      onChange: onChange,
      onReferenceChange: function (mode) {
        Object.assign(state, S.referenceValues(mode));
        S.REFERENCE_KEYS.forEach(function (key) { delete fieldErrors[key]; });
        renderPage();
        refresh();
      },
    });
    var fillText = controls.fillText;
    var row = controls.row;

    function focusControl(key) {
      var el = $('input-' + key);
      if (!el || typeof el.focus !== 'function') return;
      try { el.focus({ preventScroll: true }); } catch (error) { el.focus(); }
    }
    function onChange(key, value) {
      state[key] = value;
      delete fieldErrors[key];
      if (S.WAKE_KEYS.indexOf(key) >= 0) S.WAKE_KEYS.forEach(function (item) { delete fieldErrors[item]; });
      if (S.REVEAL_KEYS.indexOf(key) >= 0 || S.clusterByGate[key]) {
        renderPage();
        if (S.REVEAL_KEYS.indexOf(key) >= 0) focusControl(key);
      }
      refresh();
    }

    function visibleField(key, group) {
      if (!schema[key]) return false;
      var when = S.VISIBLE_WHEN[key];
      if (when && !when(state)) return false;
      if (!query) return true;
      if (key === '@reference') {
        var text = '聊天参考方式 按需工具查询 关键词预取 每轮背景 activity_prefetch_enabled capability_context_digest';
        return text.toLowerCase().indexOf(query) >= 0;
      }
      return S.matches(key, schema[key] || {}, query, group.title);
    }
    function fieldVisibleInCluster(key, cluster, group) {
      if (!schema[key]) return false;
      var when = S.VISIBLE_WHEN[key];
      if (when && !when(state)) return false;
      if (!query) return true;
      return S.matches(key, schema[key], query, group.title) || cluster.title.toLowerCase().indexOf(query) >= 0;
    }
    function groupBlocks(group) {
      var blocks = [], seen = {};
      group.fields.forEach(function (key) {
        if (seen[key]) return;
        var cluster = S.clusterByGate[key];
        if (cluster) {
          blocks.push({ cluster: cluster });
          cluster.gates.concat(cluster.children, cluster.lists).forEach(function (child) { seen[child] = true; });
          return;
        }
        if (S.clusterByChild[key]) return;
        blocks.push({ key: key });
      });
      return blocks;
    }

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

    function clusterShouldOpen(cluster) {
      if (S.own(clusterOpen, cluster.id)) return !!clusterOpen[cluster.id];
      return S.clusterGateOn(cluster, state);
    }
    function applyClusterOpen(cluster) {
      var open = clusterShouldOpen(cluster);
      var body = $('cluster-body-' + cluster.id);
      var toggle = $('cluster-toggle-' + cluster.id);
      if (body) setBodyOpen(body, open);
      if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function toggleCluster(cluster) {
      clusterOpen[cluster.id] = !clusterShouldOpen(cluster);
      saveMap(S.CLUSTER_KEY, clusterOpen);
      applyClusterOpen(cluster);
    }
    function clusterHasEntries(cluster) {
      return cluster.lists.some(function (key) { return S.parseList(state[key]).length > 0; });
    }
    function listShouldOpen(cluster) {
      if (S.own(listOpen, cluster.id)) return !!listOpen[cluster.id];
      return clusterHasEntries(cluster);
    }
    function applyListOpen(cluster) {
      var open = listShouldOpen(cluster);
      var body = $('list-body-' + cluster.id);
      var bar = $('list-bar-' + cluster.id);
      if (body) setBodyOpen(body, open);
      if (bar) bar.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function toggleList(cluster) {
      listOpen[cluster.id] = !listShouldOpen(cluster);
      saveMap(S.LIST_KEY, listOpen);
      applyListOpen(cluster);
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
      var summary = S.listSummary(state, cluster.lists);
      var summaryEl = node('span', 'subblock-count', summary.fields + ' 个名单 · 共 ' + summary.items + ' 项');
      summaryEl.id = 'list-summary-' + cluster.id;
      head.appendChild(summaryEl);
      var pending = node('span', 'nav-badge'); pending.id = 'list-pending-' + cluster.id; pending.hidden = true;
      head.appendChild(pending);
      var chev = chevronIcon();
      if (chev) head.appendChild(chev);
      wrap.appendChild(head);

      var body = node('div', 'subblock-body');
      body.id = 'list-body-' + cluster.id;
      body.hidden = !open;
      body.__nxmOpen = open;
      keys.forEach(function (key) { var r = row(key); if (r) body.appendChild(r); });
      wrap.appendChild(body);
      return wrap;
    }

    function clusterCard(cluster, group) {
      var card = node('section', 'card cluster');
      card.id = 'cluster-' + cluster.id;
      var open = clusterShouldOpen(cluster);
      var head = node('div', 'card-head cluster-head');

      var titleWrap = node('div', 'cluster-title-wrap');
      var title = node('h2', 'card-title'); fillText(title, cluster.title);
      titleWrap.appendChild(title);
      if (cluster.gates.length === 1 && schema[cluster.gates[0]] && schema[cluster.gates[0]].hint) {
        var hint = node('p', 'cluster-hint'); fillText(hint, schema[cluster.gates[0]].hint);
        titleWrap.appendChild(hint);
      }
      var pill = node('span', 'state-pill', S.clusterGateOn(cluster, state) ? '' : '已关闭');
      pill.id = 'cluster-state-' + cluster.id;
      pill.classList.toggle('off', !S.clusterGateOn(cluster, state));
      titleWrap.appendChild(pill);
      var pending = node('span', 'cluster-pending'); pending.id = 'cluster-pending-' + cluster.id; pending.hidden = true;
      titleWrap.appendChild(pending);
      head.appendChild(titleWrap);

      var actions = node('div', 'cluster-actions');
      if (cluster.gates.length === 1 && schema[cluster.gates[0]]) {
        var gateKey = cluster.gates[0];
        var gateWrap = node('div', 'gate-control'); gateWrap.id = 'field-' + gateKey; gateWrap.dataset.key = gateKey;
        var label = node('label', 'switch');
        var input = node('input'); input.type = 'checkbox'; input.id = 'input-' + gateKey;
        input.checked = S.parseBool(state[gateKey]);
        input.setAttribute('aria-label', cluster.title);
        var text = node('span', 'switch-text', input.checked ? '开' : '关');
        input.addEventListener('change', function () {
          text.textContent = input.checked ? '开' : '关';
          if (input.checked && !S.own(clusterOpen, cluster.id)) {
            clusterOpen[cluster.id] = true;
            saveMap(S.CLUSTER_KEY, clusterOpen);
          } else if (!input.checked) {
            clusterOpen[cluster.id] = false;
            saveMap(S.CLUSTER_KEY, clusterOpen);
          }
          onChange(gateKey, input.checked);
        });
        label.appendChild(input); label.appendChild(text);
        gateWrap.appendChild(label);
        actions.appendChild(gateWrap);
      }
      var toggle = button('', function () { toggleCluster(cluster); }, 'icon-btn');
      toggle.id = 'cluster-toggle-' + cluster.id;
      toggle.setAttribute('aria-label', '展开或收起' + cluster.title + '设置');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-controls', 'cluster-body-' + cluster.id);
      var chev = chevronIcon();
      if (chev) toggle.appendChild(chev);
      actions.appendChild(toggle);
      head.appendChild(actions);
      card.appendChild(head);

      var body = node('div', 'card-body');
      body.id = 'cluster-body-' + cluster.id;
      body.hidden = !open;
      body.__nxmOpen = open;
      if (cluster.gates.length > 1) {
        cluster.gates.forEach(function (key) { if (schema[key]) body.appendChild(row(key)); });
      }
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
      var gateMatch = cluster.gates.some(function (key) { return schema[key] && S.matches(key, schema[key], query, group.title); });
      var children = cluster.children.filter(function (key) { return schema[key] && S.matches(key, schema[key], query, group.title); });
      var lists = cluster.lists.filter(function (key) { return schema[key] && S.matches(key, schema[key], query, group.title); });
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
        var when = S.VISIBLE_WHEN[block.key];
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

    function ensureStatusMounted() {
      var statusRoot = $('status-root'); if (!statusRoot) return;
      if (residentHandle && $('resident-root')) return;
      statusRoot.replaceChildren();
      statusRoot.appendChild(pageHead('运行与状态', '自主运行记录、当前状态与表达风格；长内容点击展开。', false));
      var host = node('div', 'resident-panel'); host.id = 'resident-root';
      statusRoot.appendChild(host);
      if (win.NuoxiaomiaoResidentPanel && typeof win.NuoxiaomiaoResidentPanel.mount === 'function') {
        residentHandle = win.NuoxiaomiaoResidentPanel.mount(host, win);
      } else {
        host.appendChild(node('p', 'empty', '状态面板未加载，请刷新页面重试。'));
      }
    }

    function renderNav() {
      var nav = $('nav'); if (!nav) return;
      nav.replaceChildren();
      function navItem(id, label, iconName, onClick, isStatus) {
        var btn = button('', onClick, 'nav-item');
        btn.dataset[isStatus ? 'page' : 'group'] = id;
        var svg = icon(iconName); if (svg) btn.appendChild(svg);
        btn.appendChild(node('span', 'nav-label', label));
        if (!isStatus) {
          var badge = node('span', 'nav-badge');
          badge.id = 'nav-pending-' + id; badge.hidden = true;
          btn.appendChild(badge);
        }
        return btn;
      }
      groups.forEach(function (group) {
        nav.appendChild(navItem(group.id, group.title, group.id, function () { selectPage(group.id); }, false));
      });
      nav.appendChild(node('div', 'nav-sep'));
      nav.appendChild(navItem('status', '运行与状态', 'status', function () { selectPage('status'); }, true));
      updateNavActive();
    }

    function updateNavActive() {
      var nav = $('nav'); if (!nav) return;
      nav.querySelectorAll('.nav-item').forEach(function (item) {
        var target = item.dataset.group || item.dataset.page;
        var active = !query && target === activePage;
        item.classList.toggle('active', active);
        item.setAttribute('aria-current', active ? 'page' : 'false');
      });
    }

    function selectPage(id) {
      activePage = id;
      saveMap(S.PAGE_KEY, { page: activePage });
      if (query) { query = ''; var s = $('search'); if (s) s.value = ''; }
      renderPage(); refresh();
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
      if (statusMode) ensureStatusMounted();
      else if (query) renderSearch();
      else renderConfigPage(groupById(activePage) || groups[0]);
      if (viewKey !== lastView) {
        lastView = viewKey;
        playPageIn();
        if (typeof win.scrollTo === 'function') win.scrollTo(0, 0);
      } else if (keepId && !statusMode) {
        var again = $(keepId);
        if (again && typeof again.focus === 'function') {
          try { again.focus({ preventScroll: true }); } catch (error) { again.focus(); }
          if (typeof win.scrollTo === 'function') win.scrollTo(0, keepY);
        }
      }
      var savebar = $('savebar');
      if (savebar) savebar.hidden = statusMode && !S.dirtyKeys(state, baseline, schema).length;
    }

    function refreshCluster(cluster, keys) {
      var allKeys = cluster.gates.concat(cluster.children, cluster.lists);
      var count = allKeys.filter(function (key) { return keys.indexOf(key) >= 0; }).length;
      var pending = $('cluster-pending-' + cluster.id);
      if (pending) { pending.textContent = count ? '待保存 ' + count + ' 项' : ''; pending.hidden = !count; }
      var listCount = cluster.lists.filter(function (key) { return keys.indexOf(key) >= 0; }).length;
      var listPending = $('list-pending-' + cluster.id);
      if (listPending) { listPending.textContent = listCount ? String(listCount) : ''; listPending.hidden = !listCount; }
      var on = S.clusterGateOn(cluster, state);
      var pill = $('cluster-state-' + cluster.id);
      if (pill) { pill.textContent = on ? '' : '已关闭'; pill.classList.toggle('off', !on); }
      var summaryEl = $('list-summary-' + cluster.id);
      if (summaryEl) {
        var summary = S.listSummary(state, cluster.lists);
        summaryEl.textContent = summary.fields + ' 个名单 · 共 ' + summary.items + ' 项';
      }
      applyClusterOpen(cluster);
      applyListOpen(cluster);
    }

    function refresh() {
      var keys = S.dirtyKeys(state, baseline, schema);
      var dirty = $('dirty'), detail = $('save-detail');
      if (dirty) dirty.textContent = saving ? '正在保存…' : keys.length ? '待保存 ' + keys.length + ' 项' : '没有待保存的修改';
      if (detail) detail.textContent = saving ? '保存期间继续编辑的内容会保留' : keys.length ? '点击保存后生效 · Ctrl / ⌘ + S' : loaded ? '当前修改均已保存' : '配置加载后可编辑';
      var saveBtn = $('save'), revertBtn = $('revert'), factoryBtn = $('restore-factory');
      if (saveBtn) saveBtn.disabled = !loaded || saving || !keys.length;
      if (revertBtn) revertBtn.disabled = !loaded || saving || !keys.length;
      if (factoryBtn) factoryBtn.disabled = !loaded || saving;
      var saveDef = $('save-default'), applyDef = $('apply-default'), clearDef = $('clear-default');
      if (saveDef) saveDef.disabled = !loaded || saving;
      if (applyDef) applyDef.disabled = !loaded || saving || !userDefault;
      if (clearDef) { clearDef.disabled = !loaded || saving || !userDefault; clearDef.hidden = !userDefault; }
      var clear = $('search-clear'); if (clear) clear.hidden = !query;
      var savebar = $('savebar');
      if (savebar && savebar.classList) savebar.classList.toggle('is-dirty', !!loaded && keys.length > 0);
      groups.forEach(function (group) {
        var count = S.pendingCount(group, keys);
        var badge = $('nav-pending-' + group.id);
        if (badge) { badge.textContent = count ? String(count) : ''; badge.hidden = !count; }
      });
      var group = groupById(activePage);
      var reset = $('group-reset');
      if (reset) reset.hidden = !group || !S.pendingCount(group, keys);
      S.CLUSTERS.forEach(function (cluster) { refreshCluster(cluster, keys); });
      doc.querySelectorAll('[data-key]').forEach(function (el) {
        var key = el.dataset.key;
        var pending = key === '@reference' ? S.REFERENCE_KEYS.some(function (k) { return keys.indexOf(k) >= 0; }) : keys.indexOf(key) >= 0;
        el.classList.toggle('pending', pending);
        var error = key === '@reference' ? S.REFERENCE_KEYS.map(function (k) { return fieldErrors[k]; }).filter(Boolean).join('；') : fieldErrors[key];
        var errorNode = $('error-' + key);
        if (errorNode) { errorNode.textContent = Array.isArray(error) ? error.join('；') : error || ''; errorNode.hidden = !error; }
        el.querySelectorAll('input,textarea,select').forEach(function (input) { input.setAttribute('aria-invalid', error ? 'true' : 'false'); });
      });
      var banner = $('banner');
      if (banner) {
        banner.hidden = S.parseBool(baseline.enabled) && S.parseBool(state.enabled);
        banner.textContent = !S.parseBool(baseline.enabled)
          ? '当前运行：插件已关闭。' + (S.parseBool(state.enabled) ? '草稿已开启，保存后生效。' : '可先编辑设置，开启总开关并保存后恢复功能。')
          : '草稿已关闭总开关；保存后插件功能暂停。';
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
      var sig = keys.join('|');
      if (sig === lastErrorSig) return;
      lastErrorSig = sig;
      box.replaceChildren(); box.hidden = !keys.length;
      if (!keys.length) return;
      box.appendChild(node('strong', '', '有 ' + keys.length + ' 项需要修正，尚未保存。'));
      keys.forEach(function (key) { box.appendChild(button(S.fieldTitle(schema[key], key), function () { focusField(key); }, 'link-btn')); });
    }

    function focusField(key) {
      var target = S.REFERENCE_KEYS.indexOf(key) >= 0 ? '@reference' : key;
      query = ''; var search = $('search'); if (search) search.value = '';
      var home = S.groupOf(key, groups);
      if (home) { activePage = home.id; saveMap(S.PAGE_KEY, { page: activePage }); }
      var cluster = S.clusterByChild[key] || S.clusterByGate[key];
      if (cluster) {
        clusterOpen[cluster.id] = true; saveMap(S.CLUSTER_KEY, clusterOpen);
        listOpen[cluster.id] = true; saveMap(S.LIST_KEY, listOpen);
      }
      var when = S.VISIBLE_WHEN[key];
      if (when && !when(state)) {
        renderPage(); refresh();
        showToast('「' + S.fieldTitle(schema[key], key) + '」当前被条件开关隐藏，请先检查对应模式开关。');
        return;
      }
      renderPage(); refresh();
      var el = $('field-' + target); if (!el) return;
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
      flashRow(el);
      var input = $('input-' + key) || el.querySelector('input,textarea,select');
      if (input && typeof input.focus === 'function') input.focus();
    }

    async function save() {
      if (!loaded || saving) return;
      var patch = S.buildPatch(state, baseline, schema);
      if (!Object.keys(patch).length) return;
      var localErrors = S.validateValues(state, patch, schema);
      if (Object.keys(localErrors).length) {
        fieldErrors = localErrors; renderPage(); refresh();
        focusField(Object.keys(localErrors)[0]);
        return;
      }
      saving = true; refresh();
      var snapshot = S.clone(state);
      try {
        var response = await win.AstrBotPluginPage.apiPost('config', { config: patch, return_validation_errors: true });
        if (response && response.status === 'error') throw response;
        var data = response && response.data && typeof response.data === 'object' ? response.data : response || {};
        if (data.validation_error) throw data;
        var merged = S.mergeSavedState(state, baseline, snapshot, patch, data.config || {}, schema);
        state = merged.state; baseline = merged.baseline; fieldErrors = {};
        if (Array.isArray(data.diagnostics)) diagnostics = data.diagnostics;
        renderDiagnostics(); renderPage(); refresh();
        var hasNewEdits = S.dirtyKeys(state, baseline, schema).length > 0;
        showToast(hasNewEdits ? '已保存提交的修改；保存期间的新修改仍待保存。' : '配置已保存并生效。');
      } catch (error) {
        fieldErrors = S.errorFields(error); renderPage(); refresh();
        showToast('保存失败：' + (error && error.message ? error.message : '请重试'));
        if (Object.keys(fieldErrors).length) focusField(Object.keys(fieldErrors)[0]);
      } finally { saving = false; refresh(); }
    }

    async function saveAsDefault() {
      if (!loaded || saving) return;
      var allPayload = {};
      Object.keys(schema).forEach(function (key) {
        if (!S.isHiddenKey(key, schema[key])) allPayload[key] = S.clone(state[key]);
      });
      var localErrors = S.validateValues(state, allPayload, schema);
      if (Object.keys(localErrors).length) {
        fieldErrors = localErrors; renderPage(); refresh();
        focusField(Object.keys(localErrors)[0]);
        return;
      }
      saving = true; refresh();
      var snapshot = S.clone(state);
      try {
        var response = await win.AstrBotPluginPage.apiPost('config/default', { config: allPayload, return_validation_errors: true });
        if (response && response.status === 'error') throw response;
        var data = response && response.data && typeof response.data === 'object' ? response.data : response || {};
        if (data.validation_error) throw data;
        if (data.user_default) userDefault = data.user_default;
        var merged = S.mergeSavedState(state, baseline, snapshot, allPayload, data.config || {}, schema);
        state = merged.state; baseline = merged.baseline; fieldErrors = {};
        if (Array.isArray(data.diagnostics)) diagnostics = data.diagnostics;
        renderDiagnostics(); renderPage(); refresh();
        showToast('已保存并设为「我的默认」。');
      } catch (error) {
        fieldErrors = S.errorFields(error); renderPage(); refresh();
        showToast('保存默认失败：' + (error && error.message ? error.message : '请重试'));
      } finally { saving = false; refresh(); }
    }

    function applyUserDefault() {
      if (!loaded || saving || !userDefault) return;
      Object.keys(schema).forEach(function (key) {
        if (!S.isHiddenKey(key, schema[key])) {
          var val = S.clone(defaultOf(key));
          if (schema[key] && schema[key].type === 'bool') val = S.parseBool(val);
          state[key] = val;
        }
      });
      fieldErrors = {}; renderPage(); refresh();
      showToast('已将「我的默认」填入草稿，保存后生效。');
    }

    async function clearUserDefault() {
      if (!loaded || saving || !userDefault) return;
      saving = true; refresh();
      try {
        var response = await win.AstrBotPluginPage.apiPost('config/default', { clear: true });
        if (response && response.status === 'error') throw response;
        userDefault = null;
        renderPage(); refresh();
        showToast('已清除「我的默认」快照（当前运行配置不变）。');
      } catch (error) {
        showToast('清除默认失败：' + (error && error.message ? error.message : '请重试'));
      } finally { saving = false; refresh(); }
    }

    function revert() {
      if (!loaded || saving) return;
      state = S.clone(baseline); fieldErrors = {}; renderPage(); refresh(); showToast('已撤销待保存的修改。');
    }
    function restoreFactory() {
      if (!loaded || saving) return;
      Object.keys(schema).forEach(function (key) {
        var val = S.clone(defaults[key]);
        if (schema[key] && schema[key].type === 'bool') val = S.parseBool(val);
        state[key] = val;
      });
      fieldErrors = {}; renderPage(); refresh(); showToast('已恢复出厂默认，保存后生效。');
    }
    function resetGroup(group) {
      if (!group || !loaded || saving) return;
      group.fields.forEach(function (key) {
        if (key === '@reference') {
          S.REFERENCE_KEYS.forEach(function (name) { state[name] = S.clone(defaultOf(name)); delete fieldErrors[name]; });
          return;
        }
        state[key] = S.clone(defaultOf(key)); delete fieldErrors[key];
      });
      renderPage(); refresh();
      showToast('已恢复「' + group.title + '」的默认值，保存后生效。');
    }

    function wire(id, type, fn) { var el = $(id); if (el) el.addEventListener(type, fn); }
    function init() {
      wire('save', 'click', save);
      wire('save-default', 'click', saveAsDefault);
      wire('apply-default', 'click', applyUserDefault);
      wire('clear-default', 'click', clearUserDefault);
      wire('revert', 'click', revert);
      wire('restore-factory', 'click', restoreFactory);
      wire('search', 'input', function () {
        var next = $('search').value.trim().toLowerCase();
        if (typeof win.requestAnimationFrame !== 'function') { query = next; renderPage(); refresh(); return; }
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { query = next; renderPage(); refresh(); }, 120);
      });
      wire('search-clear', 'click', function () { $('search').value = ''; query = ''; renderPage(); refresh(); });
      doc.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); return; }
        if (event.key === 'Escape' && query && doc.activeElement === $('search')) { event.preventDefault(); $('search').value = ''; query = ''; renderPage(); refresh(); }
      });
      win.addEventListener('beforeunload', function (event) { if (!S.dirtyKeys(state, baseline, schema).length) return; event.preventDefault(); event.returnValue = ''; });
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
        groups = S.deriveGroups(schema);
        state = {};
        Object.keys(schema).forEach(function (key) {
          var rawVal = S.own(data.config, key) ? data.config[key] : defaults[key];
          if (schema[key] && schema[key].type === 'bool') rawVal = S.parseBool(rawVal);
          state[key] = S.clone(rawVal);
        });
        baseline = S.clone(state);
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
          bootBox.appendChild(node('p', '', error && error.message ? error.message : '请确认网络或登录状态后重试。'));
          bootBox.appendChild(button('重新加载', load, 'btn primary'));
        }
      }
    }

    init();
    if (win.AstrBotPluginPage && typeof win.AstrBotPluginPage.ready === 'function') {
      Promise.resolve(win.AstrBotPluginPage.ready()).then(load).catch(load);
    } else {
      load();
    }
    return { reload: load, destroy: function () { if (residentHandle && typeof residentHandle.destroy === 'function') residentHandle.destroy(); } };
  }

  return Object.assign({}, S, { mount: mount });
});
