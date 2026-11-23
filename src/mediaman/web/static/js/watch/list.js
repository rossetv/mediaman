/* Watch: the admin list view. Switches between Browse and List in place,
 * and pages, filters, sorts and searches the list by swapping the server's
 * /watch/list fragment in, with the address kept in step so Back works.
 * Row actions go through MMWatch.admin; when one is done the row changes
 * where it is, or leaves and the rows below slide up. Needs watch/core.js
 * and watch/admin.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var admin = window.MMWatch.admin;
  var byId = core.byId;
  var el = core.el;
  var toArray = core.toArray;

  var list = byId('watch-list');
  if (!list) return;
  var browse = byId('watch-browse');
  var results = byId('watch-results');
  var search = byId('watch-search');

  var DEFAULTS = { q: '', type: '', sort: 'added_desc', page: '1', per_page: '20' };
  var KEYS = ['q', 'type', 'sort', 'page', 'per_page'];
  var PLACEHOLDER = { list: 'Search your library', rows: 'Search films and shows' };

  var _view = list.hidden ? 'rows' : 'list';
  var _request = 0;   // the newest list fetch; older replies are dropped
  var _switch = 0;    // the newest view switch; an older one's fade stops short
  var _stale = false; // an admin action changed the library while the list was hidden

  /* ── State and addresses ── */

  function root() { return list.querySelector('[data-list-root]'); }

  function norm(state) {
    var out = {};
    KEYS.forEach(function (k) {
      var v = state && state[k] != null ? String(state[k]) : '';
      out[k] = v === '' ? DEFAULTS[k] : v;
    });
    out.q = out.q.trim();
    return out;
  }

  function stateOf(r) {
    if (!r) return norm({});
    var d = r.dataset;
    return norm({ q: d.q, type: d.type, sort: d.sort, page: d.page, per_page: d.perPage });
  }

  function stateFromSearch(text) {
    var params = new URLSearchParams(text || '');
    var state = {};
    KEYS.forEach(function (k) { if (params.has(k)) state[k] = params.get(k); });
    return norm(state);
  }

  function query(state) {
    return KEYS.filter(function (k) { return state[k] !== DEFAULTS[k]; })
      .map(function (k) { return k + '=' + encodeURIComponent(state[k]); })
      .join('&');
  }

  function pageUrl(state) {
    var qs = query(state);
    return '/watch?view=list' + (qs ? '&' + qs : '');
  }

  function same(a, b) { return query(a) === query(b); }

  /* ── Fetching and swapping the fragment ── */

  function fetchList(state) {
    var qs = query(state);
    return fetch('/watch/list' + (qs ? '?' + qs : ''), {
      credentials: 'same-origin',
      headers: { Accept: 'text/html' }
    }).then(function (resp) {
      /* A redirect means the session is gone or no longer an admin's: let
         the full page load deal with it. */
      if (resp.redirected) {
        window.location.assign(pageUrl(state));
        return new Promise(function () {});
      }
      if (!resp.ok) throw new Error('list ' + resp.status);
      return resp.text();
    }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var fresh = doc.querySelector('[data-list-root]');
      if (!fresh) throw new Error('list: no root');
      return document.importNode(fresh, true);
    });
  }

  function part(r, name) { return r ? r.querySelector('[data-list-part="' + name + '"]') : null; }

  /* A selector that finds the same control in the new fragment. */
  function focusKey(node) {
    var r = root();
    if (!r || !node || !r.contains(node)) return null;
    if (node.id) return function (fresh) { return fresh.querySelector('#' + node.id); };
    var type = node.getAttribute('data-type');
    if (type !== null && node.classList.contains('fpill')) {
      return function (fresh) {
        return toArray(fresh.querySelectorAll('.fpill')).filter(function (p) { return p.getAttribute('data-type') === type; })[0];
      };
    }
    var col = node.getAttribute('data-sort-col');
    if (col) return function (fresh) { return fresh.querySelector('.th-sort[data-sort-col="' + col + '"]'); };
    if (node.classList.contains('page-btn')) return function (fresh) { return fresh.querySelector('.page-btn.active'); };
    return null;
  }

  function placeFocus(find, fresh) {
    var target = find && find(fresh);
    if (target) target.focus({ preventScroll: true });
  }

  /* Put *fresh* in place of the list now showing: the stats, summary and
     toolbar change where they are, and the rows cross-fade. */
  function apply(fresh, find) {
    var r = root();
    if (!r) {
      var skeleton = list.firstElementChild;
      if (skeleton) core.swap(skeleton, fresh);
      else list.appendChild(fresh);
      list.removeAttribute('aria-busy');
      placeFocus(find, fresh);
      return;
    }
    ['q', 'type', 'sort', 'page', 'perPage'].forEach(function (k) { r.dataset[k] = fresh.dataset[k] || ''; });
    ['stats', 'summary', 'toolbar'].forEach(function (name) {
      var old = part(r, name);
      var next = part(fresh, name);
      if (old && next) old.replaceWith(next);
    });
    var oldResults = part(r, 'results');
    var newResults = part(fresh, 'results');
    if (oldResults && newResults) core.swap(oldResults, newResults);
    r.removeAttribute('aria-busy');
    placeFocus(find, r);
  }

  function skeleton() {
    var box = el('div', 'wl-skel');
    box.setAttribute('aria-hidden', 'true');
    var stats = el('div', 'wl-skel__stats');
    for (var i = 0; i < 4; i++) stats.appendChild(el('span', 'skel wl-skel__stat'));
    box.appendChild(stats);
    box.appendChild(el('span', 'skel skel--line wl-skel__summary'));
    box.appendChild(el('span', 'skel skel--pill wl-skel__bar'));
    var rows = el('div', 'wl-skel__rows');
    for (var j = 0; j < 8; j++) {
      var row = el('div', 'wl-skel__row');
      row.appendChild(el('span', 'skel skel--poster'));
      row.appendChild(el('span', 'skel skel--line'));
      rows.appendChild(row);
    }
    box.appendChild(rows);
    return box;
  }

  function showFailure(state) {
    var r = root();
    if (r) {
      r.removeAttribute('aria-busy');
      window.UIFeedback.error("Couldn't load the list. Try again.");
      return;
    }
    var note = el('p', 'watch-notice');
    note.setAttribute('role', 'status');
    note.appendChild(el('span', null, "Couldn't load the list."));
    var retry = el('button', 'watch-notice__retry', 'Try again');
    retry.type = 'button';
    retry.addEventListener('click', function () { load(state, {}); });
    note.appendChild(retry);
    list.replaceChildren(note);
    list.removeAttribute('aria-busy');
  }

  /* Show the list for *state*. opts.history: 'push' or 'replace' to move
     the address along; opts.focus: what to focus once it is in. */
  function load(state, opts) {
    var request = ++_request;
    var r = root();
    if (r) {
      r.setAttribute('aria-busy', 'true');
    } else {
      list.replaceChildren(skeleton());
      list.setAttribute('aria-busy', 'true');
    }
    if (opts.history === 'push') window.history.pushState({ view: 'list' }, '', pageUrl(state));
    else if (opts.history === 'replace') window.history.replaceState({ view: 'list' }, '', pageUrl(state));
    return fetchList(state).then(function (fresh) {
      if (request !== _request) return;
      _stale = false;
      apply(fresh, opts.focus);
      if (opts.toTop) toTop();
    }, function () {
      if (request === _request) showFailure(state);
    });
  }

  /* After paging from the bottom, bring the top of the list back into view. */
  function toTop() {
    var r = root();
    if (!r) return;
    var area = core.clearArea();
    var top = r.getBoundingClientRect().top;
    if (top < area.top) {
      window.scrollBy({ top: top - area.top - 8, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
    }
  }

  /* ── Browse / List ── */

  function fadeOut(nodes, then) {
    var token = ++_switch;
    var shown = nodes.filter(function (n) { return n && !n.hidden; });
    if (core.reduceMotion() || !shown.length || !shown[0].animate) {
      shown.forEach(function (n) { n.hidden = true; });
      then();
      return;
    }
    var opts = { duration: core.tokenMs('--dur-fast', 150), easing: core.token('--ease-out', 'ease-out'), fill: 'forwards' };
    var anims = shown.map(function (n) { return n.animate([{ opacity: 1 }, { opacity: 0 }], opts); });
    anims[0].onfinish = function () {
      if (token !== _switch) return;
      shown.forEach(function (n) { n.hidden = true; });
      anims.forEach(function (a) { a.cancel(); });
      then();
    };
  }

  function reveal(node) {
    node.hidden = false;
    if (!core.reduceMotion()) core.fadeIn(node);
  }

  function markToggle(view) {
    toArray(document.querySelectorAll('#watch-viewtoggle [data-view]')).forEach(function (link) {
      if (link.getAttribute('data-view') === view) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if (search) {
      search.setAttribute('placeholder', PLACEHOLDER[view]);
      search.setAttribute('aria-label', PLACEHOLDER[view]);
    }
  }

  /* opts.history: 'push' when the switch was a click, null when the
     address already says where to go (Back and Forward). */
  function setView(view, opts) {
    var state = opts.state;
    if (view === _view) {
      if (view === 'list' && state && !same(state, stateOf(root()))) load(state, { history: opts.history });
      return;
    }
    _view = view;
    markToggle(view);
    if (view === 'list') {
      var current = state || norm({ q: search ? search.value : '' });
      if (opts.history === 'push') window.history.pushState({ view: 'list' }, '', pageUrl(current));
      fadeOut([browse, results], function () { reveal(list); });
      if (!root() || _stale || !same(current, stateOf(root()))) load(current, {});
    } else {
      if (opts.history === 'push') window.history.pushState({ view: 'rows' }, '', '/watch');
      /* With text in the field the search shows its results instead. */
      fadeOut([list], function () { if (!results || results.hidden) reveal(browse); });
    }
    document.dispatchEvent(new CustomEvent('mm:watch-view', { detail: { view: view }, bubbles: true }));
  }

  function plainClick(e) {
    return !(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
  }

  /* ── Rows ── */

  function rows() {
    var r = root();
    return r ? toArray(r.querySelectorAll('tr[data-row-key]')) : [];
  }

  function rowByKey(key, within) {
    var all = within ? toArray(within.querySelectorAll('tr[data-row-key]')) : rows();
    return all.filter(function (row) { return row.getAttribute('data-row-key') === key; })[0] || null;
  }

  function keyFor(detail) {
    return detail.showRk ? 'show:' + detail.showRk : 'id:' + detail.id;
  }

  function firstAction(row) {
    return row ? row.querySelector('.keep-trigger, .wl-actions .btn') : null;
  }

  function focusWasIn(row) {
    var active = document.activeElement;
    return !active || active === document.body || row.contains(active);
  }

  /* The row fades and slides away; the rows below then slide up into its
     place, and focus moves on to the next row's first action. */
  function leave(row) {
    if (!row.parentNode) return;
    var tbody = row.parentNode;
    var hadFocus = focusWasIn(row);
    var below = [];
    for (var n = row.nextElementSibling; n; n = n.nextElementSibling) below.push(n);
    var next = below[0] || row.previousElementSibling;
    function gone() {
      var play = core.flip(below);
      row.remove();
      play();
      if (hadFocus && next) core.focusSoftly(firstAction(next) || next);
      if (!tbody.querySelector('tr')) load(stateOf(root()), {});
    }
    if (core.reduceMotion()) { gone(); return; }
    row.classList.add('is-leaving');
    setTimeout(gone, core.tokenMs('--dur', 200));
  }

  /* Read the list again as it stands and bring the rows named by *keys*
     up to date where they are: changed in place, or gone. The stats, the
     summary and the pager follow. */
  function refresh(keys, opts) {
    var r = root();
    if (!r) return Promise.resolve();
    var state = stateOf(r);
    var request = _request;
    return fetchList(state).then(function (fresh) {
      var now = root();
      if (request !== _request || !now || !same(state, stateOf(now))) return;
      ['stats', 'summary'].forEach(function (name) {
        var old = part(now, name);
        var next = part(fresh, name);
        if (old && next) old.replaceWith(next);
      });
      var oldPager = now.querySelector('.pagination');
      var newPager = fresh.querySelector('.pagination');
      if (oldPager && newPager) oldPager.replaceWith(newPager);
      keys.forEach(function (key) {
        var row = rowByKey(key);
        if (!row) return;
        var next = rowByKey(key, fresh);
        if (!next) { leave(row); return; }
        var hadFocus = row.contains(document.activeElement);
        row.replaceWith(next);
        if (opts && opts.justKept) {
          var pill = next.querySelector('.keep-trigger');
          if (pill) pill.classList.add('is-just-kept');
        }
        if (hadFocus) {
          var target = firstAction(next);
          if (target) target.focus({ preventScroll: true });
        }
      });
    }, function () {
      window.UIFeedback.error("Couldn't refresh the list. Reload to see the change.");
    });
  }

  var _labels = new WeakMap();

  function busyButton(btn, on) {
    if (on) {
      if (!_labels.has(btn)) _labels.set(btn, toArray(btn.childNodes));
      var spin = el('span', 'btn-spinner');
      spin.setAttribute('aria-hidden', 'true');
      btn.replaceChildren(spin, document.createTextNode('Deleting…'));
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      return;
    }
    var label = _labels.get(btn);
    if (label) btn.replaceChildren.apply(btn, label);
    _labels.delete(btn);
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }

  function setBusy(row, on) {
    row.classList.toggle('is-busy', on);
    var del = row.querySelector('[data-action="confirm-delete"], [data-action="pick-season-delete"]');
    if (del) busyButton(del, on);
    var keepTrigger = row.querySelector('.keep-trigger');
    if (keepTrigger) keepTrigger.disabled = on;
  }

  document.addEventListener('mm:watch-admin', function (e) {
    var d = e.detail || {};
    if (_view !== 'list' || !root()) {
      _stale = true;
      return;
    }
    var key = keyFor(d);
    var row = rowByKey(key);
    if (d.phase === 'busy') {
      if (row) setBusy(row, true);
      return;
    }
    if (d.phase === 'failed') {
      if (row) setBusy(row, false);
      return;
    }
    if (d.action === 'delete' && !d.showRk) {
      if (row) leave(row);
      refresh([]);
      return;
    }
    refresh([key], { justKept: d.action === 'keep' });
  });

  /* ── Wiring ── */

  function navigate(href, find, toTopAfter) {
    var at = href.indexOf('?');
    load(stateFromSearch(at < 0 ? '' : href.slice(at + 1)), { history: 'push', focus: find, toTop: toTopAfter });
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var toggle = e.target.closest('#watch-viewtoggle [data-view]');
    if (toggle) {
      if (!plainClick(e)) return;
      e.preventDefault();
      var view = toggle.getAttribute('data-view') === 'list' ? 'list' : 'rows';
      setView(view, { history: 'push' });
      return;
    }
    if (!list.contains(e.target)) return;

    var keepBtn = e.target.closest('[data-keep-dur]');
    if (keepBtn) {
      e.preventDefault();
      admin.keep(keepBtn.closest('.keep-wrapper'), keepBtn.getAttribute('data-keep-dur'));
      return;
    }
    var dialogRemove = e.target.closest('[data-remove-keep-dialog]');
    if (dialogRemove) {
      e.preventDefault();
      /* The dialog's Remove all clears the show's and every season's keep. */
      admin.keep(dialogRemove.closest('.keep-wrapper'));
      return;
    }
    var removeBtn = e.target.closest('[data-remove-keep-id]');
    if (removeBtn) {
      e.preventDefault();
      var mid;
      try { mid = JSON.parse(removeBtn.getAttribute('data-remove-keep-id')); } catch (_) { return; }
      admin.removeKeep(removeBtn.closest('.keep-wrapper'), mid);
      return;
    }
    var deleteBtn = e.target.closest('[data-action="confirm-delete"]');
    if (deleteBtn) {
      e.preventDefault();
      var title;
      try { title = JSON.parse(deleteBtn.getAttribute('data-title')); } catch (_) { title = ''; }
      admin.confirmDelete(deleteBtn.getAttribute('data-id'), title, {
        kind: deleteBtn.getAttribute('data-kind') || 'movie',
        size: deleteBtn.getAttribute('data-size') || '',
        showRk: deleteBtn.getAttribute('data-show-rk') || ''
      });
      return;
    }
    var showDeleteBtn = e.target.closest('[data-action="pick-season-delete"]');
    if (showDeleteBtn) {
      e.preventDefault();
      var showTitle;
      try { showTitle = JSON.parse(showDeleteBtn.getAttribute('data-title')); } catch (_) { showTitle = ''; }
      admin.pickSeasonDelete(showDeleteBtn.getAttribute('data-show-rk') || '', showTitle);
      return;
    }
    var clear = e.target.closest('[data-list-clear]');
    if (clear) {
      if (!plainClick(e)) return;
      e.preventDefault();
      if (search) search.value = '';
      var clearBtn = byId('watch-search-clear');
      if (clearBtn) clearBtn.hidden = true;
      load(norm({}), {
        history: 'push',
        focus: function (fresh) { return fresh.querySelector('.fpill'); }
      });
      return;
    }
    var nav = e.target.closest('a[data-list-nav]');
    if (nav) {
      if (!plainClick(e)) return;
      e.preventDefault();
      navigate(nav.getAttribute('href'), focusKey(nav), nav.classList.contains('page-btn'));
    }
  });

  document.addEventListener('change', function (e) {
    var select = e.target;
    if (!select || !select.hasAttribute || !select.hasAttribute('data-list-sort') || !list.contains(select)) return;
    var state = stateOf(root());
    state.sort = select.value;
    state.page = '1';
    load(norm(state), { history: 'push', focus: function (fresh) { return fresh.querySelector('#watch-list-sort'); } });
  });

  /* The one search field at the top filters the list while it is showing. */
  document.addEventListener('mm:watch-list-search', function (e) {
    if (_view !== 'list') return;
    var state = stateOf(root());
    var q = String((e.detail && e.detail.q) || '').trim();
    if (q === state.q) return;
    state.q = q;
    state.page = '1';
    load(norm(state), { history: 'replace' });
  });

  window.addEventListener('popstate', function () {
    var params = new URLSearchParams(window.location.search);
    if (params.get('view') === 'list') setView('list', { history: null, state: stateFromSearch(window.location.search) });
    else setView('rows', { history: null });
  });
})();
