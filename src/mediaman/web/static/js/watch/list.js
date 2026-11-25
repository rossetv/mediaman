/* Watch: the admin list view. Switches between Browse and List in place,
 * and pages, filters, sorts and searches the list by swapping the server's
 * /watch/list fragment in, with the address kept in step (watch/address.js) so Back works.
 * Row actions go through MMWatch.admin; when one is done the row changes
 * where it is, or leaves and the rows below slide up. Needs watch/core.js,
 * watch/address.js and watch/admin.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var admin = window.MMWatch.admin;
  var address = window.MMWatch.address;
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
  var _drawn = 0;     // how many of them have been drawn
  var _asked = null;  // the state that fetch asks for, until its reply is in or fails
  var _again = false; // what is scheduled changed while a list was loading
  var _stale = false; // the library changed and the list could not show it, so it is read again when next shown
  var _working = Object.create(null); // the admin actions still running, by the key of their row

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

  /* What the list will show once the fetch in flight is in: the base for the next change. */
  function wanted() { return norm(_asked || stateOf(root())); }

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
      fresh = document.importNode(fresh, true);
      Object.keys(_working).forEach(function (key) {
        var row = rowByKey(key, fresh);
        if (row) showWorking(row);
      });
      return fresh;
    });
  }

  /* A part of the list on view: results still fading out of a swap are inert, and passed over. */
  function part(r, name) {
    if (!r) return null;
    return toArray(r.querySelectorAll('[data-list-part="' + name + '"]')).filter(function (n) {
      return !n.hasAttribute('inert');
    })[0] || null;
  }

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
    var stat = node.getAttribute('data-stat');
    if (stat) return function (fresh) { return fresh.querySelector('.wl-stat[data-stat="' + stat + '"]'); };
    var col = node.getAttribute('data-sort-col');
    if (col) return function (fresh) { return fresh.querySelector('.th-sort[data-sort-col="' + col + '"]'); };
    if (node.classList.contains('page-btn')) {
      // A phone shows only the two arrows: the same one, or the other at either end of the pages.
      var label = node.getAttribute('aria-label');
      return function (fresh) {
        var arrows = toArray(fresh.querySelectorAll('a.page-btn')).filter(function (a) { return a.hasAttribute('aria-label'); });
        return label && (arrows.filter(function (a) { return a.getAttribute('aria-label') === label; })[0] || arrows[0]) ||
          fresh.querySelector('.page-btn.active');
      };
    }
    return null;
  }

  /* Put *fresh* in place of the list now showing: the stats, summary and
     toolbar change where they are, and the rows cross-fade. */
  function apply(fresh, find) {
    // Found before the parts move, so never in the rows fading out.
    var target = find && find(fresh);
    var r = root();
    if (!r) {
      var skeleton = list.firstElementChild;
      if (skeleton) core.swap(skeleton, fresh);
      else list.appendChild(fresh);
      list.removeAttribute('aria-busy');
      if (target) target.focus({ preventScroll: true });
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
    if (target) target.focus({ preventScroll: true });
  }

  function skeleton() {
    var box = el('div', 'wl-skel');
    box.setAttribute('aria-hidden', 'true');
    var stats = el('div', 'wl-skel__stats');
    for (var i = 0; i < 4; i++) stats.appendChild(el('span', 'skel wl-skel__stat'));
    box.appendChild(stats);
    box.appendChild(el('span', 'skel skel--line wl-skel__summary'));
    box.appendChild(el('span', 'skel skel--pill wl-skel__bar'));
    box.appendChild(el('span', 'skel skel--pill wl-skel__bar wl-skel__bar--sort'));
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
      /* The list, the pills and the address still show the old state; so must the select. */
      var select = r.querySelector('[data-list-sort]');
      if (select) select.value = stateOf(r).sort;
      if (_view === 'list') window.UIFeedback.error("Couldn't load the list. Try again.");
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

  /* What the list now shows, for a screen reader. */
  var status = el('p', 'sr-only');
  status.setAttribute('role', 'status');
  list.parentNode.insertBefore(status, list.nextSibling);

  /* Show the list for *state*. opts.quiet: nobody asked for it, so it is not told.
     opts.told: nor is its failure, the one before it was.
     opts.history: 'push' or 'replace' to move
     the address along once the list is in, so a failed load leaves it as
     it was; opts.focus: what to focus once it is in. */
  function load(state, opts) {
    var request = ++_request;
    _asked = state;
    var r = root();
    if (r) {
      r.setAttribute('aria-busy', 'true');
    } else {
      list.replaceChildren(skeleton());
      list.setAttribute('aria-busy', 'true');
    }
    return fetchList(state).then(function (fresh) {
      if (request !== _request) return;
      _asked = null;
      _drawn++;
      _stale = false;
      // Left for the rows meanwhile: the address is theirs.
      if (opts.history && _view === 'list') address[opts.history]('list', pageUrl(state));
      apply(fresh, opts.focus);
      if (opts.toTop) toTop();
      if (!opts.quiet && _view === 'list') status.textContent = fresh.dataset.said || '';
      if (_again) reload();
    }, function () {
      if (request !== _request) return;
      _asked = null;
      if (!opts.told) showFailure(state);
      else if (root()) root().removeAttribute('aria-busy');
      if (!_again) return;
      if (_view === 'list' && root()) { reload({ told: true }); return; }
      _again = false;
      _stale = true;
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

  var _leaving = []; // what ends the fade of each view on its way out

  function endLeaving() {
    _leaving.splice(0).forEach(function (end) { end(); });
  }

  /* Take *node* out of the flow where it stands. Its margins no longer collapse into the page's,
     so its content is measured, not itself. */
  function lift(node) {
    var first = node.firstElementChild || node;
    function at() { return first.getBoundingClientRect().top - node.parentNode.getBoundingClientRect().top; }
    var was = at();
    Object.assign(node.style, { position: 'absolute', top: '0px', left: '0px', right: '0px' });
    node.style.top = was - at() + 'px';
  }

  /* Give way to what *then* shows: *nodes* fade out over it while it fades in, so the page is
     never empty between them. Out of the flow first, they neither move nor push what comes. */
  function giveWay(nodes, then) {
    // Out of the flow they leave the page without what stands below them, which can be too
    // short for where it is scrolled.
    var y = window.scrollY;
    var tall = document.documentElement.scrollHeight;
    endLeaving();
    var lifted = [];
    nodes.forEach(function (n) {
      if (!n || n.hidden) return;
      if (core.reduceMotion() || !n.animate) { n.hidden = true; return; }
      lift(n);
      lifted.push(n);
      n.setAttribute('inert', '');
      var fade = n.animate([{ opacity: 1 }, { opacity: 0 }],
        { duration: core.tokenMs('--dur-fast', 150), easing: core.token('--ease-out', 'ease-out'), fill: 'forwards' });
      function end() {
        fade.onfinish = null;
        n.hidden = true;
        n.removeAttribute('inert');
        Object.assign(n.style, { position: '', top: '', left: '', right: '', paddingBottom: '' });
        fade.cancel();
      }
      fade.onfinish = function () {
        _leaving.splice(_leaving.indexOf(end), 1);
        end();
      };
      _leaving.push(end);
    });
    var lost = tall - document.documentElement.scrollHeight;
    if (lost > 0 && lifted.length) {
      lifted.forEach(function (n) { n.style.paddingBottom = parseFloat(getComputedStyle(n).paddingBottom) + lost + 'px'; });
      window.scrollTo(0, y);
    }
    then();
    // Where the page went to its top, they stay where they were on the screen.
    if (window.scrollY !== y) lifted.forEach(function (n) { n.style.top = parseFloat(n.style.top) + window.scrollY - y + 'px'; });
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
    // What a row has asked or opened must not outlive it, over a page that would have the focus.
    if (opts.history === null && window.ModalA11y) window.ModalA11y.closeFrom(list);
    var state = opts.state;
    if (view === _view) {
      if (view === 'list' && state && !same(state, wanted())) load(state, { history: opts.history, focus: sameControl });
      if (view === 'list' && state) sayText(state.q)();
      return;
    }
    _view = view;
    status.textContent = '';
    // Text typed in the list stays there when Back leaves it.
    if (view === 'rows' && opts.history === null) sayText('')();
    var said = view === 'list' && state ? sayText(state.q) : null;
    markToggle(view);
    var current = state || norm({ q: search ? search.value : '' });
    var rowsOff = browse.hidden;
    if (opts.history === 'push') address.push(view === 'list' ? 'list' : null, view === 'list' ? pageUrl(current) : '/watch');
    if (view === 'list' && (!root() || _stale || !same(current, wanted()))) load(current, {});
    // Leaving a category's page brings the rows back: they were not on view, so they do not fade.
    if (view === 'list' && rowsOff) browse.hidden = true;
    giveWay([view === 'list' ? browse : list], function () {
      // The search puts its results away or shows them first.
      document.dispatchEvent(new CustomEvent('mm:watch-view', { detail: { view: view }, bubbles: true }));
      if (said) said();
      if (view === 'list') reveal(list);
      else showRows();
    });
  }

  /* With text in the field the search shows its results instead. Back may lead to a row's grid,
     which opens once the list has closed. */
  function showRows() {
    if (results && !results.hidden) reveal(results);
    Promise.resolve().then(function () {
      if (_view === 'rows' && (!results || results.hidden) && address.value('grid') === null) reveal(browse);
    });
  }

  function plainClick(e) {
    return !(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
  }

  /* The search field says what the address searches for. Its input event brings the rest of
     the bar (its clear button, a search still waiting to be sent) into line; it is for the
     list only, the rows would search. */
  function sayText(q) {
    if (!search || search.value.trim() === q) return function () {};
    search.value = q;
    return function () { search.dispatchEvent(new Event('input', { bubbles: true })); };
  }

  /* ── Rows ── */

  function rows() {
    var block = part(root(), 'results');
    return block ? toArray(block.querySelectorAll('tr[data-row-key]')) : [];
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

  function deleteButton(row) {
    return row.querySelector('[data-action="confirm-delete"], [data-action="pick-season-delete"]');
  }

  /* The control an action starts from: Delete for a delete, Keep otherwise. */
  function actionButton(row, action) {
    return (action === 'delete' ? deleteButton(row) : row.querySelector('.keep-trigger')) || firstAction(row);
  }

  /* Where the focus goes in a row: to what *action* starts from, or the title while that is held. */
  function focusTarget(row, action) {
    var btn = actionButton(row, action);
    if (btn && !btn.disabled) return btn;
    var title = row.querySelector('a.wl-title');
    if (!title) row.tabIndex = -1;
    return title || row;
  }

  /* A dialog opened from *row* gives the focus back to *to*. */
  function handOn(row, to) {
    if (window.ModalA11y) window.ModalA11y.handOn(function (from) { return row.contains(from) ? to : null; });
  }

  /* Nothing has the focus, or a toast whose button was pressed, which takes it along as it
     goes. Not while Details or a dialog is open over the list: the focus is theirs. */
  function focusIsFree() {
    var active = document.activeElement;
    if (document.documentElement.classList.contains('has-ttl') || document.querySelector('.wl-dialog.active')) return false;
    return !active || active === document.body || !!active.closest('.ui-toast.is-leaving');
  }

  function focusWasIn(row) {
    return row.contains(document.activeElement) || focusIsFree();
  }

  /* Focus is in the row, or was until the row went busy and disabled the
     focused button (the browser then drops focus to the page). */
  function focusBelongsTo(row) {
    return row.contains(document.activeElement) || (row.classList.contains('is-busy') && focusIsFree());
  }

  /* The focus of an Undo whose row is not on this page: to the first row in sight, or the
     filter that shows none. */
  function focusNearby() {
    if (_view !== 'list' || !root() || !focusIsFree()) return;
    var top = core.clearArea().top;
    var row = rows().filter(function (n) {
      return !n.matches('.is-busy, .is-leaving') && n.getBoundingClientRect().top >= top;
    })[0];
    var target = firstAction(row) || root().querySelector('.fpill.on');
    if (target) target.focus({ preventScroll: true });
  }

  function parseJson(value) {
    try { return JSON.parse(value); } catch (_) { return ''; }
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
      if (next) handOn(row, focusTarget(next));
      if (hadFocus && next) core.focusSoftly(focusTarget(next));
      if (!tbody.querySelector('tr') && !_asked) load(stateOf(root()), {});
    }
    if (core.reduceMotion()) { gone(); return; }
    row.classList.add('is-leaving');
    setTimeout(gone, core.tokenMs('--dur', 200));
  }

  /* Put the row as it is now in place of the one showing. */
  function replaceRow(row, next, action) {
    var hadFocus = focusBelongsTo(row);
    var target = focusTarget(next, action);
    row.replaceWith(next);
    handOn(row, target);
    if (action === 'keep') {
      var pill = next.querySelector('.keep-trigger');
      if (pill) pill.classList.add('is-just-kept');
    }
    if (hadFocus) target.focus({ preventScroll: true });
  }

  /* A keep can take a row off this page (kept rows sort last by deletion
     date, and leave the Stale filter). It stays where it is, changed, until
     the list is paged, filtered or sorted, so its Undo still has a row: it
     is found again by its title in the whole library. */
  function rowElsewhere(row) {
    var w = row.querySelector('.keep-wrapper');
    if (!w) return Promise.resolve(null);
    var title = w.getAttribute('data-tv') === 'true' ? w.getAttribute('data-show-title') : parseJson(w.getAttribute('data-title'));
    if (!title) return Promise.resolve(null);
    return fetchList(norm({ q: title, per_page: '100' })).then(function (fresh) {
      return rowByKey(row.getAttribute('data-row-key'), fresh);
    }, function () { return null; });
  }

  /* After a delete the page is one row short: the server's page brings in
     the rows from the next page that now belong on this one. */
  function refill(now, fresh) {
    var block = part(now, 'results');
    var tbody = block && block.querySelector('tbody');
    if (!tbody) return;
    toArray(fresh.querySelectorAll('tr[data-row-key]')).forEach(function (row) {
      if (rowByKey(row.getAttribute('data-row-key'))) return;
      tbody.appendChild(row);
      if (!core.reduceMotion()) core.fadeIn(row);
    });
  }

  function replacePager(now, fresh) {
    var block = part(now, 'results');
    if (!block) return;
    var oldPager = block.querySelector('.pagination');
    var newPager = fresh.querySelector('.pagination');
    if (oldPager && newPager) oldPager.replaceWith(newPager);
    else if (oldPager) oldPager.remove();
    else if (newPager) block.appendChild(newPager);
  }

  /* Read the list again as it stands after the action *d* and bring the rows named by *keys*
     up to date where they are: changed in place, or, after a delete, gone.
     The stats, the summary and the pager follow. */
  function refresh(keys, d) {
    var action = d.action;
    var r = root();
    if (!r) return Promise.resolve();
    var state = stateOf(r);
    var drawn = _drawn;
    // Not once another list was drawn: that one is as new.
    function current() { return drawn === _drawn ? root() : null; }
    return fetchList(state).then(function (fresh) {
      var now = current();
      if (!now) return;
      ['stats', 'summary'].forEach(function (name) {
        var old = part(now, name);
        var next = part(fresh, name);
        if (old && next) old.replaceWith(next);
      });
      replacePager(now, fresh);
      keys.forEach(function (key) {
        var row = rowByKey(key);
        if (!row) return;
        var next = rowByKey(key, fresh);
        if (next) { replaceRow(row, next, action); return; }
        if (action === 'delete') { leave(row); return; }
        rowElsewhere(row).then(function (found) {
          if (!current() || !row.parentNode) return;
          if (found) { replaceRow(row, found, action); return; }
          _stale = true;
          letGo(row, d);
        });
      });
      /* A page the server moved back (its last row went) is loaded whole once the row has left. */
      if (action === 'delete' && same(state, stateOf(fresh))) refill(now, fresh);
    }, function () {
      _stale = true;
      keys.forEach(function (key) {
        var row = rowByKey(key);
        if (row) letGo(row, d);
      });
      if (_view === 'list') window.UIFeedback.error("Couldn't refresh the list. Reload to see the change.");
    });
  }

  /* The change is saved but the row as it now is could not be read: let go
     of it, showing a saved keep as kept, and give focus back to the control. */
  function letGo(row, d) {
    var refocus = focusBelongsTo(row);
    showWorking(row);
    var w = row.querySelector('.keep-wrapper');
    if (w && d.keep !== undefined) w.setAttribute('data-keep', JSON.stringify(d.keep));
    if (_working[row.getAttribute('data-row-key')]) return;
    if (d.action === 'keep') markKept(row);
    var btn = focusTarget(row, d.action);
    handOn(row, btn);
    if (refocus) btn.focus({ preventScroll: true });
  }

  /* The row as the server draws a kept one: a Kept pill, Kept for its
     deletion and no Delete. */
  function markKept(row) {
    row.classList.add('wl-row--kept');
    var w = row.querySelector('.keep-wrapper');
    if (w) w.setAttribute('data-kept', 'true');
    var trigger = row.querySelector('.keep-trigger');
    if (trigger) {
      var label = trigger.querySelector('span');
      if (label) label.textContent = 'Kept';
      if (!trigger.querySelector('.keep-trigger__tick')) {
        trigger.insertBefore(MM.dom.icon('i-check', 'keep-trigger__tick'), trigger.firstChild);
      }
      trigger.classList.add('keep-trigger--kept', 'is-just-kept');
      var title = w ? parseJson(w.getAttribute('data-title')) : '';
      if (title) trigger.setAttribute('aria-label', 'Manage keep on ' + title);
    }
    var del = deleteButton(row);
    if (del) del.remove();
    var when = row.querySelector('.wl-del');
    if (when) when.parentNode.replaceChildren(el('span', 'wl-del wl-del--kept', 'Kept'));
  }

  var _labels = new WeakMap();

  var WORKING = { keep: 'Keeping…', show: 'Keeping…', unkeep: 'Removing…', delete: 'Deleting…' };

  function busyButton(btn, on, text) {
    if (on) {
      if (!_labels.has(btn)) _labels.set(btn, toArray(btn.childNodes));
      var spin = el('span', 'btn-spinner');
      spin.setAttribute('aria-hidden', 'true');
      btn.replaceChildren(spin, document.createTextNode(text));
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

  /* Draw the row as busy. The focus of a control that is held now, or of the Undo, waits on its title. */
  function hold(row) {
    var mine = _view === 'list' && focusWasIn(row);
    showWorking(row);
    var to = focusTarget(row);
    handOn(row, to);
    // Not before whatever else answers this has moved it.
    if (mine) Promise.resolve().then(function () {
      var active = document.activeElement;
      if (focusIsFree() || (row.contains(active) && active.disabled)) to.focus({ preventScroll: true });
    });
  }

  /* Draw the row for the actions still running on it: the control the first of them started
     from says what it is doing and the row's other one is held; with none, both are let go. */
  function showWorking(row) {
    var running = _working[row.getAttribute('data-row-key')];
    var main = running && actionButton(row, running[0]);
    row.classList.toggle('is-busy', !!running);
    [row.querySelector('.keep-trigger'), deleteButton(row)].forEach(function (btn) {
      if (!btn) return;
      busyButton(btn, false);
      if (btn === main) busyButton(btn, true, WORKING[running[0]] || 'Saving…');
      else btn.disabled = !!running;
    });
  }

  /* Note that an action on a row starts or ends; true while another one on it still runs. */
  function working(key, d) {
    var running = _working[key] || [];
    if (d.phase === 'busy') running.push(d.action);
    else running.splice(Math.max(0, running.indexOf(d.action)), 1);
    if (running.length) _working[key] = running;
    else delete _working[key];
    return running.length > 0;
  }

  document.addEventListener('mm:watch-admin', function (e) {
    var d = e.detail || {};
    var key = keyFor(d);
    var more = working(key, d);
    var row = rowByKey(key);
    // The list on its way may have been read before this was saved.
    if (d.phase === 'done' && _asked) _again = true;
    if (d.phase === 'done' && (_view !== 'list' || !root())) {
      _stale = true;
      if (row) showWorking(row);
      return;
    }
    if (d.phase === 'busy') {
      if (row) hold(row);
      else if (d.action === 'unkeep') focusNearby();
      return;
    }
    if (d.phase === 'failed') {
      if (!row) return;
      var refocus = _view === 'list' && !more && focusBelongsTo(row);
      showWorking(row);
      var btn = focusTarget(row, d.action);
      if (!more) handOn(row, btn);
      if (refocus) btn.focus({ preventScroll: true });
      return;
    }
    if (d.action === 'delete' && !d.showRk) {
      if (row) leave(row);
      refresh([], d);
      return;
    }
    refresh([key], d);
  });

  /* The same control in the list coming in, of the row the focus is in, or else the filter that is on. */
  function sameControl(fresh) {
    var active = document.activeElement;
    if (!list.contains(active)) return null;
    var find = focusKey(active);
    var row = active.closest('tr[data-row-key]');
    var next = row && (rowByKey(row.getAttribute('data-row-key'), fresh) || fresh.querySelector('tr[data-row-key]'));
    return (find ? find(fresh) : next && focusTarget(next, active === deleteButton(row) ? 'delete' : null)) ||
      fresh.querySelector('.fpill.on');
  }

  /* The list as it is now, its totals and dates too. */
  function reload(opts) {
    _again = false;
    _stale = true; // until it is in, or the next one that is
    load(stateOf(root()), Object.assign({ focus: sameControl, quiet: true }, opts));
  }

  document.addEventListener('mm:watch-scheduled', function () {
    if (_asked) _again = true;
    if (_view !== 'list' || !root()) _stale = true;
    else if (!_asked) reload();
  });

  /* ── Wiring ── */

  /* A pill, tile, header or page link, whose address is for the list showing.
     While another list loads it changes only its own part of that one; a page
     means nothing in a list of other titles or another order, so it is left
     to load. */
  function follow(nav) {
    var href = nav.getAttribute('href');
    var at = href.indexOf('?');
    var state = stateFromSearch(at < 0 ? '' : href.slice(at + 1));
    var paging = nav.classList.contains('page-btn');
    if (_asked) {
      var base = wanted();
      if (paging) {
        base.page = state.page;
        if (!same(base, state)) return;
      } else {
        var key = nav.hasAttribute('data-sort-col') ? 'sort' : 'type';
        base[key] = state[key];
        base.page = '1';
        state = base;
      }
    }
    load(state, { history: 'push', focus: focusKey(nav), toTop: paging });
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
      deleteBtn.focus({ preventScroll: true }); // as Safari does not on a click, for when the confirm closes
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
      showDeleteBtn.focus({ preventScroll: true }); // as Safari does not on a click, for when the dialog closes
      var showRow = showDeleteBtn.closest('tr');
      var showKeep = showRow && showRow.querySelector('.keep-wrapper');
      admin.pickSeasonDelete(showDeleteBtn.getAttribute('data-show-rk') || '', showTitle,
        Number(showKeep && showKeep.getAttribute('data-seasons')) || 0);
      return;
    }
    var clear = e.target.closest('[data-list-clear]');
    if (clear) {
      if (!plainClick(e)) return;
      e.preventDefault();
      if (search) search.value = '';
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
      follow(nav);
    }
  });

  document.addEventListener('change', function (e) {
    var select = e.target;
    if (!select || !select.hasAttribute || !select.hasAttribute('data-list-sort') || !list.contains(select)) return;
    var state = wanted();
    state.sort = select.value;
    state.page = '1';
    load(norm(state), { history: 'push', focus: function (fresh) { return fresh.querySelector('#watch-list-sort'); } });
  });

  /* The one search field at the top filters the list while it is showing. */
  document.addEventListener('mm:watch-list-search', function (e) {
    if (_view !== 'list') return;
    var state = wanted();
    var q = String((e.detail && e.detail.q) || '').trim();
    if (q === state.q) return;
    state.q = q;
    state.page = '1';
    load(norm(state), { history: 'replace' });
  });

  address.layer({
    name: 'list',
    depth: 5,
    params: ['view'].concat(KEYS),
    read: function (here) { return here.params.get('view') === 'list' ? query(stateFromSearch(here.params)) : null; },
    open: function (value) { setView('list', { history: null, state: stateFromSearch(value) }); },
    close: function () { setView('rows', { history: null }); }
  });
})();
