/* Watch: the pane over Watch that holds Profile, History and Settings (templates/_you.html), on MM.you.
 *
 *   MM.you.shown(group)          whether the pane is open on that group: profile, history or settings
 *   MM.you.scroller(view)        the element that scrolls that view, in place of the window
 *   MM.you.go(path, {replace})   opens the pane at a path, or steps to it, as one entry of the history
 *   MM.you.close()
 *   MM.you.guard(group, {dirty(from, to), discard(), title, body})
 *                                asked before every way out of a view of the group; *to* is the
 *                                path it goes to, null when the pane closes or the page is left
 *   MM.you.insert(html)          puts a group's fragment in the pane and gives back its root
 *   mm:you-shown, mm:you-hidden  on document, with detail {group, view}, view being the path
 *   MM.history.address           {read(), write(params, 'push'|'replace')}: History's filters, in h
 *
 * The address is /?you=<path>[&h=<History's query>]: the layer 'you' of watch/address.js. Each
 * group's fragment is fetched once per page life, then its styles, then its scripts in order.
 *
 * Needs watch/address.js, modal-a11y.js, core/dom.js and ui-feedback.js.
 */
(function () {
  'use strict';

  var root = document.getElementById('you');
  var address = window.MMWatch && window.MMWatch.address;
  if (!root || !address) return;

  var PATHS = ['profile', 'profile/picture', 'profile/email', 'profile/password', 'profile/plex', 'profile/devices',
    'history', 'settings', 'settings/overview', 'settings/general', 'settings/libraries', 'settings/cleanup',
    'settings/integrations', 'settings/tvs', 'settings/newsletter', 'settings/users',
    'settings/integrations/plex', 'settings/integrations/sonarr', 'settings/integrations/radarr',
    'settings/integrations/nzbget', 'settings/integrations/mailgun', 'settings/integrations/openai',
    'settings/integrations/tmdb', 'settings/integrations/omdb'];
  var H_KEYS = ['q', 'user', 'action', 'title', 'since', 'until', 'date', 'page', 'event'];
  var PAGES = { profile: '/account', history: '/history', settings: '/settings' };
  var TITLES = { profile: 'Profile', history: 'History', settings: 'Settings' };
  var FAILED = 'That did not load. Try again.';
  var PULL_PX = 120; // pulled this far down, the sheet closes
  var FLICK_PX_MS = 0.6; // or flicked down this fast, from 30 px on
  var HIDE_MS = 260; // the panel's slide (--dur-slow) and a frame
  var CALM_MS = 160; // its fade (--dur-fast) and a frame

  var admin = root.hasAttribute('data-admin');
  var panel = root.querySelector('.you__panel');
  var views = root.querySelector('.you__views');
  var backBtn = root.querySelector('[data-you-back]');
  var backLabel = root.querySelector('.you__back-label');
  var title = root.querySelector('.you__title');
  var veil = root.querySelector('.you__veil');
  var calm = window.matchMedia('(prefers-reduced-motion: reduce)');
  var wideMq = window.matchMedia('(min-width: 1000px)');
  var phone = window.matchMedia('(max-width: 699.98px)');

  var openNow = false;
  var path = null; // what is on show
  var opener = null; // what had the focus before the pane opened
  var letPageGo = null;
  var hideTimer = null;
  var loads = {}; // each group's fragment and assets, by group: a Promise of whether it is in
  var ready = {}; // the groups that are in
  var fetched = {}; // each style and script asked for, by its address
  var guards = {};
  var cameFrom = {}; // the path each entry the pane pushed was stepped to from, by the entry's id
  var asking = null; // the question on show
  var turn = 0; // counts each show, so a group that comes in is placed for the latest only
  var pressedIn = false; // the press that ends in a click began in the panel

  /* ── Paths and addresses ── */

  function group(p) { return p.split('/')[0]; }
  function clean(p) {
    p = String(p || '').replace(/^\/+|\/+$/g, '');
    return PATHS.indexOf(p) === -1 ? 'profile' : p;
  }
  function allowed(p) { return admin || group(p) !== 'settings'; }
  function wide() { return wideMq.matches; }
  function section(p) { var x = p.split('/'); return x[0] === 'settings' && x[1] ? x[1] : null; }
  function depth(p) { return p === 'profile' ? 0 : section(p) && !wide() ? 2 : 1; }
  function parent(p) {
    if (p === 'profile') return null;
    return section(p) && !wide() ? 'settings' : 'profile';
  }

  /* The place an old link's hash named. */
  function fromHash(p, hash) {
    var m;
    if (group(p) === 'settings' && (m = /^setg-([a-z]+)$/.exec(hash))) return 'settings/' + m[1];
    if (group(p) === 'settings' && (m = /^intg-([a-z]+)$/.exec(hash))) return 'settings/integrations/' + m[1];
    if (group(p) === 'profile' && hash === 'plex') return 'profile/plex';
    return null;
  }

  function hOf(params) {
    var out = new URLSearchParams();
    var get = typeof params.get === 'function' ? function (k) { return params.get(k); } : function (k) { return params[k]; };
    H_KEYS.forEach(function (k) {
      var v = get(k);
      if (v !== null && v !== undefined && v !== '') out.set(k, String(v));
    });
    return out.toString();
  }

  /* The page's own address with the pane at *p*; no hash, so what a hash opens closes under it. */
  function addressOf(p, h) {
    var base = address.url({ you: null, h: null }, '');
    return base + (base.indexOf('?') === -1 ? '?' : '&') + 'you=' + p + (h ? '&h=' + encodeURIComponent(h) : '');
  }

  /* The pane's place a link leads to: an old page of it, or the pane's own address. */
  function placeOf(href) {
    var to;
    try { to = new URL(href, window.location.href); } catch (e) { return null; }
    if (to.origin !== new URL(window.location.href).origin) return null;
    var hash = to.hash.replace(/^#/, '');
    var p = { '/account': 'profile', '/history': 'history', '/settings': 'settings' }[to.pathname];
    var h = '';
    if (p === 'history') h = hOf(to.searchParams);
    else if (!p && to.pathname === '/' && to.searchParams.has('you')) {
      p = clean(to.searchParams.get('you'));
      h = to.searchParams.get('h') || '';
    }
    if (!p) return null;
    return { path: clean(fromHash(p, hash) || p), h: h };
  }

  /* The address the page was loaded at, put as the pane has it: an old hash becomes its place,
     and a viewer sent to Settings is left on Watch. */
  (function settle() {
    var query = new URLSearchParams(window.location.search);
    if (!query.has('you')) return;
    var p = clean(query.get('you'));
    var mapped = fromHash(p, window.location.hash.replace(/^#/, ''));
    if (mapped) p = clean(mapped);
    var h = query.get('h');
    query.delete('you');
    query.delete('h');
    var rest = query.toString();
    var to = window.location.pathname + (rest ? '?' + rest : '');
    if (allowed(p)) to += (rest ? '&' : '?') + 'you=' + p + (h ? '&h=' + encodeURIComponent(h) : '');
    if (!mapped) to += window.location.hash;
    window.history.replaceState(window.history.state, '', to);
  })();

  function entryId() {
    var state = window.history.state;
    return state && state.mm ? state.mm.id : null;
  }

  /* ── The guard ── */

  /* True when nothing is lost; else a Promise of whether the person let it go. */
  function ask(to) {
    var g = openNow && path ? guards[group(path)] : null;
    if (!g || !g.dirty(path, to)) return true;
    if (asking) return asking;
    asking = window.UIFeedback.confirm({
      title: g.title || 'Discard unsaved changes?',
      body: g.body || '',
      confirmLabel: 'Discard',
      confirmVariant: 'danger',
      cancelLabel: 'Keep editing'
    }).then(function (yes) {
      asking = null;
      if (yes) g.discard();
      return yes;
    });
    return asking;
  }

  function then(answer, fn) {
    if (answer === true) fn();
    else answer.then(function (yes) { if (yes) fn(); });
  }

  /* ── Loading ── */

  function words(text) { return String(text || '').split(/\s+/).filter(Boolean); }

  function had(tag, attr, url) {
    return Array.prototype.some.call(document.querySelectorAll(tag), function (n) { return n.getAttribute(attr) === url; });
  }

  /* Only the app's own files: a path under /static/, never another host's. */
  function ours(url) { return /^\/static\/[^\\]*$/.test(url) && url.indexOf('//') === -1; }

  function style(href) {
    if (!ours(href)) return Promise.resolve();
    if (!fetched[href]) {
      fetched[href] = had('link', 'href', href) ? Promise.resolve() : new Promise(function (resolve) {
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = href;
        // A style that fails leaves the pane plainer, not shut.
        link.onload = link.onerror = function () { resolve(); };
        document.head.appendChild(link);
      });
    }
    return fetched[href];
  }

  function script(src) {
    if (!ours(src)) return Promise.reject(new Error(src));
    if (!fetched[src]) {
      fetched[src] = had('script', 'src', src) ? Promise.resolve() : new Promise(function (resolve, reject) {
        var el = document.createElement('script');
        el.src = src;
        el.async = false;
        el.onload = function () { resolve(); };
        el.onerror = function () { delete fetched[src]; el.remove(); reject(new Error(src)); };
        document.head.appendChild(el);
      });
    }
    return fetched[src];
  }

  function insert(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var got = doc.querySelector('[data-you-group]');
    if (!got) throw new Error('no fragment');
    var node = document.importNode(got, true);
    Array.prototype.forEach.call(node.querySelectorAll('.you-view'), function (v) { v.inert = true; });
    views.appendChild(node);
    return node;
  }

  function fetchGroup(g) {
    var h = g === 'history' ? hOf(new URLSearchParams(new URLSearchParams(window.location.search).get('h') || '')) : '';
    return window.fetch(PAGES[g] + '?pane=1' + (h ? '&' + h : ''), { credentials: 'same-origin' }).then(function (res) {
      if (res.redirected && new URL(res.url, window.location.href).pathname === '/login') {
        window.location.assign('/login?next=' + encodeURIComponent(window.location.pathname + window.location.search));
        return null;
      }
      if (res.redirected || res.status !== 200) {
        var err = new Error(String(res.status));
        err.refused = res.redirected;
        throw err;
      }
      return res.text();
    }).then(function (html) {
      if (html === null) return false;
      var node = window.MM.you.insert(html);
      return Promise.all(words(node.getAttribute('data-styles')).map(style)).then(function () {
        return words(node.getAttribute('data-scripts')).reduce(function (chain, src) {
          return chain.then(function () { return script(src); });
        }, Promise.resolve());
      }).then(function () {
        ready[g] = true;
        return true;
      });
    });
  }

  function load(g) {
    if (!loads[g]) {
      loads[g] = fetchGroup(g).catch(function (err) {
        delete loads[g];
        window.UIFeedback.error(FAILED);
        fail(g, !!err.refused);
        return false;
      });
    }
    return loads[g];
  }

  /* What did not load is not left on show: Profile is, else nothing, as for Settings refused. */
  function fail(g, refused) {
    if (!openNow || group(path) !== g) return;
    if (g !== 'profile' && !(g === 'settings' && refused)) {
      address.replace('you', addressOf('profile'));
      show('profile', 'replace');
    } else {
      address.close('you');
    }
  }

  /* ── On show ── */

  function viewOf(p) {
    for (var q = p; q; q = q.split('/').slice(0, -1).join('/')) {
      var v = views.querySelector('.you-view[data-view="' + q + '"]');
      if (v) return v;
    }
    return null;
  }

  function titleOf(p) {
    var v = ready[group(p)] ? viewOf(p) : null;
    // A section stands alone on a narrow pane, so it is named; beside the list, the pane is Settings.
    var sec = v && section(p) && !wide() ? v.querySelector('.stx-sec[data-sec="' + section(p) + '"]') : null;
    return (sec && sec.getAttribute('data-title')) || (v && v.getAttribute('data-title')) || TITLES[group(p)];
  }

  function seen(node) {
    return !!node && node !== document.body && document.body.contains(node) &&
      node.getClientRects().length > 0 && !node.closest('[inert]');
  }

  function tell(type, g, view) {
    document.dispatchEvent(new CustomEvent(type, { detail: { group: g, view: view } }));
  }

  function drawBar(p) {
    var up = parent(p);
    // Faded by a class, as the page's [hidden] would cut it; its label stays while it fades.
    backBtn.classList.toggle('is-off', !up);
    backBtn.inert = !up;
    if (up) backLabel.textContent = titleOf(up);
    title.textContent = titleOf(p);
  }
  // Up and the title of a Settings section depend on the width.
  if (wideMq.addEventListener) wideMq.addEventListener('change', function () { if (openNow && path) drawBar(path); });

  /* A step leaves the focus on what was pressed if it is still there; else, on the way back, on
     what led here; else on the title. */
  function keepFocus(prev, to) {
    var a = document.activeElement;
    if (a && a !== panel && panel.contains(a) && seen(a)) return;
    var back = prev ? to.querySelector('[data-you-go="' + prev + '"]') : null;
    (seen(back) ? back : title).focus({ preventScroll: true });
  }

  function place(was, p, how) {
    var to = viewOf(p);
    if (!to) return;
    var from = views.querySelector('.you-view.is-on');
    if (from !== to) {
      var dir = !was || depth(p) >= depth(was) ? 1 : -1;
      if (how === 'pop' && was && depth(p) === depth(was)) dir = -1;
      to.style.setProperty('--dir', dir);
      if (from) {
        from.style.setProperty('--dir', -dir);
        from.classList.remove('is-on');
        from.inert = true;
      }
      void to.offsetWidth;
      to.classList.add('is-on');
      to.inert = false;
    }
    root.classList.toggle('is-wide', to.hasAttribute('data-wide'));
    drawBar(p);
    if (how !== 'start' && was) keepFocus(was, to);
    tell('mm:you-shown', group(p), p);
  }

  function reveal() {
    openNow = true;
    clearTimeout(hideTimer);
    root.hidden = false;
    panel.style.transform = '';
    void panel.offsetWidth;
    root.classList.add('is-open');
    letPageGo = window.MM.dom.holdPage();
    window.MM.inertPage.hold(root, function () {
      return ['watch-browse', 'watch-results', 'watch-all'].map(function (id) { return document.getElementById(id); }).filter(Boolean);
    });
    var button = document.querySelector('.profile-btn');
    if (button) button.setAttribute('aria-expanded', 'true');
    panel.focus({ preventScroll: true });
  }

  function show(p, how) {
    if (!openNow) reveal();
    var was = path;
    if (was && group(was) !== group(p)) tell('mm:you-hidden', group(was), was);
    path = p;
    var mine = ++turn;
    drawBar(p);
    if (ready[group(p)]) { place(was, p, how); return; }
    root.classList.add('is-loading');
    load(group(p)).then(function (ok) {
      if (path !== p || mine !== turn) return;
      root.classList.remove('is-loading');
      if (ok) place(was, p, how);
    });
  }

  function hide() {
    if (!openNow) return;
    openNow = false;
    var was = path;
    path = null;
    root.classList.remove('is-open', 'is-loading');
    window.MM.inertPage.release(root);
    if (letPageGo) letPageGo();
    letPageGo = null;
    var button = document.querySelector('.profile-btn');
    if (button) button.setAttribute('aria-expanded', 'false');
    var back = seen(opener) ? opener : button;
    opener = null;
    if (back) back.focus({ preventScroll: true });
    if (was) tell('mm:you-hidden', group(was), was);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      if (openNow) return;
      root.hidden = true;
      Array.prototype.forEach.call(views.querySelectorAll('.you-view.is-on'), function (v) {
        v.classList.remove('is-on');
        v.inert = true;
      });
    }, calm.matches ? CALM_MS : HIDE_MS);
  }

  /* ── Ways in and out ── */

  function go(p, opts) {
    opts = opts || {};
    p = clean(p);
    if (!allowed(p)) return;
    var h = opts.h || '';
    if (!openNow) {
      opener = document.activeElement;
      address.push('you', addressOf(p, h));
      cameFrom[entryId()] = null;
      show(p, 'push');
      return;
    }
    if (p === path && !h) return;
    then(ask(p), function () {
      var from = path;
      if (opts.replace) address.replace('you', addressOf(p, h));
      else {
        address.push('you', addressOf(p, h));
        cameFrom[entryId()] = from;
      }
      show(p, opts.replace ? 'replace' : 'push');
    });
  }

  function close() {
    if (!openNow) return;
    then(ask(null), function () { address.close('you'); });
  }

  /* Up goes back, where the entry before is where it leads; else it takes this entry's place. */
  function up() {
    var to = path && parent(path);
    if (!to) return;
    then(ask(to), function () {
      if (cameFrom[entryId()] === to) {
        window.history.back();
        return;
      }
      address.replace('you', addressOf(to));
      show(to, 'pop');
    });
  }

  address.layer({
    name: 'you',
    depth: 45,
    params: ['you', 'h'],
    under: true,
    read: function (here) {
      var p = here.params.get('you');
      return p === null ? null : clean(p);
    },
    open: function (p, how) {
      if (allowed(p)) show(p, how);
    },
    close: function () { hide(); },
    leave: function (to) { return ask(to); }
  });

  root.addEventListener('pointerdown', function (e) { pressedIn = panel.contains(e.target); });
  root.addEventListener('click', function (e) {
    var t = e.target;
    var dragged = pressedIn;
    pressedIn = false;
    // A drag that began in the panel and ended on the veil closes nothing.
    if (t === veil && dragged) return;
    if (t.closest('[data-you-close]')) { close(); return; }
    if (t.closest('[data-you-back]')) { up(); return; }
    var step = t.closest('[data-you-go]');
    if (step) {
      e.preventDefault();
      go(step.getAttribute('data-you-go'));
    }
  });

  /* A link to the pane's place steps to it; one that leaves the page from the pane asks first. */
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var link = e.target.closest && e.target.closest('a[href]');
    if (!link || (link.getAttribute('target') || '_self') !== '_self' || link.hasAttribute('download')) return;
    var href = link.getAttribute('href');
    var to = placeOf(href);
    if (to) {
      e.preventDefault();
      if (allowed(to.path)) go(to.path, { h: to.h });
      return;
    }
    if (!openNow || !root.contains(link) || href.charAt(0) === '#') return;
    var answer = ask(null);
    if (answer === true) return;
    e.preventDefault();
    answer.then(function (yes) { if (yes) window.location.assign(href); });
  });

  // After the modules' own: a menu, a fold or a rename takes its Escape first.
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !openNow || e.defaultPrevented) return;
    e.preventDefault();
    if (depth(path) === 2) up(); else close();
  });

  // A form whose script has not taken it would post to Watch.
  window.addEventListener('submit', function (e) {
    var form = e.target;
    if (!e.defaultPrevented && root.contains(form) && !form.hasAttribute('action')) e.preventDefault();
  });

  window.addEventListener('beforeunload', function (e) {
    var g = openNow && path ? guards[group(path)] : null;
    if (!g || !g.dirty(path, null)) return;
    e.preventDefault();
    e.returnValue = '';
  });

  /* Phones: the sheet is pulled down by its handle or its bar; past a point, or flicked, it closes. */
  var pull = null;
  function onDown(e) {
    if (!openNow || !phone.matches || e.button || (e.target.closest && e.target.closest('button'))) return;
    pull = { id: e.pointerId, y: e.clientY, t: e.timeStamp, by: 0 };
    if (e.currentTarget.setPointerCapture) e.currentTarget.setPointerCapture(e.pointerId);
    panel.classList.add('is-dragged');
  }
  function onMove(e) {
    if (!pull || e.pointerId !== pull.id) return;
    pull.by = Math.max(0, e.clientY - pull.y);
    panel.style.transform = 'translateY(' + pull.by + 'px)';
    veil.style.opacity = String(Math.max(0, 1 - pull.by / (panel.offsetHeight || 1)));
  }
  function onUp(e) {
    if (!pull || e.pointerId !== pull.id) return;
    var by = pull.by;
    var fast = by > 30 && by / Math.max(1, e.timeStamp - pull.t) > FLICK_PX_MS;
    pull = null;
    panel.classList.remove('is-dragged');
    veil.style.opacity = '';
    panel.style.transform = '';
    if (e.type === 'pointerup' && (by > PULL_PX || fast)) close();
  }
  [root.querySelector('.you__grab'), root.querySelector('.you__bar')].forEach(function (el) {
    if (!el) return;
    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
    el.addEventListener('lostpointercapture', onUp);
  });

  window.MM = window.MM || {};
  window.MM.you = {
    shown: function (g) { return openNow && !!path && group(path) === g; },
    scroller: function (view) { return viewOf(view || path || 'profile'); },
    go: go,
    close: close,
    guard: function (g, def) { guards[g] = def; },
    insert: insert
  };
  window.MM.history = window.MM.history || {};
  window.MM.history.address = {
    read: function () { return new URLSearchParams(new URLSearchParams(window.location.search).get('h') || ''); },
    write: function (params, how) {
      var to = addressOf('history', hOf(params || {}));
      if (how === 'push') address.push('you', to);
      else address.replace('you', to);
    }
  };
})();
