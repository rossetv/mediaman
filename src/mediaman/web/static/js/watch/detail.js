/* Watch page: the title detail overlay at #title=<ref>, on window.MMWatch.detail.
 * Opens from any [data-title-ref] and from the address; Back closes it, and
 * finds a title left for another page as it was left.
 * Needs watch/core.js, watch/address.js and watch/cast.js; uses watch/explore.js
 * for the genres, ratings.js for the person's own rating, watch/keep.js for a
 * viewer's Keep and MMWatch.admin, when loaded, for the admin facts' Keep and Delete.
 */
(function () {
  'use strict';

  var core = window.MMWatch && window.MMWatch.core;
  var root = document.getElementById('ttl');
  if (!core || !root) return;

  var address = window.MMWatch.address;
  var cast = window.MMWatch.cast;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var toArray = core.toArray;
  var safePath = core.safePath;

  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^(rec|pick)-[0-9]{1,10}$/;
  var PICK_RE = /^(rec|pick)-[0-9]{1,10}$/;
  var TRAILER_ORIGIN = 'https://www.youtube-nocookie.com';
  var YOUTUBE_KEY_RE = /^[A-Za-z0-9_-]{11}$/;
  var POLL_MS = 5000;
  /* A download that has not moved for this long is polled at POLL_SLOW_MS. */
  var POLL_BACKOFF_AFTER_MS = 120000;
  var POLL_SLOW_MS = 30000;
  /* How often an open title asks again whether a download the Arr no longer
     queues was removed or has not reached the Arr yet. */
  var RECHECK_MS = 60000;
  /* How long after this page's own Download a reply that knows nothing of it
     is the Arr catching up rather than the download being gone. */
  var DOWNLOAD_GRACE_MS = 30000;
  var PENDING_MS = 2000;  // between readings of a title whose seasons' states are still to come
  var PENDING_ASKS = 5;
  var COPIED_MS = 2000;
  /* How long a title waits for its picture, which settles the hero's height, for why it is a pick and for whether it can be rated. */
  var PICTURE_WAIT_MS = 400;
  var SEASON_WAIT_MS = 1500;
  /* How long after a close the next press of a double click can still come. */
  var DOUBLE_CLICK_MS = 600;
  var EXIT_FALLBACK = 80; // past an animation's own length, for one that never ends

  var sheet = root.querySelector('.ttl__sheet');
  var bar = root.querySelector('.ttl__bar');
  var hero = root.querySelector('.ttl__hero');
  var head = root.querySelector('.ttl__head');
  var titleEl = root.querySelector('#ttl-title');
  var part = function (name) { return root.querySelector('[data-ttl-part="' + name + '"]'); };
  var heroSkel = part('hero-skel');
  var art = part('art');
  var fill = part('fill');
  var poster = part('poster');
  var player = part('player');
  var closeBtn = root.querySelector('.ttl__close');
  var kindEl = part('kind');
  var taglineEl = part('tagline');
  var metaEl = part('meta');
  var body = part('body');
  var statusEl = part('status');

  var isAdmin = false;
  try {
    var dataNode = document.getElementById('watch-page-data');
    isAdmin = !!(dataNode && JSON.parse(dataNode.textContent).is_admin);
  } catch (e) { isAdmin = false; }

  var thumbs = null;   // the set of thumbs on show
  var cur = null;      // the title on show: { ref, data, facts, ... }
  var isOpen = false;
  var kept = {};       // the titles left, by ref, as they were left
  var closedAt = 0;
  var leaving = null;  // the close still animating, finished early by a new open
  var pictures = '';   // the addresses the hero shows or is trying
  var trying = false;  // the hero's picture is not known yet
  var onSettled = null;
  var trailerLeftAt = 0;
  var heroEase = null; // the hero's height, easing as the trailer comes or goes
  var seq = 0;
  var pollTimer = null;
  var seasonTimer = null;
  var shareTimer = null;
  /* What this page knows of a title's download, by ref: it outlives the
     overlay, so a title reopened mid-download still shows its state. */
  var known = {};

  /* ── Small builders ── */

  function button(cls, label, iconName) {
    var b = el('button', cls);
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName));
    if (label) b.appendChild(document.createTextNode(label));
    return b;
  }

  function skel(cls, width) {
    var s = el('span', 'skel ' + (cls || ''));
    if (width) s.style.width = width;
    s.setAttribute('aria-hidden', 'true');
    return s;
  }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function dur(minutes) {
    if (minutes < 60) return minutes + ' min';
    return Math.floor(minutes / 60) + ' h' + (minutes % 60 ? ' ' + (minutes % 60) + ' min' : '');
  }

  function day(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function hide(node, on) { node.hidden = !!on; }

  function apiUrl(ref) { return '/api/watch/title/' + encodeURIComponent(ref); }

  /* Cards of the title under another ref (a pick, a result) are found by what
     they print: its title, kind and year, whichever title is open by now. */
  function fire(ref, state, pct) {
    if (typeof CustomEvent !== 'function') return;
    var k = stateOf(ref);
    document.dispatchEvent(new CustomEvent('mm:title-changed', {
      detail: {
        ref: ref, download_state: state, download_pct: pct === undefined ? null : pct,
        title: k.title, kind: k.kind, year: k.year
      }
    }));
  }

  function learn(ref, data) {
    var k = stateOf(ref);
    k.title = data.title;
    k.kind = data.kind;
    k.year = data.year;
  }

  /* Queued from elsewhere on the page: the title's panel follows it as its own. */
  document.addEventListener('mm:title-changed', function (e) {
    var k = stateOf(e.detail.ref);
    if (e.detail.download_state !== 'queued' || k.state || k.busy) return;
    k.state = 'queued';
    k.sentAt = Date.now();
    redrawActions(e.detail.ref);
    if (isOpen && cur && cur.ref === e.detail.ref) {
      schedulePoll(POLL_MS);
      if (cur.data && cur.data.kind !== 'movie') readSeasons(cur);
    }
  });

  function nowInLibrary(ref) {
    var k = stateOf(ref);
    k.state = 'in_library';
    k.pct = null;
    k.tracked = false;
    fire(ref, 'in_library', null);
  }

  /* ── Address ── */

  function hashRef() {
    var m = /^#title=(.+)$/.exec(window.location.hash || '');
    if (!m) return null;
    var ref;
    try { ref = decodeURIComponent(m[1]); } catch (e) { return null; }
    return REF_RE.test(ref) ? ref : null;
  }

  function titleUrl(ref) { return address.url({}, 'title=' + encodeURIComponent(ref)); }

  /* The entry keeps the pick it was opened as: opened again from it, it is still one. */
  function keepPick() {
    if (!cur.pickRef || cur.pickRef === cur.ref) return;
    window.history.replaceState(Object.assign({}, window.history.state, { mmPick: cur.pickRef }), '');
  }

  /* ── Motion ── */

  function wide() { return window.matchMedia('(min-width: 700px)').matches; }

  /* Run *fn* once *node*'s animation ends, or after *ms* if it never does. */
  function afterAnimation(node, ms, fn) {
    var done = false;
    function go() {
      if (done) return;
      done = true;
      node.removeEventListener('animationend', onEnd);
      fn();
    }
    function onEnd(e) { if (e.target === node) go(); }
    node.addEventListener('animationend', onEnd);
    setTimeout(go, ms + EXIT_FALLBACK);
    return go;
  }

  /* The sheet grows out of the card that opened it (desktop only). */
  function growFrom(origin) {
    sheet.style.transformOrigin = '';
    if (!origin || !document.body.contains(origin) || !wide()) return;
    var from = (origin.querySelector && origin.querySelector('.watch-card__poster')) || origin;
    var r = from.getBoundingClientRect();
    var s = sheet.getBoundingClientRect();
    sheet.style.transformOrigin = Math.round(r.left + r.width / 2 - s.left) + 'px ' +
      Math.round(r.top + r.height / 2 - s.top) + 'px';
  }

  /* ── Opening and closing ── */

  function lockPage(on) {
    var html = document.documentElement;
    if (on) {
      // The scrollbar goes with the page's scroll; its width is held so nothing shifts.
      var bar = window.innerWidth - html.clientWidth;
      if (bar > 0) html.style.paddingRight = bar + 'px';
      html.classList.add('has-ttl');
    } else {
      html.classList.remove('has-ttl');
      html.style.paddingRight = '';
    }
  }

  function show(origin) {
    root.hidden = false;
    root.classList.remove('is-leaving', 'is-aside', 'is-trailer');
    nameClose();
    lockPage(true);
    MM.inertPage.hold(root);
    isOpen = true;
    growFrom(origin);
    root.classList.add('is-entering');
    var ms = Math.max(core.tokenMs('--dur-slow', 250), core.tokenMs('--dur', 200));
    afterAnimation(sheet, ms, function () { root.classList.remove('is-entering'); });
  }

  /* Open the title *ref*. opts: origin (the card), title (what the card
     already knows), pick (the AI pick it was listed as, when *ref* is the
     library's), push (add a history entry), atEpisodes (show its episodes),
     season (the number of the season it opens on, if not the show's own choice). */
  function open(ref, opts) {
    opts = opts || {};
    if (!REF_RE.test(ref)) return;
    if (isOpen && cur && cur.ref === ref) return;
    if (leaving) leaving();
    letGo();
    var wasOpen = isOpen;
    closeDialogs();
    if (wasOpen) keepPlace();
    stopPolling();
    dropRatings();
    cur = {
      ref: ref,
      origin: opts.origin || (wasOpen && cur ? cur.origin : null),
      title: opts.title || '',
      kind: kindOf(ref, opts.kind),
      data: null,
      facts: null,
      factsError: null,  // 'failed', or 'unknown' for a title no scan has seen
      keepIds: {},         // what has been offered to keep while it is open
      cardKeep: keepOnCard(opts.origin), // the season its card is of, by what keeps it
      pickRef: PICK_RE.test(ref) ? ref : PICK_RE.test(opts.pick || '') ? opts.pick : null,
      pick: null,          // why it is a pick, once known: { reason, because }
      onPick: null,
      onThumbs: null,
      atEpisodes: !!opts.atEpisodes,
      opensOn: opts.season,
      recId: /^rec-/.test(ref) ? ref.slice(4) : null,
      picks: null,         // the seasons ticked, for what *picksFor* says
      picksFor: null,
      ask: null,           // seasons asked for: { busy, text, bad, failed }
      asks: 0,
      pendingAsks: 0,
      season: null,
      chosen: false,        // the person picked the season, or their link named it
      pressed: false,       // something has been pressed since it came
      picked: null,         // the episodes of the season on show, when not the title's own
      pickedAbout: null,    // what is said of that season: { summary, year, words_pending }
      words: null,          // the season whose words are asked for: { number }
      opened: {},           // the texts opened by More, by what they are of
      seasonLoading: null,  // the season being loaded: { number }
      room: 0,              // what is left above the seasons, for them to stay where they were
      shareUrl: null,
      shareMsg: null,
      shareState: null,  // null, 'busy' or 'copied'
      justAdded: false,
      addedNow: false,
      movedAt: 0,
      checkedAt: 0,
      openedAs: ref,     // a title scanned meanwhile goes on under its library ref
      marksAsked: 0,
      ratings: null,     // those that come after the title: { early, came, asked }; early came with it, asked is the request
      keepComes: null,   // where the focus waits for a Keep button
      ctrl: null,
      seq: ++seq
    };
    if (opts.push) {
      address.push('title', titleUrl(ref));
      keepPick();
    }
    closeTrailer(false);
    renderLoading();
    if (!wasOpen) show(cur.origin);
    // The overlay is the scroller and keeps its offset across closes.
    root.scrollTop = 0;
    titleEl.focus({ preventScroll: true });
    load(cur);
  }

  /* What the title on show needs to be found as it is left. */
  function keepPlace() {
    if (!cur || !cur.data || cur.gone) return;
    cur.at = root.scrollTop;
    cur.focus = focusKey();
    cur.ring = !!document.activeElement && document.activeElement.matches(':focus-visible');
    kept[cur.ref] = cur;
  }

  /* A card off the page takes no focus, and would keep all it stood among. */
  function letGo() {
    Object.keys(kept).map(function (ref) { return kept[ref]; }).concat(cur || []).forEach(function (c) {
      if (c.origin && !document.body.contains(c.origin)) c.origin = null;
    });
  }

  /* Show a title left earlier as it was left, then bring it up to date. */
  function comeBack(c) {
    var wasOpen = isOpen;
    isOpen = true;
    if (leaving) leaving(); // it turns round from where it is
    closeDialogs();
    if (wasOpen) keepPlace();
    if (cur && cur.ctrl) cur.ctrl.abort();
    stopPolling();
    dropRatings();
    cur = c;
    c.ctrl = null;
    c.seasonLoading = null;
    closeTrailer(false);
    root.hidden = false;
    root.classList.remove('is-entering', 'is-leaving', 'is-trailer', 'ttl--admin');
    nameClose();
    if (!wasOpen) root.classList.add('is-aside');
    lockPage(true);
    MM.inertPage.hold(root);
    statusEl.textContent = '';
    body.style.marginBottom = '';
    showPictures(c.data);
    if (trying) {
      root.classList.remove('is-short', 'is-late');
      afterPictures(function () { if (c === cur && isOpen) settleHero(); });
    }
    renderContent(false);
    root.scrollTop = c.at;
    aboutMiddle();
    void root.offsetWidth; // it comes back from where it went
    root.classList.remove('is-aside');
    cast.land(c.focus);
    if (c.focus) restoreFocus(c.focus, c.ring);
    else titleEl.focus({ preventScroll: true });
    refresh(true);
    if (pollable()) schedulePoll(0);
  }

  /* What shrinks or grows does it about the middle of what is in sight. */
  function aboutMiddle() {
    sheet.style.transformOrigin = '50% ' + Math.round(root.scrollTop + window.innerHeight / 2 - sheet.offsetTop) + 'px';
  }

  /* 'movie', 'show' or '' while the title loads: from the ref, else from
     whoever opened it or a card of it on the page (a pick's ref says neither). */
  function kindOf(ref, given) {
    var m = /^(movie|tmdb-movie|show|tmdb-tv)-/.exec(ref);
    if (m) return /movie/.test(m[1]) ? 'movie' : 'show';
    if (given === 'movie' || given === 'show') return given;
    var cards = toArray(document.querySelectorAll('[data-title-ref][data-kind]')).filter(function (n) {
      return n.getAttribute('data-title-ref') === ref;
    });
    var kind = cards.length ? cards[0].getAttribute('data-kind') : '';
    return kind === 'movie' || kind === 'show' ? kind : '';
  }

  /* Where focus goes from a title no card opened, or whose card has gone. */
  function cardOf(ref) {
    return toArray(document.querySelectorAll('[data-title-ref]')).filter(function (n) {
      return n.getAttribute('data-title-ref') === ref && n.getClientRects().length && !n.closest('.is-leaving');
    })[0];
  }

  /* What the title on show has asked or opened must not outlive it, over a page that
     would have the focus. */
  function closeDialogs() {
    if (window.ModalA11y) window.ModalA11y.closeFrom(root);
  }

  function requestClose() { address.close('title'); }

  /* *aside*: another page takes the place of what is under the title, which
     Back will bring back. */
  function close(aside) {
    if (!isOpen) return;
    closeDialogs();
    keepPlace();
    isOpen = false;
    closedAt = Date.now();
    stopPolling();
    closeTrailer(false);
    closeAdminMenu(false);
    MM.inertPage.release(root);
    cast.drop();
    if (cur && cur.ctrl) cur.ctrl.abort();
    dropRatings();
    if (window.MMWatch.keep) window.MMWatch.keep.closeMenu(false);
    var origin = cur && cur.origin;
    seq++;
    root.classList.remove('is-entering');
    if (aside) {
      aboutMiddle();
      lockPage(false);
    }
    root.classList.add(aside ? 'is-aside' : 'is-leaving');
    leaving = afterAnimation(sheet, aside ? core.tokenMs('--dur-slow', 250) : core.tokenMs('--dur', 200), function () {
      leaving = null;
      if (isOpen) return;
      root.hidden = true;
      root.classList.remove('is-leaving', 'is-aside', 'ttl--admin');
      root.removeAttribute('aria-busy');
      emptyBody();
      lockPage(false);
    });
    var to = aside ? null : origin && document.body.contains(origin) && !origin.closest('.is-leaving') ? origin
      : cur && (cur.fallback || cardOf(cur.ref) || cardOf(cur.openedAs));
    if (to && document.body.contains(to)) to.focus({ preventScroll: true });
    else if (root.contains(document.activeElement)) document.activeElement.blur();
  }

  /* The title left the library: its cards and banner go from the library's rows, and those
     of what can be had lead to it there. The focus goes on from what it was on. */
  function drop(ref) {
    var path = '/watch/' + ref.replace('-', '/');
    function was(c) { return c && (c.ref === ref || c.openedAs === ref); }
    Object.keys(kept).forEach(function (key) { if (was(kept[key])) delete kept[key]; });
    var mine = was(cur) ? cur : null;
    if (mine) mine.gone = true; // not to be kept as it closes
    /* Where the focus goes from *node*, as *take* takes it away. */
    function leave(node, take) {
      var lost = node.contains(document.activeElement);
      var from = cur && cur.origin && node.contains(cur.origin);
      var to = take() || core.spareFocus();
      if (from) {
        cur.origin = null;
        cur.fallback = to;
      }
      if (lost && to) to.focus({ preventScroll: true });
    }
    toArray(document.querySelectorAll('.watch-banner')).forEach(function (banner) {
      var link = banner.querySelector('[data-title-ref]');
      if (banner.hasAttribute('inert') || (banner.getAttribute('data-dismiss-ref') !== ref &&
          !(link && link.getAttribute('data-title-ref') === ref))) return;
      leave(banner, function () {
        if (!(window.scrollY > 0)) {
          window.MMWatch.keep.foldAway(banner);
          banner.setAttribute('inert', '');
          return;
        }
        // Above what is on the screen, which stays where it is.
        var below = banner.nextElementSibling;
        var top = below && below.getBoundingClientRect().top;
        banner.remove();
        if (below) window.scrollBy({ top: below.getBoundingClientRect().top - top, behavior: 'instant' });
      });
    });
    toArray(document.querySelectorAll('.watch-card')).forEach(function (card) {
      var link = card.querySelector('.watch-card__link');
      if (!link || root.contains(card) || card.classList.contains('is-kept') || card.classList.contains('is-gone')) return;
      var own = link.getAttribute('data-title-ref');
      if (own !== ref && card.getAttribute('data-dismiss-ref') !== ref &&
          (link.getAttribute('href') || '').split('?')[0] !== path) return;
      var tmdb = link.getAttribute('data-tmdb-id');
      var there = /^tmdb-/.test(own || '') ? own
        : tmdb ? (/^movie-/.test(ref) ? 'tmdb-movie-' : 'tmdb-tv-') + tmdb : null;
      if (link.hasAttribute('data-in-library') && there) {
        link.removeAttribute('data-in-library');
        link.setAttribute('data-title-ref', there);
        link.setAttribute('href', '/watch#title=' + there);
        toArray(card.querySelectorAll('.watch-badge--in, .watch-badge--leaving')).forEach(function (b) { b.remove(); });
        return;
      }
      leave(card, function () {
        if (card.parentNode.querySelector('[data-results-head]')) return dropResult(card);
        return window.MMWatch.keep.fromRow(card, card.closest('.watch-row'), card.getClientRects().length > 0, '.watch-card__link').focus;
      });
    });
    // Opened from what stays but leads nowhere now, such as a suggestion.
    if (!mine || mine.fallback) return;
    var from = mine.origin && mine.origin.closest('[data-title-ref]');
    if (!from || from.getAttribute('data-title-ref') === ref) {
      mine.origin = null;
      mine.fallback = core.spareFocus();
    }
  }

  /* Out of the search results: its group's heading counts one fewer, and goes with the last. */
  function dropResult(card) {
    var grid = card.parentNode;
    function isCard(n) { return n && n.classList.contains('watch-card'); }
    var before = card.previousElementSibling;
    var after = card.nextElementSibling;
    var head = before;
    while (head && !head.hasAttribute('data-results-head')) head = head.previousElementSibling;
    card.remove();
    if (head && (isCard(before) || isCard(after))) {
      var count = head.querySelector('.results-group__count');
      count.textContent = count.textContent.replace(/\d+/g, function (n) { return n - 1; }).replace(/^1 titles/, '1 title');
    } else if (head) head.remove();
    var next = isCard(after) ? after : isCard(before) ? before : grid.querySelector('.watch-card');
    if (next) return next.querySelector('.watch-card__link');
    if (!grid.querySelector('.results-none')) grid.insertBefore(el('p', 'results-none', 'No title matches'), grid.children[0] || null);
    return null;
  }

  /* ── Loading ── */

  function get(url, ctrl) {
    return MM.api.get(url, ctrl ? { signal: ctrl.signal } : undefined);
  }

  function load(c) {
    if (c.ctrl) c.ctrl.abort();
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    c.ctrl = ctrl;
    // Asked for with the title, so that it is mostly known when the title arrives.
    var first = ownPicture(c.ref);
    if (first) el('img').setAttribute('src', first);
    if (c.pickRef && c.pickRef !== c.ref) askPick(c, ctrl);
    if (window.MM.ratings) window.MM.ratings.read([c.ref]);
    get(apiUrl(c.ref), ctrl).then(function (data) {
      if (c !== cur || !isOpen) return;
      if (c.ref === c.pickRef) takePick(c, data);
      if (data.library_ref && data.library_ref !== c.ref && REF_RE.test(data.library_ref)) {
        switchTo(c, data);
        return;
      }
      showPictures(data);
      afterPictures(function () {
        afterPick(c, function () {
          afterThumbs(c, function () {
            afterSeason(c, data, function () { if (c === cur && isOpen) arrive(c, data); });
          });
        });
      });
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (c !== cur || !isOpen) return;
      renderFailed(err);
    });
  }

  /* The episodes of the season the link named, where the show opens on another: on its
     own one if they cannot be had, or take long: they then come in their own time. */
  function afterSeason(c, data, then) {
    var s = (data.seasons || []).filter(function (one) { return one.number === c.opensOn && !one.selected; })[0];
    if (!s) { then(); return; }
    var late = false;
    function go() {
      clearTimeout(timer);
      if (!late) then();
      late = true;
    }
    var timer = setTimeout(go, SEASON_WAIT_MS);
    get(apiUrl(c.ref) + '/season/' + s.number, c.ctrl).then(function (got) {
      if (late) {
        comeLate(c, s, got);
        return;
      }
      takeSeason(c, s, got);
      go();
    }).catch(go);
  }

  /* The link's season, once the show stands open on its own: it takes over unless the person
     has begun something there (a Keep menu or dialog only opens by a press), or Keep is of
     another season that leaves. */
  function comeLate(c, s, got) {
    if (c !== cur || !isOpen || c.pressed || !body.querySelector('.ttl__seasonsec')) return;
    var active = document.activeElement;
    if (active && active.closest && active.closest('.ttl__season, .ttl__season-mark')) return;
    var now = numbered(c.data, s.number);
    if (!now) return;
    var left = leavingNow();
    var leaves = (c.data.leaving_seasons || []).filter(function (one) { return one.season === s.number && one.keep_id; })[0];
    if (left && left.id !== (leaves ? String(leaves.keep_id) : null)) return;
    var sec = body.querySelector('.ttl__seasonsec');
    markSeason(sec, s.number);
    var line = sec.querySelector('.watch-seasons');
    if (line) showPill(line, 'auto');
    showSeason(c, now, got, root.scrollTop ? null : titleEl);
  }

  /* A TMDB title found in the library opens as the library title. */
  function switchTo(c, data) {
    learn(c.ref, data);
    fire(c.ref, 'in_library', null);
    if (hashRef() === c.ref) readdress(data.library_ref);
    c.ref = data.library_ref;
    load(c);
  }

  /* The entry the address is at is the same title's, under another ref. */
  function readdress(ref) { address.replace('title', titleUrl(ref)); }

  /* ── Why it is a pick ── */

  function takePick(c, data) {
    c.pick = { reason: data.reason || '', because: data.because_of || [] };
  }

  /* A title open under its library ref says nothing of the pick it was listed as. */
  function askPick(c, ctrl) {
    if (c.pick) return;
    function known(data) {
      takePick(c, data);
      if (c !== cur || !isOpen) return;
      if (c.onPick) c.onPick();
      var box = c.data && body.querySelector('.ttl__reason');
      if (box) box.replaceWith(pickBox(c.pick));
    }
    get(apiUrl(c.pickRef), ctrl).then(known, function (err) {
      if (!(err && err.name === 'AbortError')) known({});
    });
  }

  /* Run *fn* once why the title is a pick is known, or that takes too long: it
     then comes into a box that waits for it. */
  function afterPick(c, fn) {
    if (!c.pickRef || c.pick) { fn(); return; }
    function go() {
      if (c.onPick !== go) return;
      c.onPick = null;
      clearTimeout(timer);
      fn();
    }
    var timer = setTimeout(go, PICTURE_WAIT_MS);
    c.onPick = go;
  }

  /* The AI label, the reason and the titles behind it; *pick* null: not known yet. */
  function pickBox(pick) {
    var box = el('section', 'ttl__reason');
    box.setAttribute('aria-label', 'Why it’s here');
    var head = el('p', 'ttl__reason-head');
    head.appendChild(core.aiLabel('AI pick'));
    box.appendChild(head);
    if (!pick) {
      box.setAttribute('aria-busy', 'true');
      box.appendChild(skel('skel--line'));
      box.appendChild(skel('skel--line', '62%'));
      return box;
    }
    if (pick.reason || pick.because.length) head.appendChild(el('span', '', 'Why it’s here'));
    if (pick.reason) box.appendChild(el('p', '', pick.reason));
    if (pick.because.length) {
      var because = el('p', 'ttl__because', 'Because you played ');
      pick.because.forEach(function (title, i) {
        if (i) because.appendChild(document.createTextNode(i === pick.because.length - 1 ? ' and ' : ', '));
        because.appendChild(el('b', '', title));
      });
      box.appendChild(because);
    }
    return box;
  }


  /* ── The person's own rating ── */

  /* Run *fn* once it is known whether the title can be rated, or that takes too long. One
     named by TMDB always can. */
  function afterThumbs(c, fn) {
    var ratings = window.MM.ratings;
    function known() { return ratings.canRate(c.ref) !== undefined; }
    if (!ratings || /^tmdb-/.test(c.ref) || known()) { fn(); return; }
    function go() {
      if (c.onThumbs !== told) return;
      c.onThumbs = null;
      clearTimeout(timer);
      fn();
    }
    function told() { if (known()) go(); }
    var timer = setTimeout(go, PICTURE_WAIT_MS);
    c.onThumbs = told;
  }

  document.addEventListener('mm:rating', function () { if (cur && cur.onThumbs) cur.onThumbs(); });

  function mountThumbs(main) {
    var ratings = window.MM.ratings;
    if (!ratings || ratings.canRate(cur.ref) === false) return;
    thumbs = ratings.mount(cur.ref);
    toArray(thumbs.node.querySelectorAll('button')).forEach(function (b, i) {
      b.setAttribute('data-ttl-act', 'rate-' + ratings.LEVELS[i].level);
    });
    main.appendChild(thumbs.node);
  }

  function dropThumbs() {
    if (thumbs) thumbs.destroy();
    thumbs = null;
  }

  function arrive(c, data) {
    c.data = data;
    c.title = data.title || c.title;
    learn(c.ref, data);
    takeDownload(c.ref, data.download, true);
    settleHero();
    renderContent(true);
    if (c.atEpisodes) toEpisodes();
    if (hasFacts(c)) loadFacts(c);
    if (pollable()) schedulePoll(0);
  }

  /* To the show's episodes, unless the person has scrolled or moved the focus meanwhile. */
  function toEpisodes() {
    var sec = body.querySelector('.ttl__seasonsec');
    if (!sec || root.scrollTop || document.activeElement !== titleEl) return;
    var heading = sec.querySelector('.ttl__sec-title');
    heading.setAttribute('tabindex', '-1');
    heading.focus({ preventScroll: true });
    // Not by its box, which is off while the sheet grows.
    root.scrollTo({ top: sheet.offsetTop + sec.offsetTop - bar.offsetHeight - 8, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* A title with no picture has a short head. */
  function settleHero() {
    root.classList.toggle('is-short', [heroSkel, art, fill, poster].every(function (n) { return n.hidden; }));
  }

  /* What a title reply says of its download, into what this page knows. A
     null state counts only when the Arr answered, and not just after this
     page's own Download, which the Arr may not list yet. *opened*: the title
     was just opened, so its cards are brought into line even with no change.
     Says whether the panel has changed. */
  function takeDownload(ref, dl, opened) {
    if (!dl) return false;
    var k = stateOf(ref);
    var was = k.state + ':' + k.pct;
    var wasTracked = k.tracked;
    var st = dl.state || null;
    if (st) {
      k.state = st;
      k.pct = st === 'downloading' && typeof dl.pct === 'number' ? Math.max(0, Math.min(100, Math.round(dl.pct)))
        : st === 'downloading' ? k.pct : null;
      k.tracked = false;
    } else if (dl.answered === true && !inGrace(k)) {
      if (inFlight(k)) {
        k.state = null;
        k.pct = null;
      }
      k.tracked = false;
    }
    var moved = k.state + ':' + k.pct !== was;
    if (moved || (opened && (k.state || dl.answered === true))) fire(ref, k.state, k.pct);
    return moved || k.tracked !== wasTracked;
  }

  /* A later reply of a title asked for again, into the panel's facts and what
     this page knows. Says whether the panel has changed. */
  function takeReply(ref, data) {
    if (data.in_library || data.library_ref) {
      nowInLibrary(ref);
      return true;
    }
    if (cur && cur.ref === ref && cur.data) cur.data = data;
    return takeDownload(ref, data.download);
  }

  /* Load the title again without the skeleton (after a Keep or a finished
     download); what changed is swapped in where it is. *quiet*: nothing is
     drawn again when the title is as it was. */
  function refresh(quiet) {
    var c = cur;
    if (!c || !isOpen || !c.data) return;
    get(apiUrl(c.ref)).then(function (data) {
      if (c !== cur || !isOpen) return;
      if (data.library_ref && data.library_ref !== c.ref && REF_RE.test(data.library_ref)) {
        c.justAdded = true;
        if (hashRef() === c.ref) readdress(data.library_ref);
        c.ref = data.library_ref;
        refresh();
        return;
      }
      keepStates(allSeasons(c.data), data);
      var same = quiet && JSON.stringify(data) === JSON.stringify(c.data);
      c.data = data;
      if (!same) renderContent(false);
      if (hasFacts(c)) loadFacts(c);
    }).catch(function (err) {
      if (c === cur && isOpen && err && err.status === 404 && /^(movie|show)-/.test(c.ref)) leftLibrary(c.ref);
    });
  }

  /* Deleted from under the overlay: nothing left to show. */
  function leftLibrary(ref) {
    drop(ref);
    if (isOpen && cur && cur.ref === ref) requestClose();
  }

  function hasFacts(c) { return isAdmin && /^(movie|show)-/.test(c.ref); }

  function loadFacts(c) {
    get('/api/watch/admin/title/' + encodeURIComponent(c.ref)).then(function (facts) {
      if (c !== cur || !isOpen) return;
      c.facts = facts;
      c.factsError = null;
      renderFacts(true);
    }).catch(function (err) {
      if (c !== cur || !isOpen) return;
      var gone = !!err && err.status === 404;
      if (gone && c.facts) {
        leftLibrary(c.ref);
        return;
      }
      c.factsError = gone ? 'unknown' : 'failed';
      renderFacts(false);
    });
  }

  /* ── Rendering ── */

  function emptyBody() {
    cast.drop(); // it stands on the people in there
    dropThumbs();
    body.textContent = '';
  }

  function renderLoading() {
    root.setAttribute('aria-busy', 'true');
    root.classList.remove('is-short', 'is-late');
    var origin = cur.origin && cur.origin.closest ? cur.origin : null;
    var card = origin && origin.closest('.watch-card');
    var held = /^(movie|show)-/.test(cur.ref) ||
      !!(origin && (origin.closest('[data-in-library]') || (card && card.querySelector('[data-in-library]'))));
    root.classList.toggle('ttl--admin', isAdmin && held);
    if (heroEase) heroEase.cancel();
    hide(heroSkel, false);
    clearPictures();
    kindEl.textContent = cur.kind ? kindLabel({ kind: cur.kind }) : '';
    titleEl.textContent = cur.title;
    if (!cur.title) titleEl.appendChild(skel('skel--line ttl__title-skel'));
    hide(taglineEl, false);
    taglineEl.textContent = '';
    taglineEl.appendChild(skel('skel--line ttl__tagline-skel'));
    metaEl.textContent = '';
    metaEl.appendChild(skel('skel--line ttl__meta-skel'));
    player.textContent = '';
    statusEl.textContent = '';

    emptyBody();
    body.style.marginBottom = '';
    var lead = el('div', 'ttl__lead');
    var main = el('div', 'ttl__main');
    var actions = el('div', 'ttl__actions');
    actions.appendChild(skel('skel--pill', '150px'));
    if (held && window.MM.playOn) actions.appendChild(skel('skel--pill ttl__play-on'));
    var search = window.MMWatch.search;
    if (search && search.canLike && search.canLike(cur.ref)) actions.appendChild(skel('skel--pill', '205px')).style.minWidth = '181px'; // as its words are on a phone
    actions.appendChild(skel('skel--pill', '158px')); // Trailer
    if (held) actions.appendChild(skel('skel--pill', '200px')).style.minWidth = '175px'; // Watched
    var leaves = held && card && card.querySelector('.watch-badge--leaving');
    if (leaves && !isAdmin) actions.appendChild(skel('skel--pill ttl__keep-skel', '94px'));
    main.appendChild(actions);
    var under = el('div', 'ttl__under');
    // Its card says how far it was watched and that it leaves, as the page is about to.
    if (held && card && card.querySelector('.watch-bar')) {
      var progress = el('div', 'ttl__progress');
      progress.appendChild(skel('skel--line', '240px'));
      under.appendChild(progress);
    }
    if (leaves) {
      var leaving = skel('ttl__leaving-skel');
      leaving.textContent = 'Leaving in 10 days. Keep it to stop the deletion.';
      var line = el('p', 'ttl__leaving');
      line.setAttribute('aria-hidden', 'true');
      line.appendChild(icon('i-clock'));
      line.appendChild(leaving);
      under.appendChild(line);
    }
    if (under.children.length) main.appendChild(under);
    if (window.MM.ratings && window.MM.ratings.canRate(cur.ref) !== false) {
      var thumbsSkel = el('div', 'ttl__rate-skel');
      thumbsSkel.appendChild(skel('skel--pill'));
      main.appendChild(thumbsSkel);
    }
    var pills = el('div', 'ttl__ratings ttl__ratings-skel');
    pills.appendChild(skel('skel--line', '220px'));
    main.appendChild(pills);
    var lines = el('div', 'ttl-skel-lines');
    lines.appendChild(skel('skel--line'));
    lines.appendChild(skel('skel--line'));
    lines.appendChild(skel('skel--line', '72%'));
    main.appendChild(lines);
    if (held && cur.kind === 'movie') main.appendChild(el('p', 'ttl__credit')).appendChild(skel('skel--line', '200px'));
    lead.appendChild(main);
    if (isAdmin && held) {
      var facts = factsCard();
      factsToCome(facts, false);
      lead.appendChild(facts.aside);
    }
    body.appendChild(lead);
    if (!held && (cur.kind === 'show' || /^tmdb-tv-/.test(cur.ref))) {
      var some = el('section', 'ttl__askseasons');
      some.setAttribute('aria-hidden', 'true');
      some.appendChild(skel('skel--line ttl__sec-skel'));
      var chips = el('div', 'season-pick__list');
      for (var n = 0; n < 4; n++) chips.appendChild(skel('season-chip__skel'));
      some.appendChild(chips);
      body.appendChild(some);
    }
    if (held && cur.kind !== 'movie') {
      var eps = el('section', 'ttl__seasonsec');
      eps.setAttribute('aria-hidden', 'true');
      eps.appendChild(skel('skel--line ttl__sec-skel'));
      eps.appendChild(episodeSkels(3));
      body.appendChild(eps);
    }
    var films = window.MMWatch.collection && (cur.kind === 'movie' || /^(tmdb-)?movie-/.test(cur.ref)) &&
      window.MMWatch.collection.skeleton(cur.ref);
    if (films) body.appendChild(films);
    var strip = el('section', 'ttl__castsec');
    strip.setAttribute('aria-hidden', 'true');
    strip.appendChild(skel('skel--line ttl__sec-skel'));
    var grid = el('div', 'ttl-cast');
    for (var i = 0; i < 8; i++) {
      var item = el('div', 'ttl-cast__item');
      item.appendChild(skel('skel--circle ttl-cast__avatar'));
      item.appendChild(skel('skel--line ttl-cast__name-skel'));
      grid.appendChild(item);
    }
    strip.appendChild(grid);
    body.appendChild(strip);
  }

  function renderFailed(err) {
    root.removeAttribute('aria-busy');
    hide(heroSkel, true);
    clearPictures();
    root.classList.add('is-short');
    hide(taglineEl, true);
    metaEl.textContent = '';
    if (!cur.title) titleEl.textContent = 'Details';
    emptyBody();
    var box = el('div', 'ttl__failed fade-in');
    box.setAttribute('role', 'alert');
    box.appendChild(icon('i-circle-exclamation'));
    box.appendChild(el('p', 'ttl__failed-line', "Couldn't load details"));
    var why = err && err.status === 404 ? 'This title is not available any more.'
      : err && err.error === 'tmdb_not_configured' ? 'Titles outside the library need TMDB, which is not set up.'
      : err && err.error === 'network_error' ? err.message
      : 'Something went wrong. Try again in a moment.';
    box.appendChild(el('p', 'ttl__failed-why', why));
    var row = el('div', 'ttl__failed-actions');
    var retry = button('btn btn--lg watch-btn--grey', 'Try again');
    retry.addEventListener('click', function () {
      if (!cur) return;
      renderLoading();
      titleEl.focus({ preventScroll: true });
      load(cur);
    });
    if (!err || err.status !== 404) row.appendChild(retry);
    var shut = button('btn btn--lg btn--ghost', 'Close');
    shut.addEventListener('click', requestClose);
    row.appendChild(shut);
    box.appendChild(row);
    body.appendChild(box);
  }

  function kindLabel(d) { return d.kind === 'movie' ? 'Movie' : 'TV series'; }

  function metaParts(d) {
    var parts = [];
    if (d.year) parts.push(d.year_end && d.year_end !== d.year ? d.year + '–' + d.year_end : String(d.year));
    if (d.kind === 'movie' && d.runtime_min) parts.push(dur(d.runtime_min));
    if (d.kind !== 'movie' && d.season_count) parts.push(plural(d.season_count, 'season', 'seasons'));
    return parts;
  }

  /* Those Explore has open there; the rest are words. */
  function genresPart(d) {
    var part = el('span');
    d.genres.slice(0, 3).forEach(function (name, i) {
      if (i) part.appendChild(document.createTextNode(', '));
      var ref = (d.genre_refs || []).filter(function (r) { return r.name === name; })[0];
      part.appendChild(ref ? genreLink(name, ref.genre) : document.createTextNode(name));
    });
    return part;
  }

  function genreLink(name, id) {
    return exploreLink('ttl__genre', name, 'genre=' + encodeURIComponent(id), 'genre-' + id, function (explore) {
      explore.openGenre(id, null, null);
    });
  }

  function directorLink(d) {
    return exploreLink('ttl__director', d.director, 'person=' + encodeURIComponent(d.director_id), 'director', function (explore) {
      explore.openPerson({ person_id: d.director_id, name: d.director, job: 'Director', profile_url: null }, null);
    });
  }

  /* A link to a page of Explore, which a plain press opens in place. */
  function exploreLink(cls, text, query, act, open) {
    var a = el('a', cls, text);
    a.setAttribute('href', '/watch?' + query);
    a.setAttribute('data-ttl-act', act);
    a.addEventListener('click', function (e) {
      if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || !window.MMWatch.explore) return;
      e.preventDefault();
      a.focus({ preventScroll: true }); // Safari leaves a pressed link without it
      open(window.MMWatch.explore);
    });
    return a;
  }

  /* ── The hero's picture ── */

  function clearPictures() {
    pictures = '';
    trying = false;
    onSettled = null;
    [art, fill, poster].forEach(function (img) {
      hide(img, true);
      img.onload = img.onerror = null;
      img.removeAttribute('src');
      img.removeAttribute('data-broken');
      img.classList.remove('is-loaded');
    });
  }

  function ownPicture(ref) {
    var key = /^(?:movie|show)-([0-9]+)$/.exec(ref || '');
    return key && '/api/poster/' + key[1] + '?kind=art';
  }

  /* Wide pictures to try in turn. For a title in the library the first is the
     banner's own, so opening the banner's title does not change its picture. */
  function widePictures(d) {
    return [d.in_library && ownPicture(d.library_ref), safePath(d.backdrop_url)].filter(Boolean);
  }

  function pictureSettled() {
    trying = false;
    var fn = onSettled;
    onSettled = null;
    if (fn) fn();
  }

  /* Run *fn* once the hero's picture has loaded or is known to be missing, or
     when that takes too long: the hero then keeps its full height. */
  function afterPictures(fn) {
    if (!trying) { fn(); return; }
    var timer = setTimeout(function () {
      if (onSettled !== go) return;
      onSettled = null;
      root.classList.add('is-late');
      fn();
    }, PICTURE_WAIT_MS);
    function go() {
      clearTimeout(timer);
      fn();
    }
    onSettled = go;
  }

  /* The first picture that loads and is wide, else the poster as a poster. */
  function showPictures(d) {
    var wide = widePictures(d);
    var last = safePath(d.poster_url);
    var wanted = wide.concat(last || '').join(' ');
    if (wanted === pictures) return;
    clearPictures();
    pictures = wanted;
    trying = true;
    hide(heroSkel, false);
    tryNext();

    function tryNext() {
      var src = wide.shift();
      if (!src) { showPoster(last); return; }
      art.removeAttribute('data-broken');
      art.onload = function () {
        // Loaded but marked: no wide picture (broken-poster.js).
        if (art.hasAttribute('data-broken')) { tryNext(); return; }
        hide(heroSkel, true);
        art.classList.add('is-loaded');
        pictureSettled();
      };
      art.onerror = tryNext;
      hide(art, false);
      art.setAttribute('src', src);
    }
  }

  /* Sharp and whole at its own shape, over a blurred copy that fills the hero. */
  function showPoster(src) {
    hide(art, true);
    hide(heroSkel, true);
    if (!src) { pictureSettled(); return; }
    var left = 2;
    function done() { if (!--left) pictureSettled(); }
    [fill, poster].forEach(function (img) {
      img.onload = function () { img.classList.add('is-loaded'); done(); };
      img.onerror = function () { hide(img, true); done(); };
      hide(img, false);
      img.setAttribute('src', src);
    });
  }

  function renderHead(d, fresh) {
    kindEl.textContent = kindLabel(d);
    var name = d.title || cur.title;
    titleEl.textContent = name;
    titleEl.classList.toggle('ttl__title--long', name.length > 40);
    taglineEl.textContent = d.tagline || '';
    metaEl.textContent = '';
    metaParts(d).forEach(function (p) { metaEl.appendChild(el('span', '', p)); });
    if (d.genres && d.genres.length) metaEl.appendChild(genresPart(d));
    if (d.certificate) {
      var cert = el('span', 'ttl__cert', d.certificate);
      cert.setAttribute('aria-label', 'Rated ' + d.certificate);
      metaEl.appendChild(cert);
    }
    showPictures(d);
    if (fresh) [kindEl, metaEl, taglineEl].forEach(core.fadeIn);
  }

  /* Re-render the content, keeping focus on the same control when it is
     drawn again (or on the first action when it is gone). */
  function renderContent(fresh) {
    var d = cur.data;
    closeOtherKeep();
    var key = focusKey();
    var keepComes = key && key === cur.keepComes;
    cur.keepComes = null;
    var line = body.querySelector('.watch-seasons');
    var lineAt = line ? line.scrollLeft : null;
    root.removeAttribute('aria-busy');
    renderHead(d, fresh);
    emptyBody();
    var lead = el('div', 'ttl__lead');
    var main = el('div', 'ttl__main');
    lead.appendChild(main);
    var actions = el('div', 'ttl__actions');
    actions.setAttribute('data-ttl-part', 'actions');
    var under = el('div', 'ttl__under');
    under.setAttribute('data-ttl-part', 'under');
    main.appendChild(actions);
    main.appendChild(under);
    fillActions(actions, under);
    hide(under, !under.children.length);
    mountThumbs(main);
    var ratings = ratingsRow(d);
    if (ratings) main.appendChild(ratings);
    askRatings(cur);
    if (cur.pickRef) main.appendChild(pickBox(cur.pick));
    if (d.overview) main.appendChild(el('p', 'ttl__overview', d.overview));
    if (d.director && d.kind === 'movie') {
      var credit = el('p', 'ttl__credit');
      credit.appendChild(el('span', 'ttl__credit-label', 'Director'));
      credit.appendChild(d.director_id ? directorLink(d) : document.createTextNode(d.director));
      main.appendChild(credit);
    }
    body.appendChild(lead);
    if (!d.in_library) placeSeasons();
    if (d.in_library && d.kind !== 'movie' && d.episodes && d.episodes.length) body.appendChild(episodesSection(d));
    else cur.seasonLoading = null;
    if (d.in_library) placeSeasons();
    if (window.MMWatch.collection) window.MMWatch.collection.into(body, cur.ref, d.kind, d.in_collection);
    if (d.cast && d.cast.length) body.appendChild(cast.section(d.cast));
    if (hasFacts(cur)) renderFacts(false);
    startSeasonLine(lineAt);
    var season = body.querySelector('.ttl__season');
    if (season) fitTexts(season);
    if (fresh) {
      if (season) season.classList.add('is-arriving');
      toArray(main.children).concat(toArray(body.children).slice(1)).forEach(function (n) {
        n.classList.add('is-swapping-in');
      });
    }
    if (season) askWords(cur);
    scheduleSeasons();
    restoreFocus(keepComes && byAct('keep') ? 'keep' : key);
  }

  function keepOnCard(origin) {
    var card = origin && origin.closest && origin.closest('.watch-card');
    return (card && card.getAttribute('data-keep-id')) || '';
  }

  /* What of the title leaves, and a Keep here keeps: of a show the season the person
     picked; opened by a link that names a season, the one on show; else the one on the
     card it was opened from, else the first that goes.
     Never another season than the one asked about. */
  function leavingNow() {
    var d = cur.data;
    var found = d.leaving;
    if (d.kind !== 'movie') {
      found = (d.leaving_seasons || []).filter(function (s) {
        if (cur.chosen) return s.season === cur.season;
        if (cur.opensOn !== undefined && d.seasons && d.seasons.length) return s.season === ownSeason(d).number;
        return !cur.cardKeep || String(s.keep_id) === cur.cardKeep;
      })[0];
    }
    if (!found || !found.keep_id) return null;
    cur.keepIds[found.keep_id] = true;
    return { id: String(found.keep_id), days: found.days, season: d.kind === 'movie' ? null : found.season };
  }

  function leavesWhen(days) {
    return days < 0 ? 'any day' : days === 0 ? 'today' : 'in ' + plural(days, 'day', 'days');
  }

  function leavingText(leaves) {
    return (leaves.season === null ? 'Leaving ' : 'Season ' + leaves.season + ' leaves ') + leavesWhen(leaves.days) + '.';
  }

  var RATINGS = ['imdb', 'rt', 'tmdb', 'metascore'];
  var WIDEST = { imdb: '10.0', rt: '100%', metascore: '100' };

  function ratingPill(key, value) {
    var glyph = { imdb: '★', rt: '🍅', tmdb: '★' }[key];
    var text = key === 'imdb' ? 'IMDb ' + value : key === 'rt' ? 'RT ' + value
      : key === 'tmdb' ? 'TMDB ' + value.toFixed(1) : ' ' + String(value).replace(/\/100$/, '');
    var p = el('span', 'ttl-rating ttl-rating--' + (key === 'metascore' ? 'meta' : key));
    p.setAttribute('data-rating', key);
    if (glyph) {
      var g = el('span', 'ttl-rating__icon', glyph);
      g.setAttribute('aria-hidden', 'true');
      p.appendChild(g);
    }
    var words = p.appendChild(el('span', 'ttl-rating__words'));
    if (key === 'metascore') {
      words.appendChild(el('span', 'ttl-rating__full', 'Metascore'));
      var brief = words.appendChild(el('span', 'ttl-rating__brief'));
      brief.setAttribute('data-brief', 'Meta');
      brief.setAttribute('aria-hidden', 'true');
    }
    words.appendChild(document.createTextNode(text));
    return p;
  }

  /* Those *r* has. */
  function ratingKeys(r) {
    return RATINGS.filter(function (key) { return key === 'tmdb' ? typeof r.tmdb === 'number' && r.tmdb > 0 : !!r[key]; });
  }

  function pillsRow(keys, r) {
    var row = el('div', 'ttl__ratings');
    keys.forEach(function (key) { row.appendChild(ratingPill(key, r[key])); });
    return row;
  }

  /* What is not here yet, as far as there is room on the line. */
  function pillsToCome(keys) {
    var wait = el('div', 'ttl__ratings-wait');
    wait.setAttribute('aria-hidden', 'true');
    Object.keys(WIDEST).forEach(function (key) {
      if (keys.indexOf(key) > -1) return;
      var p = wait.appendChild(ratingPill(key, WIDEST[key]));
      p.className = 'ttl-rating skel';
      p.removeAttribute('data-rating');
    });
    return wait;
  }

  function ratingsRow(d) {
    var r = d.ratings || {};
    if (d.ratings_pending && !cur.ratings) cur.ratings = { early: r, came: null, asked: null };
    var w = cur.ratings;
    if (!w) return ratingKeys(r).length ? pillsRow(ratingKeys(r), r) : null;
    // What was here first keeps its place.
    var early = ratingKeys(w.early);
    var all = Object.assign({}, w.early);
    [r, w.came || {}].forEach(function (more) {
      ratingKeys(more).forEach(function (key) { all[key] = more[key]; });
    });
    var keys = early.concat(ratingKeys(all).filter(function (key) { return early.indexOf(key) === -1; }));
    if (w.came && !keys.length) return null;
    var row = pillsRow(keys, all);
    row.setAttribute('data-ttl-part', 'ratings');
    if (!w.came) row.appendChild(pillsToCome(keys));
    return row;
  }

  /* Ratings that take their time are asked for once the title is on show. */
  function askRatings(c) {
    var w = c.ratings;
    if (!w || w.asked) return;
    var asked = w.asked = new AbortController();
    function take(ratings) {
      if (w.asked !== asked || c !== cur || !isOpen) return;
      w.came = ratings;
      var was = part('ratings');
      if (was) drawRatings(was, ratingsRow(c.data));
    }
    get(apiUrl(c.ref) + '/ratings', asked).then(function (reply) { take((reply && reply.ratings) || {}); }, function () { take({}); });
  }

  /* Those that came in the place held for them. What follows is slid to where more lines, or none,
     put it, or stays where it is when they are out of sight above. */
  function drawRatings(was, row) {
    var below = followers(was);
    function swap() {
      if (!row) { was.remove(); return; }
      was.replaceWith(row);
      toArray(row.children).forEach(function (p) {
        if (!was.querySelector('[data-rating="' + p.getAttribute('data-rating') + '"]')) core.fadeIn(p);
      });
    }
    if (below.length && was.getBoundingClientRect().bottom < root.getBoundingClientRect().top) {
      holdStill(function () { return below[0]; }, swap);
      return;
    }
    var slide = core.flip(below);
    swap();
    slide();
  }

  /* What stands after *node* in the body, by its largest parts. */
  function followers(node) {
    var out = [];
    for (; node && node !== body; node = node.parentNode) {
      for (var n = node.nextElementSibling; n; n = n.nextElementSibling) out.push(n);
    }
    return out;
  }

  /* For when the title is come back to, they are asked for again. */
  function dropRatings() {
    var w = cur && cur.ratings;
    if (!w || !w.asked || w.came) return;
    w.asked.abort();
    w.asked = null;
  }

  /* ── Focus ── */

  function focusKey() {
    var a = document.activeElement;
    if (!a || !root.contains(a)) return null;
    var keyed = a.closest && a.closest('[data-ttl-act]');
    return keyed ? keyed.getAttribute('data-ttl-act') : null;
  }

  function byAct(key) { return key && root.querySelector('[data-ttl-act="' + key + '"]'); }

  /* *ring*: whether it is shown to have it; left to the browser when not given. */
  function restoreFocus(key, ring) {
    if (!key) return;
    var target = byAct(key) ||
      root.querySelector('[data-ttl-part="actions"] > *');
    if (target) target.focus(Object.assign({ preventScroll: true }, ring === undefined ? null : { focusVisible: ring }));
    else titleEl.focus({ preventScroll: true });
  }

  /* A dialog opened from a button drawn anew gives the focus back to the one there is. */
  function handOnDialogs() {
    if (!window.ModalA11y) return;
    window.ModalA11y.handOn(function (from) {
      var act = !from.isConnected && from.getAttribute('data-ttl-act');
      return act ? byAct(act) || titleEl : null;
    });
  }

  /* ── Actions ── */

  function fillActions(actions, under) {
    var d = cur.data;
    if (d.in_library && d.play && !cur.justAdded) {
      actions.appendChild(playLink(d.play));
      if (window.MM.playOn) {
        var tv = actions.appendChild(window.MM.playOn.button(d.play.href, 'btn btn--lg watch-btn--grey ttl__play-on'));
        tv.setAttribute('data-ttl-act', 'play-on');
      }
      if (d.play.progress_pct) {
        var progress = el('div', 'ttl__progress');
        var bar = el('progress', 'watch-bar watch-bar--banner');
        bar.max = 100;
        bar.value = d.play.progress_pct;
        bar.setAttribute('aria-hidden', 'true');
        progress.appendChild(bar);
        if (d.play.minutes_left) progress.appendChild(el('span', '', dur(d.play.minutes_left) + ' left'));
        under.appendChild(progress);
      }
    } else if (!d.in_library || cur.justAdded) {
      downloadPart(actions, under);
    }
    var leaves = leavingNow();
    if (leaves) {
      var line = el('p', 'ttl__leaving');
      line.appendChild(icon('i-clock'));
      line.appendChild(document.createTextNode(leavingText(leaves) + ' Keep it to stop the deletion.'));
      under.appendChild(line);
    }
    var like = likeButton(d);
    if (like) actions.appendChild(like);
    var trailer = trailerButton(d);
    if (trailer) actions.appendChild(trailer);
    var mark = titleMark(d);
    if (mark) actions.appendChild(mark);
    var keep = keepButton(d);
    if (keep) actions.appendChild(keep);
    var share = shareButton();
    if (share) actions.appendChild(share);
    if (cur.shareMsg) under.appendChild(note(cur.shareMsg));
    if (cur.shareUrl) under.appendChild(shareField(cur.shareUrl));
  }

  function playLink(play) {
    var a = el('a', 'btn btn--lg watch-btn--play');
    a.setAttribute('href', safePath(play.href) || '#');
    a.setAttribute('data-ttl-act', 'primary');
    a.appendChild(icon('i-play'));
    a.appendChild(document.createTextNode(play.label || 'Play'));
    return a;
  }

  /* Titles like it, where there is an AI to find them. */
  function likeButton(d) {
    var search = window.MMWatch.search || {};
    var byId = d.download && d.download.tmdb_id ? 'tmdb-' + (d.kind === 'movie' ? 'movie-' : 'tv-') + d.download.tmdb_id : '';
    var ref = [d.library_ref, d.ref, cur.ref, byId].filter(function (r) { return search.canLike && search.canLike(r); })[0];
    if (!ref) return null;
    var b = button('btn btn--lg watch-btn--grey', 'Find more like this', 'i-sparkles');
    b.setAttribute('data-ttl-act', 'like');
    b.addEventListener('click', function () { search.like(ref, d.title || cur.title, d.poster_url); });
    return b;
  }

  // It may come to be known after the title has opened.
  document.addEventListener('mm:search-assist', function () { if (cur) redrawActions(cur.ref); });

  /* ── Watched marks: watch/marks.js works them, then says so. ── */

  function markRef() { return cur.data.library_ref || cur.ref; }

  function titleMark(d) {
    var mark = core.markOf(d);
    if (!mark) return null;
    var b = core.markButton('btn btn--lg watch-btn--grey', mark, d.watched, d.title || cur.title,
      d.kind === 'movie' ? 'Mark as watched' : 'Mark show as watched');
    b.setAttribute('data-ttl-act', 'mark');
    return b;
  }

  function seasonMark(s) {
    var mark = s && core.markOf({ mark_kind: 'season', mark_key: s.key });
    if (!mark) return null;
    var b = core.markButton('btn watch-btn--grey ttl__season-mark', mark, s.watched, seasonName(s), 'Mark season as watched');
    b.setAttribute('data-mark-ref', markRef());
    b.setAttribute('data-ttl-act', 'mark-season');
    return b;
  }

  function episodeMark(e) {
    var mark = core.markOf({ mark_kind: 'episode', mark_key: e.key });
    if (!mark) return null;
    var b = core.markToggle(mark, e.watched, e.title || 'Episode ' + e.number);
    b.setAttribute('data-mark-ref', markRef());
    b.setAttribute('data-ttl-act', 'mark-' + e.key);
    return b;
  }

  /* A mark changes what Play plays and what the seasons and episodes say. The season on
     show stays, whichever the show would now open on, and so does what was pressed. */
  function reloadMarks(c) {
    var asked = ++c.marksAsked;
    var season = c.season;
    Promise.all([get(apiUrl(c.ref)), season === null ? null : get(apiUrl(c.ref) + '/season/' + encodeURIComponent(season))])
      .then(function (got) {
        if (c !== cur || !isOpen || asked !== c.marksAsked) return;
        c.data = got[0];
        if (got[1] && c.season === season) takeEpisodes(c, got[1]);
        var key = focusKey();
        if (byAct(key)) holdStill(function () { return byAct(key); }, function () { renderContent(false); });
        else renderContent(false);
        if (core.markOf(c.data)) window.MMWatch.marks.paint(core.markOf(c.data), c.data.watched);
      }).catch(function () {});
  }

  document.addEventListener('mm:watch-marked', function (e) {
    if (isOpen && cur && cur.data && e.detail.ref === markRef()) reloadMarks(cur);
  });

  function note(text, iconName, iconClass) {
    var p = el('p', 'ttl__note');
    if (iconName) p.appendChild(icon(iconName, iconClass));
    p.appendChild(el('span', '', text));
    return p;
  }

  /* A viewer keeps a leaving title through the shared Keep menu (keep.js);
     an admin keeps from the facts panel. */
  function keepButton(d) {
    var api = window.MMWatch.admin;
    var leaves = leavingNow();
    if (!d.keep_allowed || !leaves || (isAdmin && api && api.keep)) return null;
    var b = el('button', 'btn watch-btn--keep');
    b.type = 'button';
    b.setAttribute('data-watch-keep', leaves.id);
    b.setAttribute('data-ttl-act', 'keep');
    b.setAttribute('aria-haspopup', 'menu');
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-label', 'Keep ' + (d.title || cur.title) + (leaves.season === null ? '' : ', Season ' + leaves.season));
    b.appendChild(el('span', '', 'Keep'));
    b.appendChild(icon('i-chevron-down', 'watch-keep__chev'));
    return b;
  }

  /* The Keep menu open on a season that the actions about to be drawn are not of
     closes, its focus on the button they take the place of. */
  function closeOtherKeep() {
    var keep = window.MMWatch.keep;
    var trigger = keep && keep.menuTrigger();
    if (!trigger || !root.contains(trigger)) return;
    var leaves = leavingNow();
    if (!leaves || leaves.id !== trigger.getAttribute('data-watch-keep')) keep.closeMenu(true);
  }

  /* ── Download ── */

  function stateOf(ref) {
    if (!known[ref]) known[ref] = { state: null, pct: null, sizeDone: '', sizeTotal: '', eta: '', busy: false, error: null, sentAt: 0, tracked: false, title: null, kind: null, year: null };
    return known[ref];
  }

  function inFlight(k) { return k.state === 'queued' || k.state === 'downloading'; }

  function inGrace(k) { return !!k.sentAt && Date.now() - k.sentAt < DOWNLOAD_GRACE_MS; }

  /* Whether the Download button of a title not in the library is on show. */
  function offersDownload(d) {
    var k = stateOf(cur.ref);
    if (d.in_library || cur.justAdded || !d.download || k.state || k.tracked) return false;
    if (d.kind === 'movie') return true;
    return !d.sonarr_tracked && !allHeld(d);
  }

  function allHeld(d) {
    return !!d.seasons && d.seasons.length > 0 && d.seasons.every(function (s) { return s.in_library; });
  }

  function seasonPickable(d) {
    return d.kind !== 'movie' && d.download && d.download.request && d.seasons && d.seasons.length;
  }

  /* A show's seasons, those Plex has and those only TMDB knows, by number. */
  function allSeasons(d) {
    return (d.seasons || []).concat(d.more_seasons || []).sort(function (a, b) { return a.number - b.number; });
  }

  /* Whether a season can be ticked: with the Download button every one not here, until
     Sonarr says otherwise; else those that can be asked for. */
  function canPick(d, s) {
    if (offersDownload(d) && seasonPickable(d)) return s.status ? !!s.askable : !s.in_library;
    return !!d.seasons_request && s.askable === true;
  }

  /* With the Download button those that are out are ticked to begin with, else none. */
  function picksOf(d) {
    var whole = offersDownload(d);
    if (!cur.picks || cur.picksFor !== whole) {
      cur.picks = {};
      cur.picksFor = whole;
      if (whole) allSeasons(d).forEach(function (s) { if (canPick(d, s) && s.status !== 'not_out') cur.picks[s.number] = true; });
    }
    return cur.picks;
  }

  function pickedSeasons(d) {
    var picks = picksOf(d);
    return allSeasons(d).filter(function (s) { return canPick(d, s) && picks[s.number]; })
      .map(function (s) { return s.number; });
  }

  function statePill(cls, text, iconName, spinner) {
    var s = el('span', 'dl-state ' + cls);
    s.setAttribute('role', 'status');
    s.setAttribute('tabindex', '-1');
    s.setAttribute('data-ttl-act', 'primary');
    if (spinner) {
      var sp = el('span', 'btn-spinner');
      sp.setAttribute('aria-hidden', 'true');
      s.appendChild(sp);
    } else if (iconName) {
      s.appendChild(icon(iconName));
    }
    s.appendChild(document.createTextNode(text));
    return s;
  }

  function followNote(text) {
    var p = el('p', 'ttl__note');
    var words = p.appendChild(el('span', '', text + 'Follow it in '));
    var link = words.appendChild(el('a', '', 'Downloads'));
    link.setAttribute('href', '/downloads');
    words.appendChild(document.createTextNode('.'));
    return p;
  }

  function downloadPart(actions, under) {
    var d = cur.data;
    var k = stateOf(cur.ref);
    var rec = !!cur.pickRef;
    if (k.state === 'in_library' || cur.justAdded) {
      actions.appendChild(d.play && d.in_library ? playLink(d.play) : statePill('dl-state--done', 'In library', 'i-circle-check'));
      if (cur.addedNow) under.appendChild(note('In your library. Added just now.', 'i-circle-check', 'ttl__note-ok'));
      return;
    }
    if (k.state === 'queued') {
      actions.appendChild(statePill('dl-state--queued', 'Queued', 'i-clock'));
      under.appendChild(followNote('Waiting for a download slot. '));
      if (rec && d.download && d.download.notify === true) under.appendChild(emailHint(d));
      return;
    }
    if (k.state === 'downloading') {
      var pct = k.pct || 0;
      actions.appendChild(statePill('dl-state--downloading', 'Downloading ' + pct + '%', null, true));
      var box = el('div', 'dl-progress');
      var bar = el('progress', 'watch-bar watch-bar--banner');
      bar.max = 100;
      bar.value = pct;
      bar.setAttribute('aria-label', 'Download progress');
      box.appendChild(bar);
      var line = el('div', 'dl-progress__line');
      line.appendChild(el('span', '', k.sizeDone && k.sizeTotal ? k.sizeDone + ' of ' + k.sizeTotal : ''));
      line.appendChild(el('span', '', k.eta ? k.eta.replace(/^~/, 'about ').replace(/ remaining$/, ' left') : ''));
      box.appendChild(line);
      under.appendChild(box);
      under.appendChild(followNote(''));
      if (rec && d.download && d.download.notify === true) under.appendChild(emailHint(d));
      return;
    }
    if (!d.download) return;
    var tv = d.kind !== 'movie';
    if (k.tracked || (tv && d.sonarr_tracked)) {
      actions.appendChild(statePill('dl-state--done', tv ? 'Already in Sonarr' : 'Already in Radarr', 'i-circle-check'));
      return;
    }
    if (tv && allHeld(d)) {
      actions.appendChild(statePill('dl-state--done', 'All seasons in library', 'i-circle-check'));
      return;
    }
    var dl = button('btn btn--lg btn--primary ttl__download', '', 'i-download');
    dl.setAttribute('data-ttl-act', 'primary');
    dl.appendChild(el('span', 'ttl__download-label'));
    if (k.busy) {
      dl.textContent = '';
      var spin = el('span', 'btn-spinner');
      spin.setAttribute('aria-hidden', 'true');
      dl.appendChild(spin);
      dl.appendChild(el('span', 'ttl__download-label', 'Adding…'));
      dl.setAttribute('aria-disabled', 'true');
    }
    dl.addEventListener('click', submitDownload);
    actions.appendChild(dl);
    if (!k.busy) labelDownload(dl, d);
    if (k.error) {
      var err = el('p', 'ttl__error');
      err.setAttribute('role', 'alert');
      err.appendChild(icon('i-circle-exclamation'));
      err.appendChild(el('span', '', k.error));
      var again = button('ttl__retry', 'Try again');
      again.setAttribute('data-ttl-act', 'retry');
      again.addEventListener('click', submitDownload);
      err.appendChild(again);
      under.appendChild(err);
    }
  }

  function labelDownload(dl, d) {
    var label = dl.querySelector('.ttl__download-label');
    if (!seasonPickable(d)) {
      label.textContent = 'Download';
      return;
    }
    var n = pickedSeasons(d).length;
    label.textContent = n ? 'Download ' + plural(n, 'season', 'seasons') : 'Pick seasons';
    if (n) dl.removeAttribute('aria-disabled'); else dl.setAttribute('aria-disabled', 'true');
  }

  /* Only for a pick being downloaded, and only to a person the server says it
     will email. */
  function emailHint(d) {
    return note("You can close this — you'll get an email when " +
      (d.kind === 'movie' ? 'the movie' : 'the show') + " is ready.", 'i-envelope');
  }

  /* ── A show's seasons: the state of each, and asking for more ── */

  var ON_THE_WAY = { downloading: 1, queued: 1, asked: 1 };
  var REFUSED = { leaving: 'Leaving soon', removed: 'Deleted', not_listed: 'Not in Sonarr yet', show_unmonitored: 'Off in Sonarr' };

  function held(s) { return s.status ? s.status === 'held' : !!s.in_library; }

  function comes(s) { return !!ON_THE_WAY[s.status] || (!!s.asked && (s.status === 'partly_held' || s.status === 'not_out')); }

  /* TMDB's day, which is of no time zone. */
  function outOn(s) {
    var d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(s.air_date || '') ? s.air_date + 'T12:00:00Z' : NaN);
    if (isNaN(d.getTime()) || d.getTime() < Date.now()) return 'Not out yet';
    return 'Out ' + d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  /* What a season's chip says under its name, and the icon of it. */
  function seasonState(d, s) {
    var count = s.episode_count ? plural(s.episode_count, 'episode', 'episodes') : '';
    var no = REFUSED[s.refusal] || '';
    function lead(text, after) { return after ? text + ' · ' + after : text; }
    if (held(s)) return { name: 'held', icon: 'i-check', text: 'In library' };
    if (s.status === 'downloading') return { name: 'coming', icon: 'i-arrow-down', text: 'Downloading' };
    if (s.status === 'queued') return { name: 'queued', icon: 'i-clock', text: 'Queued' };
    if (s.status === 'asked') return { name: 'queued', icon: 'i-clock', text: 'Asked for' };
    if (s.status === 'partly_held') {
      return { name: s.asked ? 'queued' : 'some', icon: s.asked ? 'i-clock' : 'i-check',
        text: lead('Partly here', s.asked ? 'asked for' : no || (s.askable ? 'get the rest' : '')) };
    }
    if (s.status === 'not_out') {
      return { name: s.asked ? 'queued' : 'none', icon: s.asked ? 'i-clock' : 'i-check', text: lead(outOn(s), s.asked ? 'asked for' : no) };
    }
    if (no) return { name: 'none', icon: 'i-lock', text: no };
    if (d.seasons_pending) return { name: 'none', icon: 'i-check', text: 'Checking…' };
    return { name: 'none', icon: 'i-check', text: count || (s.status || canPick(d, s) ? '' : 'Not here') };
  }

  /* The seasons the section names: of a show in the library, those not wholly here. */
  function listed(d) {
    return d.kind === 'movie' ? [] : allSeasons(d).filter(function (s) { return !d.in_library || !held(s); });
  }

  function placeSeasons() {
    var wrap = listed(cur.data).length ? body.appendChild(seasonsSection(cur.data)) : null;
    // A show's episodes come first; the line of them says what there is below.
    linkSeasons(wrap);
  }

  /* On the line that says how many episodes a season has, so that no episode moves for it. */
  function linkSeasons(wrap) {
    var line = body.querySelector('.ttl__season:not(.is-gone) .ttl__season-line');
    if (!line) return;
    var link = line.querySelector('.ttl__season-more');
    if (!wrap) {
      if (link) link.remove();
      return;
    }
    if (!link) {
      link = button('ttl__season-more', 'More seasons');
      link.setAttribute('data-ttl-act', 'to-seasons');
      link.addEventListener('click', function () {
        var sec = body.querySelector('.ttl__askseasons');
        sec.querySelector('#ttl-pick-label').focus({ preventScroll: true });
        root.scrollTo({ top: sheet.offsetTop + sec.offsetTop - bar.offsetHeight - 8, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
      });
      core.fadeIn(line.appendChild(link));
    }
  }

  function seasonsSection(d) {
    var wrap = el('section', 'ttl__askseasons season-pick');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-labelledby', 'ttl-pick-label');
    var head = el('div', 'season-pick__head');
    var label = el('h3', 'ttl__sec-title', d.in_library ? 'More seasons' : 'Seasons');
    label.id = 'ttl-pick-label';
    label.setAttribute('tabindex', '-1');
    head.appendChild(label);
    var all = button('season-pick__all', 'Select all');
    all.setAttribute('data-ttl-act', 'pick-all');
    var none = button('season-pick__all season-pick__clear', 'Clear');
    none.setAttribute('data-ttl-act', 'pick-none');
    head.appendChild(all);
    head.appendChild(none);
    wrap.appendChild(head);
    wrap.appendChild(el('div', 'season-pick__list'));
    var foot = el('div', 'season-pick__foot');
    var words = el('div', 'season-pick__words');
    var summary = el('p', 'season-pick__summary');
    summary.setAttribute('aria-live', 'polite');
    words.appendChild(summary);
    hide(words.appendChild(el('p', 'season-pick__why')), true);
    foot.appendChild(words);
    var go = button('btn btn--lg watch-btn--grey season-pick__go');
    go.setAttribute('data-ttl-act', 'ask-seasons');
    go.addEventListener('click', function () { askSeasons(pickedSeasons(cur.data)); });
    foot.appendChild(go);
    wrap.appendChild(foot);
    wrap.appendChild(el('div', 'season-pick__out'));
    function setAll(on) {
      var now = cur.data;
      if (asking()) return;
      var picks = picksOf(now);
      allSeasons(now).forEach(function (s) { if (canPick(now, s)) picks[s.number] = on; });
      changedPicks();
    }
    all.addEventListener('click', function () { setAll(true); });
    none.addEventListener('click', function () { setAll(false); });
    fillSeasons(wrap, d, true);
    return wrap;
  }

  function asking() { return stateOf(cur.ref).busy || !!(cur.ask && cur.ask.busy); }

  function seasonChip(number) {
    var chip = el('button', 'season-chip');
    chip.type = 'button';
    chip.setAttribute('role', 'checkbox');
    chip.setAttribute('data-season', String(number));
    chip.setAttribute('data-ttl-act', 'season-' + number);
    var box = el('span', 'season-chip__box');
    box.setAttribute('aria-hidden', 'true');
    chip.appendChild(box);
    chip.appendChild(el('span', 'season-chip__name'));
    chip.appendChild(el('span', 'season-chip__sub'));
    chip.addEventListener('click', function () {
      var s = allSeasons(cur.data).filter(function (one) { return one.number === number; })[0];
      if (!s || !canPick(cur.data, s) || asking()) return;
      var picks = picksOf(cur.data);
      picks[number] = !picks[number];
      changedPicks();
    });
    return chip;
  }

  /* Picking changes the chips, the summary and the button's count in place. An
     error about the picks before goes, its space kept so nothing moves. */
  function changedPicks() {
    var wrap = root.querySelector('.ttl__askseasons');
    var d = cur.data;
    if (!wrap || !fillSeasons(wrap, d, false)) return;
    var dl = root.querySelector('.ttl__download');
    if (dl && offersDownload(d)) labelDownload(dl, d);
    stateOf(cur.ref).error = null;
    var err = root.querySelector('[data-ttl-part="under"] .ttl__error');
    if (err) err.setAttribute('inert', '');
    if (cur.ask && cur.ask.bad) {
      var out = wrap.querySelector('.season-pick__out > p');
      if (out) out.setAttribute('inert', '');
      cur.ask = null;
    }
  }

  /* The section as the title on show now is, changed in place. Says whether a tick changed. */
  function fillSeasons(wrap, d, first) {
    var seasons = listed(d);
    var list = wrap.querySelector('.season-pick__list');
    var picks = picksOf(d);
    var changed = false;
    var old = toArray(list.children);
    if (old.length !== seasons.length || old.some(function (chip, i) { return chip.getAttribute('data-season') !== String(seasons[i].number); })) {
      list.textContent = '';
      old = seasons.map(function (s) { return list.appendChild(seasonChip(s.number)); });
    }
    seasons.forEach(function (s, i) {
      var chip = old[i];
      var pick = canPick(d, s);
      var state = seasonState(d, s);
      var on = held(s) || (pick && picks[s.number]) ? 'true' : 'false';
      if (chip.getAttribute('aria-checked') !== on) changed = true;
      chip.setAttribute('aria-checked', on);
      if (pick) {
        chip.removeAttribute('aria-disabled');
        chip.removeAttribute('tabindex');
      } else {
        chip.setAttribute('aria-disabled', 'true');
        chip.setAttribute('tabindex', '-1');
      }
      chip.setAttribute('data-state', pick ? 'pick' : state.name);
      var glyph = pick ? 'i-check' : state.icon;
      if (chip.getAttribute('data-icon') !== glyph) {
        chip.setAttribute('data-icon', glyph);
        var box = chip.querySelector('.season-chip__box');
        box.textContent = '';
        box.appendChild(icon(glyph));
      }
      chip.querySelector('.season-chip__name').textContent = seasonName(s);
      var sub = chip.querySelector('.season-chip__sub');
      if (sub.textContent !== state.text) {
        sub.textContent = state.text;
        if (!first) core.fadeIn(sub);
      }
    });
    if (d.seasons_pending) wrap.classList.add('has-wait'); // their room is kept once the states are there
    var open = seasons.filter(function (s) { return canPick(d, s); });
    hide(wrap.querySelector('[data-ttl-act="pick-all"]'), open.length < 2);
    hide(wrap.querySelector('[data-ttl-act="pick-none"]'), open.length < 2);
    fillSummary(wrap, d, seasons, open);
    fillAsk(wrap, d, open);
    return changed;
  }

  function fillSummary(wrap, d, seasons, open) {
    var summary = wrap.querySelector('.season-pick__summary');
    var parts = [];
    function part(n, after) { if (n) parts.push([plural(n, 'season', 'seasons'), after]); }
    part(pickedSeasons(d).length, ' will be added and searched');
    // What is to be asked for stands alone beside its button, so that ticking adds no line.
    if (!parts.length || offersDownload(d)) {
      part(seasons.filter(held).length, ' already in library');
      part(seasons.filter(comes).length, ' on the way');
    }
    var said = parts.map(function (p) { return p.join(''); }).join(' · ') || (open.length ? 'No seasons picked' : '');
    if (summary.textContent !== said) {
      summary.textContent = parts.length ? '' : said;
      parts.forEach(function (p, i) {
        if (i) summary.appendChild(document.createTextNode(' · '));
        summary.appendChild(el('strong', '', p[0]));
        summary.appendChild(document.createTextNode(p[1]));
      });
    }
    var why = wrap.querySelector('.season-pick__why');
    var told = whyNot(d, seasons);
    if (why.textContent !== told) {
      why.textContent = told;
      hide(why, !told);
      core.fadeIn(why);
    }
  }

  /* Why seasons that are not here cannot be asked for. */
  function whyNot(d, seasons) {
    var has = {};
    seasons.forEach(function (s) { if (s.refusal) has[s.refusal] = true; });
    return [
      has.leaving && 'Keep a season that is leaving before you ask for it.',
      has.removed && 'Only an admin can bring back a deleted season.',
      has.not_listed && 'A season Sonarr doesn’t list yet can be asked for once it does.',
      has.show_unmonitored && (isAdmin ? 'This show is off in Sonarr, and turning it on would also start a season that was deleted or is leaving.'
        : 'This show is off in Sonarr, and turning it on would also start other seasons. Ask an admin.'),
      unknown(d) && 'Can’t tell what else can be asked for right now.'
    ].filter(Boolean).join(' ');
  }

  /* A season that is not here, and nobody could say whether it is wanted. */
  function unknown(d) {
    return !offersDownload(d) && allSeasons(d).some(function (s) { return s.status === null && !s.in_library; }) &&
      (!d.seasons_pending || cur.pendingAsks >= PENDING_ASKS);
  }

  function fillAsk(wrap, d, open) {
    var go = wrap.querySelector('.season-pick__go');
    var ask = cur.ask || {};
    var lost = unknown(d) && !open.length;
    // Its room is kept, so that nothing moves when it comes or goes.
    var shown = !offersDownload(d) && (open.length > 0 || lost || !!ask.busy);
    if (shown || (d.seasons_pending && (d.more_seasons || []).length)) wrap.classList.add('has-ask');
    if (!shown && document.activeElement === go) wrap.querySelector('#ttl-pick-label').focus({ preventScroll: true });
    hide(go, !shown);
    var n = pickedSeasons(d).length;
    var label = ask.checks ? 'Checking…' : ask.busy ? 'Adding…' : lost ? 'Check again' : n ? 'Download ' + plural(n, 'season', 'seasons') : 'Pick seasons';
    if (go.textContent !== label) {
      go.textContent = '';
      if (ask.busy) go.appendChild(el('span', 'btn-spinner')).setAttribute('aria-hidden', 'true');
      else go.appendChild(icon(lost ? 'i-rotate-right' : 'i-download'));
      go.appendChild(document.createTextNode(label));
    }
    if (ask.busy || (!n && !lost)) go.setAttribute('aria-disabled', 'true'); else go.removeAttribute('aria-disabled');
    var out = wrap.querySelector('.season-pick__out');
    var said = out.firstElementChild;
    if (!ask.text) {
      if (said && !said.hasAttribute('inert')) said.remove();
      return;
    }
    if (said && cur.ask === out.said) return;
    out.said = cur.ask;
    out.textContent = '';
    var line = out.appendChild(el('p', ask.bad ? 'ttl__error' : 'ttl__note ttl__note-ok'));
    line.appendChild(icon(ask.bad ? 'i-circle-exclamation' : 'i-circle-check'));
    line.appendChild(el('span', '', ask.text));
    if (ask.failed && ask.failed.length) {
      var again = button('ttl__retry', 'Try again');
      again.setAttribute('data-ttl-act', 'ask-again');
      again.addEventListener('click', function () {
        askSeasons(ask.failed);
        restoreFocus('ask-seasons');
      });
      line.appendChild(again);
    }
    core.fadeIn(line);
  }

  function drawSeasons() {
    if (!isOpen || !cur || !cur.data) return;
    var wrap = body.querySelector('.ttl__askseasons');
    var key = focusKey();
    var label = wrap && document.activeElement === wrap.querySelector('#ttl-pick-label');
    var before = wrap || body.querySelector('.ttl__seasonsec');
    if (!before) return;
    slideBelow(before, function () {
      if (!listed(cur.data).length) {
        if (wrap) wrap.remove();
        wrap = null;
      } else if (wrap) {
        fillSeasons(wrap, cur.data, false);
      } else {
        before.after(seasonsSection(cur.data));
        wrap = before.nextElementSibling;
        core.fadeIn(wrap);
      }
      linkSeasons(wrap);
    });
    if ((key && !byAct(key)) || (label && !wrap)) (wrap ? wrap.querySelector('#ttl-pick-label') : titleEl).focus({ preventScroll: true });
    else if (key && focusKey() !== key) restoreFocus(key);
  }

  /* What stands below slides as the section grows or shrinks, and once the section is out of
     sight above, stays where it is. */
  function slideBelow(wrap, change) {
    var next = wrap.nextElementSibling;
    if (next && next.getBoundingClientRect().top < root.getBoundingClientRect().top) {
      holdStill(function () { return next; }, change);
      return;
    }
    var all = toArray(body.children);
    var slide = core.flip(all.slice(all.indexOf(wrap) + 1));
    change();
    slide();
  }

  /* Ask for seasons of a show that Sonarr has, or that the library holds. */
  function askSeasons(numbers) {
    var c = cur;
    var d = c.data;
    if (asking()) return;
    if (!numbers.length) {
      if (!unknown(d)) return;
      c.pendingAsks = 0;
      c.ask = { busy: true, checks: true };
      drawSeasons();
      readSeasons(c);
      return;
    }
    var req = d.seasons_request;
    var url = req && safePath(req.url);
    if (!url) return;
    c.ask = { busy: true };
    c.asks++;
    drawSeasons();
    MM.api.post(url, Object.assign({}, req.body, { search_seasons: numbers })).then(function (r) {
      var ok = r || {};
      tookSeasons(c, numbers, ok.seasons, ok.message || plural(numbers.length, 'season', 'seasons') + ' asked for.', true);
    }, function (err) {
      var said = err && err.data && err.data.seasons;
      tookSeasons(c, numbers, said, MM.api.messageOf(err, "Couldn't add " + (numbers.length === 1 ? 'the season' : 'the seasons') + '. Try again.'), false);
    });
  }

  /* What came of asking for *numbers*: each season's own result, or, without one, *ok* for all. */
  function tookSeasons(c, numbers, results, text, ok) {
    var got = results || numbers.map(function (n) { return { number: n, result: ok ? 'asked' : 'failed' }; });
    var failed = [];
    var refused = false;
    c.asks++;
    got.forEach(function (one) {
      var s = allSeasons(c.data).filter(function (x) { return x.number === one.number; })[0];
      if (one.result === 'failed') failed.push(one.number);
      else if (REFUSED[one.result]) refused = true;
      if (!s || one.result === 'failed') return;
      s.askable = false;
      if (c.picks) c.picks[one.number] = false;
      if (one.result === 'held') s.status = 'held';
      else if (REFUSED[one.result]) s.refusal = one.result;
      else takenSeason(s);
    });
    c.ask = { busy: false, text: text, bad: !ok || failed.length > 0 || refused, failed: failed };
    if (c !== cur || !isOpen) {
      core.toast(text, ok ? { variant: 'success' } : undefined);
      return;
    }
    statusEl.textContent = text;
    drawSeasons();
    c.pendingAsks = 0;
    readSeasons(c);
  }

  function takenSeason(s) {
    s.asked = true;
    s.askable = false;
    if (s.status !== 'partly_held' && s.status !== 'not_out') s.status = 'asked';
  }

  /* States known stay while Sonarr's are still to come. */
  function keepStates(was, data) {
    if (!data.seasons_pending) return;
    var old = {};
    was.forEach(function (s) { old[s.number] = s; });
    allSeasons(data).forEach(function (s) {
      var o = old[s.number];
      if (o && o.status && s.status !== 'held') ['status', 'asked', 'askable', 'refusal'].forEach(function (key) { s[key] = o[key]; });
    });
  }

  /* Read the title again for its seasons' states alone; the rest of it stays as it is. */
  function readSeasons(c) {
    clearTimeout(seasonTimer);
    var ref = c.ref;
    var asks = c.asks;
    get(apiUrl(ref)).then(function (data) {
      if (c.ask && c.ask.checks) c.ask = null;
      if (c !== cur || !isOpen || c.ref !== ref) return;
      if (asks !== c.asks) { scheduleSeasons(); return; }
      takeSeasons(c, data);
    }).catch(function () {
      if (c.ask && c.ask.checks) c.ask = null;
      if (c !== cur || !isOpen || c.ref !== ref) return;
      drawSeasons();
      scheduleSeasons();
    });
  }

  function takeSeasons(c, data) {
    var d = c.data;
    if (data.library_ref && data.library_ref !== c.ref) { refresh(); return; }
    function same(a, b) {
      return (a || []).map(function (s) { return s.number; }).join() === (b || []).map(function (s) { return s.number; }).join();
    }
    if (d.in_library && !same(d.seasons, data.seasons)) { refresh(); return; }
    keepStates(allSeasons(d), data);
    if (d.in_library) {
      d.seasons.forEach(function (s) {
        var fresh = numbered(data, s.number);
        ['status', 'asked', 'askable', 'refusal'].forEach(function (key) { s[key] = fresh[key]; });
      });
    } else {
      d.seasons = data.seasons;
      d.sonarr_tracked = data.sonarr_tracked;
    }
    d.more_seasons = data.more_seasons;
    d.seasons_pending = data.seasons_pending;
    d.seasons_request = data.seasons_request;
    if (!d.in_library && takeDownload(c.ref, data.download)) {
      redrawActions(c.ref);
      schedulePoll(POLL_MS);
    }
    noteTaken(c);
    drawSeasons();
    scheduleSeasons();
  }

  /* What failed may have reached Sonarr all the same: then that is what is said. */
  function noteTaken(c) {
    var ask = c.ask;
    if (!ask || !ask.failed || !ask.failed.length) return;
    function names(list) { return list.map(seasonName).join(', '); }
    var failed = allSeasons(c.data).filter(function (s) { return ask.failed.indexOf(s.number) >= 0; });
    var taken = failed.filter(function (s) { return comes(s) || held(s); });
    if (!taken.length) return;
    var not = failed.filter(function (s) { return taken.indexOf(s) < 0; });
    var text = names(taken) + (taken.length === 1 ? ' is' : ' are') + ' asked for, but mediaman couldn’t note it.' +
      (not.length ? ' Couldn’t add ' + names(not) + '.' : '');
    if (text === ask.text) return;
    c.ask = { busy: false, text: text, bad: true, failed: ask.failed };
    statusEl.textContent = text;
  }

  /* Read again soon while the states are still to come, and now and then while one moves or nobody could tell. */
  function scheduleSeasons() {
    clearTimeout(seasonTimer);
    var c = cur;
    if (!isOpen || !c || !c.data || c.data.kind === 'movie' || document.hidden) return;
    var wait = 0;
    if (c.data.seasons_pending) {
      if (c.pendingAsks < PENDING_ASKS) wait = PENDING_MS;
    } else if (unknown(c.data) || allSeasons(c.data).some(function (s) { return s.status === 'downloading' || s.status === 'queued'; })) {
      wait = POLL_SLOW_MS;
    }
    if (!wait) return;
    seasonTimer = setTimeout(function () {
      if (c !== cur || !isOpen) return;
      c.pendingAsks++;
      readSeasons(c);
    }, wait);
  }

  function submitDownload() {
    var c = cur;
    var d = c && c.data;
    if (!d || !d.download || !d.download.request) return;
    var k = stateOf(c.ref);
    if (k.busy || k.state) return;
    var req = d.download.request;
    var url = safePath(req.url);
    if (!url) return;
    var payload = req.body && Object.assign({}, req.body);
    var ticked = [];
    if (seasonPickable(d)) {
      ticked = pickedSeasons(d);
      if (!ticked.length) return;
      // A pick with all ticked that can be is asked for as the whole show, with what is still to be announced.
      if (req.body || d.seasons.some(function (s) { return canPick(d, s) && ticked.indexOf(s.number) < 0; })) {
        payload = Object.assign({}, payload);
        payload.monitored_seasons = ticked.concat(d.seasons.filter(function (s) { return s.in_library; }).map(function (s) { return s.number; }));
        payload.search_seasons = ticked;
      }
    }
    var ref = c.ref;
    var title = d.title || c.title;
    k.busy = true;
    k.error = null;
    c.asks++;
    redrawActions(ref);
    MM.api.post(url, payload).then(function (r) {
      k.busy = false;
      k.state = 'queued';
      k.pct = null;
      k.sentAt = Date.now();
      fire(ref, 'queued', null);
      var each = r && r.seasons;
      c.asks++;
      if (each && each.length) {
        tookSeasons(c, ticked, each, r.message || title + ' is queued.', true);
      } else {
        core.toast(title + ' is queued.', { variant: 'success' });
        (d.seasons || []).forEach(function (s) { if (ticked.indexOf(s.number) >= 0) takenSeason(s); });
      }
      redrawActions(ref);
      if (cur && cur.ref === ref && isOpen) {
        cur.movedAt = Date.now();
        schedulePoll(POLL_MS);
        if (cur.pickRef) askNotify(cur);
        else if (ticked.length) readSeasons(cur);
      }
    }).catch(function (err) {
      // 409: the Arr already has it, so trying again can only fail again. The
      // title asked for again says whether it is queued, coming in or in the
      // library. A movie's panel stays busy until it answers; a show is already
      // in Sonarr whatever of it is downloaded, and says so meanwhile.
      if (err && err.status === 409) {
        var movie = d.kind === 'movie';
        core.toast(MM.api.messageOf(err, title + ' is already in ' + (movie ? 'Radarr.' : 'Sonarr.')));
        if (!movie) {
          k.busy = false;
          k.tracked = true;
          redrawActions(ref);
        }
        settleExisting(ref, k);
        return;
      }
      k.busy = false;
      k.error = "Couldn't add " + title + ': ' + MM.api.messageOf(err, 'Something went wrong. Try again.');
      redrawActions(ref);
    });
  }

  /* Whether an accepted download of a pick will be emailed to this person is
     the title's own reply to say; until it does, nothing is promised. */
  function askNotify(c) {
    var ref = c.ref;
    get(apiUrl(ref)).then(function (data) {
      if (c !== cur || !isOpen || c.ref !== ref) return;
      if (data.kind !== 'movie') takeSeasons(c, data);
      if (!data.download || data.download.notify !== true) return;
      c.data.download.notify = true;
      redrawActions(ref);
    }).catch(function () {});
  }

  /* A title the Arr already has: what its reply says is the title's state, on
     the panel and on every card of it. */
  function settleExisting(ref, k) {
    get(apiUrl(ref)).then(function (data) {
      k.busy = false;
      takeReply(ref, data);
      // The Arr has it yet lists nothing: pressing Download again can only 409 again.
      k.tracked = !k.state;
      redrawActions(ref);
      if (!cur || cur.ref !== ref || !isOpen) return;
      if (data.library_ref) refresh();
      else {
        cur.movedAt = Date.now();
        schedulePoll(POLL_MS);
      }
    }).catch(function () {
      k.busy = false;
      k.tracked = true;
      redrawActions(ref);
      if (cur && cur.ref === ref) schedulePoll(POLL_SLOW_MS);
    });
  }

  /* Swap the actions and the line under them for the title's current state,
     in place: the state takes the button's spot at the button's size. */
  function redrawActions(ref) {
    if (!isOpen || !cur || cur.ref !== ref || !cur.data) return;
    var actions = root.querySelector('[data-ttl-part="actions"]');
    var under = root.querySelector('[data-ttl-part="under"]');
    if (!actions || !under) return;
    closeOtherKeep();
    var key = focusKey();
    var before = actions.querySelector('[data-ttl-act="primary"]');
    var beforeClass = before ? before.className : '';
    actions.textContent = '';
    under.textContent = '';
    fillActions(actions, under);
    hide(under, !under.children.length);
    var after = actions.querySelector('[data-ttl-act="primary"]');
    if (after && after.className !== beforeClass) core.fadeIn(after);
    restoreFocus(key);
    drawSeasons();
  }

  /* ── Download progress ── */

  function pollable() {
    if (!cur || !cur.data || !cur.data.download || !isOpen) return false;
    var k = stateOf(cur.ref);
    return inFlight(k) || k.tracked;
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(delay) {
    stopPolling();
    if (!pollable() || document.hidden) return;
    if (!cur.movedAt) cur.movedAt = Date.now();
    pollTimer = setTimeout(poll, inFlight(stateOf(cur.ref)) ? delay : POLL_SLOW_MS);
  }

  /* Every 5 s while the download moves, every 30 s once it has sat still
     for two minutes. */
  function nextPoll() {
    return Date.now() - cur.movedAt < POLL_BACKOFF_AFTER_MS ? POLL_MS : POLL_SLOW_MS;
  }

  /* The Arr no longer queues the download: ask the title again, whose reply
     says whether the Arr still holds it. */
  function recheck(c) {
    var k = stateOf(c.ref);
    if (k.state !== 'queued' || inGrace(k)) return;
    if (c.checkedAt && Date.now() - c.checkedAt < RECHECK_MS) return;
    c.checkedAt = Date.now();
    askAgain(c).catch(function () {});
  }

  /* Ask the open title again and show what its reply says. */
  function askAgain(c) {
    var ref = c.ref;
    return get(apiUrl(ref)).then(function (data) {
      if (c !== cur || !isOpen || c.ref !== ref) return;
      if (!takeReply(ref, data)) return;
      c.movedAt = Date.now();
      redrawActions(ref);
      if (data.library_ref) refresh();
    });
  }

  function poll() {
    pollTimer = null;
    if (!pollable()) return;
    var c = cur;
    var ref = c.ref;
    if (!inFlight(stateOf(ref))) {
      // Already in the Arr with no download to follow: the title may yet be
      // queued there, or removed from it.
      askAgain(c).then(function () {
        if (c === cur && c.ref === ref) schedulePoll(POLL_MS);
      }, function (err) { pollFailed(err, c, ref); });
      return;
    }
    var dl = c.data.download;
    var url = '/api/download/status?service=' + encodeURIComponent(dl.service) +
      '&tmdb_id=' + encodeURIComponent(dl.tmdb_id);
    MM.api.get(url).then(function (r) {
      var k = stateOf(ref);
      var was = k.state + ':' + k.pct;
      // 'unknown' is an Arr that did not answer: no news, so nothing changes.
      if (r.state === 'ready') {
        k.state = 'in_library';
        k.pct = null;
      } else if (r.state === 'downloading' || r.state === 'almost_ready') {
        k.state = 'downloading';
        k.pct = Math.max(0, Math.min(100, Math.round(r.progress || 0)));
        k.sizeDone = r.size_done || '';
        k.sizeTotal = r.size_total || '';
        k.eta = r.eta || '';
      } else if (r.state === 'queued' || r.state === 'searching') {
        k.state = 'queued';
        k.pct = null;
      }
      if (k.state + ':' + k.pct !== was) {
        if (c === cur) c.movedAt = Date.now();
        fire(ref, k.state, k.pct);
        if (k.state === 'in_library' && c === cur) {
          c.justAdded = true;
          c.addedNow = !!k.sentAt;
          redrawActions(ref);
          refresh();
        } else {
          redrawActions(ref);
        }
      }
      if (c !== cur || c.ref !== ref) return;
      if (r.state === 'searching') recheck(c);
      schedulePoll(r.state === 'unknown' ? POLL_SLOW_MS : nextPoll());
    }).catch(function (err) { pollFailed(err, c, ref); });
  }

  function pollFailed(err, c, ref) {
    if (err && (err.status === 401 || err.status === 403)) return;
    if (c === cur && c.ref === ref) schedulePoll(POLL_SLOW_MS);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPolling();
    else if (pollable()) schedulePoll(0);
    scheduleSeasons();
  });

  /* ── Trailer ── */

  function trailerKey(d) {
    return d && d.trailer_key && YOUTUBE_KEY_RE.test(d.trailer_key) ? d.trailer_key : null;
  }

  function trailerOn() { return root.classList.contains('is-trailer'); }

  /* Both its labels are laid out, so it keeps its width; the one in force shows. */
  function trailerButton(d) {
    if (!trailerKey(d)) return null;
    var b = button('btn btn--lg watch-btn--grey ttl__trailer');
    b.setAttribute('data-ttl-act', 'trailer');
    [['ttl__trailer-off', 'Trailer', 'i-film'], ['ttl__trailer-on', 'Stop trailer', 'i-stop']].forEach(function (f) {
      var face = el('span', f[0]);
      face.appendChild(icon(f[2]));
      face.appendChild(document.createTextNode(f[1]));
      b.appendChild(face);
    });
    b.addEventListener('click', function (e) {
      if (e.detail > 1) return; // the rest of a double click
      if (trailerOn()) {
        leaveTrailer();
        return;
      }
      address.push('trailer', address.url({}));
      keepPick();
      openTrailer();
    });
    return b;
  }

  /* Make a *change* that alters the hero's height, and ease between the heights
     measured before and after: the text can hold it taller than its CSS asks. */
  function easeHero(change) {
    var from = hero.offsetHeight;
    if (heroEase) heroEase.cancel();
    change();
    var to = hero.offsetHeight;
    if (from === to || !hero.animate || core.reduceMotion()) return;
    heroEase = hero.animate([{ minHeight: from + 'px' }, { minHeight: to + 'px' }], {
      duration: core.tokenMs('--dur-slow', 250),
      easing: core.token('--ease-out', 'ease-out')
    });
  }

  function tellFrame(frame, message) {
    if (frame.contentWindow) frame.contentWindow.postMessage(JSON.stringify(message), TRAILER_ORIGIN);
  }

  /* The player takes the hero's box, so nothing below it moves; a hero too small
     for it grows. */
  function openTrailer() {
    var key = cur && trailerKey(cur.data);
    if (!key || trailerOn()) return;
    player.textContent = '';
    var frame = el('iframe', 'ttl__frame');
    frame.setAttribute('src', TRAILER_ORIGIN + '/embed/' + key + '?autoplay=1&rel=0&playsinline=1&enablejsapi=1&origin=' +
      encodeURIComponent(window.location.origin));
    frame.setAttribute('title', 'Trailer: ' + (cur.data.title || cur.title));
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox');
    frame.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
    frame.setAttribute('referrerpolicy', 'strict-origin');
    frame.setAttribute('allowfullscreen', '');
    // YouTube's player reports its state only to a page that has asked.
    frame.addEventListener('load', function () { tellFrame(frame, { event: 'listening' }); });
    player.appendChild(frame);
    player.setAttribute('aria-hidden', 'false');
    easeHero(function () { root.classList.add('is-trailer'); });
    nameClose();
    closeBtn.focus({ preventScroll: true });
    root.scrollTo({ top: 0, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* Close the trailer and nothing else. *refocus*: move focus from what goes
     with it to the Trailer button, and let the video fade into the artwork. */
  function closeTrailer(refocus) {
    if (!trailerOn()) return;
    var active = document.activeElement;
    var lost = !root.contains(active) || player.contains(active) || active === closeBtn || active === titleEl; // out of sight under a trailer
    easeHero(function () { root.classList.remove('is-trailer'); });
    player.setAttribute('aria-hidden', 'true');
    nameClose();
    trailerLeftAt = Date.now();
    var frame = player.querySelector('iframe');
    if (frame) {
      tellFrame(frame, { event: 'command', func: 'pauseVideo', args: [] });
      if (refocus && !core.reduceMotion()) setTimeout(function () { frame.remove(); }, core.tokenMs('--dur', 200));
      else frame.remove();
    }
    var b = root.querySelector('[data-ttl-act="trailer"]');
    if (refocus && lost && b) b.focus({ preventScroll: true });
  }

  /* Close takes the trailer first. */
  function nameClose() {
    closeBtn.setAttribute('aria-label', trailerOn() ? 'Close trailer' : 'Close');
    if (trailerOn()) closeBtn.setAttribute('title', 'Close trailer');
    else closeBtn.removeAttribute('title');
  }

  /* Leave the trailer at the person's request, or at its end. */
  function leaveTrailer() {
    if (trailerOn()) address.back('trailer');
  }

  window.addEventListener('message', function (e) {
    var frame = player.querySelector('iframe');
    if (!frame || e.source !== frame.contentWindow || e.origin !== TRAILER_ORIGIN) return;
    var m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (!m) return;
    var state = m.event === 'onStateChange' ? m.info
      : m.event === 'infoDelivery' && m.info ? m.info.playerState : null;
    if (state === 0) leaveTrailer();
  });

  /* ── Share (admins, Recommended picks) ── */

  /* The button is drawn from cur.shareState, so a redraw mid-share (a poll)
     keeps it as it was. */
  function shareButton() {
    if (!isAdmin || !cur.recId) return null;
    var st = cur.shareState;
    var b = button('btn btn--lg watch-btn--grey ttl__share');
    b.setAttribute('data-ttl-act', 'share');
    // The longest label, laid out unseen beside the icon, holds the button's width in
    // every state; the icon and the label sit centred together inside it.
    b.setAttribute('data-widest', 'Sharing…');
    var group = el('span', 'ttl__share-group');
    group.appendChild(icon(st === 'copied' ? 'i-check' : 'i-share-nodes'));
    group.appendChild(el('span', 'ttl__share-label', st === 'busy' ? 'Sharing…' : st === 'copied' ? 'Copied' : 'Share'));
    b.appendChild(group);
    if (st === 'busy') b.setAttribute('aria-disabled', 'true');
    b.addEventListener('click', share);
    return b;
  }

  function setShare(c, st) {
    c.shareState = st;
    if (c !== cur || !isOpen) return;
    if (st) statusEl.textContent = st === 'copied' ? 'Link copied.' : '';
    var b = root.querySelector('[data-ttl-act="share"]');
    var fresh = shareButton();
    if (b && fresh) {
      var focused = document.activeElement === b;
      b.replaceWith(fresh);
      if (focused) fresh.focus({ preventScroll: true });
    }
  }

  function share() {
    var c = cur;
    if (!c || c.shareState === 'busy') return;
    clearTimeout(shareTimer);
    var stale = c.shareMsg || c.shareUrl;
    c.shareMsg = null;
    c.shareUrl = null;
    setShare(c, 'busy');
    if (stale) redrawActions(c.ref);
    MM.api.post('/api/recommended/' + encodeURIComponent(c.recId) + '/share-token').then(function (data) {
      var url = data && data.share_url;
      if (!url) throw new Error('no link');
      var copy = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(url) : Promise.reject(new Error('no clipboard'));
      return copy.then(function () {
        setShare(c, 'copied');
        shareTimer = setTimeout(function () { setShare(c, null); }, COPIED_MS);
      }, function () {
        c.shareUrl = url;
        setShare(c, null);
        redrawActions(c.ref);
        if (c !== cur) return;
        var field = root.querySelector('.ttl-share__input');
        if (field) { field.focus({ preventScroll: true }); field.select(); }
      });
    }).catch(function (err) {
      var status = err && err.status;
      c.shareMsg = status === 422 ? "This pick can't be shared: it has no TMDB id."
        : status === 503 ? 'Set the public address in Settings before sharing links.'
        : "Couldn't make a share link. Try again.";
      setShare(c, null);
      redrawActions(c.ref);
      if (c === cur && isOpen) statusEl.textContent = c.shareMsg;
    });
  }

  function shareField(url) {
    var wrap = el('div', 'ttl-share');
    var label = el('label', 'ttl-share__label', 'Copy this link');
    label.setAttribute('for', 'ttl-share-url');
    var input = el('input', 'inp ttl-share__input');
    input.id = 'ttl-share-url';
    input.type = 'text';
    input.readOnly = true;
    input.value = url;
    input.setAttribute('data-ttl-act', 'share-field');
    input.addEventListener('focus', function () { input.select(); });
    wrap.appendChild(label);
    wrap.appendChild(input);
    return wrap;
  }

  /* ── Seasons and episodes (library shows) ── */

  /* The episode Play plays, unless it has been seen: the show then starts over. */
  function isNext(e) {
    var play = cur.data && cur.data.play;
    return !!play && play.href === e.href && !e.watched;
  }

  function episodeFacts(e) {
    var facts = [];
    if (e.minutes) facts.push(dur(e.minutes));
    // A date alone is read as UTC midnight, the day before west of it.
    var at = e.aired ? new Date(e.aired + 'T12:00:00') : null;
    var said = at ? day(at) : '';
    if (said) facts.push(at > Date.now() ? 'Airs ' + said : said);
    if (e.state === 'in_progress' && e.minutes_left) facts.push(dur(e.minutes_left) + ' left');
    return facts.join(' · ');
  }

  function episodeRow(e) {
    var next = isNext(e);
    var begun = e.state === 'in_progress';
    var name = e.title || 'Episode ' + e.number;
    var li = el('li', 'watch-eprow');
    var a = el('a', 'watch-ep' + (begun ? ' watch-ep--in-progress' : '') + (next ? ' watch-ep--next' : '') +
      (e.watched ? ' watch-ep--watched' : ''));
    a.setAttribute('href', safePath(e.href) || '#');
    a.appendChild(el('span', 'watch-ep__num', String(e.number)));
    var thumb = el('span', 'watch-ep__thumb');
    thumb.appendChild(icon('i-film', 'watch-ep__noart'));
    var src = safePath(e.thumb_url);
    if (src) {
      var img = el('img');
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('src', src);
      img.setAttribute('data-broken-on-error', '');
      thumb.appendChild(img);
    }
    if (e.watched) {
      var tick = el('span', 'watch-ep__tick');
      tick.appendChild(icon('i-check'));
      thumb.appendChild(tick);
    }
    var play = el('span', 'watch-ep__play');
    play.appendChild(icon('i-play'));
    thumb.appendChild(play);
    if (begun && e.progress_pct) {
      var bar = el('progress', 'watch-bar');
      bar.max = 100;
      bar.value = e.progress_pct;
      bar.setAttribute('aria-hidden', 'true');
      thumb.appendChild(bar);
    }
    a.appendChild(thumb);
    var text = el('span', 'watch-ep__text');
    if (next || begun) {
      text.appendChild(el('span', 'watch-ep__state watch-ep__state--active',
        next ? (begun ? 'Continue' : 'Next up') : 'In progress'));
      text.appendChild(el('span', 'sr-only', ', '));
    }
    var title = el('span', 'watch-ep__title', name);
    if (e.watched) title.appendChild(el('span', 'sr-only', ', watched'));
    text.appendChild(title);
    text.appendChild(el('span', 'watch-ep__meta', episodeFacts(e)));
    a.appendChild(text);
    li.appendChild(a);
    describe(li, e);
    var mark = episodeMark(e);
    if (mark) li.appendChild(mark);
    return li;
  }

  /* Put what episode *e* is about into its row. */
  function describe(li, e) {
    var a = li.querySelector('.watch-ep');
    if (!e.summary || a.querySelector('.watch-ep__about')) return null;
    // The link's description, not a part of its name.
    var about = el('span', 'watch-ep__about ttl-clamp', e.summary);
    about.id = 'ttl-ep-' + (++seq);
    about.setAttribute('aria-hidden', 'true');
    a.setAttribute('aria-describedby', about.id);
    a.appendChild(about);
    a.after(moreButton('watch-ep__more', about, li, e.title || 'Episode ' + e.number, 'ep-' + e.number));
    return about;
  }

  /* Opens the rest of *text* by a press: *holder* is open meanwhile, and again when it is drawn
     anew as *key*. */
  function moreButton(cls, text, holder, name, key) {
    var c = cur;
    var b = button('ttl-more ' + cls, 'More');
    b.setAttribute('aria-controls', text.id);
    b.setAttribute('data-ttl-act', 'more-' + key);
    function show() {
      var open = !!c.opened[key];
      holder.classList.toggle('is-open', open);
      b.textContent = open ? 'Less' : 'More';
      b.setAttribute('aria-label', (open ? 'Less' : 'More') + ' about ' + name);
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    b.addEventListener('click', function () {
      c.opened[key] = !c.opened[key];
      show();
    });
    show();
    return b;
  }

  /* Which texts are cut off, and how high each is in full, for it to ease open. */
  function fitNow(panel) {
    toArray(panel.querySelectorAll('.ttl-clamp')).map(function (text) {
      return [text, text.scrollHeight, parseFloat(getComputedStyle(text).lineHeight)];
    }).forEach(function (one) {
      one[0].style.setProperty('--full', one[1] + 'px');
      one[0].parentNode.classList.toggle('is-long', one[1] > 2 * one[2] + 2);
    });
  }

  function fitTexts(panel) {
    if (typeof ResizeObserver !== 'function') return;
    function fit() { fitNow(panel); }
    var width = panel.offsetWidth;
    fit();
    // A text open from the start, as under the mark just pressed, is drawn open.
    panel.classList.add('is-still');
    new ResizeObserver(function () {
      requestAnimationFrame(function () { panel.classList.remove('is-still'); });
      if (panel.offsetWidth === width) return; // a text opening, not the screen
      width = panel.offsetWidth;
      requestAnimationFrame(fit);
    }).observe(panel);
  }

  function episodeList(episodes, label) {
    var ol = el('ol', 'watch-episodes');
    ol.setAttribute('aria-label', label);
    episodes.forEach(function (e) { ol.appendChild(episodeRow(e)); });
    return ol;
  }

  /* The season on show: what is said of it, and its episodes. */
  function seasonPanel(s, episodes) {
    var about = (cur.picked && cur.pickedAbout) || s || {};
    var name = s ? seasonName(s) : 'Season';
    var panel = el('div', 'ttl__season');
    var box = el('div', 'ttl__season-about');
    var line = el('div', 'ttl__season-line');
    line.appendChild(el('p', 'ttl__season-facts', seasonFacts(about, episodes)));
    box.appendChild(line);
    sayOfSeason(box, about, name);
    panel.appendChild(box);
    panel.appendChild(episodeList(episodes, name + ' episodes'));
    return panel;
  }

  function seasonFacts(about, episodes) {
    var facts = [plural(episodes.length, 'episode', 'episodes')];
    if (about.year) facts.unshift(String(about.year));
    return facts.join(' · ');
  }

  function sayOfSeason(box, about, name) {
    if (!about.summary || about.summary === cur.data.overview || box.querySelector('.ttl__season-text')) return null;
    var text = el('p', 'ttl__season-text ttl-clamp', about.summary);
    text.id = 'ttl-season-' + (++seq);
    box.appendChild(text);
    box.appendChild(moreButton('', text, box, name, 'season'));
    return text;
  }

  /* ── Words that come after their season ── */

  function askWords(c) {
    var n = c.season;
    if (!(c.picked ? c.pickedAbout : c.data).words_pending || (c.words && c.words.number === n)) return;
    var w = c.words = { number: n };
    get(apiUrl(c.ref) + '/season/' + encodeURIComponent(n) + '?words=1', c.ctrl).then(function (data) {
      betweenPresses(function () {
        if (c.words !== w) return;
        c.words = null;
        if (c === cur && isOpen && c.data && c.season === n) takeWords(c, data);
      });
    }, function () { if (c.words === w) c.words = null; });
  }

  /* Run *fn* now, or once what is being pressed has been let go of and has answered. A press
     whose end never comes, such as one that opened a menu, is waited for 3 s. */
  function betweenPresses(fn, waited) {
    waited = waited || 0;
    if (!pressing || waited >= 3000) { fn(); return; }
    setTimeout(function () { betweenPresses(fn, waited + 100); }, 100);
  }

  var pressing = false;
  document.addEventListener('pointerdown', function () { pressing = true; }, true);
  ['pointerup', 'pointercancel'].forEach(function (type) {
    document.addEventListener(type, function () { pressing = false; }, true);
  });

  /* The words into the rows on show, which make room for them: what is pointed at or has the
     focus stays where it is, and the rest slides. What was seen is as held: a mark may have
     been made since the words were asked for. */
  function takeWords(c, data) {
    var s = numbered(c.data, c.season);
    if (!s) return;
    var episodes = c.picked || c.data.episodes || [];
    var about = c.picked ? c.pickedAbout : s;
    var told = {};
    (data.episodes || []).forEach(function (e) { told[e.key] = e; });
    episodes.forEach(function (e) {
      var t = told[e.key] || {};
      e.summary = t.summary || e.summary;
      e.aired = t.aired || e.aired;
    });
    about.summary = data.summary || about.summary;
    about.year = data.year || about.year;
    delete (c.picked ? c.pickedAbout : c.data).words_pending;
    var sec = body.querySelector('.ttl__seasonsec');
    var panel = sec && sec.querySelector('.ttl__season');
    if (!panel) return;
    var rows = toArray(panel.querySelectorAll('.watch-eprow'));
    var still = keptStill(sec, rows);
    var others = toArray(body.children).concat(toArray(sec.children), toArray(panel.children));
    var slide = core.flip(rows.concat(others.filter(function (n) { return n !== sec && n !== panel && !n.contains(rows[0]); })));
    function say() {
      var box = panel.querySelector('.ttl__season-about');
      box.querySelector('.ttl__season-facts').textContent = seasonFacts(about, episodes);
      var fresh = [sayOfSeason(box, about, seasonName(s))];
      rows.forEach(function (li, i) {
        li.querySelector('.watch-ep__meta').textContent = episodeFacts(episodes[i]);
        fresh.push(describe(li, episodes[i]));
      });
      if (typeof ResizeObserver === 'function') fitNow(panel);
      fresh.filter(Boolean).forEach(function (text) {
        core.fadeIn(text);
        core.fadeIn(panel.querySelector('[aria-controls="' + text.id + '"]'));
      });
    }
    if (still) holdStill(function () { return still; }, say);
    else say();
    slide();
  }

  /* What of the seasons must not move: what the pointer is on, what has the focus, or, with their
     top out of sight, the first row to be seen. */
  function keptStill(sec, rows) {
    function stays(node) { return node && sec.contains(node) && (node.closest('.watch-eprow') || node.closest('button')); }
    var at = stays(toArray(sec.querySelectorAll(':hover')).pop()) || stays(document.activeElement);
    if (at) return at;
    var top = root.getBoundingClientRect().top;
    if (!rows.length || rows[0].getBoundingClientRect().top >= top) return null;
    return rows.filter(function (li) { return li.getBoundingClientRect().bottom > top; })[0] || sec.nextElementSibling;
  }

  function episodeSkels(n) {
    var box = el('div', 'ttl-epskels');
    box.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < n; i++) {
      var row = el('div', 'ttl-epskel');
      row.appendChild(skel('ttl-epskel__thumb'));
      var lines = el('div', 'ttl-skel-lines');
      ['38%', '22%', '94%', '70%'].forEach(function (w) { lines.appendChild(skel('skel--line', w)); });
      row.appendChild(lines);
      box.appendChild(row);
    }
    return box;
  }

  /* While another season loads, rows still to come stand over those of the one on show, which
     takes no press unseen. */
  function seasonWaits(panel, on) {
    var list = panel.querySelector('.watch-episodes');
    var about = panel.querySelector('.ttl__season-about');
    list.setAttribute('aria-busy', on ? 'true' : 'false');
    list.inert = about.inert = on;
    panel.classList.toggle('is-waiting', on);
    toArray(panel.querySelectorAll('.ttl__season-wait')).forEach(function (n) { n.remove(); });
    if (!on) return;
    var head = el('div', 'ttl__season-wait ttl-aboutskel ttl-skel-lines');
    head.setAttribute('aria-hidden', 'true');
    ['130px'].concat(about.querySelector('.ttl__season-text') ? ['100%', '84%'] : []).forEach(function (w) {
      head.appendChild(skel('skel--line', w));
    });
    panel.appendChild(head);
    var rows = episodeSkels(6);
    rows.classList.add('ttl__season-wait');
    panel.appendChild(rows);
  }

  /* The season that leaves fades over the one that comes. It is never taken off the page
     before: what is easing in it would start over. */
  function crossfade(old, fresh) {
    var last = old.parentNode.querySelector('.ttl__season.is-gone');
    if (last) last.remove();
    old.before(fresh);
    fitTexts(fresh);
    fresh.classList.add('is-arriving');
    if (!old.animate || core.reduceMotion()) {
      old.remove();
      return;
    }
    old.style.top = fresh.offsetTop + 'px';
    old.classList.add('is-gone');
    old.setAttribute('aria-hidden', 'true');
    old.inert = true;
    var fade = old.animate([{ opacity: 1 }, { opacity: 0 }], { duration: core.tokenMs('--dur', 200), fill: 'forwards' });
    fade.onfinish = fade.oncancel = function () { old.remove(); };
  }

  function numbered(d, n) { return (d.seasons || []).filter(function (s) { return s.number === n; })[0]; }

  /* The season a show opens on by itself. */
  function ownSeason(d) { return d.seasons.filter(function (s) { return s.selected; })[0] || d.seasons[0]; }

  function seasonName(s) { return s.name || 'Season ' + s.number; }

  function episodesSection(d) {
    var sec = el('section', 'ttl__seasonsec');
    if (cur.room) sec.style.marginTop = cur.room + 'px';
    var head = el('div', 'ttl__sec-head');
    head.appendChild(el('h3', 'ttl__sec-title', 'Episodes'));
    sec.appendChild(head);
    var shown = cur.picked && numbered(d, cur.season);
    if (!shown) {
      shown = ownSeason(d);
      cur.season = shown ? shown.number : null;
      cur.picked = null;
      cur.pickedAbout = null;
      cur.chosen = false;
    }
    if (cur.seasonLoading && !numbered(d, cur.seasonLoading.number)) cur.seasonLoading = null;
    var mark = seasonMark(shown);
    if (mark) head.appendChild(mark);
    var pressed = cur.seasonLoading ? cur.seasonLoading.number : cur.season;
    if (d.seasons.length > 1) {
      var nav = el('div', 'watch-seasons');
      nav.classList.add('watch-row__scroll');
      nav.setAttribute('role', 'group');
      nav.setAttribute('aria-label', 'Seasons');
      d.seasons.forEach(function (s) {
        var pill = button('watch-pill', seasonName(s));
        var on = s.number === pressed;
        pill.classList.toggle('on', on);
        pill.setAttribute('aria-pressed', on ? 'true' : 'false');
        pill.setAttribute('data-season', String(s.number));
        if (s.watched) {
          pill.setAttribute('aria-label', seasonName(s) + ', watched');
          pill.appendChild(icon('i-eye', 'watch-pill__mark'));
        }
        pill.setAttribute('data-ttl-act', 'pill-' + s.number);
        pill.addEventListener('click', function () { pickSeason(s); });
        nav.appendChild(pill);
      });
      var viewport = el('div', 'watch-row__viewport ttl-seasons');
      viewport.appendChild(nav);
      sec.appendChild(viewport);
    }
    var panel = seasonPanel(shown, cur.picked || d.episodes);
    if (cur.seasonLoading) seasonWaits(panel, true);
    sec.appendChild(panel);
    return sec;
  }

  /* The seasons stand on one line that scrolls. Once it is on the page it gets
     the rows' arrows and fades, and stands where it stood (*at*) or shows the
     picked season. */
  function startSeasonLine(at) {
    var line = body.querySelector('.watch-seasons');
    if (!line) return;
    if (at === null) showPill(line, 'auto');
    else line.scrollLeft = at;
    if (window.MMWatch.rows) window.MMWatch.rows.init(line.parentNode);
  }

  function showPill(line, behavior) {
    var pill = line.querySelector('[aria-pressed="true"]');
    if (!pill) return;
    var left = pill.offsetLeft - (line.scrollLeft || 0);
    if (left >= 0 && left + pill.offsetWidth <= line.clientWidth) return;
    line.scrollTo({ left: pill.offsetLeft - (line.clientWidth - pill.offsetWidth) / 2, behavior: behavior });
  }

  function markSeason(sec, number) {
    toArray(sec.querySelectorAll('.watch-pill')).forEach(function (p) {
      var on = Number(p.getAttribute('data-season')) === number;
      p.classList.toggle('on', on);
      p.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  /* Make a *change* without moving what *find* finds before and after it: the overlay
     scrolls as far as that moved, and where it cannot, room is left at the page's end, or
     above the seasons. */
  function holdStill(find, change) {
    var from = find().getBoundingClientRect().top;
    change();
    body.style.marginBottom = '';
    leaveRoom(0);
    var node = find();
    var moved = node ? node.getBoundingClientRect().top - from : 0;
    if (!moved) return;
    if (moved > 0) body.style.marginBottom = moved + 'px';
    var was = root.scrollTop;
    root.scrollTop += moved;
    leaveRoom(root.scrollTop - was - moved);
  }

  function leaveRoom(px) {
    var sec = body.querySelector('.ttl__seasonsec');
    cur.room = px >= 1 ? px : 0; // a scroll stops at a whole pixel
    if (sec) sec.style.marginTop = cur.room ? cur.room + 'px' : '';
  }

  function takeEpisodes(c, data) {
    c.picked = data.episodes || [];
    c.pickedAbout = { summary: data.summary, year: data.year, words_pending: data.words_pending };
  }

  function takeSeason(c, s, data) {
    c.season = s.number;
    c.chosen = true;
    takeEpisodes(c, data);
    s.key = data.key;
    s.watched = data.watched;
  }

  /* Draw season *s* in place of the one on show. *still*: what stays where it stands, the
     season's pill if not given. */
  function showSeason(c, s, data, still) {
    var leftOn = leavingNow();
    takeSeason(c, s, data);
    c.opened = {};
    var leaves = leavingNow();
    var fresh = seasonPanel(s, c.picked);
    var sec = body.querySelector('.ttl__seasonsec');
    still = still || sec.querySelector('.watch-pill[data-season="' + s.number + '"]') || sec;
    holdStill(function () { return still; }, function () {
      crossfade(sec.querySelector('.ttl__season'), fresh);
      linkSeasons(body.querySelector('.ttl__askseasons'));
      if ((leftOn && leftOn.id) !== (leaves && leaves.id)) {
        redrawActions(c.ref);
        if (hasFacts(c)) renderFacts(false);
      }
      var oldMark = sec.querySelector('.ttl__season-mark');
      var mark = seasonMark(s);
      if (oldMark && mark) oldMark.replaceWith(mark);
    });
    askWords(c);
  }

  /* A season pill swaps the list in place; focus stays on the pill. When the
     season will not load, the one on show stays and offers a retry. The section
     may be drawn again meanwhile, so it is looked up when the reply comes. */
  function pickSeason(s) {
    var c = cur;
    if (!c || (s.number === c.season && !c.seasonLoading)) return;
    var token = { number: s.number };
    c.seasonLoading = token;
    var sec = body.querySelector('.ttl__seasonsec');
    markSeason(sec, s.number);
    showPill(sec.querySelector('.watch-seasons'), core.reduceMotion() ? 'auto' : 'smooth');
    var oldErr = sec.querySelector('.ttl__error');
    if (oldErr) oldErr.remove();
    seasonWaits(sec.querySelector('.ttl__season'), true);
    get(apiUrl(c.ref) + '/season/' + encodeURIComponent(s.number)).then(function (data) {
      if (c !== cur || c.seasonLoading !== token) return;
      c.seasonLoading = null;
      showSeason(c, s, data);
    }).catch(function () {
      if (c !== cur || c.seasonLoading !== token) return;
      c.seasonLoading = null;
      var sec = body.querySelector('.ttl__seasonsec');
      var list = sec.querySelector('.watch-episodes');
      markSeason(sec, c.season);
      seasonWaits(sec.querySelector('.ttl__season'), false);
      var err = el('p', 'ttl__error');
      err.setAttribute('role', 'alert');
      err.appendChild(el('span', '', 'Could not load ' + seasonName(s) + '.'));
      var again = button('ttl__retry', 'Try again');
      again.addEventListener('click', function () {
        var pill = body.querySelector('.ttl__seasonsec .watch-pill[data-season="' + s.number + '"]');
        pickSeason(s);
        if (pill) pill.focus({ preventScroll: true });
      });
      err.appendChild(again);
      list.before(err);
    });
  }

  /* ── Admin facts ── */

  var adminMenu = null;

  function factsCard() {
    var aside = el('aside', 'ttl-facts');
    aside.setAttribute('aria-label', 'Library facts, admins only');
    var eyebrow = el('p', 'ttl-facts__eyebrow');
    eyebrow.appendChild(icon('i-lock'));
    eyebrow.appendChild(document.createTextNode('Admin only'));
    aside.appendChild(eyebrow);
    var dl = el('dl', 'ttl-facts__list');
    return {
      aside: aside,
      dl: dl,
      row: function (label, value, cls, sub) {
        dl.appendChild(el('dt', '', label));
        var dd = el('dd', cls || '', value);
        if (sub) dd.appendChild(el('span', 'ttl-facts__sub', sub));
        dl.appendChild(dd);
        return dd;
      }
    };
  }

  /* The same rows as the facts have, so that nothing below moves when they come. */
  function factsToCome(card, leaves) {
    card.aside.setAttribute('aria-busy', 'true');
    ['Size on disk', 'Added', 'Last watched by anyone', 'Deletion', 'Keep'].forEach(function (label) {
      var dd = card.row(label, '', 'is-pending');
      dd.appendChild(skel('skel--line ttl-facts__skel'));
      if (label === 'Deletion' && leaves) dd.appendChild(el('span', 'ttl-facts__sub', '\u00a0'));
    });
    card.aside.appendChild(card.dl);
    if (!window.MMWatch.admin) return;
    var foot = el('div', 'ttl-facts__foot');
    foot.appendChild(skel('skel--pill'));
    foot.appendChild(skel('skel--pill'));
    card.aside.appendChild(foot);
  }

  function renderFacts(fresh) {
    var f = cur.facts;
    var pending = !f && !cur.factsError;
    var lead = body.querySelector('.ttl__lead');
    if (!lead) return;
    if (adminMenu) closeAdminMenu(adminMenu.menu.contains(document.activeElement));
    var key = focusKey();
    var old = lead.querySelector('.ttl-facts');
    var card = factsCard();
    var aside = card.aside;
    var dl = card.dl;
    var row = card.row;
    if (pending) factsToCome(card, !!cur.data.leaving);
    if (f) {
      row('Size on disk', f.size || '—');
      row('Added', f.added ? day(f.added) : '—');
      row('Last watched by anyone', f.last_watched ? day(f.last_watched) : 'Never', f.last_watched ? '' : 'is-warning');
      var gone = deletionShown(f);
      if (gone) {
        row('Deletion', day(gone.execute_at), gone.days <= 7 ? 'is-danger' : '',
          (gone.season == null ? '' : 'Season ' + gone.season + ', ') +
          (gone.days < 0 ? (gone.season == null ? 'Leaving' : 'leaving') + ' any day' : leavesWhen(gone.days)));
      } else {
        row('Deletion', 'Not scheduled');
      }
      var keep = f.keep || { state: 'none', label: 'Not kept' };
      row('Keep', keep.label, keep.state !== 'none' ? 'is-success' : '');
      aside.appendChild(dl);
    }
    if (cur.factsError) aside.appendChild(factsErrorLine());
    var foot = f && factsFooter(f);
    if (foot) aside.appendChild(foot);
    if (old) old.replaceWith(aside); else lead.appendChild(aside);
    root.classList.add('ttl--admin');
    if (fresh) aside.classList.add('is-swapping-in');
    if (key === 'facts-retry' && !cur.factsError) key = 'admin-keep';
    restoreFocus(key);
    handOnDialogs();
  }

  /* The deletion of what the title says leaves, else the first there is. */
  function deletionShown(f) {
    var leaves = leavingNow();
    return (leaves && toArray(f.deletions || []).filter(function (d) { return String(d.keep_id) === leaves.id; })[0]) || f.deletion;
  }

  /* What an admin sees in place of the facts, or under ones that may be old. */
  function factsErrorLine() {
    var line = el('p', 'ttl__error ttl-facts__error');
    line.setAttribute('role', 'alert');
    if (cur.factsError === 'unknown') {
      line.appendChild(el('span', '', 'No scan has seen this title yet.'));
      return line;
    }
    line.appendChild(el('span', '', cur.facts ? "Couldn't load the facts again; these may be old." : "Couldn't load the library facts."));
    var again = button('ttl__retry', 'Try again');
    again.setAttribute('data-ttl-act', 'facts-retry');
    again.addEventListener('click', function () {
      if (again.getAttribute('aria-disabled') === 'true') return;
      again.setAttribute('aria-disabled', 'true');
      loadFacts(cur);
    });
    line.appendChild(again);
    return line;
  }

  function factsFooter(f) {
    var api = window.MMWatch.admin;
    var actions = f.actions || {};
    if (!api) return null;
    var foot = el('div', 'ttl-facts__foot');
    var kept = !!(f.keep && f.keep.state !== 'none');
    // What the admin Keep reads: a scheduled title kept for days can be undone.
    var wrapper = actions.keep_wrapper && Object.assign({}, actions.keep_wrapper, {
      title: actions.keep_wrapper.show_title || cur.title, kept: kept, deletion: !!f.deletion
    });
    if (api.keep && actions.keep_wrapper) {
      var wrap = el('div', 'ttl-facts__keep');
      var trigger = button('btn watch-btn--keep ttl-facts__btn', '', null);
      trigger.setAttribute('data-ttl-act', 'admin-keep');
      trigger.setAttribute('aria-haspopup', 'menu');
      trigger.setAttribute('aria-expanded', 'false');
      if (f.keep && f.keep.state !== 'none') trigger.appendChild(icon('i-check'));
      trigger.appendChild(document.createTextNode(f.keep && f.keep.state !== 'none' ? 'Kept' : 'Keep'));
      trigger.appendChild(icon('i-chevron-down', 'watch-keep__chev'));
      var menu = el('div', 'ttl-facts__menu');
      menu.setAttribute('role', 'menu');
      menu.setAttribute('aria-label', 'Keep for');
      menu.hidden = true;
      [['7 days', 'Keep 7 days'], ['30 days', 'Keep 30 days'], ['90 days', 'Keep 90 days'], ['forever', 'Keep forever']]
        .forEach(function (opt) {
          var item = button('watch-keep-menu__item', opt[1]);
          item.setAttribute('role', 'menuitem');
          item.setAttribute('tabindex', '-1');
          item.addEventListener('click', function () {
            closeAdminMenu(true);
            var leaves = leavingNow();
            api.keep(Object.assign({}, wrapper, { season_id: leaves ? leaves.id : '' }), opt[0]);
          });
          menu.appendChild(item);
        });
      if (f.keep && f.keep.state !== 'none' && api.removeKeep) {
        var remove = button('watch-keep-menu__item', 'Remove keep');
        remove.setAttribute('role', 'menuitem');
        remove.setAttribute('tabindex', '-1');
        remove.addEventListener('click', function () {
          closeAdminMenu(true);
          // A show's keeps can sit on several seasons and on the show; the dialog's Remove all clears them.
          if (wrapper.is_tv) api.keep(Object.assign({}, wrapper, { season_id: '' }));
          else api.removeKeep(wrapper);
        });
        menu.appendChild(remove);
      }
      trigger.addEventListener('click', function () {
        if (adminMenu) closeAdminMenu(false); else openAdminMenu(trigger, menu);
      });
      wrap.appendChild(trigger);
      wrap.appendChild(menu);
      foot.appendChild(wrap);
    }
    var del = actions.delete;
    var canDelete = del && (del.kind === 'movie' ? api.confirmDelete : api.pickSeasonDelete);
    if (canDelete) {
      var delBtn = button('btn btn--danger ttl-facts__btn', 'Delete', 'i-trash');
      delBtn.setAttribute('data-ttl-act', 'admin-delete');
      if (del.kind !== 'movie') delBtn.setAttribute('aria-haspopup', 'dialog');
      delBtn.addEventListener('click', function () {
        delBtn.focus({ preventScroll: true }); // as Safari does not on a click, for when the dialog closes
        if (del.kind === 'movie') api.confirmDelete(del.id, del.title, { kind: 'movie', size: f.size });
        else api.pickSeasonDelete(del.show_rk, del.title, (actions.keep_wrapper || {}).seasons);
      });
      foot.appendChild(delBtn);
    }
    return foot.children.length ? foot : null;
  }

  /* The admin actions announce what they did; one on this title redraws the
     facts and actions, and a deleted movie closes the overlay (a show whose
     last season went closes once its facts are gone). */
  document.addEventListener('mm:watch-admin', function (e) {
    var d = e.detail || {};
    if (d.phase !== 'done') return;
    if (d.action === 'delete') {
      if (!d.showRk) {
        leftLibrary('movie-' + d.id);
        return;
      }
      // Closed meanwhile, or another title: was it the show's last season?
      if (!isOpen || !cur || cur.ref !== 'show-' + d.showRk) {
        get(apiUrl('show-' + d.showRk)).catch(function (err) {
          if (err && err.status === 404) leftLibrary('show-' + d.showRk);
        });
        return;
      }
    }
    if (!isOpen || !cur || !cur.facts) return;
    var a = cur.facts.actions || {};
    var w = a.keep_wrapper || {};
    var del = a.delete || {};
    var ids = [w.id, del.id].filter(function (x) { return x != null; }).map(String);
    var showRk = w.show_rk || del.show_rk || '';
    var mine = (d.id && ids.indexOf(String(d.id)) >= 0) || (showRk && d.showRk === showRk);
    if (mine) refresh();
  });

  function menuItems(menu) { return toArray(menu.querySelectorAll('[role="menuitem"]')); }

  function openAdminMenu(trigger, menu) {
    adminMenu = { trigger: trigger, menu: menu };
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    void menu.offsetWidth; // start the entrance from the closed look
    menu.classList.add('is-open');
    var first = menuItems(menu)[0];
    if (first) first.focus({ preventScroll: true });
  }

  function closeAdminMenu(refocus) {
    if (!adminMenu) return;
    var m = adminMenu;
    adminMenu = null;
    m.menu.classList.remove('is-open');
    m.menu.hidden = true;
    m.trigger.setAttribute('aria-expanded', 'false');
    if (refocus && document.body.contains(m.trigger)) m.trigger.focus({ preventScroll: true });
  }

  /* ── A viewer's Keep: keep.js marks the title busy while it saves, and announces
     what it saved, such as an Undo pressed in a toast from before the title was
     opened; then the overlay loads the title again to show what changed. ── */

  var keptAsked = false;

  /* Once for a Keep, however many ways it is heard of. */
  function keptChanged() {
    if (!isOpen || !cur || keptAsked) return;
    keptAsked = true;
    Promise.resolve().then(function () {
      keptAsked = false;
      refresh(true);
    });
  }

  document.addEventListener('mm:watch-kept', keptChanged);

  if (typeof MutationObserver === 'function') {
    new MutationObserver(function (records) {
      var settled = cur && records.some(function (r) {
        var t = r.target;
        return cur.keepIds[t.getAttribute('data-watch-keep')] &&
          r.oldValue === 'true' && t.getAttribute('aria-busy') === 'false';
      });
      if (settled) keptChanged();
    }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['aria-busy'], attributeOldValue: true });
  }

  /* ── Keys and focus ── */

  /* Elsewhere focus may go while the overlay is open: menus, dialogs and
     toasts opened over it, and the bar of what plays on a TV. */
  function allowedOutside(node) {
    return !!(node && node.closest &&
      node.closest('[role="menu"], [role="dialog"], [role="alertdialog"], .modal-backdrop, .ui-toast, .ui-toast-host, #tv-mini'));
  }

  document.addEventListener('keydown', function (e) {
    if (!isOpen) return;
    var target = e.target;
    var inside = root.contains(target) || target === document.body || target === document.documentElement || !target ||
      !!target.closest('#tv-mini');
    if (e.key === 'Escape') {
      if (e.defaultPrevented || !inside) return;
      e.preventDefault();
      if (adminMenu) closeAdminMenu(true);
      else if (cast.dismiss()) return;
      else if (trailerOn()) leaveTrailer();
      else requestClose();
      return;
    }
    if (adminMenu && adminMenu.menu.contains(target)) {
      var items = menuItems(adminMenu.menu);
      var i = items.indexOf(target);
      if (e.key === 'ArrowDown') items[(i + 1) % items.length].focus();
      else if (e.key === 'ArrowUp') items[(i - 1 + items.length) % items.length].focus();
      else if (e.key === 'Home') items[0].focus();
      else if (e.key === 'End') items[items.length - 1].focus();
      else if (e.key === 'Tab') { closeAdminMenu(true); return; }
      else return;
      e.preventDefault();
      return;
    }
  });

  document.addEventListener('focusin', function (e) {
    if (!isOpen || root.contains(e.target) || allowedOutside(e.target)) return;
    titleEl.focus({ preventScroll: true });
  });

  root.addEventListener('click', function () { if (cur && cur.data) cur.pressed = true; }, true);

  /* Where Close stays on screen, what scrolls under it is out of sight and of reach. */
  function markPast() {
    if (!isOpen) return;
    root.classList.toggle('is-past', root.scrollTop > 0 &&
      head.getBoundingClientRect().top <= bar.getBoundingClientRect().bottom);
  }
  root.addEventListener('scroll', markPast, { passive: true });
  window.addEventListener('resize', markPast);

  /* ── Clicks ── */

  root.addEventListener('click', function (e) {
    var t = e.target;
    if (adminMenu && !adminMenu.menu.contains(t) && !adminMenu.trigger.contains(t)) closeAdminMenu(false);
    var toggle = t.closest && t.closest('[data-mark-toggle]');
    if (toggle) toggle.focus({ preventScroll: true }); // Safari leaves a pressed button without it
    if (t.closest && t.closest('[data-ttl-close]')) {
      e.preventDefault();
      if (trailerOn()) leaveTrailer();
      // The rest of a double click that closed the trailer leaves the title.
      else if (!(e.detail > 1) || Date.now() - trailerLeftAt > DOUBLE_CLICK_MS) requestClose();
    }
  });

  /* The rest of a double click on Close, which would press what is under it. */
  document.addEventListener('click', function (e) {
    if (isOpen || !(e.detail > 1) || Date.now() - closedAt > DOUBLE_CLICK_MS) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  /* A toast goes with the press of its button, and would take the focus too. After the
     Undo of a Keep it goes to the Keep button once that is back. */
  document.addEventListener('click', function (e) {
    if (!isOpen || !e.target.closest || !e.target.closest('.ui-toast__action')) return;
    var active = document.activeElement;
    // The action has moved it already; from behind the overlay it only came back to the title.
    if (active !== titleEl && root.contains(active)) return;
    var to = byAct('admin-keep') || byAct('keep') || root.querySelector('[data-ttl-part="actions"] > *') || titleEl;
    to.focus({ preventScroll: true });
    cur.keepComes = focusKey();
  });

  function cardTitle(node) {
    var named = node.getAttribute('data-title') || node.getAttribute('aria-label') || '';
    if (named) return named;
    var caption = node.querySelector('.watch-card__title, .watch-card__name');
    return caption ? caption.textContent.trim() : '';
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest || e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var card = t.closest('[data-title-ref]');
    if (!card || root.contains(card)) return;
    // A control inside the card (Keep) does its own thing.
    var control = t.closest('button, input, select, textarea, [data-watch-keep]');
    if (control && control !== card && card.contains(control)) return;
    var ref = card.getAttribute('data-title-ref');
    if (!REF_RE.test(ref)) return;
    e.preventDefault();
    var season = /[?&]season=([0-9]{1,4})(?:&|$)/.exec(card.getAttribute('href') || '');
    open(ref, { origin: card, title: cardTitle(card), kind: card.getAttribute('data-kind'), pick: card.getAttribute('data-pick-ref'),
      season: season ? Number(season[1]) : undefined, atEpisodes: card.getAttribute('data-title-at') === 'episodes', push: true });
  });

  /* ── The address ── */

  /* A title the address opens: as it was left when Back leads to it. */
  function openAt(ref, how) {
    if (how === 'pop' && kept[ref]) comeBack(kept[ref]);
    else open(ref, { pick: window.history.state && window.history.state.mmPick });
  }

  address.layer({
    name: 'title',
    depth: 50,
    params: [],
    hash: true,
    read: hashRef,
    open: function (ref, how) {
      // The title's own entry, left as it was when its trailer's took the library ref.
      if (isOpen && ref === cur.openedAs && ref !== cur.ref) readdress(cur.ref);
      else if (!isOpen || cur.ref !== ref) openAt(ref, how);
    },
    close: function (how) {
      close(how === 'push');
      if (!cur || !cur.gone || how !== 'pop') return false;
      // The page has been put right for what left it. Safari moves it as it steps back.
      var y = window.scrollY;
      Promise.resolve().then(function () {
        window.scrollTo({ top: y, behavior: 'instant' });
        address.hold(root);
      });
      return true;
    }
  });

  /* In no address: a title opened by a link, or again, starts without it. */
  address.layer({
    name: 'trailer',
    depth: 60,
    params: [],
    read: function (here) { return here.entry === 'trailer' && hashRef() ? 'on' : null; },
    open: function (value, how) {
      if (how === 'start' || !cur || !cur.data) address.back('trailer');
      else openTrailer();
    },
    close: function (how) { closeTrailer(how === 'pop' && !!hashRef()); }
  });

  var old = /^#recommendation-([0-9]{1,10})$/.exec(window.location.hash || '');
  if (old) address.replace(null, address.url({}, 'title=rec-' + old[1]));

  window.MMWatch.detail = {
    open: function (ref, opts) {
      opts = opts || {};
      open(ref, { origin: opts.origin, title: opts.title, kind: opts.kind, pick: opts.pick, push: true });
    },
    close: requestClose
  };
})();
