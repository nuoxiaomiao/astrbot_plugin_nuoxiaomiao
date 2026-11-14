/* 糯小喵设置 — 字段控件与名单编辑器构建器（浏览器 + Node UMD）。 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.NuoxiaomiaoConfigControls = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  function createDomHelpers(doc) {
    function node(tag, cls, text) {
      var n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = String(text);
      return n;
    }
    function button(text, handler, cls) {
      var n = node('button', cls, text);
      n.type = 'button';
      n.addEventListener('click', handler);
      return n;
    }
    function textNode(text) {
      if (typeof doc.createTextNode === 'function') return doc.createTextNode(text);
      var span = doc.createElement('span');
      span.textContent = text;
      return span;
    }
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
    return { node: node, button: button, textNode: textNode, icon: icon, chevronIcon: chevronIcon };
  }

  function createControlFactory(ctx) {
    var win = ctx.win;
    var doc = ctx.doc;
    var S = ctx.S;
    var dom = ctx.dom;
    var node = dom.node;
    var button = dom.button;
    var textNode = dom.textNode;

    function fillText(target, text) {
      var parts = S.highlightParts(text, ctx.getQuery());
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
    function labelInput(input, key) {
      input.id = 'input-' + key;
      input.setAttribute('aria-label', S.fieldTitle(ctx.getSchema()[key], key));
      return input;
    }
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
      var state = ctx.getState();
      var residentTools = ctx.getResidentTools();
      var wrap = node('div', 'tool-list'), names = S.parseList(state[key]), seen = [];
      residentTools.filter(function (item) { return !item.base; }).forEach(function (item) {
        seen.push(item.name);
        var label = node('label', 'tool-option'), input = node('input');
        input.type = 'checkbox'; input.value = item.name; input.checked = names.indexOf(item.name) >= 0;
        input.disabled = (!item.active || !item.selectable) && !input.checked;
        input.addEventListener('change', function () {
          var next = S.parseList(ctx.getState()[key]).filter(function (name) { return name !== item.name; });
          if (input.checked) next.push(item.name);
          ctx.onChange(key, next);
        });
        label.appendChild(input);
        label.appendChild(node('span', '', item.name + (item.reason ? ' · ' + item.reason : !item.active ? ' · 已停用' : '')));
        wrap.appendChild(label);
      });
      names.filter(function (name) { return seen.indexOf(name) < 0; }).forEach(function (name) {
        var label = node('label', 'tool-option'), input = node('input');
        input.type = 'checkbox'; input.checked = true;
        input.addEventListener('change', function () {
          ctx.onChange(key, S.parseList(ctx.getState()[key]).filter(function (x) { return x !== name; }));
        });
        label.appendChild(input); label.appendChild(node('span', '', name + ' · 当前不可用，可取消选择'));
        wrap.appendChild(label);
      });
      return wrap;
    }
    function providerControl(key) {
      var state = ctx.getState();
      var providers = ctx.getProviders();
      var holder = node('div'), input = labelInput(node('select'), key);
      var auto = node('option', '', '使用全局默认模型'); auto.value = ''; input.appendChild(auto);
      providers.forEach(function (provider) {
        var o = node('option', '', provider.label || provider.id);
        o.value = provider.id; input.appendChild(o);
      });
      if (state[key] && !providers.some(function (provider) { return provider.id === state[key]; })) {
        var old = node('option', '', '当前不可用：' + state[key]); old.value = state[key]; input.appendChild(old);
        holder.appendChild(node('small', 'provider-warning', '原配置的模型当前不可用，已保留。请选择可用模型后保存。'));
      }
      input.value = state[key] || '';
      input.addEventListener('change', function () {
        ctx.onChange(key, input.value);
        var warning = holder.querySelector('.provider-warning');
        if (warning) warning.remove();
      });
      holder.prepend(input); return holder;
    }
    function fitListInput(el) {
      if (!el || !el.style || typeof el.scrollHeight !== 'number' || !el.scrollHeight) return;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, S.LIST_MAX_HEIGHT) + 'px';
    }
    function listControl(key) {
      var schema = ctx.getSchema();
      var state = ctx.getState();
      var spec = schema[key] || {};
      var title = S.fieldTitle(spec, key);
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
      input.rows = S.LIST_ROWS;
      input.placeholder = '每行输入一项（也支持逗号、顿号、分号分隔）';
      var rawStats = S.parseListStats(raw);
      input.value = rawStats.items.join('\n');
      wrap.appendChild(input);

      var foot = node('div', 'list-editor-foot');
      var meta = node('span', 'list-meta');
      meta.id = 'list-meta-' + key;
      foot.appendChild(meta);
      var actions = node('div', 'list-actions');
      var tidyBtn = button('整理', tidy, 'link-btn list-action');
      var clearBtn = button('清空', clear, 'link-btn list-action danger');
      actions.appendChild(tidyBtn);
      actions.appendChild(clearBtn);
      foot.appendChild(actions);
      wrap.appendChild(foot);

      var armed = false, armTimer = null;
      function disarm() {
        armed = false; clearTimeout(armTimer);
        clearBtn.textContent = '清空';
        if (clearBtn.classList) clearBtn.classList.remove('armed');
      }
      function sync(animate) {
        var stats = S.parseListStats(input.value);
        count.textContent = '共 ' + stats.count + ' 项';
        meta.textContent = stats.duplicates
          ? '已合并 ' + stats.duplicates + ' 项重复'
          : stats.count ? '重复项会自动合并' : '留空表示不限';
        if (wrap.classList) wrap.classList.toggle('is-empty', stats.count === 0);
        tidyBtn.disabled = stats.duplicates === 0 && stats.blanks === 0 && input.value === stats.items.join('\n');
        clearBtn.disabled = stats.count === 0;
        fitListInput(input);
        if (animate) ctx.pop(count);
        return stats;
      }
      function tidy() {
        disarm();
        var stats = S.parseListStats(input.value);
        input.value = stats.items.join('\n');
        sync(true);
        ctx.onChange(key, stats.items);
        ctx.showToast(stats.duplicates ? '已整理：合并了 ' + stats.duplicates + ' 项重复内容。' : '名单已经是整理好的状态。');
      }
      function clear() {
        if (!armed) {
          armed = true;
          clearBtn.textContent = '确认清空？';
          if (clearBtn.classList) clearBtn.classList.add('armed');
          ctx.showToast('再点一次「确认清空？」即可清空这个名单。');
          clearTimeout(armTimer);
          armTimer = setTimeout(disarm, 3200);
          return;
        }
        disarm();
        input.value = '';
        sync(true);
        ctx.onChange(key, []);
        ctx.showToast('已清空「' + title + '」，保存后生效。');
      }
      input.addEventListener('input', function () {
        if (armed) disarm();
        ctx.onChange(key, sync(true).items);
      });
      sync(false);
      if (rawStats.duplicates) meta.textContent = '已合并 ' + rawStats.duplicates + ' 项重复';
      if (typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(function () { fitListInput(input); });
      return wrap;
    }
    function buildControl(key, spec) {
      var value = ctx.getState()[key], input;
      if (key === 'resident_extra_tools') return residentToolsControl(key);
      if (/_provider_id$/.test(key) && spec.type === 'string') return providerControl(key);
      if (key === 'merge_join_separator') {
        input = labelInput(node('select'), key);
        [['\n', '换行（推荐）'], [' ', '空格'], ['@custom', '自定义']].forEach(function (item) {
          var o = node('option', '', item[1]); o.value = item[0]; input.appendChild(o);
        });
        input.value = value === '\n' || value === ' ' ? value : '@custom';
        var custom = node('textarea'); custom.rows = 2; custom.value = value || '';
        custom.setAttribute('aria-label', '自定义合并分隔符'); custom.hidden = input.value !== '@custom';
        input.addEventListener('change', function () {
          custom.hidden = input.value !== '@custom';
          ctx.onChange(key, custom.hidden ? input.value : custom.value);
        });
        custom.addEventListener('input', function () { ctx.onChange(key, custom.value); });
        var holder = node('div', 'stack-control'); holder.appendChild(input); holder.appendChild(custom); return holder;
      }
      if (spec.type === 'bool') {
        var label = node('label', 'switch');
        input = labelInput(node('input'), key);
        input.type = 'checkbox';
        input.checked = S.parseBool(value);
        var text = node('span', 'switch-text', input.checked ? '开' : '关');
        input.addEventListener('change', function () {
          text.textContent = input.checked ? '开' : '关';
          ctx.onChange(key, input.checked);
        });
        label.appendChild(input); label.appendChild(text); return label;
      }
      if (spec.type === 'list') return listControl(key);
      if (spec.type === 'text') { input = labelInput(node('textarea', 'prompt'), key); input.rows = 5; }
      else {
        input = labelInput(node('input'), key);
        input.type = S.TIME_KEYS.indexOf(key) >= 0 ? 'time' : (spec.type === 'int' || spec.type === 'float' ? 'number' : 'text');
      }
      input.value = value === null || value === undefined ? '' : value;
      if (input.type === 'number') {
        input.step = spec.type === 'int' ? 1 : 'any';
        if (spec.slider) { input.min = spec.slider.min; input.max = spec.slider.max; }
      }
      input.addEventListener('input', function () {
        ctx.onChange(key, input.type === 'number' ? (input.value === '' ? '' : Number(input.value)) : input.value);
      });
      return input;
    }
    function referenceRow() {
      var state = ctx.getState();
      var wrap = node('div', 'row block'); wrap.id = 'field-@reference'; wrap.dataset.key = '@reference';
      var copy = node('div', 'row-copy');
      var label = node('strong', 'row-label'); fillText(label, '聊天参考方式'); copy.appendChild(label);
      var desc = node('p', 'row-desc'); fillText(desc, '不改变记录范围与查看权限。'); copy.appendChild(desc);
      var err = node('p', 'row-error'); err.id = 'error-@reference'; err.hidden = true; copy.appendChild(err);
      wrap.appendChild(copy);
      var control = node('div', 'row-control');
      control.appendChild(optionsControl('reference-mode', S.referenceMode(state), [
        { id: 'tools', title: '按需工具查询', note: '不自动附带记录，由模型需要时调用速览工具。' },
        { id: 'prefetch', title: '关键词预取', note: '问到别处聊天或翻记录时，自动附带近期记录。' },
        { id: 'background', title: '每轮背景', note: '带工具的回复每轮附带别处近况，不再同时关键词预取。' },
      ], function (mode) { ctx.onReferenceChange(mode); }));
      wrap.appendChild(control);
      return wrap;
    }
    function row(key) {
      if (key === '@reference') return referenceRow();
      var spec = ctx.getSchema()[key]; if (!spec) return null;
      var isBlock = spec.type === 'list' || spec.type === 'text' || key === 'merge_join_separator';
      var el = node('div', 'row' + (isBlock ? ' block' : ''));
      el.id = 'field-' + key; el.dataset.key = key;
      var copy = node('div', 'row-copy');
      var label = node('label', 'row-label'); label.id = 'label-' + key; fillText(label, S.fieldTitle(spec, key)); copy.appendChild(label);
      var desc = node('p', 'row-desc'); desc.id = 'hint-' + key; fillText(desc, spec.hint || ''); desc.hidden = !spec.hint; copy.appendChild(desc);
      var err = node('p', 'row-error'); err.id = 'error-' + key; err.hidden = true; copy.appendChild(err);
      el.appendChild(copy);
      var control = node('div', 'row-control'); control.appendChild(buildControl(key, spec)); el.appendChild(control);
      var target = control.querySelector('#input-' + key);
      if (target) {
        label.htmlFor = 'input-' + key;
        target.setAttribute('aria-describedby', 'hint-' + key + ' error-' + key);
      } else {
        if (typeof label.removeAttribute === 'function') label.removeAttribute('for');
        control.setAttribute('aria-labelledby', 'label-' + key);
      }
      return el;
    }
    return { fillText: fillText, row: row };
  }

  return { createDomHelpers: createDomHelpers, createControlFactory: createControlFactory };
});
