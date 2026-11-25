/* Watch: the storage meter and its panel, for admins: what fills the disks, what
 * is scheduled for deletion, what went, and Scan now. Asked for once the page has
 * loaded, so a slow disk never holds it up. Needs watch/core.js, watch/address.js
 * and watch/keep.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var byId = core.byId;
  var el = core.el;
  var toArray = core.toArray;
  var MM = window.MM;

  var root = byId('space');
  if (!root) return;
  var panel = root.querySelector('.space__panel');
  var panes = { storage: byId('space-storage'), scheduled: byId('space-scheduled'), deleted: byId('space-deleted') };
  var TABS = ['storage', 'scheduled', 'deleted'];
  var foot = root.querySelector('.space__foot');
  var ink = root.querySelector('.space__ink');
  var live = byId('space-live');
  var scan = byId('space-scan');

  var TYPES = {
    movies: { name: 'Movies', unit: 'film', list: 'movie', cls: 'space-c--movies' },
    tv: { name: 'TV Shows', unit: 'show', list: 'tv', cls: 'space-c--tv' },
    anime: { name: 'Anime', unit: 'show', list: 'anime', cls: 'space-c--anime' },
    other: { name: 'Other', cls: 'space-c--other' },
    free: { name: 'Free', cls: 'space-c--free' }
  };
  var ORDER = ['movies', 'tv', 'anime', 'other', 'free'];
  var LISTED = ['movies', 'tv', 'anime'];
  var HEAVIEST = 8;
  var NEARLY_FULL = 95;
  var HOVER_MS = 110; // before the first segment opens
  var NEXT_MS = 70; // from one segment to the next
  var LEAVE_MS = 260; // after the pointer has left
  var INK_W = 100; // the width the tab's pill is scaled from

  var stats = null;
  var scheduled = null; // the rows, once loaded
  var deleted = null;
  var failed = {}; // what could not be loaded and is not known from before
  var heavy = {}; // by type: { state: 'loading' | 'ok' | 'failed', items, total, unsized }
  var heavyGen = 0;

  var isOpen = false;
  var tab = 'storage';
  var opener = null;
  var act = null; // the type the bar shows, if any
  var pinned = false;

  /* ── Small things ── */

  function add(node) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) node.append(arguments[i]);
    return node;
  }

  function button(cls, label, fn) {
    var b = el('button', cls);
    b.type = 'button';
    if (label) b.append(el('span', '', label));
    b.addEventListener('click', fn);
    return b;
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  /* The server's "8.0 TB" as "8 TB". */
  function trim(label) { return String(label).replace(/\.0 /, ' '); }

  /* The server's format_bytes, for a sum it does not send. */
  function size(n) {
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return trim((i && n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + units[i]);
  }

  /* What no scan has given a size yet takes some room all the same. */
  function known(bytes, label) { return bytes ? trim(label) : 'Size not known'; }

  /* Before what scheduled titles add up to, when a scan has yet to size one of them. *state*: of those alone. */
  function least(state) {
    var lacks = scheduled ? scheduled.some(function (item) {
      return !item.file_size_bytes && (!state || item.state === state);
    }) : stats.scheduled.unsized_count > 0;
    return lacks ? 'at least ' : '';
  }

  function reclaimable(bytes, label, state) { return bytes ? least(state) + trim(label) + ' to reclaim' : 'size not known'; }

  /* The server's "330 days ago" as "11 months ago". */
  function ago(label) {
    var m = /^(\d+) days ago$/.exec(label || '');
    if (!m) return label || '';
    var days = Number(m[1]);
    var by = days >= 365 ? [365, 'year'] : days >= 60 ? [30, 'month'] : days >= 14 ? [7, 'week'] : [1, 'day'];
    return plural(Math.floor(days / by[0]), by[1]) + ' ago';
  }

  function daysLeft(n) { return n < 0 ? 'Leaving any day' : n === 0 ? 'Leaving today' : n === 1 ? '1 day left' : n + ' days left'; }

  function withSeason(title, season) {
    if (season === null || season === undefined) return title;
    var name = season === 0 ? 'Specials' : 'Season ' + season;
    return /(season\s*\d+|\bs\d+|specials)$/i.test(title) ? title : title + ' · ' + name;
  }

  function poster(url) {
    var box = el('span', 'space-mini');
    var src = core.safePath(url);
    if (!src) return box;
    var img = el('img');
    img.alt = '';
    img.loading = 'lazy';
    img.setAttribute('data-broken-on-error', '');
    img.src = src;
    box.append(img);
    return box;
  }

  function posterOf(item) { return poster(item.plex_rating_key ? '/api/poster/' + encodeURIComponent(item.plex_rating_key) : ''); }

  function titleLink(node, ref, title) {
    if (!ref) return node;
    node.href = '/watch#title=' + encodeURIComponent(ref);
    node.setAttribute('data-title-ref', ref);
    node.setAttribute('data-title', title);
    return node;
  }

  var still = false; // while a drawing is only brought up to date

  function anim(node, frames, ms, fill) {
    if (still || core.reduceMotion() || !node.animate) return null;
    return node.animate(frames, { duration: ms, easing: core.token('--ease-out', 'ease-out'), fill: fill || 'none' });
  }

  function fast() { return core.tokenMs('--dur-fast', 150); }
  function dur() { return core.tokenMs('--dur', 200); }
  function slow() { return core.tokenMs('--dur-slow', 250); }

  function after(a, fn) {
    if (a) a.onfinish = a.oncancel = fn; else fn();
  }

  /* Put *node* in *box* over what it holds, which fades from where it is. */
  function swap(box, node, dx) {
    toArray(box.children).forEach(function (old) {
      if (old.inert) return;
      old.inert = true;
      var now = getComputedStyle(old);
      after(anim(old, [{ opacity: now.opacity, transform: now.transform },
        { opacity: 0, transform: 'translateX(' + (-dx || 0) + 'px)' }], fast(), 'forwards'),
      function () { old.remove(); });
    });
    box.append(node);
    anim(node, [{ opacity: 0, transform: 'translateX(' + (dx || 0) + 'px)' }, { opacity: 1, transform: 'none' }], dur());
  }

  /* ── What the server knows ── */

  /* Keeps in flight hold a refresh back until the last one settles, so a row
     whose request has not finished is not put back by an earlier answer. keepGen
     moves whenever a keep starts or ends, and refreshSeq with each refresh: an
     answer asked for before either moved is out of date. */
  var keepsInFlight = 0;
  var keepSaved = false; // one of them was
  var keepGen = 0;
  var refreshSeq = 0;
  var refreshWaiting = false;

  function get(url) {
    return MM.api.get(url).then(function (data) { return { data: data }; }, function () { return { error: true }; });
  }

  /* Ask again for everything that is, or will be, on show. Resolves true when
     the page shows the server's latest, false when a newer refresh does; rejects
     when it could not be had. */
  function refresh() {
    if (keepsInFlight > 0) { refreshWaiting = true; return Promise.resolve(false); }
    refreshWaiting = false;
    var seq = ++refreshSeq;
    var gen = keepGen;
    var lists = isOpen || scheduled !== null;
    return Promise.all([
      get('/api/dashboard/stats'),
      lists ? get('/api/dashboard/scheduled') : null,
      lists ? get('/api/dashboard/deleted') : null
    ]).then(function (got) {
      if (seq !== refreshSeq) return false;
      if (gen !== keepGen) return refresh();
      failed.stats = got[0].error && !stats;
      if (got[0].data) stats = got[0].data;
      if (got[1]) {
        failed.scheduled = got[1].error && !scheduled;
        if (got[1].data) scheduled = got[1].data.items;
      }
      if (got[2]) {
        failed.deleted = got[2].error && !deleted;
        if (got[2].data) deleted = got[2].data.items;
      }
      render();
      if (got[1] && got[1].data) followPage();
      if (lists) loadHeavy();
      if (got.some(function (g) { return g && g.error; })) throw new Error('refresh failed');
      return true;
    });
  }

  function refreshQuietly() { refresh().catch(function () {}); }

  /* The heaviest titles of each type that has any. The old list stands until
     the new one comes. */
  function loadHeavy(only) {
    if (!stats) return;
    var gen = only ? heavyGen : ++heavyGen;
    LISTED.forEach(function (k) {
      if (only && k !== only) return;
      if (!stats.storage.counts[k]) { delete heavy[k]; return; }
      if (!heavy[k] || heavy[k].state !== 'ok') heavy[k] = { state: 'loading' };
      MM.api.get('/api/library?type=' + TYPES[k].list + '&sort=size_desc&per_page=' + HEAVIEST).then(function (data) {
        if (gen !== heavyGen) return;
        var before = JSON.stringify(heavy[k].items);
        heavy[k] = {
          state: 'ok',
          items: data.items,
          total: data.total,
          unsized: (data.stats && data.stats.no_size && data.stats.no_size[k]) || 0
        };
        if (JSON.stringify(data.items) !== before) heavyCame(k);
      }, function () {
        if (gen !== heavyGen || heavy[k].state === 'ok') return;
        heavy[k] = { state: 'failed' };
        heavyCame(k);
      });
    });
  }

  function render() {
    renderMeters();
    renderRowLink();
    renderCount();
    if (!isOpen) return;
    foot.hidden = !stats && !!failed.stats;
    renderStorage();
    renderScheduled();
    renderDeleted();
  }

  /* Whether a scan would delete what is due. */
  function deletes() { return !stats.deletions_off && !stats.dry_run; }

  function leaving() { return stats ? stats.scheduled.leaving : 0; }

  /* The one disk whose threshold can be marked on the bar, as every library on it starts there. */
  function markedDisk() {
    var disks = stats.storage.disks;
    return disks.length === 1 && disks[0].readable && disks[0].threshold_pct !== null ? disks[0] : null;
  }

  function usedPct() {
    var s = stats.storage;
    return s.total_bytes ? s.used_bytes / s.total_bytes * 100 : 0;
  }

  /* 'bad', 'warn' or '', and what to say of it. One disk among several is named. */
  function pressure() {
    var pct = usedPct();
    var disks = stats.storage.disks;
    var over = disks.filter(function (d) { return d.over_threshold; });
    var limit = '';
    if (over.length) {
      limit = 'above your ' + (over.length > 1 ? 'limits' : over[0].threshold_pct + '% limit');
      if (disks.length > 1) limit += ' on ' + over.map(function (d) { return d.path; }).join(' and ');
    }
    return { level: pct >= NEARLY_FULL ? 'bad' : over.length ? 'warn' : '', limit: limit, pct: Math.round(pct) };
  }

  /* ── The meter ── */

  function meters() { return toArray(document.querySelectorAll('[data-space-open]')); }

  function renderMeters() {
    var busy = scan.classList.contains('is-busy');
    toArray(document.querySelectorAll('.space-meter')).forEach(function (b) {
      var base = 'space-meter' + (b.classList.contains('space-meter--phone') ? ' space-meter--phone' : '');
      if (!stats && !failed.stats) return;
      b.removeAttribute('aria-busy');
      b.textContent = '';
      b.className = base + (busy ? ' space-meter--scanning' : '');
      if (busy) b.setAttribute('aria-busy', 'true');
      if (!stats || !stats.storage.readable) {
        var text = !stats || !stats.show_storage ? 'Storage' : 'Can’t read storage';
        if (text !== 'Storage' || !stats) b.classList.add('space-meter--off');
        var words = el('span', 'space-meter__free', text === 'Storage' ? text : 'Can’t read');
        if (text !== 'Storage') words.append(el('span', 'space-meter__word', ' storage'));
        add(b, MM.dom.icon('i-hard-drive'), words);
        b.setAttribute('aria-label', !stats ? 'Storage, could not be loaded' + leavingLabel()
          : (text === 'Storage' ? text : 'Storage: the disks can’t be read') + marks(b) + leavingLabel());
        return;
      }
      var s = stats.storage;
      var p = pressure();
      if (p.level) b.classList.add('space-meter--' + p.level);
      var bar = el('span', 'space-meter__bar');
      var fill = el('span', 'space-meter__fill');
      fill.style.setProperty('--used', s.total_bytes ? s.used_bytes / s.total_bytes : 0);
      bar.append(fill);
      var free = el('span', 'space-meter__free', trim(s.free));
      free.append(el('span', 'space-meter__word', ' free'));
      add(b, bar, free);
      b.setAttribute('aria-label', 'Storage: ' + trim(s.free) + ' free of ' + trim(s.total) +
        (p.level === 'bad' ? ', nearly full' : '') + (p.limit ? ', ' + p.limit : '') +
        marks(b) + leavingLabel());
      if (leaving()) b.append(el('span', 'space-meter__leaving', leaving() + ' leaving'));
    });
  }

  /* The marks of a missing folder and of deletions being off, and their words. */
  function marks(b) {
    var said = '';
    if (stats.unavailable_roots.length) {
      b.append(MM.dom.icon('i-triangle-exclamation', 'space-meter__warn'));
      said += stats.unavailable_roots.length === 1 ? ', a folder is missing' : ', ' + stats.unavailable_roots.length + ' folders are missing';
    }
    if (stats.deletions_off) {
      b.append(MM.dom.icon('i-circle-info', 'space-meter__info'));
      said += ', deletions are off';
    }
    return said;
  }

  function leavingLabel() {
    return (scan.classList.contains('is-busy') ? ', scanning' : '') + (leaving() ? '. ' + plural(leaving(), 'title') + ' scheduled for deletion' : '');
  }

  /* Beside Leaving soon's heading. */
  function renderRowLink() {
    var row = leavingRow();
    var title = row && row.querySelector('.watch-row__title');
    if (!title || !stats) return;
    var link = row.querySelector('.space-row-link');
    if (!leaving()) { if (link) link.remove(); return; }
    if (!link) {
      var head = title.closest('.watch-row__head');
      if (!head) {
        head = el('div', 'watch-row__head');
        title.before(head);
        head.append(title);
      }
      link = el('button', 'space-row-link');
      link.type = 'button';
      link.setAttribute('data-space-open', 'scheduled');
      link.setAttribute('aria-haspopup', 'dialog');
      link.setAttribute('aria-controls', 'space');
      title.after(link);
    }
    link.textContent = '';
    var freed = reclaimable(stats.scheduled.leaving_bytes, stats.scheduled.leaving_size, 'leaving');
    add(link, document.createTextNode(leaving() + ' · ' + freed), MM.dom.icon('i-chevron-right'));
    link.setAttribute('aria-label', 'Manage scheduled deletions: ' + plural(leaving(), 'title') + ', ' + freed);
  }

  function renderCount() {
    var t = byId('space-tab-scheduled');
    t.querySelector('.space-tab__count').textContent = leaving() || '';
    t.setAttribute('aria-label', 'Scheduled for deletion' + (leaving() ? ', ' + plural(leaving(), 'title') : ''));
    placeInk(false);
  }

  /* ── Opening and closing ── */

  function hashOf(name) { return name === 'storage' ? 'storage' : 'storage-' + name; }

  function openPanel(name, from, byKey) {
    opener = from;
    show(name, true, byKey);
    watch.address.push('storage', watch.address.url({}, hashOf(name)));
  }

  function closePanel() { watch.address.close('storage'); }

  var hideTimer = null;

  /* The page is out of reach under the panel; its menus and toasts are not, so the
     panel is no aria-modal, which would hide them from a screen reader. */
  function holdPage() {
    MM.inertPage.hold(root);
    var html = document.documentElement;
    // The scrollbar goes with the page's scroll; its width is held so nothing shifts.
    var bar = window.innerWidth - html.clientWidth || parseFloat(html.style.paddingRight) || 0;
    if (bar > 0) document.body.style.paddingRight = bar + 'px';
    html.classList.add('has-space');
  }

  function releasePage() {
    MM.inertPage.release(root);
    document.documentElement.classList.remove('has-space');
    document.body.style.paddingRight = '';
  }

  function expanded(on) {
    meters().forEach(function (b) {
      if (b.classList.contains('space-meter')) b.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
  }

  /* *fresh*: as a first opening; not Back, which finds the panel as it was left.
     *byKey*: a key opened it, so the focus goes on to the tabs. */
  function show(name, fresh, byKey) {
    clearTimeout(hideTimer);
    watch.keep.closeMenu(false);
    isOpen = true;
    root.hidden = false;
    root.inert = false;
    root.classList.remove('is-under');
    panel.style.transform = '';
    releasePage();
    holdPage();
    expanded(true);
    setTab(name, false);
    loadingPanes();
    if (fresh) {
      act = null;
      pinned = false;
      drawn = '';
    }
    render();
    void panel.offsetWidth; // start the entrance from the closed look
    root.classList.add('is-open');
    placeInk(false);
    // Safari rings whatever a script focuses after a press it gave no focus to.
    if (fresh) (byKey ? tabButton(tab) : panel).focus({ preventScroll: true });
    if (name === 'storage' && !act) {
      toArray(panes.storage.querySelectorAll('.space-seg')).forEach(function (s, i) {
        anim(s, [{ opacity: 0, transform: 'scaleX(.6)' }, { opacity: 1, transform: 'none' }], dur() + i * 20);
      });
    }
    refreshQuietly();
  }

  /* *under*: something opened over it, and has the focus. */
  function hide(under) {
    if (!isOpen) return;
    isOpen = false;
    // What it asks is of the panel: it must not outlive it, over a page that has the focus.
    window.UIFeedback.dismissConfirm();
    clearTimeout(enterTimer);
    clearTimeout(leaveTimer);
    watch.keep.closeMenu(false);
    root.classList.remove('is-open');
    root.classList.toggle('is-under', !!under);
    panel.style.transform = '';
    releasePage();
    root.inert = true;
    expanded(false);
    hideTimer = setTimeout(function () { root.hidden = true; }, core.reduceMotion() ? 0 : slow());
    if (under) return;
    var to = opener && document.body.contains(opener) && opener.getClientRects().length ? opener
      : meters().filter(function (b) { return b.getClientRects().length; })[0];
    if (to) to.focus({ preventScroll: true });
  }

  watch.address.layer({
    name: 'storage',
    depth: 40,
    hash: true,
    read: function (here) {
      var m = /^storage(?:-(scheduled|deleted))?$/.exec(here.hash);
      return m ? m[1] || 'storage' : null;
    },
    open: function (name, how) {
      if (isOpen) { setTab(name, true); return; }
      // Back to the panel: address.js puts the focus where it was, if that is still there.
      if (how !== 'pop') opener = null;
      show(name, how !== 'pop');
      if (how === 'pop') {
        setTimeout(function () {
          if (isOpen && !root.contains(document.activeElement)) panel.focus({ preventScroll: true });
        }, 0);
      }
    },
    close: function (how) { hide(how === 'push'); }
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var from = e.target.closest('[data-space-open]');
    if (from) openPanel(from.getAttribute('data-space-open'), from, !e.detail);
    else if (isOpen && e.target.closest('[data-space-close]')) closePanel();
  });

  document.addEventListener('keydown', function (e) {
    if (!isOpen || e.key !== 'Escape' || e.defaultPrevented) return;
    if (watch.address.top() !== 'storage' || document.querySelector('.ui-confirm-overlay:not(.is-leaving)')) return;
    e.preventDefault();
    if (tab === 'storage' && act) collapse();
    else closePanel();
  });

  /* Phones: pull the sheet down by its handle. */
  (function () {
    var grab = root.querySelector('.space__grab');
    var from = null;
    var by = 0;
    var since = 0;
    grab.addEventListener('pointerdown', function (e) {
      from = e.clientY;
      since = e.timeStamp;
      by = 0;
      grab.setPointerCapture(e.pointerId);
      panel.classList.add('is-dragged');
    });
    grab.addEventListener('pointermove', function (e) {
      if (from === null) return;
      by = Math.max(0, e.clientY - from);
      panel.style.transform = 'translateY(' + by + 'px)';
    });
    function drop(e) {
      if (from === null) return;
      from = null;
      panel.classList.remove('is-dragged');
      void panel.offsetWidth;
      panel.style.transform = '';
      if (e.type === 'pointerup' && (by > 120 || (by > 30 && by / (e.timeStamp - since) > 0.6))) closePanel();
    }
    grab.addEventListener('pointerup', drop);
    grab.addEventListener('pointercancel', drop);
  })();

  /* ── Tabs ── */

  function tabButton(name) { return byId('space-tab-' + name); }

  function placeInk(move) {
    var t = tabButton(tab);
    if (!t.offsetWidth) return;
    var was = getComputedStyle(ink).transform;
    var to = 'translateX(' + t.offsetLeft + 'px) scaleX(' + t.offsetWidth / INK_W + ')';
    ink.style.transform = to;
    if (move) anim(ink, [{ transform: was }, { transform: to }], dur());
  }

  function setTab(name, focus) {
    var ahead = TABS.indexOf(name) > TABS.indexOf(tab) ? 1 : -1;
    var moved = name !== tab;
    TABS.forEach(function (k) {
      var on = k === name;
      tabButton(k).setAttribute('aria-selected', on ? 'true' : 'false');
      tabButton(k).tabIndex = on ? 0 : -1;
      panes[k].style.setProperty('--dir', on ? ahead : -ahead);
    });
    void panes[name].offsetWidth; // the pane comes from its own side
    TABS.forEach(function (k) {
      panes[k].classList.toggle('is-on', k === name);
      panes[k].inert = k !== name;
    });
    tab = name;
    placeInk(moved);
    if (focus) tabButton(name).focus({ preventScroll: true });
  }

  function chooseTab(name, focus) {
    setTab(name, focus);
    watch.address.replace('storage', watch.address.url({}, hashOf(name)));
  }

  root.querySelector('.space__tabs').addEventListener('click', function (e) {
    var t = e.target.closest('[data-space-tab]');
    if (t) chooseTab(t.getAttribute('data-space-tab'), false);
  });

  root.querySelector('.space__tabs').addEventListener('keydown', function (e) {
    var i = TABS.indexOf(tab);
    var to = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : null;
    if (to === null) return;
    e.preventDefault();
    chooseTab(TABS[(to + TABS.length) % TABS.length], true);
  });

  window.addEventListener('resize', function () { if (isOpen) placeInk(false); });

  /* ── States every tab has ── */

  function skel(cls) { return el('span', 'skel ' + cls); }

  /* Placeholders, in the panes that have nothing yet. */
  function loadingPanes() {
    TABS.forEach(function (k) {
      if (panes[k].firstChild) return;
      var box = el('div', 'space-skel');
      box.setAttribute('aria-busy', 'true');
      add(box, skel('space-skel__fig'), skel('space-skel__line'), skel('space-skel__bar'));
      for (var i = 0; i < 4; i++) box.append(skel('space-skel__row'));
      box.append(el('span', 'sr-only', 'Loading'));
      panes[k].append(box);
    });
  }

  function failure(text, retry) {
    var s = el('div', 'space-say');
    s.setAttribute('role', 'alert');
    var again = button('btn btn--secondary', 'Try again', retry);
    again.prepend(MM.dom.icon('i-rotate-right'));
    return add(s, el('b', '', text), el('span', '', 'Nothing has changed. Check the connection and try again.'), again);
  }

  function tryAgain(e) {
    var b = e.currentTarget;
    b.setAttribute('aria-disabled', 'true');
    function done() { b.removeAttribute('aria-disabled'); }
    refresh().then(done, done);
  }

  function notices() {
    var out = [];
    if (stats.unavailable_roots.length) {
      var warn = el('div', 'space-notice');
      warn.setAttribute('role', 'alert');
      var list = el('ul');
      stats.unavailable_roots.forEach(function (r) {
        add(list, add(el('li'), el('b', '', r.path), document.createTextNode(' is empty or not mounted: ' +
          plural(r.waiting, 'scheduled deletion') + (r.waiting === 1 ? ' is waiting' : ' are waiting'))));
      });
      out.push(add(warn, MM.dom.icon('i-triangle-exclamation'), list));
    }
    if (stats.deletions_off) {
      var info = el('div', 'space-notice space-notice--info');
      info.setAttribute('role', 'status');
      out.push(add(info, MM.dom.icon('i-circle-info'),
        el('p', '', 'Deletions are off: set MEDIAMAN_DELETE_ROOTS to the folders mediaman may delete from.')));
    }
    if (stats.dry_run) {
      var dry = el('div', 'space-notice space-notice--info');
      dry.setAttribute('role', 'status');
      out.push(add(dry, MM.dom.icon('i-circle-info'),
        el('p', '', 'Dry run is on: scans delete nothing until it is turned off.')));
    }
    return out;
  }

  function empty(icon, title, text, link) {
    var box = el('div', 'space-empty');
    box.tabIndex = -1;
    add(box, MM.dom.icon(icon), el('b', '', title), el('p', '', text));
    if (link) {
      var a = el('a', 'btn btn--secondary', 'Open Settings');
      a.href = '/settings';
      box.append(a);
    }
    return box;
  }

  function connectPlex() {
    return empty('i-plug', 'Connect Plex to get started', 'Add your Plex URL and token in Settings.', true);
  }

  /* Redraw a pane, its place and the focus in it kept: a control that went
     hands the focus to *spare()*. */
  function redraw(pane, draw, spare) {
    var top = pane.scrollTop;
    var active = document.activeElement;
    var key = pane.contains(active) ? focusKey(active) : null;
    pane.textContent = '';
    draw();
    pane.scrollTop = top;
    if (key === null) return;
    var same = key && pane.querySelector(key);
    var to = same || (spare && spare()) || tabButton(tab);
    if (to) to.focus({ preventScroll: true });
  }

  function focusKey(node) {
    var k = node.getAttribute('data-k');
    if (k) return '.' + node.classList[0] + '[data-k="' + k + '"]';
    return node.classList.contains('space-back') ? '.space-layer:not([inert]) .space-back' : '';
  }

  /* ── Storage ── */

  var Z = {}; // the parts of the drawing
  var drawn = ''; // what it was drawn from
  var enterTimer = null;
  var leaveTimer = null;
  var kind = ''; // what the stage shows: cats, heavy, load, fail, say or wait

  function bytesOf(k) {
    var s = stats.storage;
    return k === 'free' ? s.free_bytes : s[k + '_bytes'];
  }

  function labelOf(k) {
    var s = stats.storage;
    return trim(k === 'free' ? s.free : s[k + '_label']);
  }

  function pctOf(k) {
    var s = stats.storage;
    if (!s.readable || !s.total_bytes) return null;
    // Types that add up to more than is used are drawn to fit it: their shares are not real.
    if (k !== 'free' && s.movies_bytes + s.tv_bytes + s.anime_bytes > s.used_bytes) return null;
    return k === 'free' ? Math.round(s.free_bytes / s.total_bytes * 1000) / 10 : s[k + '_pct'];
  }

  function count(k) { return stats.storage.counts[k] || 0; }

  function none(k) { return !bytesOf(k) && !count(k); }

  function renderStorage() {
    var pane = panes.storage;
    if (!stats) {
      if (!failed.stats || drawn === 'failed') return;
      drawn = 'failed';
      redraw(pane, function () { pane.append(failure('Couldn’t load storage.', tryAgain)); });
      return;
    }
    // The labels, not the bytes: a download moves those all the time.
    var now = JSON.stringify([stats, least('leaving')], function (key, value) { return /_bytes$/.test(key) ? undefined : value; });
    if (now === drawn) return;
    drawn = now;
    redraw(pane, drawStorage);
  }

  function drawStorage() {
    var pane = panes.storage;
    var s = stats.storage;
    clearTimeout(enterTimer);
    clearTimeout(leaveTimer);
    notices().forEach(function (n) { pane.append(n); });
    if (!stats.show_storage) { pane.append(connectPlex()); act = null; return; }
    var body = el('div', 'space-body');
    pane.append(body);
    Z = { body: body, segs: [] };
    if (!s.readable) {
      var say = el('div', 'space-say');
      var again = button('btn btn--secondary', 'Try again', tryAgain);
      again.prepend(MM.dom.icon('i-rotate-right'));
      body.append(add(say, el('b', '', 'Can’t read the disks right now.'),
        el('span', '', 'The library sizes below come from the last scan. Free space is unknown until the disks answer.'), again));
    } else {
      Z.fig = el('div', 'space-fig');
      Z.fig.setAttribute('aria-hidden', 'true');
      body.append(Z.fig);
    }
    drawBar(body, s);
    Z.stage = el('div', 'space-stage');
    Z.stage.id = 'space-stage';
    body.append(Z.stage);
    body.addEventListener('pointerenter', function () { clearTimeout(leaveTimer); });
    body.addEventListener('pointerleave', function (e) {
      clearTimeout(enterTimer);
      if (e.pointerType !== 'mouse' || pinned || !act) return;
      leaveTimer = setTimeout(function () {
        var f = document.activeElement;
        if (!body.contains(f) || !f.matches(':focus-visible')) activate(null);
      }, LEAVE_MS);
    });
    body.addEventListener('focusout', function (e) {
      if (!pinned && act && e.relatedTarget && !body.contains(e.relatedTarget)) activate(null);
    });
    // What was open stays open, if it still has something to show.
    var was = act && (s.readable || LISTED.indexOf(act) >= 0) && !none(act) ? act : null;
    act = undefined;
    kind = '';
    if (!was) pinned = false;
    still = true;
    activate(was);
    still = false;
  }

  function drawBar(body, s) {
    var bar = Z.bar = el('div', 'space-bar');
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'Disk space by type. Arrow keys move, Enter keeps it open.');
    var shown = ORDER.filter(function (k) { return bytesOf(k) > 0 && (s.readable || LISTED.indexOf(k) >= 0); });
    // Titles can add up to more than the disks say is used: their segments share what is.
    var types = shown.filter(function (k) { return k !== 'free'; });
    var sum = types.reduce(function (n, k) { return n + bytesOf(k); }, 0);
    var scale = s.readable && sum ? s.used_bytes / sum : 1;
    var rest = s.readable ? Math.max(0, s.total_bytes - s.used_bytes - s.free_bytes) : 0;
    var full = s.readable ? s.total_bytes : sum;
    // A flex factor below 1 in all would leave the bar part empty, so they are shares of 1000.
    function share(bytes) { return full ? bytes / full * 1000 : 0; }
    shown.forEach(function (k, i) {
      var b = el('button', 'space-seg space-seg--' + k + ' ' + TYPES[k].cls +
        (i === 0 ? ' space-seg--first' : '') + (i === shown.length - 1 && share(rest) < 1 ? ' space-seg--last' : ''));
      b.type = 'button';
      b.setAttribute('data-k', k);
      b.style.setProperty('--v', share(bytesOf(k) * (k === 'free' ? 1 : scale)));
      b.tabIndex = i ? -1 : 0;
      var pct = pctOf(k);
      b.setAttribute('aria-label', TYPES[k].name + ', ' + labelOf(k) + (pct === null ? '' : ', ' + Math.round(pct) + ' per cent') +
        (count(k) ? ', ' + plural(count(k), TYPES[k].unit) : ''));
      b.setAttribute('aria-expanded', 'false');
      b.setAttribute('aria-pressed', 'false');
      b.setAttribute('aria-controls', 'space-stage');
      b.addEventListener('pointerenter', function (e) {
        if (e.pointerType !== 'mouse') return;
        clearTimeout(leaveTimer);
        clearTimeout(enterTimer);
        if (!pinned) enterTimer = setTimeout(function () { activate(k); }, act ? NEXT_MS : HOVER_MS);
      });
      b.addEventListener('pointerleave', function () { clearTimeout(enterTimer); });
      b.addEventListener('focus', function () {
        if (!b.matches(':focus-visible')) return;
        clearTimeout(leaveTimer);
        activate(k);
      });
      b.addEventListener('click', function () {
        clearTimeout(enterTimer);
        pinned = !(pinned && act === k);
        if (pinned) { activate(k); markPin(); } else activate(null);
      });
      Z.segs.push(b);
      bar.append(b);
    });
    if (share(rest) >= 1) {
      var r = el('span', 'space-bar__rest');
      r.style.setProperty('--v', share(rest));
      bar.append(r);
    }
    bar.addEventListener('keydown', function (e) {
      var i = Z.segs.indexOf(document.activeElement);
      var to = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? Z.segs.length - 1 : -1;
      if (i < 0 || to < 0 || to >= Z.segs.length) return;
      e.preventDefault();
      Z.segs[to].focus();
    });
    body.append(bar);
    var disk = s.readable ? markedDisk() : null;
    if (!disk || !disk.threshold_shared) return;
    var tick = el('span', 'space-tick');
    tick.style.setProperty('--at', disk.threshold_pct + '%');
    bar.append(tick);
    var line = el('div', 'space-tick-label');
    line.setAttribute('aria-hidden', 'true');
    var words = el('span', '', 'Scans start at ' + disk.threshold_pct + '%');
    // Beside the tick, on the side that has the room.
    if (disk.threshold_pct >= 50) words.style.right = 100 - disk.threshold_pct + '%';
    else words.style.left = disk.threshold_pct + '%';
    line.append(words);
    body.append(line);
  }

  function segOf(k) { return Z.segs.filter(function (b) { return b.getAttribute('data-k') === k; })[0] || null; }

  function markPin() {
    Z.segs.forEach(function (b) { b.setAttribute('aria-pressed', pinned && b.getAttribute('data-k') === act ? 'true' : 'false'); });
    var pin = Z.stage.querySelector('.space-layer:not([inert]) .space-pin');
    if (pin) pin.textContent = pinned ? 'Pinned' : 'Click to pin';
  }

  function figure(k) {
    var box = el('div');
    var n = el('div', 'space-fig__n');
    var s = stats.storage;
    if (k) {
      box.className = TYPES[k].cls;
      add(n, el('span', 'space-dot'), document.createTextNode(labelOf(k)), el('small', '', TYPES[k].name));
      var bits = [];
      if (pctOf(k) !== null) bits.push(Math.round(pctOf(k)) + '% of the ' + (s.disks.length > 1 ? 'disks' : 'disk'));
      if (count(k)) bits.push(plural(count(k), TYPES[k].unit));
      return add(box, n, el('p', 'space-fig__line', bits.join(' · ')));
    }
    add(n, document.createTextNode(trim(s.free)), el('small', '', 'free of ' + trim(s.total)));
    var p = pressure();
    var readable = s.disks.filter(function (d) { return d.readable; }).length;
    var text = p.level ? p.pct + '% full' + (readable > 1 ? ' overall' : '')
      : trim(s.used) + ' used, ' + p.pct + '%' + (readable > 1 ? ' across ' + readable + ' disks' : '');
    var unread = s.disks.length - readable;
    // The warning first: what the line has no room for is cut at its end.
    if (p.limit) text += ', ' + p.limit;
    if (unread) text += ', ' + plural(unread, 'disk') + ' can’t be read';
    return add(box, n, el('p', 'space-fig__line' + (p.level ? ' space-fig__line--' + p.level : ''), text));
  }

  /* Show type *k*, or all of them. */
  function activate(k) {
    if (k === act) { markPin(); return; }
    var dx = act && k ? (ORDER.indexOf(k) > ORDER.indexOf(act) ? 12 : -12) : 0;
    act = k;
    Z.bar.classList.toggle('has-active', !!k);
    Z.segs.forEach(function (b, i) {
      var on = b.getAttribute('data-k') === k;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-expanded', on ? 'true' : 'false');
      if (k && segOf(k)) b.tabIndex = on ? 0 : -1;
      else if (!k && !Z.segs.some(function (o) { return o.tabIndex === 0; })) b.tabIndex = i ? -1 : 0;
    });
    if (Z.fig) swap(Z.fig, figure(k), dx);
    stage(dx);
    markPin();
  }

  function stage(dx) {
    var k = act;
    var got = k && heavy[k];
    var next = !k ? 'cats' : k === 'other' || k === 'free' ? 'say'
      : !got || got.state === 'loading' ? 'load' : got.state === 'failed' ? 'fail' : 'heavy';
    if (next === 'heavy' && kind === 'heavy') { morph(k); return; }
    kind = next;
    var l = el('div', 'space-layer');
    ({ cats: cats, heavy: heaviest, say: say, load: loading, fail: failing })[next](l, k);
    swap(Z.stage, l, dx);
    if (next === 'heavy') grow(l);
  }

  /* The heaviest titles of *k* came, or could not be had. */
  function heavyCame(k) {
    if (!isOpen || !Z.stage || !Z.stage.isConnected) return;
    if (act === k) {
      var back = Z.stage.querySelector('.space-layer:not([inert]) .space-back');
      var had = back && document.activeElement && Z.stage.contains(document.activeElement);
      stage(0);
      if (had) focusBack();
    } else if (!act) {
      peek(Z.stage.querySelector('.space-layer:not([inert]) .space-peek[data-k="' + k + '"]'), k);
    }
  }

  function focusBack() {
    var b = Z.stage.querySelector('.space-layer:not([inert]) .space-back');
    if (b) b.focus({ preventScroll: true });
  }

  /* Back to all types, the focus on the segment that was open. */
  function collapse() {
    var seg = segOf(act);
    var row = act;
    pinned = false;
    activate(null);
    if (seg) {
      // Focus by keyboard opens a segment: not the one just closed.
      seg.focus({ preventScroll: true });
      if (act) activate(null);
      return;
    }
    var cat = Z.stage.querySelector('.space-layer:not([inert]) .space-cat[data-k="' + row + '"]');
    if (cat) cat.focus({ preventScroll: true });
  }

  function backLine(l, text) {
    var h = el('div', 'space-back-line');
    var b = button('space-back', '', collapse);
    b.append(MM.dom.icon('i-chevron-left'));
    b.setAttribute('aria-label', 'Back to all space');
    l.append(add(h, b, el('span', '', text), el('span', 'space-pin')));
    return h;
  }

  function peek(box, k) {
    var got = heavy[k];
    if (!box || box.firstChild || !got || got.state !== 'ok') return;
    got.items.slice(0, 3).forEach(function (item) { box.append(posterOf(item)); });
    anim(box, [{ opacity: 0 }, { opacity: 1 }], dur());
  }

  function cats(l) {
    var s = stats.storage;
    var box = el('div', 'space-cats');
    ORDER.forEach(function (k) {
      if (!s.readable && LISTED.indexOf(k) < 0) return;
      var nothing = LISTED.indexOf(k) >= 0 ? none(k) : !bytesOf(k);
      var b = el('button', 'space-cat ' + TYPES[k].cls);
      b.type = 'button';
      b.setAttribute('data-k', k);
      var text = el('span');
      add(text, el('b', '', TYPES[k].name), el('small', '',
        k === 'free' ? 'Ready to use' : k === 'other' ? 'Not part of any title'
          : nothing ? 'No titles' : plural(count(k), TYPES[k].unit)));
      var pics = el('span', 'space-peek');
      pics.setAttribute('data-k', k);
      var sz = el('span', 'space-cat__size', labelOf(k));
      if (pctOf(k) !== null) sz.append(el('small', '', pctOf(k) + '%'));
      add(b, el('span', 'space-dot'), text, pics, sz, MM.dom.icon('i-chevron-right'));
      if (LISTED.indexOf(k) >= 0) peek(pics, k);
      if (nothing) b.setAttribute('aria-disabled', 'true');
      else {
        b.addEventListener('click', function () {
          pinned = true;
          activate(k);
          focusBack();
        });
        b.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') dim(k); });
        b.addEventListener('pointerleave', function () { dim(null); });
      }
      box.append(b);
    });
    l.append(box);
    if (s.readable && s.disks.length > 1) disks(l);
    if (leaving() && s.readable) {
      var more = button('space-after', plural(leaving(), 'scheduled title') + 
        (!stats.scheduled.leaving_bytes ? ', size not known'
          : (deletes() ? ' will' : ' would') + ' free ' + least('leaving') + trim(stats.scheduled.leaving_size)),
        function () { chooseTab('scheduled', true); });
      more.prepend(MM.dom.icon('i-clock-regular'));
      more.append(MM.dom.icon('i-chevron-right'));
      l.append(more);
    }
  }

  function dim(k) {
    if (act) return;
    Z.bar.classList.toggle('has-active', !!k);
    Z.segs.forEach(function (b) { b.classList.toggle('is-active', b.getAttribute('data-k') === k); });
  }

  function disks(l) {
    var list = el('ul', 'space-disks');
    stats.storage.disks.forEach(function (d) {
      var li = el('li', 'space-disk' + (d.over_threshold ? ' space-disk--over' : ''));
      var line = el('div', 'space-disk__line');
      var path = el('span', 'space-disk__path', d.path);
      path.title = d.path;
      add(line, path, el('span', 'space-disk__free', !d.readable ? 'Can’t be read'
        : trim(d.free) + ' free of ' + trim(d.total) + (d.over_threshold ? ' · above ' + d.threshold_pct + '%' : '')));
      var bar = el('div', 'space-disk__bar');
      if (d.readable) {
        var fill = el('span', 'space-disk__fill');
        fill.style.setProperty('--used', d.used_bytes / d.total_bytes);
        bar.append(fill);
      }
      list.append(add(li, line, bar));
    });
    add(l, el('h3', 'space-sub', 'Disks'), list);
  }

  function heavyRow(k, item, max) {
    var a = item.ref ? titleLink(el('a', 'space-heavy__row'), item.ref, item.title) : el('div', 'space-heavy__row');
    a.classList.add(TYPES[k].cls);
    var text = el('span', 'space-heavy__text');
    var sub = el('small');
    if (item.protected) add(sub, el('span', 'space-tag space-tag--kept', 'Kept'), document.createTextNode(' · '));
    if (item.deletion_days !== null && item.deletion_days !== undefined) {
      add(sub, el('span', 'space-tag space-tag--leaving', daysLeft(item.deletion_days)), document.createTextNode(' · '));
    }
    var bits = [];
    if (item.season_count) bits.push(plural(item.season_count, 'season'));
    bits.push(item.last_watched ? 'Watched ' + ago(item.last_watched) : 'Never watched');
    sub.append(bits.join(' · '));
    var line = el('span', 'space-line');
    var fill = el('span', 'space-line__fill');
    fill.style.setProperty('--share', max ? item.file_size_bytes / max : 0);
    line.append(fill);
    add(text, el('b', '', item.title), sub, line);
    return add(a, posterOf(item), text, item.file_size_bytes
      ? el('span', 'space-heavy__size', trim(item.file_size))
      : el('span', 'space-heavy__size space-heavy__size--none', 'Not known'));
  }

  function heaviestOf(k) { return (heavy[k].items[0] || {}).file_size_bytes || 0; }

  function heaviest(l, k) {
    headLine(backLine(l, 'Heaviest first'), k);
    var list = el('ol', 'space-heavy');
    heavy[k].items.forEach(function (item) {
      list.append(add(el('li', 'space-heavy__slot'), heavyRow(k, item, heaviestOf(k))));
    });
    add(l, list, el('div', 'space-heavy__notes'));
    notes(l, k);
  }

  function headLine(h, k) {
    var old = h.querySelector('.space-all');
    if (old) old.remove();
    var got = heavy[k] && heavy[k].state === 'ok' ? heavy[k] : null;
    var total = got ? got.total : count(k);
    if (total <= (got ? got.items.length : HEAVIEST)) return;
    var all = el('a', 'space-all');
    all.href = '/watch?view=list&type=' + TYPES[k].list + '&sort=size_desc';
    h.append(add(all, document.createTextNode('See all ' + total + ' by size'), MM.dom.icon('i-chevron-right')));
  }

  function notes(l, k) {
    var box = l.querySelector('.space-heavy__notes');
    box.textContent = '';
    var n = heavy[k].unsized;
    if (n) {
      box.append(el('p', 'space-note', plural(n, TYPES[k].unit) + (n === 1 ? ' has' : ' have') +
        ' no size yet. ' + (n === 1 ? 'It is' : 'They are') + ' ranked last until a scan reads ' + (n === 1 ? 'it.' : 'them.')));
    }
    if (heavy[k].total <= heavy[k].items.length) box.append(el('p', 'space-note', plural(heavy[k].total, TYPES[k].unit) + ' in all'));
  }

  function grow(l) {
    toArray(l.querySelectorAll('.space-heavy__slot')).forEach(function (slot, i) {
      anim(slot, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], dur() + i * 12);
      var fill = slot.querySelector('.space-line__fill');
      anim(fill, [{ transform: 'scaleX(0)' }, { transform: 'scaleX(' + fill.style.getPropertyValue('--share') + ')' }], slow());
    });
  }

  /* One list to the next: each slot keeps its place and its bar runs on from
     where it was. */
  function morph(k) {
    var l = Z.stage.querySelector('.space-layer:not([inert])');
    var list = l.querySelector('.space-heavy');
    var items = heavy[k].items;
    var max = heaviestOf(k);
    var slots = toArray(list.children).filter(function (li) { return !li.inert; });
    for (var i = 0; i < Math.max(slots.length, items.length); i++) {
      var li = slots[i];
      if (!items[i]) { fadeOut(li); continue; }
      if (!li) { li = el('li', 'space-heavy__slot'); list.append(li); }
      var rows = toArray(li.children);
      var was = rows.length ? scaleOf(rows[rows.length - 1].querySelector('.space-line__fill')) : 0;
      rows.forEach(function (r) {
        if (r.inert) return;
        r.classList.add('is-old');
        fadeOut(r);
      });
      var row = heavyRow(k, items[i], max);
      li.append(row);
      anim(row, [{ opacity: 0 }, { opacity: 1 }], dur());
      var fill = row.querySelector('.space-line__fill');
      anim(fill, [{ transform: 'scaleX(' + was + ')' }, { transform: 'scaleX(' + fill.style.getPropertyValue('--share') + ')' }], slow());
    }
    headLine(l.querySelector('.space-back-line'), k);
    notes(l, k);
  }

  function scaleOf(node) {
    var m = /^matrix\(([^,]+)/.exec(getComputedStyle(node).transform);
    return m ? parseFloat(m[1]) : 1;
  }

  function fadeOut(node) {
    node.inert = true;
    after(anim(node, [{ opacity: getComputedStyle(node).opacity }, { opacity: 0 }], fast(), 'forwards'), function () { node.remove(); });
  }

  function say(l, k) {
    backLine(l, k === 'free' ? 'Free space' : 'Other files');
    var s = el('div', 'space-say');
    if (k === 'other') {
      add(s, el('b', '', 'Nothing to list here'),
        el('span', '', 'Space the disk uses that no title accounts for: unfinished downloads, extra versions, extras, and anything else stored on it.'));
    } else if (leaving()) {
      var sized = stats.scheduled.leaving_bytes;
      add(s, el('b', '', !sized ? 'Scheduled titles have no size yet' : least('leaving').replace('a', 'A') + size(stats.storage.free_bytes + stats.scheduled.leaving_bytes) +
          (deletes() ? ' once the scheduled titles go' : ' if the scheduled titles went')),
        el('span', '', plural(leaving(), 'title') + ', ' + (sized ? least('leaving') + trim(stats.scheduled.leaving_size) : 'size not known') + '. Keep any you want to stay.'),
        button('btn btn--secondary', 'See what is scheduled', function () { chooseTab('scheduled', true); }));
    } else {
      add(s, el('b', '', 'Nothing scheduled, so this stays as it is'), el('span', '', 'The next scan will look again.'));
    }
    l.append(s);
  }

  function loading(l, k) {
    headLine(backLine(l, 'Heaviest first'), k);
    var box = el('div', 'space-heavy');
    box.setAttribute('aria-busy', 'true');
    for (var i = 0; i < 7; i++) box.append(add(el('div', 'space-skel__heavy'), el('span', 'skel'), el('span', 'skel'), el('span', 'skel')));
    box.append(el('span', 'sr-only', 'Loading titles'));
    l.append(box);
  }

  function failing(l, k) {
    headLine(backLine(l, 'Heaviest first'), k);
    l.append(failure('Couldn’t load the titles.', function () {
      heavy[k] = { state: 'loading' };
      stage(0);
      focusBack();
      loadHeavy(k);
    }));
  }

  /* ── Scheduled ── */

  function listHead(title, sub, action) {
    var h = el('div', 'space-list-head');
    var words = el('div');
    var t = el('h2', 'space-list-head__title', title);
    t.tabIndex = -1;
    return add(h, add(words, t, el('small', '', sub)), action);
  }

  function scheduledLine() {
    var s = stats.scheduled;
    var more = [];
    if (s.overdue) more.push(s.overdue + ' overdue');
    if (s.deleting) more.push(s.deleting + ' being deleted');
    return (s.leaving || !more.length ? [plural(s.leaving, 'title')] : []).concat(more).join(', ') +
      ' · ' + reclaimable(s.total_bytes, s.total_size);
  }

  var KINDS = { movie: 'Movie', anime: 'Anime', anime_season: 'Anime' };

  function whenOf(item) { return item.state === 'deleting' ? 'Being deleted now' : item.countdown; }

  function scheduledRow(item) {
    var li = el('li', 'space-item');
    li.setAttribute('data-id', item.media_item_id);
    var name = withSeason(item.title, item.season_number);
    var text = el('div', 'space-item__text');
    var when = el('small', 'space-item__when' + (item.state !== 'leaving' || (item.days_left !== null && item.days_left <= 1) ? ' space-item__when--soon' : ''));
    add(when, MM.dom.icon('i-clock-regular'), el('span', '', whenOf(item)));
    var meta = [KINDS[item.media_type] || 'TV', known(item.file_size_bytes, item.file_size)];
    if (item.added_ago) meta.push('Added ' + ago(item.added_ago));
    add(text, titleLink(el(item.ref ? 'a' : 'span', 'space-item__title', name), item.ref, item.title),
      whenOf(item) ? when : null, el('small', '', meta.join(' · ')));
    var keep = el('button', 'watch-keep');
    keep.type = 'button';
    keep.setAttribute('data-watch-keep', item.media_item_id);
    if (item.state !== 'leaving') keep.setAttribute('data-keep-late', '');
    keep.setAttribute('aria-haspopup', 'menu');
    keep.setAttribute('aria-expanded', 'false');
    keep.setAttribute('aria-label', 'Keep ' + name.replace(' · ', ', '));
    add(keep, el('span', '', 'Keep'), MM.dom.icon('i-chevron-down', 'watch-keep__chev'));
    if (watch.keep.isBusy(item.media_item_id)) {
      keep.setAttribute('aria-disabled', 'true');
      keep.setAttribute('aria-busy', 'true');
    }
    return add(li, posterOf(item), text, keep);
  }

  var scheduledHead = '';

  function renderScheduled() {
    var pane = panes.scheduled;
    if (!stats || !scheduled) {
      if (!(failed.stats || failed.scheduled) || scheduledHead === 'failed') return;
      scheduledHead = 'failed';
      redraw(pane, function () { pane.append(failure('Couldn’t load what is scheduled.', tryAgain)); });
      return;
    }
    var list = pane.querySelector('.space-list');
    if (!list || !scheduled.length) {
      var key = JSON.stringify([scheduled.length, stats.unavailable_roots, stats.deletions_off, stats.dry_run, stats.plex_configured]);
      if (!scheduled.length && key === scheduledHead) return;
      scheduledHead = key;
      redraw(pane, drawScheduled, function () { return pane.querySelector('.space-empty'); });
      return;
    }
    noticesOf(pane);
    pane.querySelector('.space-list-head small').textContent = scheduledLine();
    updateList(list);
  }

  function drawScheduled() {
    var pane = panes.scheduled;
    noticesOf(pane);
    if (!scheduled.length) {
      pane.append(stats.plex_configured
        ? empty('i-circle-check-regular', 'Nothing scheduled for deletion', 'Library is healthy. The next scan will look again.')
        : connectPlex());
      return;
    }
    var clear = el('button', 'btn btn--sm btn--danger', 'Clear all');
    clear.type = 'button';
    clear.addEventListener('click', function () { clearScheduled(clear); });
    var list = el('ul', 'space-list');
    scheduled.forEach(function (item) { list.append(scheduledRow(item)); });
    add(pane, listHead('Scheduled for deletion', scheduledLine(), clear), list);
  }

  /* The notices at the top of a pane, changed only when they say something else. */
  function noticesOf(pane) {
    var fresh = notices();
    var old = toArray(pane.querySelectorAll('.space-notice'));
    if (old.map(textOf).join('|') === fresh.map(textOf).join('|')) return;
    old.forEach(function (n) { n.remove(); });
    fresh.reverse().forEach(function (n) { pane.prepend(n); });
  }

  function textOf(node) { return node.textContent; }

  function liveRows(list) { return toArray(list.querySelectorAll('.space-item')).filter(function (li) { return !li.inert; }); }

  /* Bring the rows into line with the list: those that stay slide, one that
     goes fades where it stands, one that comes eases in. */
  function updateList(list) {
    var rows = liveRows(list);
    var byKey = {};
    rows.forEach(function (li) { byKey[li.getAttribute('data-id')] = li; });
    var active = document.activeElement;
    var place = list.contains(active) ? rows.indexOf(active.closest('.space-item')) : -1;
    var listed = {};
    scheduled.forEach(function (item) { listed[item.media_item_id] = true; });
    function stays(li) { return listed[li.getAttribute('data-id')]; }
    var play = core.flip(rows.filter(stays));
    var box = list.getBoundingClientRect();
    var gone = rows.filter(function (li) { return !stays(li); }).map(function (li) {
      return { li: li, top: li.getBoundingClientRect().top - box.top };
    });
    gone.forEach(function (g) {
      g.li.inert = true;
      if (core.reduceMotion() || !g.li.animate) { g.li.remove(); return; }
      g.li.classList.add('is-gone');
      g.li.style.top = g.top + 'px';
      after(anim(g.li, [{ opacity: 1 }, { opacity: 0 }], fast(), 'forwards'), function () { g.li.remove(); });
    });
    scheduled.forEach(function (item) {
      var li = byKey[item.media_item_id];
      var fresh = scheduledRow(item);
      if (li && !li.inert) {
        // Only its words can have changed: the button, and the focus on it, stay.
        var words = li.querySelector('.space-item__text');
        var now = fresh.querySelector('.space-item__text');
        if (!words.isEqualNode(now)) words.replaceWith(now);
        li.querySelector('.watch-keep').toggleAttribute('data-keep-late', item.state !== 'leaving');
      } else {
        li = fresh;
        byKey[item.media_item_id] = li;
        list.append(li);
        anim(li, [{ opacity: 0, transform: 'scale(.98)' }, { opacity: 1, transform: 'none' }], dur());
      }
    });
    // In the server's order, moving only what is out of place: a moved node loses the focus.
    var at = null;
    scheduled.forEach(function (item) {
      var li = byKey[item.media_item_id];
      var next = at ? at.nextElementSibling : list.firstElementChild;
      while (next && next.inert) next = next.nextElementSibling;
      if (next !== li) list.insertBefore(li, next);
      at = li;
    });
    play();
    if (place >= 0 && rows[place].inert) {
      var left = liveRows(list);
      var to = left[Math.min(place, left.length - 1)];
      if (!to) return;
      var button = to.querySelector('.watch-keep');
      button.focus({ preventScroll: true });
      // keep.js brings the next one into view itself.
      if (!watch.keep.isBusy(rows[place].getAttribute('data-id'))) {
        button.scrollIntoView({ block: 'nearest', behavior: core.reduceMotion() ? 'auto' : 'smooth' });
      }
    }
  }

  function shiftLeaving(by, bytes) {
    var s = stats.scheduled;
    s.total += by;
    s.leaving += by;
    s.leaving_bytes += by * bytes;
    s.total_bytes += by * bytes;
    s.leaving_size = size(s.leaving_bytes);
    s.total_size = size(s.total_bytes);
  }

  /* keep.js: the title *id* is kept. Off the list, until an Undo. */
  function kept(id, trigger) {
    if (!scheduled || !stats) return null;
    var i = scheduled.map(function (item) { return item.media_item_id; }).indexOf(id);
    if (i < 0) return null;
    var item = scheduled[i];
    var here = isOpen && panes.scheduled.contains(trigger);
    var mine = item.state === 'leaving';
    scheduled = scheduled.slice(0, i).concat(scheduled.slice(i + 1));
    if (mine) shiftLeaving(-1, item.file_size_bytes);
    render();
    var focus = null;
    if (here) {
      var rows = liveRows(panes.scheduled);
      var next = rows[Math.min(i, rows.length - 1)];
      focus = next ? next.querySelector('.watch-keep') : panes.scheduled.querySelector('.space-empty');
    }
    return {
      focus: focus,
      undo: function () {
        var there = scheduled.some(function (other) { return other.media_item_id === id; });
        if (!there) {
          scheduled = scheduled.slice(0, i).concat([item], scheduled.slice(i));
          if (mine) shiftLeaving(1, item.file_size_bytes);
          render();
        }
        if (!isOpen || tab !== 'scheduled') return null;
        var li = liveRows(panes.scheduled).filter(function (row) { return row.getAttribute('data-id') === id; })[0];
        return li ? li.querySelector('.watch-keep') : null;
      }
    };
  }

  watch.keep.joinPanel({
    kept: kept,
    started: function () { keepsInFlight += 1; keepGen += 1; },
    settled: function (saved) {
      keepsInFlight -= 1;
      keepGen += 1;
      keepSaved = keepSaved || saved;
      if (keepsInFlight > 0) return;
      if (keepSaved) tellList();
      if (keepSaved || refreshWaiting) refreshQuietly();
      keepSaved = false;
    }
  });

  /* ── The page behind ── */

  /* watch/list.js: what is scheduled has changed. */
  function tellList() { document.dispatchEvent(new CustomEvent('mm:watch-scheduled', { bubbles: true })); }

  /* Not those on their way out, of the row (is-kept) or of an open grid. */
  function liveCards(row) {
    return toArray(row.querySelectorAll('.watch-card[data-keep-id]')).filter(function (card) {
      return !card.classList.contains('is-kept') && !card.inert;
    });
  }

  /* The Leaving soon row; one whose last card went is on its way out. */
  function leavingRow() {
    return toArray(document.querySelectorAll('.watch-row[data-watch-leaving-row]')).filter(function (row) { return liveCards(row).length; })[0] || null;
  }

  function idOf(card) { return card.getAttribute('data-keep-id'); }

  /* Bring the rows into line with what is scheduled: a card that is not any
     more leaves, its Keep would fail; one that is new is read from the
     server's page. */
  function followPage() {
    var ids = {};
    var wanted = scheduled.filter(function (item) { return item.state === 'leaving'; });
    wanted.forEach(function (item) { ids[item.media_item_id] = true; });
    var stale = [];
    toArray(document.querySelectorAll('[data-watch-leaving-row]')).forEach(function (row) {
      stale = stale.concat(liveCards(row).filter(function (card) { return !ids[idOf(card)]; }));
    });
    // The one with the focus last, so that the focus goes to one that stays.
    var active = document.activeElement;
    stale.sort(function (a, b) { return a.contains(active) - b.contains(active); });
    stale.forEach(function (card) {
      var had = card.contains(active);
      var shown = card.getClientRects().length > 0;
      var to = watch.keep.fromRow(card, card.closest('[data-watch-leaving-row]'), shown, '[data-watch-keep]').focus;
      if (had && to) core.focusSoftly(to);
    });
    toArray(document.querySelectorAll('.watch-card[data-keep-id]')).forEach(function (card) {
      var badge = card.querySelector('.watch-badge--leaving');
      if (!badge || ids[idOf(card)] || card.closest('[data-watch-leaving-row]')) return;
      after(anim(badge, [{ opacity: 1 }, { opacity: 0 }], fast(), 'forwards'), function () { badge.remove(); });
    });
    var row = leavingRow();
    var here = {};
    if (row) liveCards(row).forEach(function (card) { here[idOf(card)] = true; });
    if (wanted.every(function (item) { return here[item.media_item_id]; })) return;
    var seq = refreshSeq;
    var gen = keepGen;
    fetch('/watch', { credentials: 'same-origin', headers: { Accept: 'text/html' } }).then(function (resp) {
      if (!resp.ok || resp.redirected) throw new Error('page ' + resp.status);
      return resp.text();
    }).then(function (html) {
      if (seq !== refreshSeq || gen !== keepGen) return;
      var now = new DOMParser().parseFromString(html, 'text/html').querySelector('.watch-row[data-watch-leaving-row]');
      if (now) enterLeaving(now);
    }).catch(function () {
      // The panel's list has them; the row does on the next load.
    });
  }

  /* The cards of the server's row that the page lacks, in their places, or the row itself. */
  function enterLeaving(now) {
    var row = leavingRow();
    if (!row) {
      var rows = document.querySelector('.watch-rows');
      if (!rows) return;
      row = document.importNode(now, true);
      var next = null;
      for (var n = now.nextElementSibling; n && !next; n = n.nextElementSibling) {
        var id = n.getAttribute('data-row-id');
        next = n.id ? byId(n.id) : id && rows.querySelector('.watch-row[data-row-id="' + id + '"]');
      }
      rows.insertBefore(row, next || null);
      watch.rows.init(row.querySelector('.watch-row__viewport'));
      watch.expand.row(row);
      renderRowLink();
      if (row.getClientRects().length) watch.keep.growIn(row, false);
      toGrid(liveCards(row));
      return;
    }
    var scroller = row.querySelector('.watch-row__scroll');
    var before = null;
    var fresh = [];
    liveCards(now).reverse().forEach(function (card) {
      var mine = liveCards(row).filter(function (c) { return idOf(c) === idOf(card); })[0];
      if (!mine) {
        mine = document.importNode(card, true);
        scroller.insertBefore(mine, before);
        fresh.push(mine);
        if (mine.getClientRects().length) watch.keep.growCard(mine, row);
      }
      before = mine;
    });
    toGrid(fresh);
  }

  /* Cards new in the row, in its grid too if that is open. */
  function toGrid(cards) {
    if (watch.grid && watch.grid.bring) cards.forEach(function (card) { watch.grid.bring(card); });
  }

  async function clearScheduled(b) {
    if (b.getAttribute('aria-disabled') === 'true') return;
    var proceed = await window.UIFeedback.confirm({
      title: 'Clear all scheduled deletions?',
      body: 'Nothing is deleted now. Items will be re-evaluated on the next scan and may be rescheduled.',
      confirmLabel: 'Clear all',
      confirmVariant: 'danger'
    });
    if (!proceed) return;
    // aria-disabled, not disabled, so the focus the dialog hands back stays on the button.
    b.setAttribute('aria-disabled', 'true');
    b.setAttribute('aria-busy', 'true');
    MM.api.post('/api/scan/clear-scheduled').then(function (data) {
      var text = 'Cleared ' + plural(data.cleared, 'scheduled deletion');
      if (data.already_deleting) {
        text += '; ' + data.already_deleting + (data.already_deleting === 1 ? ' was' : ' were') + ' already being deleted';
      }
      core.toast(text, { variant: 'success', duration: data.already_deleting ? 8000 : undefined });
      tellList();
      return refresh().catch(function () {
        window.UIFeedback.error('Cleared, but the list could not update. Refresh the page to see it.');
      });
    }, function (err) {
      window.UIFeedback.error("Couldn't clear scheduled deletions. " + MM.api.messageOf(err, 'Try again.'));
    }).then(function () {
      b.removeAttribute('aria-disabled');
      b.removeAttribute('aria-busy');
    });
  }

  /* ── Deleted ── */

  var deletedFrom = '';

  function renderDeleted() {
    var pane = panes.deleted;
    if (!deleted || !stats) {
      if (!(failed.stats || failed.deleted) || deletedFrom === 'failed') return;
      deletedFrom = 'failed';
      redraw(pane, function () { pane.append(failure('Couldn’t load what was deleted.', tryAgain)); });
      return;
    }
    var now = JSON.stringify([deleted, stats.reclaimed_total]);
    if (now === deletedFrom) return;
    deletedFrom = now;
    redraw(pane, drawDeleted, function () { return pane.querySelector('.space-list-head__title, .space-empty'); });
  }

  function drawDeleted() {
    var pane = panes.deleted;
    if (!deleted.length) {
      pane.append(empty('i-clock-rotate-left', 'No deletion history yet', 'When mediaman reclaims space, you’ll see it here.'));
      return;
    }
    var more = el('a', 'space-more', 'Full history');
    more.href = '/history';
    more.append(MM.dom.icon('i-chevron-right'));
    pane.append(listHead('Recently deleted',
      stats.reclaimed_total_bytes ? trim(stats.reclaimed_total) + ' reclaimed in total' : '', more));
    var list = el('ul', 'space-list');
    deleted.forEach(function (item) {
      var name = withSeason(item.title, item.redownload && item.redownload.season_number);
      var li = el('li', 'space-item space-item--deleted');
      var text = el('div', 'space-item__text');
      add(text, el('span', 'space-item__title', name), el('small', '', ('Deleted ' + ago(item.deleted_ago)).trim()),
        el('small', '', item.reclaimed === '0 B' ? 'Size not known' : 'Freed ' + trim(item.reclaimed)));
      add(li, poster(item.poster_url), text);
      // Only a stored identity with the id the route needs can be had again.
      if (item.redownload) li.append(redoButton(item, name));
      list.append(li);
    });
    pane.append(list);
  }

  var redone = {}; // what each Re-download button says after a press, by row: it outlives a redraw

  function redoButton(item, name) {
    var b = el('button', 'btn btn--sm btn--secondary space-redo', 'Re-download');
    b.type = 'button';
    b.setAttribute('data-k', String(item.id));
    b.setAttribute('aria-label', 'Re-download ' + name);
    b.addEventListener('click', function () { redownload(item, name, b); });
    if (redone[item.id]) paintRedo(b, name, redone[item.id]);
    return b;
  }

  function paintRedo(b, name, state) {
    b.textContent = '';
    b.classList.toggle('is-success', state === 'queued');
    b.classList.toggle('is-error', state !== 'queued' && state !== 'adding');
    // aria-disabled, not disabled, so a focused button keeps the focus.
    if (state === 'queued' || state === 'adding') b.setAttribute('aria-disabled', 'true');
    else b.removeAttribute('aria-disabled');
    if (state === 'queued') add(b, MM.dom.icon('i-check'), document.createTextNode('Queued'));
    else b.append(state === 'adding' ? 'Adding…' : state);
    b.setAttribute('aria-label', state === 'queued' ? name + ' queued'
      : state === 'adding' ? 'Adding ' + name : state + '. Re-download ' + name);
  }

  /* Re-download a deleted item with the ids stored when it was deleted. */
  function redownload(item, name, b) {
    if (b.getAttribute('aria-disabled') === 'true') return;
    function become(state) {
      redone[item.id] = state;
      var now = panes.deleted.querySelector('.space-redo[data-k="' + item.id + '"]');
      if (now) paintRedo(now, name, state);
    }
    become('adding');
    MM.api.post('/api/media/redownload', item.redownload).then(function () {
      become('queued');
      live.textContent = name + ' queued';
    }, function (err) {
      var msg = MM.api.messageOf(err, '');
      become(msg && msg.length < 30 ? msg : 'Failed');
      window.UIFeedback.error("Couldn't re-download. " + MM.api.messageOf(err, 'Try again.'));
    });
  }

  /* ── Scan now ── */

  /* Delays after a failed status poll, then every 10 s. Polls give up after
     POLL_MAX_FAILS in a row or POLL_MAX_MS of unbroken failure; a request that
     hangs is dropped and counts as one. */
  var POLL_MS = 3000;
  var POLL_RETRY_MS = [2000, 4000, 8000, 10000];
  var POLL_MAX_FAILS = 5;
  var POLL_MAX_MS = 10 * 60 * 1000;
  var POLL_TIMEOUT_MS = 10000;
  var DONE_MS = 2000; // how long the button says Scan complete
  var STATUS_LINGER_MS = 5000;
  var IDLE = 'A scan looks for titles to clear';

  var scanStatus = byId('space-scan-status');
  var scanStart = 0;
  var lingerTimer = null;
  var clock = null; // while the seconds run

  function scanLabel(text, icon) {
    scan.querySelector('.space__scan-label').textContent = text;
    MM.dom.setIcon(scan.querySelector('.ico'), icon || 'i-rotate-right');
  }

  /* Not a live region: its running clock would be read out. */
  function scanSays(text, cls) {
    clearTimeout(lingerTimer);
    scanStatus.textContent = text;
    scanStatus.className = 'space__scan-status' + (cls ? ' space__scan-status--' + cls : '');
  }

  function elapsed() {
    var sec = Math.floor((Date.now() - scanStart) / 1000);
    return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  }

  /* A scan's end, where nobody sees the words beside the button. */
  function tellClosed(text, variant) {
    if (!isOpen) core.toast(text, { variant: variant });
  }

  function runClock() {
    if (clock) return;
    clock = setInterval(function () { scanSays('Scanning libraries, ' + elapsed()); }, 1000);
  }

  function scanBusy(on) {
    if (!on) {
      clearInterval(clock);
      clock = null;
    }
    scan.classList.toggle('is-busy', on);
    renderMeters();
  }

  function scanStopped(reason, opts) {
    scanBusy(false);
    scan.removeAttribute('aria-disabled');
    scanLabel(opts.retry ? 'Retry scan' : 'Scan now');
    scan.classList.toggle('is-error', !!opts.retry);
    scanSays(reason + (opts.settings ? '. ' : ''), opts.retry ? 'error' : '');
    if (opts.settings) {
      var a = el('a', '', 'Open Settings');
      a.href = '/settings';
      scanStatus.append(a);
    }
    live.textContent = reason;
    tellClosed(reason, opts.retry ? 'error' : undefined);
  }

  /* *reason*: what the scan left out, if anything; that stays until the next one. */
  function scanDone(reason) {
    var left = reason ? String(reason).replace(/\.$/, '').split(/; (?=Plex library ')/) : [];
    var more = left.length > 1 ? ', and ' + (left.length - 1) + ' more, see ' : '';
    var start = 'Finished in ' + elapsed() + (reason ? '. ' + left[0] : '') + more;
    var took = start + (more ? 'History' : '');
    function says(after) {
      scanSays(more ? start : took + after, reason ? '' : 'ok');
      if (!more) return;
      var a = el('a', '', 'History');
      a.href = '/history';
      scanStatus.append(a, after);
    }
    var look = reason ? '' : 'success';
    scanBusy(false);
    scan.classList.add('is-success');
    scanLabel('Scan complete', 'i-check');
    says('');
    var said = 'Scan ' + took.replace('F', 'f');
    live.textContent = said;
    tellList();
    refresh().then(function () {
      tellClosed(said, look);
      if (!reason) lingerTimer = setTimeout(function () { scanSays(IDLE); }, STATUS_LINGER_MS);
    }, function () {
      var late = '. Refresh the page to see the results.';
      tellClosed(said + late, look);
      says(late);
    }).then(function () {
      setTimeout(function () {
        scan.classList.remove('is-success');
        scan.removeAttribute('aria-disabled');
        scanLabel('Scan now');
      }, DONE_MS);
    });
  }

  function pollScan() {
    var fails = 0;
    var firstFail = 0;
    function check() {
      var ctrl = new AbortController();
      var abort = setTimeout(function () { ctrl.abort(); }, POLL_TIMEOUT_MS);
      MM.api.get('/api/scan/status', { signal: ctrl.signal }).then(function (data) {
        fails = 0;
        firstFail = 0;
        if (data.running) {
          runClock();
          setTimeout(check, POLL_MS);
        } else if (data.status === 'skipped') {
          scanStopped(!data.reason ? 'Scan skipped' : 'Scan skipped: ' +
            (data.reason === 'Plex not configured' ? 'Plex is not configured' : data.reason), { settings: true });
        } else if (data.status === 'error') {
          scanStopped('Scan failed: ' + (data.reason || 'unknown error'), { retry: true });
        } else {
          scanDone(data.reason);
        }
      }).catch(function () {
        fails += 1;
        if (!firstFail) firstFail = Date.now();
        if (fails >= POLL_MAX_FAILS || Date.now() - firstFail >= POLL_MAX_MS) {
          scanStopped('Lost track of the scan. Refresh to check its status.', { retry: true });
          return;
        }
        setTimeout(check, POLL_RETRY_MS[Math.min(fails, POLL_RETRY_MS.length) - 1]);
      }).finally(function () { clearTimeout(abort); });
    }
    setTimeout(check, POLL_MS);
  }

  scan.addEventListener('click', function () {
    if (scan.getAttribute('aria-disabled') === 'true') return;
    scan.setAttribute('aria-disabled', 'true');
    scan.classList.remove('is-error', 'is-success');
    scanBusy(true);
    scanLabel('Scanning');
    scanStart = Date.now();
    scanSays('Starting scan…');
    live.textContent = 'Scanning libraries';
    MM.api.post('/api/scan/trigger').then(function (data) {
      var running = data.status === 'already_running';
      scanSays(running ? 'Scan already in progress' : 'Scanning libraries');
      if (!running) runClock();
      pollScan();
    }, function (err) {
      scanStopped("Couldn't start the scan. " + MM.api.messageOf(err, 'Try again.'), { retry: true });
    });
  });

  /* ── The rest of the page ── */

  /* The one Keep menu: out of the page, which the panel puts out of reach, and
     with the choice only an admin has. */
  var menu = byId('watch-keep-menu');
  if (menu) {
    var forever = el('button', 'watch-keep-menu__item', 'Keep forever');
    forever.type = 'button';
    forever.setAttribute('role', 'menuitem');
    forever.tabIndex = -1;
    forever.setAttribute('data-days', 'forever');
    menu.append(forever);
    document.body.append(menu);
  }

  /* A keep or a delete in the list or a title's details changes the figures. */
  document.addEventListener('mm:watch-admin', function (e) {
    if (e.detail && e.detail.phase === 'done') refreshQuietly();
  });

  if (document.readyState === 'complete') refreshQuietly();
  else window.addEventListener('load', refreshQuietly);
})();
