/*
 * Status widgets shared by popup and dashboard overview. Global `APWidgets`.
 * Every function returns DOM nodes built from Status (PROTOCOL.md §4).
 */
(function (root) {
  'use strict';
  var U = root.APUI;
  var h = U.h;

  var STATE_ACTIVITY = {
    QUEUED: 'Queued',
    CHECKING_ELIGIBILITY: 'Checking eligibility…',
    READY: 'Ready to apply',
    OPENING: 'Opening application…',
    APPLYING: 'Starting application…',
    ACCOUNT_REQUIRED: 'Account required…',
    VERIFYING_EMAIL: 'Verifying email…',
    FILLING: 'Filling application…',
    ANSWERING: 'Waiting for Astra answers…',
    WAITING_FOR_CAPTCHA: 'Waiting for CAPTCHA…',
    VALIDATING: 'Validating answers…',
    READY_TO_SUBMIT: 'Ready to submit',
    SUBMITTING: 'Submitting…',
    NEEDS_USER: 'Needs your attention'
  };

  function activityText(w) {
    return w.activity || STATE_ACTIVITY[w.state] || (w.state ? String(w.state) : 'Working…');
  }

  function controlHeader(status) {
    var control = String(status.control || 'STOPPED').toUpperCase();
    var bridge = status.bridge || {};
    return h('div', { class: 'control-header' },
      h('span', { class: 'control control-' + control, attrs: { 'data-control': control } }, 'AUTOPILOT: ' + control),
      h('span', { class: 'mode muted', text: status.mode ? String(status.mode).replace('_', ' ') : '' }),
      h('span', { class: 'bridge', attrs: { title: bridge.connected ? 'Firefox bridge connected' + (bridge.owned_tabs != null ? ' — ' + bridge.owned_tabs + ' owned tabs' : '') : 'Firefox bridge disconnected' } },
        h('span', { class: 'dot ' + (bridge.connected ? 'on' : 'off') }),
        h('span', { class: 'small muted', text: bridge.connected ? ' bridge' : ' bridge offline' })));
  }

  function workerCard(w, onOpen) {
    var busy = w.status === 'busy' || !!w.application_id;
    var card = h(busy ? 'button' : 'div', {
      class: 'worker' + (busy ? '' : ' idle'),
      dataset: { workerId: w.worker_id || '', appId: w.application_id || '' }
    });
    if (busy) {
      card.type = 'button';
      card.setAttribute('aria-label', 'Open ' + (w.company || '') + ' ' + (w.title || '') + ' in dashboard');
      card.addEventListener('click', function () { if (w.application_id) onOpen(w.application_id); });
      card.appendChild(h('div', { class: 'w-top' },
        h('span', { class: 'w-title', text: (w.company || 'Unknown company') + (w.title ? ' — ' + w.title : '') }),
        U.badge(w.state)));
      card.appendChild(h('div', { class: 'w-top' },
        h('span', { class: 'w-activity', text: activityText(w) }),
        h('span', { class: 'w-elapsed', dataset: { since: w.since == null ? '' : w.since }, text: U.elapsedSince(w.since) })));
      card.appendChild(h('div', { class: 'small muted', text: (w.worker_id || '') + (w.pool ? ' · ' + w.pool : '') }));
    } else {
      card.appendChild(h('div', { class: 'w-top' },
        h('span', { text: (w.worker_id || 'worker') + (w.pool ? ' · ' + w.pool : '') }),
        h('span', { class: 'small', text: 'idle' })));
    }
    return card;
  }

  function workerSection(status, onOpen, opts) {
    opts = opts || {};
    var workers = Array.isArray(status.workers) ? status.workers : [];
    var busy = workers.filter(function (w) { return w.status === 'busy' || w.application_id; });
    var shown = opts.busyOnly ? busy : workers;
    var wrap = h('div', { class: 'workers' });
    wrap.appendChild(h('h3', { class: 'workers-heading', text: 'Workers: ' + busy.length + '/' + workers.length }));
    if (!shown.length) wrap.appendChild(h('div', { class: 'muted small', text: workers.length ? 'All workers idle' : 'No workers running' }));
    var list = h('div', { class: 'worker-list' });
    shown.forEach(function (w) { list.appendChild(workerCard(w, onOpen)); });
    wrap.appendChild(list);
    return wrap;
  }

  /** Refresh elapsed timers inside `scope` (call every second). */
  function tickElapsed(scope) {
    var els = scope.querySelectorAll('.w-elapsed[data-since], .att-wait[data-since]');
    for (var i = 0; i < els.length; i++) {
      var s = els[i].getAttribute('data-since');
      if (s) els[i].textContent = (els[i].classList.contains('att-wait') ? 'Waiting ' : '') + U.elapsedSince(Number(s) || s);
    }
  }

  var COUNTERS = [
    ['submitted', 'Submitted'], ['skipped', 'Skipped'], ['ineligible', 'Ineligible'],
    ['needs_user', 'Needs attention'], ['failed', 'Failed'], ['active', 'Active']
  ];

  function counters(status) {
    var today = status.today || {};
    var wrap = h('div', { class: 'today' });
    wrap.appendChild(h('div', { class: 'queue-line' },
      h('span', { class: 'muted', text: 'Queue: ' }),
      h('strong', { class: 'queue-remaining', text: String(status.queue_remaining != null ? status.queue_remaining : 0) }),
      h('span', { class: 'muted', text: ' remaining' })));
    wrap.appendChild(h('h3', { text: 'Today' }));
    var grid = h('div', { class: 'counters' });
    COUNTERS.forEach(function (c) {
      grid.appendChild(h('div', { class: 'counter c-' + c[0], dataset: { counter: c[0] } },
        h('div', { class: 'n', text: String(today[c[0]] != null ? today[c[0]] : 0) }),
        h('div', { class: 'l', text: c[1] })));
    });
    wrap.appendChild(grid);
    return wrap;
  }

  var KIND_LABEL = {
    captcha: 'CAPTCHA REQUIRED', missing_fact: 'Missing facts', login: 'Login required',
    verification: 'Verification', unexpected: 'Unexpected page', review: 'Review needed'
  };

  /**
   * handlers: {action(id, action) -> Promise, details(id)?}
   */
  function attentionItem(item, handlers) {
    var id = item.application_id || item.id;
    var el = h('div', { class: 'att-item', dataset: { appId: id || '' } });
    el.appendChild(h('div', { class: 'att-head' },
      h('span', { class: 'att-kind', text: KIND_LABEL[item.kind] || item.kind || 'Attention' }),
      h('span', { class: 'small muted att-wait', dataset: { since: item.since == null ? '' : item.since },
        text: item.since ? 'Waiting ' + U.elapsedSince(item.since) : '' })));
    el.appendChild(h('div', { class: 'att-title', text: (item.company || 'Unknown company') + (item.title ? ' — ' + item.title : '') }));
    if (item.reason) el.appendChild(h('div', { class: 'att-reason', text: item.reason }));
    if (item.instructions) el.appendChild(h('div', { class: 'att-instr', text: item.instructions }));
    var msg = h('div', { class: 'att-msg small', attrs: { 'aria-live': 'polite' } });
    function act(action, btn) {
      return function () {
        btn.disabled = true;
        msg.textContent = '';
        Promise.resolve(handlers.action(id, action)).then(function () {
          msg.textContent = action === 'open' ? 'Focused application tab.' : 'Done: ' + action;
        }, function (err) { msg.textContent = U.errorMessage(err); })
          .then(function () { btn.disabled = false; });
      };
    }
    var row = h('div', { class: 'btn-row' });
    var captcha = item.kind === 'captcha';
    var bOpen = h('button', { type: 'button', class: 'btn btn-primary', text: captcha ? 'Open application' : 'Open', dataset: { action: 'open' } });
    var bResume = h('button', { type: 'button', class: 'btn', text: captcha ? 'Retry detection' : 'Resume',
      dataset: { action: captcha ? 'retry_captcha' : 'resume' } });
    var bSkip = h('button', { type: 'button', class: 'btn', text: captcha ? 'Skip application' : 'Skip', dataset: { action: 'skip' } });
    bOpen.addEventListener('click', act('open', bOpen));
    bResume.addEventListener('click', act(captcha ? 'retry_captcha' : 'resume', bResume));
    bSkip.addEventListener('click', act('skip', bSkip));
    row.appendChild(bOpen); row.appendChild(bResume); row.appendChild(bSkip);
    if (handlers.details) {
      row.appendChild(h('button', { type: 'button', class: 'btn', text: 'Details', dataset: { action: 'details' },
        on: { click: function () { handlers.details(id); } } }));
    }
    el.appendChild(row);
    el.appendChild(msg);
    return el;
  }

  function attentionList(items, handlers) {
    var wrap = h('div', { class: 'attention', attrs: { role: 'region', 'aria-label': 'Needs attention' } });
    wrap.appendChild(h('h3', { text: 'Needs attention (' + items.length + ')' }));
    items.forEach(function (it) { wrap.appendChild(attentionItem(it, handlers)); });
    return wrap;
  }

  function configBanner(cfg) {
    if (!cfg || cfg.ready !== false) return null;
    var missing = Array.isArray(cfg.missing) ? cfg.missing : [];
    var b = h('div', { class: 'banner banner-warn config-banner', attrs: { role: 'status' } },
      h('strong', { text: 'Configuration incomplete' }),
      h('div', { class: 'small', text: 'Run `applypilot setup` or edit your profile. Missing:' }));
    if (missing.length) {
      var ul = h('ul');
      missing.forEach(function (m) { ul.appendChild(h('li', { class: 'mono', text: U.toText(m) })); });
      b.appendChild(ul);
    }
    return b;
  }

  /**
   * Controls for the autopilot. handlers.control(action, mode) -> Promise.
   */
  function controls(status, handlers) {
    var control = String(status.control || 'STOPPED').toUpperCase();
    var row = h('div', { class: 'btn-row controls' });
    function btn(label, action, mode, cls) {
      var b = h('button', { type: 'button', class: cls || 'btn', text: label, dataset: { control: action, mode: mode || '' } });
      b.addEventListener('click', function () { run(b, action, mode); });
      return b;
    }
    function run(b, action, mode) {
      b.disabled = true;
      Promise.resolve(handlers.control(action, mode)).catch(function () { /* handler reports */ })
        .then(function () { b.disabled = false; });
    }
    if (control === 'STOPPED') {
      row.appendChild(btn('Start Backlog', 'start', 'backlog', 'btn btn-primary'));
      row.appendChild(btn('Start New Jobs', 'start', 'new_jobs', 'btn btn-primary'));
    } else if (control === 'RUNNING') {
      row.appendChild(btn('Pause All', 'pause'));
    } else if (control === 'PAUSED') {
      row.appendChild(btn('Resume', 'resume', null, 'btn btn-primary'));
    }
    if (control !== 'STOPPED') {
      var stop = U.confirmButton('Stop', 'Confirm stop?', 'btn btn-danger', function () { run(stop, 'stop'); });
      stop.dataset.control = 'stop';
      row.appendChild(stop);
    }
    if (handlers.dashboard) {
      row.appendChild(h('button', { type: 'button', class: 'btn', text: 'Open Dashboard', dataset: { control: 'dashboard' },
        on: { click: handlers.dashboard } }));
    }
    return row;
  }

  root.APWidgets = {
    activityText: activityText, controlHeader: controlHeader, workerCard: workerCard,
    workerSection: workerSection, tickElapsed: tickElapsed, counters: counters,
    attentionItem: attentionItem, attentionList: attentionList, configBanner: configBanner,
    controls: controls, KIND_LABEL: KIND_LABEL
  };
})(typeof window !== 'undefined' ? window : globalThis);
