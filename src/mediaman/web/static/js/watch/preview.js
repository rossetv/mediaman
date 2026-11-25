/* Watch page: a card rested on with the mouse grows into a short preview of its
 * title. It works from what every poster card carries
 * (.watch-card__link, data-title-ref or a Continue watching card's
 * data-dismiss-ref), so a row or grid that draws cards needs nothing more.
 * It is for the mouse alone: nothing is in it that the card and the title detail
 * do not offer a keyboard, a touch screen or a screen reader.
 * A scroll down over it opens it into the title's page, which follows the hand.
 * Needs watch/core.js, watch/keep.js, watch/card-tools.js, watch/marks.js, watch/detail.js, watch/carry.js and ratings.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch && watch.core;
  if (!core || !window.matchMedia) return;

  var el = core.el;
  var icon = window.MM.dom.icon;
  var safePath = core.safePath;

  var REF_RE = /^[a-z]+(-[a-z]+)?-[0-9]{1,18}$/;
  var PICK_RE = /^(rec|pick)-[0-9]{1,10}$/;
  var LIBRARY_RE = /^(?:movie|show)-([0-9]+)$/;
  var YOUTUBE_KEY_RE = /^[A-Za-z0-9_-]{11}$/;
  var TRAILER_ORIGIN = 'https://www.youtube-nocookie.com';
  var TRAILERS_OFF = 'mm.watch.hover-trailers';
  var SCROLLED = 'mm.watch.preview-scrolled'; // one was scrolled open, so the hint is not needed

  var INTENT_MS = 350;      // how long the mouse rests on a card
  var WARM_MS = 160;        // the same, just after another preview
  var WARM_FOR_MS = 500;
  var FAST = 0.35;          // px per ms: passing over, not resting
  var SPEED_OVER_MS = 120;
  var WAIT_MS = 200;        // how long the opening waits for the reply
  var GRACE_MS = 110;       // how long the mouse may be off it
  var SETTLE_MS = 200;      // after a scroll, cards move under a mouse that does not
  var TRAILER_AFTER_MS = 1200;
  var TRAILER_STUCK_MS = 4000; // without its player moving on
  var LISTEN_MS = 250;
  var HOLD_MS = 1500;       // how long the detail keeps the preview's picture, at most
  var FAILED_FOR_MS = 60000;
  var KEPT_FOR_MS = 300000; // what a title says of its progress and download goes stale
  var DWELL_MS = 130;       // a mouse resting on the thumb
  var OWN_CLICK_MS = 500;   // a press on it once it has opened by itself
  var LOWER_MS = 420;       // a mouse off the levels
  var HELD = 100;           // titles remembered
  var SLACK_MS = 60;        // past a motion's own length, for one a busy page starts late
  var GAP_MS = 180;         // wheel events less apart are one scroll
  var READY_MS = 100;       // how long it has stood open, to have been seen, before a scroll opens it
  var REST_MS = 170;        // a scroll that has stopped short of opening it
  var NOTCH_REST_MS = 520;  // the same of a wheel, whose next notch may be half a second away

  var live = null;     // the preview open
  var closing = null;  // the one folding back into its card
  var armed = null;    // the card about to be previewed
  var armTimer = null;
  var waitTimer = null;
  var closeTimer = null;
  var warmUntil = 0;
  var scrolledAt = -SETTLE_MS;
  var pos = null;      // where the mouse was last, unless the view has changed since
  var trail = [];
  var flight = null;   // the one request on its way: { ref, ctrl, promise }
  var kept = new Map();   // replies by ref: { data, at }
  var keptBy = new Map(); // the title each keep id met in a reply is of
  var failedAt = new Map();
  var refusedAt = 0;   // the server asked for a pause
  var badTrailers = new Map();
  var carried = null;  // the preview on its way to its title's page, or back: { p, motion, stage }
  var held = null;     // the one whose picture stays over the page it became
  var swallow = false; // the click of a press that hurried it
  var stream = null;   // the scroll under way: see ownerOf

  function now() { return performance.now(); }

  /* A page may be open for days: the latest of each are enough. */
  function hold(map, key, value) {
    map.delete(key);
    map.set(key, value);
    if (map.size > HELD) map.delete(map.keys().next().value);
  }

  function mouse() { return window.matchMedia('(hover: hover) and (pointer: fine)').matches; }

  /* Something is open over the cards (the detail, a dialog or a menu), or a grid carries them. */
  function covered() {
    return detailOpen() || document.documentElement.classList.contains('is-grid-moving') ||
      !!document.querySelector('.modal-backdrop:not([hidden]), .ui-confirm-overlay, .watch-keep-menu.is-open, .play-on[open], #tv-remote:not([hidden])');
  }

  /* ── What a card says ── */

  function cardOf(link) { return link.closest('.watch-card'); }

  function posterOf(link) { return link.querySelector('.watch-card__poster'); }

  function refOf(link) {
    var card = cardOf(link);
    var ref = link.getAttribute('data-title-ref') || (card && card.getAttribute('data-dismiss-ref')) || '';
    return REF_RE.test(ref) ? ref : '';
  }

  /* The pick a card shows, also where its title is the library's and opens by that. */
  function pickOf(link) {
    var given = link.getAttribute('data-pick-ref') || refOf(link);
    return PICK_RE.test(given) ? given : '';
  }

  /* What a card's preview is asked by: the pick, which has why it is one. */
  function askedOf(link) { return pickOf(link) || refOf(link); }

  function textOf(root, selector) {
    var node = root.querySelector(selector);
    return node ? node.textContent.trim() : '';
  }

  function readCard(link) {
    var poster = posterOf(link);
    var bar = poster.querySelector('progress');
    var img = poster.querySelector('img');
    var ref = refOf(link);
    var sub = textOf(link, '.watch-card__sub');
    var season = /^(?:Season (\d+)$|S(\d+) · E)/.exec(sub);
    return {
      ref: ref,
      title: textOf(link, '.watch-card__title'),
      href: link.getAttribute('href'),
      plays: !link.hasAttribute('data-title-ref'), // Continue watching: the card is a play link
      sub: sub,
      season: season ? Number(season[1] || season[2]) : null,
      keepId: cardOf(link).getAttribute('data-keep-id') || '',
      pct: bar ? bar.value : 0,
      leaving: textOf(poster, '.watch-badge__long'),
      poster: img && !img.hasAttribute('data-broken') ? img.getAttribute('src') : '',
      pick: pickOf(link)
    };
  }

  /* ── The reply ──
     One request at a time. A card left before its reply drops it. */

  function drop(error) { return Promise.reject(error || new Error('not asked')); }

  function ask(ref) {
    var had = kept.get(ref);
    if (had && Date.now() - had.at < KEPT_FOR_MS) return Promise.resolve(had.data);
    var since = Math.max(failedAt.get(ref) || 0, refusedAt);
    if (since && Date.now() - since < FAILED_FOR_MS) return drop();
    if (flight && flight.ref === ref) return flight.promise;
    forget();
    var f = { ref: ref, ctrl: new AbortController(), promise: null };
    flight = f;
    f.promise = MM.api.get('/api/watch/preview/' + encodeURIComponent(ref), { signal: f.ctrl.signal }).then(function (data) {
      if (flight !== f) return drop();
      flight = null;
      (data.leaving_seasons || []).concat(data.leaving || []).forEach(function (l) { hold(keptBy, String(l.keep_id), data.library_ref); });
      if (f.stale) return ask(ref);
      hold(kept, ref, { data: data, at: Date.now() });
      return data;
    }, function (err) {
      if (flight === f) {
        flight = null;
        hold(failedAt, ref, Date.now());
        if (err && err.status === 429) refusedAt = Date.now();
      }
      throw err;
    });
    return f.promise;
  }

  /* Give up the request on its way. */
  function forget() {
    if (!flight) return;
    var f = flight;
    flight = null;
    f.ctrl.abort();
  }

  function unkeep(ref) {
    kept.forEach(function (had, key) {
      if (key === ref || had.data.library_ref === ref) kept.delete(key);
    });
  }

  /* The title *ref* has changed, or any has: what was said of it, or is on its way, is from before.
     Returns the preview open of it that is to say it again. */
  function outOfDate(ref) {
    if (ref) unkeep(ref);
    else kept.clear();
    if (flight) flight.stale = true;
    var p = live;
    if (!p || !p.data || !p.data.library_ref || (ref && ref !== p.data.library_ref)) return null;
    if (!armed) return p;
    close(p); // the one request is the next card's
    return null;
  }

  /* A Keep or an Undo has finished: it offers none until it is known again what leaves. */
  function keptOrUndone(id) {
    var p = outOfDate(keptBy.get(String(id)));
    if (!p) return;
    sayAgain(p, Object.assign({}, p.data, { leaving: null, leaving_seasons: [] }));
    ask(askedOf(p.link)).then(function (data) {
      if (live === p) sayAgain(p, data);
    }, function () {
      // It stays without a Keep.
    });
  }

  /* Marked as watched or not, or that undone. */
  function marked(ref) {
    var p = outOfDate(ref);
    if (!p) return;
    ask(askedOf(p.link)).then(function (data) {
      if (live === p) sayAgain(p, data);
    }, function () {
      if (live === p) close(p); // it would go on offering to resume
    });
  }

  function sayAgain(p, data) {
    closeMenus(p);
    p.data = data;
    p.card = readCard(p.link);
    p.keep = null;
    p.body.querySelectorAll('.pv__meta')[1].replaceWith(where(p, data));
    actions(p);
    progress(p);
  }

  /* ── Resting on a card ── */

  function speed() {
    var t = now();
    trail = trail.filter(function (p) { return t - p.t < SPEED_OVER_MS; });
    if (trail.length < 2) return 0;
    var a = trail[0];
    var b = trail[trail.length - 1];
    return Math.sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y)) / SPEED_OVER_MS;
  }

  function arm(link) {
    disarm();
    armed = link;
    function fire() {
      if (armed !== link) return;
      if (speed() > FAST) {
        armTimer = setTimeout(fire, SPEED_OVER_MS);
        return;
      }
      // A busy page runs this late, with moves still queued: they are taken in first.
      window.requestAnimationFrame(function () {
        if (armed !== link) return;
        armTimer = setTimeout(function () { if (armed === link) begin(link); }, 0);
      });
    }
    armTimer = setTimeout(fire, now() < warmUntil ? WARM_MS : INTENT_MS);
  }

  function disarm() {
    clearTimeout(armTimer);
    clearTimeout(waitTimer);
    if (armed && !(live && live.link === armed)) forget();
    armed = null;
  }

  function begin(link) {
    if (document.hidden || covered() || !posterOf(link)) {
      disarm();
      return;
    }
    var ref = refOf(link);
    var first = ownArt(ref);
    if (first) el('img').setAttribute('src', first); // so that it is mostly there when the preview opens
    function go(data) {
      clearTimeout(waitTimer);
      armed = null;
      if (!open(link, data) && !data) forget();
    }
    waitTimer = setTimeout(function () { go(null); }, WAIT_MS);
    ask(askedOf(link)).then(function (data) {
      if (armed === link) go(data);
      else if (live && live.link === link && !live.data) fillIn(live, data);
    }, function () {
      // It stays a lifted card, as if it had never been rested on.
      if (armed === link) disarm();
      else if (live && live.link === link && !live.data) close(live);
    });
  }

  /* ── Opening ── */

  function ownArt(ref) {
    var key = LIBRARY_RE.exec(ref);
    return key ? '/api/poster/' + key[1] + '?kind=art' : '';
  }

  function width() { return window.innerWidth >= 1600 ? 480 : window.innerWidth >= 1000 ? 400 : 360; }

  /* Where a preview may be: clear of the window's edges, the bars and an open grid's header. */
  function room() {
    var area = core.clearArea();
    var head = document.querySelector('.watch-all__head');
    var top = head ? Math.max(area.top, head.getBoundingClientRect().bottom) : area.top;
    return { top: top + 8, bottom: area.bottom - 8, left: 12, right: document.documentElement.clientWidth - 12 };
  }

  function halfShown(r, box) {
    var across = Math.min(r.right, window.innerWidth) - Math.max(r.left, 0);
    var down = Math.min(r.bottom, box.bottom) - Math.max(r.top, box.top);
    return across >= r.width / 2 && down >= r.height / 2;
  }

  function layer(link, w) {
    var p = { link: link, card: readCard(link), w: w, data: null, closing: false, timer: null, frame: null, muted: true };
    var n = p.n = el('div', 'pv is-placing');
    n.setAttribute('aria-hidden', 'true');
    n.style.width = w + 'px';
    n.appendChild(el('div', 'pv__shadow'));
    var shell = p.shell = n.appendChild(el('div', 'pv__shell'));
    var media = p.media = shell.appendChild(el('div', 'pv__media'));
    var picture = p.picture = media.appendChild(el('a', 'pv__picture'));
    picture.setAttribute('href', p.card.href);
    picture.setAttribute('tabindex', '-1');
    picture.setAttribute('draggable', 'false');
    if (p.card.poster) picture.appendChild(image('pv__still', p.card.poster));
    p.body = shell.appendChild(el('div', 'pv__body'));
    if (p.card.pick) n.classList.add('pv--reason');
    if (!known(SCROLLED)) p.hint = shell.appendChild(hint());
    shell.appendChild(posterCopy(p));
    if (watch.marks && watch.marks.canRate(p.card.ref, link)) rating(p);
    n.addEventListener('mousedown', function (e) { e.preventDefault(); }); // the focus stays where it was
    n.addEventListener('focusin', function (e) {
      if (!e.target.hasAttribute('data-watch-keep')) focusCard(p);
    });
    shell.addEventListener('click', function (e) { pressed(p, e); });
    return p;
  }

  /* That more lies below. */
  function hint() {
    var n = el('span', 'pv__hint');
    n.appendChild(icon('i-chevron-down'));
    return n;
  }

  function image(cls, src) {
    var img = el('img', cls);
    img.setAttribute('alt', '');
    img.setAttribute('draggable', 'false');
    img.setAttribute('decoding', 'sync');
    img.setAttribute('src', src);
    return img;
  }

  /* The card's poster as it is, badges and all, to grow out of and back into. */
  function posterCopy(p) {
    var poster = posterOf(p.link);
    p.cardW = poster.offsetWidth;
    p.cardH = poster.offsetHeight;
    var box = el('div', 'pv__poster');
    var copy = poster.cloneNode(true);
    core.toArray(copy.querySelectorAll('img')).forEach(function (img) {
      img.setAttribute('loading', 'eager');
      img.setAttribute('decoding', 'sync');
    });
    box.style.width = p.cardW + 'px';
    box.appendChild(copy);
    return box;
  }

  function there(p) { return document.body.contains(p.link) && p.link.getClientRects().length > 0; }

  /* With the mouse at rest, the card of the preview that is open may go, or the keyboard open something over it. */
  var watcher = new MutationObserver(function () {
    if (!live) return;
    if (carried ? !live.link.isConnected : !there(live)) close(live, 'fade');
    else if (carried) return;
    else if (covered()) setTimeout(closeUnder); // a press on its card hands it over first
  });

  function closeUnder() {
    if (live && !carried && covered() && !pinned(live)) fadeLive();
  }

  function setLive(p) {
    live = p;
    if (p) watcher.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] });
    else watcher.disconnect();
  }

  /* Open *link*'s preview, with *data* or while it is on its way. */
  function open(link, data) {
    var box = room();
    var w = width();
    var r = posterOf(link).getBoundingClientRect();
    if (!halfShown(r, box) || w > box.right - box.left) return false;
    var p = layer(link, w);
    content(p, data);
    layerNode.appendChild(p.n);
    p.h = p.shell.offsetHeight;
    if (p.h > box.bottom - box.top) {
      p.n.remove();
      return false;
    }
    var x = Math.round(Math.max(box.left, Math.min(box.right - w, r.left + r.width / 2 - w / 2)));
    var y = Math.round(Math.max(box.top, Math.min(box.bottom - p.h, r.top + r.height / 2 - p.h / 2)));
    p.n.style.setProperty('--pv-to', 'translate(' + x + 'px,' + (y + window.scrollY) + 'px)');
    p.shell.style.height = p.h + 'px'; // what arrives later never moves it
    fold(p);
    link.classList.add('is-previewed');
    setLive(p);
    void p.n.offsetWidth; // start from the card's own look
    p.n.classList.remove('is-placing');
    unfold(p);
    if (data) planTrailer(p);
    return true;
  }

  function unfold(p) {
    p.closing = false;
    p.n.classList.add('is-open');
    later(p, growMs(), function () {
      p.n.classList.add('is-settled');
      p.settledAt = now();
      askFilm(p);
    });
  }

  function growMs() { return core.reduceMotion() ? core.tokenMs('--dur-fast', 150) : core.tokenMs('--dur-fast', 150) * 2; }

  function later(p, ms, fn) {
    clearTimeout(p.timer);
    p.timer = setTimeout(fn, ms + SLACK_MS);
  }

  /* The folded look: how the final box is moved, shrunk and cut to be the card. */
  function fold(p) {
    var r = posterOf(p.link).getBoundingClientRect();
    var s = Math.max(r.width / p.w, r.height / p.h);
    var ix = (p.w - r.width / s) / 2;
    var iy = (p.h - r.height / s) / 2;
    var grown = p.w / p.cardW;
    function set(name, value) { p.n.style.setProperty(name, value); }
    set('--pv-from', 'translate(' + (r.left - ix * s) + 'px,' + (r.top + window.scrollY - iy * s) + 'px) scale(' + s + ')');
    set('--pv-ix', ix + 'px');
    set('--pv-iy', iy + 'px');
    set('--pv-r', 10 / s + 'px');
    set('--pv-poster-from', 'translate(' + ix + 'px,' + iy + 'px) scale(' + r.width / s / p.cardW + ')');
    set('--pv-poster-to', 'translate(0,' + Math.min(0, (p.h - p.cardH * grown) / 2) + 'px) scale(' + grown + ')');
  }

  /* ── Closing ──
     *how*: 'fade' where it stands (the page or its row has moved), 'hand' over
     to the detail, 'cut' at once, else back into its card. */
  function close(p, how) {
    if (p.closing) return;
    if (carried && carried.p === p) putDown();
    p.closing = true;
    p.settledAt = 0;
    if (live === p) setLive(null);
    if (!p.data) forget();
    clearTimeout(closeTimer);
    closeTimer = null;
    warmUntil = now() + WARM_FOR_MS;
    if (holdsFocus(p)) focusCard(p);
    closeMenus(p);
    clearTimeout(p.trailerTimer);
    tell(p, 'mute');
    p.n.classList.remove('is-playing', 'is-settled');
    p.link.classList.remove('is-previewed');
    if (how === 'cut') {
      remove(p);
      return;
    }
    if (!how && !there(p)) how = 'fade';
    var ms = core.tokenMs('--dur', 200);
    if (how === 'fade') p.n.classList.add('is-fading');
    else if (how === 'hand') p.n.classList.add('is-handed');
    else {
      fold(p);
      p.n.classList.remove('is-open');
      closing = p;
    }
    later(p, ms, function () { remove(p); });
  }

  function remove(p) {
    clearTimeout(p.timer);
    if (closing === p) closing = null;
    stopTrailer(p);
    if (p.rating) p.rating.destroy();
    p.n.remove();
  }

  /* Back on the card before it has folded up: it opens again from where it is. */
  function reopen(p) {
    if (live) close(live);
    disarm();
    closing = null;
    setLive(p);
    stopTrailer(p);
    p.link.classList.add('is-previewed');
    unfold(p);
    if (p.data) {
      planTrailer(p);
      return;
    }
    ask(askedOf(p.link)).then(function (data) {
      if (live === p && !p.data) fillIn(p, data);
    }, function () {
      if (live === p) close(p);
    });
  }

  function fadeLive() {
    disarm();
    if (live) close(live, 'fade');
  }

  function viewChanged() {
    pos = null;
    fadeLive();
    letGo();
  }

  /* ── What it holds ── */

  function button(cls, label, iconName) {
    var b = el('button', 'btn ' + cls);
    b.type = 'button';
    b.setAttribute('tabindex', '-1');
    if (iconName) b.appendChild(icon(iconName));
    if (label) b.appendChild(document.createTextNode(label));
    return b;
  }

  function round(label, iconName) {
    var b = button('watch-btn--grey pv__round', '', iconName);
    name(b, label);
    return b;
  }

  function name(b, label) {
    b.setAttribute('aria-label', label);
    b.setAttribute('title', label);
  }

  function skel(width, cls) {
    var s = el('span', cls || 'skel skel--line');
    if (width) s.style.width = width;
    return s;
  }

  function dur(minutes) {
    if (minutes < 60) return minutes + ' min';
    return Math.floor(minutes / 60) + ' h' + (minutes % 60 ? ' ' + (minutes % 60) + ' min' : '');
  }

  function addPart(line, cls, text, strong) {
    if (!text && !strong) return;
    var part = line.appendChild(el('span', cls, text));
    if (strong) part.appendChild(el('b', '', strong));
  }

  /* Year, length, certificate, ratings. */
  function facts(p, d) {
    var line = el('div', 'pv__meta');
    addPart(line, '', d.year ? String(d.year) : '');
    if (d.kind === 'movie') addPart(line, '', d.runtime_min ? dur(d.runtime_min) : '');
    else addPart(line, '', d.season_count ? d.season_count + (d.season_count === 1 ? ' season' : ' seasons') : '');
    addPart(line, 'pv__cert', d.certificate);
    var r = d.ratings || {};
    if (r.imdb) addPart(line, '', 'IMDb ', r.imdb);
    else if (typeof r.tmdb === 'number' && r.tmdb > 0) addPart(line, '', 'TMDB ', r.tmdb.toFixed(1));
    if (r.rt && p.w > 365) addPart(line, '', 'RT ', r.rt);
    return line;
  }

  /* When it leaves, what is left of it to watch, its genres. */
  function where(p, d) {
    var line = el('div', 'pv__meta');
    var leaving = leavingOf(p);
    addPart(line, 'pv__leaving', leaving ? leaving.text : '');
    var left = '';
    if (p.card.plays) left = p.card.sub.replace(/^(Movie|TV) · /, '').replace(/ · ([^·]* left)$/, ', $1');
    else if (d.play && d.play.minutes_left) left = dur(d.play.minutes_left) + ' left';
    else if (d.play) left = d.play.label.replace(/^\S+ ?/, ''); // "Resume S2 · E4"
    addPart(line, 'pv__grow', [left, (d.genres || []).slice(0, 3).join(', ')].filter(Boolean).join(' · '));
    return line;
  }

  function content(p, d) {
    p.data = d;
    var head = el('div', 'pv__head');
    head.appendChild(el('p', 'pv__title', (d && d.title) || p.card.title));
    if (d) {
      head.appendChild(facts(p, d));
      head.appendChild(where(p, d));
    } else {
      ['62%', '34%'].forEach(function (w) { head.appendChild(el('div', 'pv__meta')).appendChild(skel(w)); });
    }
    p.body.textContent = '';
    p.body.appendChild(head);
    p.actions = p.body.appendChild(el('div', (p.actions && p.actions.className) || 'pv__actions'));
    actions(p);
    if (p.rating) p.body.appendChild(p.rating.line);
    if (!d && p.card.pick) p.body.appendChild(reason('')).appendChild(lines(['92%', '60%']));
    else if (d && (d.reason || p.card.pick)) p.body.appendChild(reason(d.reason || ''));
    p.n.classList.toggle('pv--reason', !!p.card.pick || !!(d && d.reason));
    var words = d && (d.overview || d.tagline);
    if (words) p.body.appendChild(el('p', 'pv__text', words));
    else if (!d) p.body.appendChild(lines(p.card.pick ? ['96%', '70%'] : ['100%', '94%', '58%']));
    progress(p);
    if (d) showArt(p, d);
  }

  function lines(widths) {
    var box = el('div', 'pv__skels');
    widths.forEach(function (w) { box.appendChild(skel(w)); });
    return box;
  }

  function reason(text) {
    var box = el('div', 'pv__reason');
    box.appendChild(core.aiLabel('AI pick'));
    if (text) box.appendChild(el('p', '', text));
    return box;
  }

  function progress(p) {
    var old = p.body.querySelector('.pv__progress');
    if (old) old.remove();
    var pct = p.card.pct || (p.data && p.data.play && p.data.play.progress_pct);
    if (!pct) return;
    var bar = el('progress', 'watch-bar pv__progress');
    bar.max = 100;
    bar.value = pct;
    p.body.appendChild(bar);
  }

  /* The reply came after it opened: everything takes its place where it stands. */
  function fillIn(p, data) {
    content(p, data);
    core.toArray(p.body.querySelectorAll('.pv__meta, .pv__text, .pv__reason, .pv__first, .pv__trailerbtn'))
      .forEach(core.fadeIn);
    planTrailer(p);
    if (p.settledAt) askFilm(p);
  }

  /* ── Its picture ──
     The card's poster, until a wide picture has loaded over it. */

  function showArt(p, d) {
    var tries = [ownArt(d.library_ref || ''), safePath(d.backdrop_url)].filter(Boolean);
    function next() {
      var src = tries.shift();
      if (!src) {
        if (p.card.poster) standIn(p);
        return;
      }
      var img = image('pv__art', src);
      img.onload = function () {
        if (img.naturalWidth < 1.2 * img.naturalHeight) {
          img.onerror();
          return;
        }
        p.art = img;
        img.classList.add('is-loaded');
      };
      img.onerror = function () {
        img.remove();
        next();
      };
      p.picture.appendChild(img);
    }
    next();
  }

  /* No wide picture: the poster whole, on a blur of itself. */
  function standIn(p) {
    var box = el('span', 'pv__art pv__standin');
    box.appendChild(image('pv__blur', p.card.poster));
    box.appendChild(image('pv__whole', p.card.poster));
    p.picture.appendChild(box);
    void box.offsetWidth; // fade it in
    box.classList.add('is-loaded');
  }

  /* ── Buttons ── */

  function actions(p) {
    var d = p.data;
    p.actions.textContent = '';
    var first = primary(p);
    if (first) {
      first.classList.add('pv__first');
      p.actions.appendChild(first);
    }
    p.actions.appendChild(el('span', 'pv__gap'));
    var plays = p.card.plays ? p.card.href : d && d.in_library && d.play && safePath(d.play.href);
    if (plays && window.MM.playOn) {
      // Its sheet may be open, which goes on with what it was opened for.
      if (!p.playOn || p.plays !== plays && p.playOn.getAttribute('aria-expanded') !== 'true') {
        p.plays = plays;
        p.playOn = window.MM.playOn.button(plays, 'btn watch-btn--grey pv__round');
        p.playOn.setAttribute('tabindex', '-1');
      }
      p.actions.appendChild(p.playOn);
    }
    p.trailerButton = null;
    if (d && trailerKey(d)) {
      p.trailerButton = p.actions.appendChild(round('', 'i-film'));
      p.trailerButton.classList.add('pv__trailerbtn');
      p.trailerButton.addEventListener('click', function () { pressTrailer(p); });
      faceTrailer(p);
    }
    p.eye = p.eye || eye(p);
    if (p.eye) p.actions.appendChild(p.eye);
    if (p.rating) p.actions.appendChild(p.rate);
    // Kept through every redraw: its menu may be open.
    p.more = p.more || moreButton();
    p.actions.appendChild(p.more);
    if (keepIdOf(p)) {
      p.keep = p.keep || keepTrigger(p);
      p.actions.appendChild(p.keep);
    }
  }

  /* ── Rate and mark ──
     A mouse that rests on the thumb, or a press, has the four rise over the picture, as they
     do over a card. */

  function rating(p) {
    p.rating = window.MM.ratings.mount(p.card.ref, { variant: 'lift' });
    p.rating.line.classList.add('pv__said');
    p.lift = p.n.appendChild(el('div', 'pv__lift'));
    p.lift.appendChild(p.rating.node);
    p.rate = round('Rate');
    p.rate.classList.add('pv__rate');
    var opened = 0;
    p.rate.addEventListener('click', function () {
      if (now() - opened < OWN_CLICK_MS) return;
      if (lifted(p)) lower(p);
      else lift(p);
    });
    p.rate.addEventListener('mouseenter', function () {
      clearTimeout(p.dwell);
      p.dwell = setTimeout(function () {
        if (lifted(p) || live !== p) return;
        opened = now();
        lift(p);
      }, DWELL_MS);
    });
    p.rate.addEventListener('mouseleave', function () { clearTimeout(p.dwell); });
    [p.rate, p.rating.node].forEach(function (n) {
      n.addEventListener('mouseenter', function () { clearTimeout(p.lowering); });
      n.addEventListener('mouseleave', function () {
        clearTimeout(p.lowering);
        p.lowering = setTimeout(function () { lower(p); }, LOWER_MS);
      });
    });
    faceRate(p);
  }

  function lifted(p) { return p.rating.node.classList.contains('is-open'); }

  function lift(p) {
    var x = 0;
    var y = 0;
    for (var n = p.rate; n && n !== p.n; n = n.offsetParent) {
      x += n.offsetLeft;
      y += n.offsetTop;
    }
    p.lift.style.left = x + (p.rate.offsetWidth - 44) / 2 + 'px';
    p.lift.style.top = y - 180 - 8 + 'px';
    void p.lift.offsetWidth;
    p.rating.node.classList.add('is-open');
  }

  function lower(p) {
    clearTimeout(p.lowering);
    p.rating.node.classList.remove('is-open');
    p.rating.pointAt(null);
  }

  /* The eye: what the card's own does. */
  function eye(p) {
    var card = cardOf(p.link);
    var mark = card && card.getAttribute('data-mark');
    if (!mark) return null;
    var b = round(core.markLabel(mark), 'i-eye');
    b.classList.add('pv__eye');
    b.setAttribute('data-mark-toggle', mark);
    b.setAttribute('data-mark-name', p.card.title + whichOf(p));
    b.setAttribute('data-mark-ref', p.card.ref);
    b.setAttribute('aria-pressed', seen(p) ? 'true' : 'false');
    b.setAttribute('title', core.markAction(seen(p)));
    return b;
  }

  function whichOf(p) {
    var which = /^(S\d+ · E\d+|Season \d+)/.exec(p.card.sub);
    return which ? ', ' + which[1] : '';
  }

  function faceRate(p) {
    p.rate.hidden = window.MM.ratings.canRate(p.card.ref) === false;
    if (p.rate.hidden) lower(p);
    watch.cardTools.faceOf(p.rate, p.card.ref);
  }

  function moreButton() {
    var b = round('More', 'i-ellipsis');
    b.setAttribute('data-preview-more', '');
    b.setAttribute('aria-haspopup', 'menu');
    b.setAttribute('aria-expanded', 'false');
    return b;
  }

  /* The Keep menu opens from here once Keep is chosen, under the More button. */
  function keepTrigger(p) {
    var b = el('button', 'pv__keep');
    b.type = 'button';
    b.setAttribute('tabindex', '-1');
    b.setAttribute('data-watch-keep', keepIdOf(p));
    b.setAttribute('aria-expanded', 'false');
    var season = leavingOf(p).season;
    b.setAttribute('aria-label', 'Keep ' + p.card.title + (season === null ? '' : ', Season ' + season));
    return b;
  }

  function playLink(label, href) {
    var a = el('a', 'btn watch-btn--play');
    a.setAttribute('href', href);
    a.setAttribute('tabindex', '-1');
    a.appendChild(icon('i-play'));
    a.appendChild(document.createTextNode(label));
    return a;
  }

  /* Play or Resume, Download, or how far a download is. */
  function primary(p) {
    var d = p.data;
    if (p.card.plays) return playLink(p.card.pct ? 'Resume' : 'Play', p.card.href);
    if (!d) {
      var held = LIBRARY_RE.test(p.card.ref) || !!posterOf(p.link).querySelector('.watch-badge--in');
      var b = button('pv__waiting skel skel--pill', held ? p.card.pct ? 'Resume' : 'Play' : 'Download', held ? 'i-play' : 'i-download');
      b.setAttribute('aria-disabled', 'true');
      return b;
    }
    if (d.in_library) {
      var href = d.play && safePath(d.play.href);
      return href ? playLink(d.play.label.split(' ')[0], href) : null;
    }
    if (!d.download) return null;
    var state = stateOf(p);
    if (state) return pill(state);
    if (d.sonarr_tracked) return pill('In Sonarr');
    var seasons = wanted(d);
    if (seasons && !seasons.length) return pill('In library');
    if (!d.download.request) return null;
    // Which seasons of a longer show is chosen in its detail.
    var many = seasons && seasons.length > 1;
    var go = button('btn--primary', many ? 'Pick seasons' : 'Download', 'i-download');
    go.addEventListener('click', function () { if (many) openDetail(p); else download(p); });
    return go;
  }

  /* The badge on the card is newer than the reply when this page queued the title. */
  function stateOf(p) {
    var dl = p.data.download;
    if (dl.state === 'downloading') return 'Downloading' + (typeof dl.pct === 'number' ? ' ' + dl.pct + '%' : '');
    // Of a show partly there, whose card says In library too, the rest may be there to download.
    if (dl.state === 'in_library' || (dl.state !== 'partial' && posterOf(p.link).querySelector('.watch-badge--in'))) return 'In library';
    if (dl.state === 'queued' || posterOf(p.link).querySelector('.watch-badge--queued')) return 'Queued';
    return '';
  }

  function pill(text) {
    var b = button('watch-btn--grey pv__state', text, /^In /.test(text) ? 'i-check' : 'i-clock');
    b.setAttribute('aria-disabled', 'true');
    return b;
  }

  /* The seasons of a show that can be downloaded, or null when it has no choice of them. */
  function wanted(d) {
    if (d.kind === 'movie' || !d.seasons || !d.seasons.length || !d.download.request || !d.download.request.body) return null;
    return d.seasons.filter(function (s) { return !s.in_library; });
  }

  function download(p) {
    var d = p.data;
    var req = d.download.request;
    var url = safePath(req.url);
    if (!url || p.sending) return;
    var payload = req.body ? Object.assign({}, req.body) : undefined;
    var seasons = wanted(d);
    if (seasons) {
      var numbers = seasons.map(function (s) { return s.number; });
      payload.monitored_seasons = numbers.concat(d.seasons.filter(function (s) { return s.in_library; })
        .map(function (s) { return s.number; }));
      payload.search_seasons = numbers;
    }
    p.sending = true;
    p.actions.querySelector('.pv__first').setAttribute('aria-disabled', 'true');
    MM.api.post(url, payload).then(function () {
      core.toast(d.title + ' is queued.', { variant: 'success' });
      d.download.state = 'queued';
    }, function (err) {
      unkeep(p.card.ref);
      if (p.card.pick) unkeep(p.card.pick);
      core.toast(MM.api.messageOf(err, 'Couldn’t add ' + d.title + '. Try again.'), { variant: err && err.status === 409 ? 'info' : 'error' });
      if (err && err.status === 409) d.download.state = 'queued';
    }).then(function () {
      p.sending = false;
      if (d.download.state !== 'queued') {
        actions(p);
        return;
      }
      document.dispatchEvent(new CustomEvent('mm:title-changed', {
        detail: { ref: p.card.ref, download_state: 'queued', download_pct: null, title: d.title, kind: d.kind, year: d.year }
      }));
    });
  }

  /* ── To the card and the detail ── */

  function focusCard(p) {
    if (document.body.contains(p.link)) p.link.focus({ preventScroll: true });
  }

  /* A press on the picture or the words is a press on the card. */
  function pressed(p, e) {
    // A press that redrew its button has left a target that is in nothing.
    if (e.target.closest('button, .btn') || !p.n.contains(e.target)) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // the picture is a real link
    e.preventDefault();
    p.link.click();
  }

  function detailOpen() { return document.documentElement.classList.contains('has-ttl'); }

  function openDetail(p) {
    if (!watch.detail) return;
    watch.detail.open(p.card.ref, { origin: p.link, title: p.card.title, kind: p.link.getAttribute('data-kind'), pick: p.card.pick });
    if (detailOpen()) handOver(p);
  }

  /* The detail grows out of the preview, its picture starting on the preview's. */
  function handOver(p) {
    var r = p.shell.getBoundingClientRect();
    close(p, 'hand');
    var root = document.getElementById('ttl');
    var sheet = root && root.querySelector('.ttl__sheet');
    if (!sheet || !sheet.animate || core.reduceMotion() || window.innerWidth < 700) return;
    var ease = core.token('--ease-out', 'ease-out');
    var ms = growMs();
    var s = r.width / sheet.offsetWidth;
    var below = Math.max(0, sheet.offsetHeight - r.height / s);
    sheet.animate([
      {
        transformOrigin: '0 0', opacity: 1,
        transform: 'translate(' + (r.left - sheet.offsetLeft) + 'px,' + (r.top - sheet.offsetTop) + 'px) scale(' + s + ')',
        clipPath: 'inset(0 0 ' + below + 'px round ' + 12 / s + 'px)'
      },
      { transformOrigin: '0 0', opacity: 1, transform: 'none', clipPath: 'inset(-60px -60px -60px round 12px)' }
    ], { duration: ms, easing: ease });
    core.toArray(sheet.querySelectorAll('.ttl__head, .ttl__body')).forEach(function (part) {
      part.animate([{ opacity: 0 }, { opacity: 1 }], { duration: core.tokenMs('--dur', 200), delay: 100, easing: ease, fill: 'backwards' });
    });
    if (p.art) holdPicture(sheet, p.art);
  }

  /* The detail reads its title before it shows a picture: until then it keeps this one. */
  function holdPicture(sheet, art) {
    var own = sheet.querySelector('.ttl__art');
    if (!own) return;
    var held = image('pv__held', art.getAttribute('src'));
    own.before(held);
    var gone = false;
    function go() {
      if (gone) return;
      gone = true;
      own.removeEventListener('load', go);
      var fade = held.animate([{ opacity: 1 }, { opacity: 0 }], { duration: core.tokenMs('--dur-slow', 250), delay: core.tokenMs('--dur-slow', 250), fill: 'forwards' });
      fade.onfinish = function () { held.remove(); };
    }
    own.addEventListener('load', go);
    setTimeout(go, HOLD_MS);
  }

  /* ── More ── */

  function known(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }

  function remember(key, value) {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch (e) {
      // Not kept past this page.
    }
  }

  function trailersOn() { return known(TRAILERS_OFF) !== 'off'; }

  function setTrailers(on) { remember(TRAILERS_OFF, on ? null : 'off'); }

  /* What a Keep here keeps and when it leaves. A card that is leaving says it itself, and the
     reply, its show's, is for every season: one shown on the card is kept, or none. */
  function leavingOf(p) {
    var d = p.data;
    var c = p.card;
    if (!d) return null;
    if (c.keepId) return c.leaving ? { id: c.keepId, season: c.season, text: whenText(c.season, c.leaving) } : null;
    var found = d.kind === 'movie' ? d.leaving : (d.leaving_seasons || []).filter(function (s) {
      return c.season === null || s.season === c.season;
    })[0];
    if (!found) return null;
    var season = d.kind === 'movie' ? null : found.season;
    return { id: d.keep_allowed && found.keep_id ? String(found.keep_id) : '', season: season, text: whenText(season, leavingText(found.days)) };
  }

  function leavingText(days) {
    if (days < 0) return 'Leaving any day';
    if (!days) return 'Leaving today';
    return days === 1 ? 'Leaving in 1 day' : 'Leaving in ' + days + ' days';
  }

  function whenText(season, text) { return season === null ? text : text.replace('Leaving', 'Season ' + season + ' leaves'); }

  function keepIdOf(p) {
    var leaving = leavingOf(p);
    return leaving ? leaving.id : '';
  }

  var menuNode = null;
  var menu = null;

  function item(act, iconName, label) {
    var b = el('button', 'watch-keep-menu__item');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.setAttribute('tabindex', '-1');
    b.setAttribute('data-act', act);
    b.appendChild(icon(iconName));
    b.appendChild(document.createTextNode(label));
    menuNode.appendChild(b);
    return b;
  }

  function seen(p) { return !!posterOf(p.link).querySelector('.watch-badge--watched'); }

  function fillMenu() {
    menuNode.textContent = '';
    if (!live) return;
    var card = cardOf(live.link);
    menuNode.setAttribute('aria-label', 'More for ' + live.card.title);
    if (watch.marks && watch.marks.likeRef(live.card.ref, live.link)) item('like', 'i-sparkles', 'Find more like this');
    if (card && card.hasAttribute('data-dismiss-ref')) item('dismiss', 'i-xmark', 'Dismiss');
    if (keepIdOf(live)) item('keep', 'i-clock', 'Keep' + (leavingOf(live).season === null ? '' : ' Season ' + leavingOf(live).season));
    item('details', 'i-circle-info', 'Details');
    menuNode.appendChild(el('hr', 'pv-menu__line'));
    var trailers = item('trailers', 'i-check', 'Play trailers on hover');
    trailers.setAttribute('role', 'menuitemcheckbox');
    trailers.setAttribute('aria-checked', trailersOn() ? 'true' : 'false');
  }

  /* Press what watch/marks.js listens for. */
  function press(attrs) {
    var b = el('button');
    b.type = 'button';
    b.hidden = true;
    Object.keys(attrs).forEach(function (k) { b.setAttribute(k, attrs[k]); });
    document.body.appendChild(b);
    b.click();
    b.remove();
  }

  function choose(trigger, chosen) {
    var p = live;
    if (!p) return;
    var card = cardOf(p.link);
    var act = chosen.getAttribute('data-act');
    if (act === 'trailers') {
      var on = !trailersOn();
      setTrailers(on);
      if (on) planTrailer(p);
      else stopTrailer(p);
    } else if (act === 'dismiss') {
      close(p);
      press({ 'data-watch-dismiss': card.getAttribute('data-dismiss-ref'), 'data-mark-name': p.card.title });
    } else if (act === 'like') {
      close(p);
      watch.search.like(watch.marks.likeRef(p.card.ref, p.link), p.card.title, p.card.poster);
    } else if (act === 'details') {
      openDetail(p);
    } else if (act === 'keep') {
      p.keep.click();
    }
  }

  /* A menu opened from the preview is open: it stays, wherever the mouse is. */
  function pinned(p) { return !!p.n.querySelector('[aria-expanded="true"]'); }

  function holdsFocus(p) {
    var active = document.activeElement;
    return !!active && (p.n.contains(active) || (pinned(p) && !!active.closest('.watch-keep-menu')));
  }

  function closeMenus(p) {
    if (menu && menu.trigger() === p.more) menu.close(false);
    if (p.keep && p.keep.getAttribute('aria-expanded') === 'true') watch.keep.closeMenu(false);
  }

  function makeMenu() {
    menuNode = el('div', 'watch-keep-menu watch-more-menu pv-menu');
    menuNode.setAttribute('role', 'menu');
    menuNode.hidden = true;
    (document.getElementById('watch-page') || document.body).appendChild(menuNode);
    menu = watch.keep.menu(menuNode, {
      trigger: '[data-preview-more]',
      fill: fillMenu,
      choose: choose,
      spare: function () { return document.getElementById('watch-search'); }
    });
  }

  /* ── Trailer ──
     YouTube's own player, muted, only once the preview has been looked at. Nothing
     lies over it: the player fades in over the art once it says it plays. */

  function trailerKey(d) {
    return d.trailer_key && YOUTUBE_KEY_RE.test(d.trailer_key) ? d.trailer_key : null;
  }

  function planTrailer(p) {
    clearTimeout(p.trailerTimer);
    if (!p.data || !trailerKey(p.data) || !trailersOn() || badTrailers.has(p.card.ref) || core.reduceMotion() || saving()) return;
    p.trailerTimer = setTimeout(function () { if (live === p && !carried) startTrailer(p); }, TRAILER_AFTER_MS);
  }

  function saving() { return !!(window.navigator.connection && window.navigator.connection.saveData); }

  function say(frame, message) {
    if (frame.contentWindow) frame.contentWindow.postMessage(JSON.stringify(message), TRAILER_ORIGIN);
  }

  function tell(p, func) {
    if (p.frame) say(p.frame, { event: 'command', func: func, args: [] });
  }

  function startTrailer(p) {
    if (p.frame) return;
    clearTimeout(p.trailerTimer);
    var frame = p.frame = el('iframe', 'pv__frame');
    frame.setAttribute('src', TRAILER_ORIGIN + '/embed/' + trailerKey(p.data) +
      '?autoplay=1&mute=1&playsinline=1&controls=0&rel=0&enablejsapi=1&origin=' + encodeURIComponent(window.location.origin));
    frame.setAttribute('title', 'Trailer: ' + p.card.title);
    frame.setAttribute('tabindex', '-1');
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox');
    frame.setAttribute('allow', 'autoplay; encrypted-media');
    frame.setAttribute('referrerpolicy', 'strict-origin');
    // YouTube's player reports its state only to a page that has asked, and only once it listens.
    frame.addEventListener('load', function () {
      p.listen = setInterval(function () { say(frame, { event: 'listening' }); }, LISTEN_MS);
      say(frame, { event: 'listening' });
    });
    p.state = null;
    waitForTrailer(p);
    p.muted = true;
    p.media.appendChild(frame);
  }

  function waitForTrailer(p) {
    clearTimeout(p.stuck);
    p.stuck = setTimeout(function () { badTrailer(p); }, TRAILER_STUCK_MS);
  }

  function stopTrailer(p) {
    clearTimeout(p.trailerTimer);
    clearTimeout(p.stuck);
    clearInterval(p.listen);
    if (!p.frame) return;
    p.frame.remove();
    p.frame = null;
    p.n.classList.remove('is-playing');
    faceTrailer(p);
  }

  /* It cannot play here, so it is not tried again by itself. */
  function badTrailer(p) {
    hold(badTrailers, p.card.ref, true);
    stopTrailer(p);
  }

  function pressTrailer(p) {
    if (!p.frame) {
      startTrailer(p);
      return;
    }
    if (!p.n.classList.contains('is-playing')) return;
    p.muted = !p.muted;
    tell(p, p.muted ? 'mute' : 'unMute');
    faceTrailer(p);
  }

  function faceTrailer(p) {
    if (!p.trailerButton) return;
    var playing = p.n.classList.contains('is-playing');
    window.MM.dom.setIcon(p.trailerButton.querySelector('svg'), !playing ? 'i-film' : p.muted ? 'i-volume-xmark' : 'i-volume-high');
    name(p.trailerButton, !playing ? 'Play trailer' : p.muted ? 'Turn sound on' : 'Turn sound off');
  }

  window.addEventListener('message', function (e) {
    var p = live || held;
    if (!p || !p.frame || e.source !== p.frame.contentWindow || e.origin !== TRAILER_ORIGIN) return;
    var m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (!m) return;
    if (m.event === 'onError' || m.event === 'onAutoplayBlocked') {
      badTrailer(p);
      melt();
      return;
    }
    clearInterval(p.listen);
    var state = m.event === 'onStateChange' ? m.info
      : m.info && typeof m.info === 'object' ? m.info.playerState : null;
    if (state === 1) {
      clearTimeout(p.stuck);
      if (!p.n.classList.contains('is-playing')) {
        p.n.classList.add('is-playing');
        faceTrailer(p);
      }
    } else if (state === 0) {
      stopTrailer(p);
      melt();
    } else if (typeof state === 'number' && state !== p.state && !p.n.classList.contains('is-playing')) {
      p.state = state;
      waitForTrailer(p);
    }
  });

  /* ── The mouse ── */

  function onMove(e) {
    if (e.pointerType !== 'mouse' || !mouse()) return;
    // A browser says again where a mouse at rest is when what is under it changes.
    var moved = !!pos && (e.clientX !== pos.x || e.clientY !== pos.y);
    pos = { x: e.clientX, y: e.clientY };
    if (detailOpen()) return;
    trail.push({ x: pos.x, y: pos.y, t: now() });
    if (trail.length > 40) trail.shift();
    var target = e.target && e.target.closest ? e.target : null;
    var link = target && target.closest('.watch-card__link');
    if (live && ((target && live.n.contains(target)) || link === live.link)) {
      clearTimeout(closeTimer);
      closeTimer = null;
      disarm();
      return;
    }
    if (live && !pinned(live) && !closeTimer) {
      closeTimer = setTimeout(function () {
        closeTimer = null;
        if (live && !pinned(live)) close(live);
      }, GRACE_MS);
    }
    // From the picture alone, and not once a level was given or while the levels are over it.
    var of = link && target.closest('.watch-card__poster') ? cardOf(link) : null;
    if (!of || !refOf(link) || of.classList.contains('is-lifted') || of.classList.contains('is-quiet')) {
      disarm();
      return;
    }
    if (!moved) return; // a card that came under a mouse at rest
    if (closing && closing.link === link) reopen(closing);
    else if (armed !== link && !e.buttons && now() - scrolledAt > SETTLE_MS && !covered()) arm(link);
  }

  function leave() {
    if (carried) {
      pos = null;
      return;
    }
    disarm();
    if (live && !pinned(live)) close(live);
  }

  /* ── The wheel ──
     Wheel events less than GAP_MS apart are one scroll. Its first says whose it is, and it stays
     theirs to its last, momentum and all: a browser hands over only a whole one.
     'card': it opens the preview. 'row': it moves the preview's row, as it would over the card.
     'page': the browser's own. 'over': nobody's any more. */

  /* The first frames of a page can hold a momentum up. */
  function gapOf(s) { return s.opened ? GAP_MS * 2 : GAP_MS; }

  function px(e, delta) { return delta * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? window.innerHeight : 1); }

  function ownerOf(e, dx, dy) {
    if (carried) return 'card';
    var p = live;
    if (!p || detailOpen() || !p.n.contains(e.target)) return 'page';
    if (Math.abs(dx) > Math.abs(dy)) return 'row';
    if (dy <= 0 || e.shiftKey || e.ctrlKey || e.metaKey || pinned(p) || (p.rating && lifted(p))) return 'page';
    return p.settledAt && now() - p.settledAt >= READY_MS ? 'card' : 'page';
  }

  /* Every wheel event of the page, before anything answers it. */
  function heard(e) {
    var dx = px(e, e.deltaX);
    var dy = px(e, e.deltaY);
    var t = e.timeStamp; // a busy page hears it late
    if (!stream || t - stream.at > gapOf(stream)) {
      glideTo = null;
      stream = {
        owner: ownerOf(e, dx, dy), p: live, at: t, sum: 0, way: 0, side: 0, row: null,
        notches: e.deltaMode === 1 || Math.abs(dy) >= 60, opened: 0, reading: false, hist: []
      };
    } else if (t - stream.at > 70 && Math.abs(dy) >= 50) stream.notches = true;
    stream.at = t;
    stream.sum += dy;
    stream.way += Math.abs(dy);
    stream.side += Math.abs(dx);
    if (stream.row) freeRow(stream.row);
  }

  /* Over a preview, and over the page one becomes. */
  function wheeled(e) {
    var s = stream;
    if (s.owner === 'page') return;
    if (s.owner === 'row') {
      slideRow(s, px(e, e.deltaX), e);
      return;
    }
    if (e.cancelable) e.preventDefault();
    if (s.owner === 'over') return;
    if (s.opened) readOn(s, e.timeStamp, px(e, e.deltaY));
    else scrollOpen(s, px(e, e.deltaY));
  }

  function scrollOpen(s, dy) {
    if (!carried) {
      if (!live || dy <= 0) return; // all the way back: the rest of this scroll moves nothing
      if (s.way < 14 && s.side > s.way) {
        s.owner = 'row';
        return;
      }
      if (plain()) {
        if (s.sum < watch.carry.OPENS) return;
        s.owner = 'over';
        remember(SCROLLED, 'yes');
        openDetail(live);
        return;
      }
      if (!pickUp(live)) return;
    }
    carried.motion.push(dy, s.notches ? NOTCH_REST_MS : REST_MS);
  }

  /* It opens without following the hand: what it would follow with is asked not to move, or is not drawn. */
  function plain() {
    return core.reduceMotion() || window.matchMedia('(forced-colors: active)').matches || window.innerWidth < 700 || window.innerHeight <= 500;
  }

  /* What is left of the push that opened the page stops at its top. A fresh one, or a finger that
     does not slow down, reads on: a momentum loses a third of its speed every 130 ms. Neither is
     told by one measure alone, as a busy page hears some events late and some as one. */
  function readOn(s, t, dy) {
    var before = s.hist.slice(-6);
    s.hist.push([t, dy]);
    if (!s.reading && dy > 0) s.reading = s.notches ? t - s.opened > 120 : before.length > 2 && reads(s.hist, before, t, dy);
    if (!s.reading) return;
    if (s.notches) glide(dy);
    else page().scrollTop += dy;
  }

  function reads(hist, before, t, dy) {
    function sum(from, to) {
      return hist.reduce(function (n, h) { return t - h[0] >= from && t - h[0] < to ? n + h[1] : n; }, 0);
    }
    var all = before.reduce(function (n, h) { return n + h[1]; }, 0);
    var last = before[before.length - 1];
    var long = t - hist[0][0];
    var most = Math.max.apply(null, before.slice(-3).map(function (h) { return h[1]; }));
    var faster = dy * (last[0] - before[0][0]) > 1.2 * (all - before[0][1]) * (t - last[0]);
    if (long > 160 && dy >= most && faster && sum(0, 80) > sum(80, 160) * 1.4 + 20) return true;
    return long > 320 && dy * before.length > 0.85 * all && sum(160, 320) > 24 && sum(0, 160) > sum(160, 320) * 0.9;
  }

  var glideTo = null;

  /* A notch at a time would jump. */
  function glide(dy) {
    var root = page();
    var going = glideTo !== null;
    glideTo = Math.max(0, Math.min(root.scrollHeight - root.clientHeight, (going ? glideTo : root.scrollTop) + dy));
    if (going) return;
    var last = now();
    function step(t) {
      if (glideTo === null) return;
      var left = glideTo - root.scrollTop;
      var by = left * (1 - Math.exp(-20 * Math.max(0.001, t - last) / 1000));
      last = t;
      if (Math.abs(left) < 1) {
        root.scrollTop = glideTo;
        glideTo = null;
        return;
      }
      root.scrollTop += Math.abs(by) < 1 ? (by < 0 ? -1 : 1) : by;
      window.requestAnimationFrame(step);
    }
    window.requestAnimationFrame(step);
  }

  /* Its snapping would pull every small step back. The row moves, so the preview over it fades. */
  function slideRow(s, dx, e) {
    var row = s.p && s.p.link.closest('.watch-row__scroll');
    if (!row) return;
    e.preventDefault();
    if (s.row !== row) {
      s.row = row;
      s.left = row.scrollLeft;
    }
    freeRow(row);
    s.left = Math.max(0, Math.min(row.scrollWidth - row.clientWidth, s.left + dx));
    row.scrollLeft = s.left;
  }

  var snapTimer = null;

  function freeRow(row) {
    clearTimeout(snapTimer);
    row.style.scrollSnapType = 'none';
    snapTimer = setTimeout(function () {
      if (stream && stream.row === row) stream.row = null;
      // Where it snaps to, and there at its own pace.
      var left = row.scrollLeft;
      row.style.scrollSnapType = '';
      var to = row.scrollLeft;
      if (to === left || core.reduceMotion()) return;
      row.style.scrollSnapType = 'none';
      row.scrollTo({ left: left, behavior: 'instant' });
      row.scrollTo({ left: to, behavior: 'smooth' });
      snapTimer = setTimeout(function () { row.style.scrollSnapType = ''; }, core.tokenMs('--dur-slow', 250) * 2);
    }, GAP_MS);
  }

  /* ── Scrolled open ── */

  function page() { return document.getElementById('ttl'); }

  function askFilm(p) {
    if (p.data && p.data.kind === 'movie' && !saving()) watch.detail.ahead(p.card.ref);
  }

  /* The page is drawn under it, not yet open, and it goes with the hand. */
  function pickUp(p) {
    var tmdb = cardOf(p.link).getAttribute('data-tmdb-ref');
    var drawn = watch.detail.draw(p.card.ref, {
      origin: p.link, title: p.card.title, kind: p.link.getAttribute('data-kind'), pick: p.card.pick,
      preview: p.data, poster: p.card.poster, noWide: !!p.media.querySelector('.pv__standin'), watched: seen(p), tmdbRef: tmdb || p.card.ref
    });
    if (!drawn) return false;
    clearTimeout(closeTimer);
    closeTimer = null;
    clearTimeout(p.trailerTimer);
    disarm();
    var stage = watch.carry.stage(p);
    carried = { p: p, stage: stage, motion: watch.carry.motion(stage.draw, function (at) { if (at) arrive(); else giveBack(); }) };
    page().addEventListener('wheel', wheeled, { passive: false });
    return true;
  }

  /* It stands at the page, which opens as by a press. */
  function arrive() {
    var p = carried.p;
    remember(SCROLLED, 'yes');
    carried.stage.end(1);
    setLive(null);
    p.link.classList.remove('is-previewed');
    p.n.classList.remove('is-carried');
    keepOver(p, carried.stage.heroFoot);
    carried = null;
    if (stream) {
      stream.opened = now();
      stream.hist = [];
    }
    watch.detail.openDrawn();
    untilStill();
  }

  /* The page takes its wheel over from the scroll that opened it. */
  function untilStill() {
    setTimeout(function () {
      if (stream && now() - stream.at <= gapOf(stream)) untilStill();
      else if (!carried) page().removeEventListener('wheel', wheeled);
    }, GAP_MS);
  }

  /* Back at the card. */
  function giveBack() {
    var p = carried.p;
    putDown();
    if (live !== p) return;
    var r = p.shell.getBoundingClientRect();
    if (pos && pos.x >= r.left && pos.x <= r.right && pos.y >= r.top && pos.y <= r.bottom) planTrailer(p);
    else close(p);
  }

  /* The preview as it was, and no page. */
  function putDown() {
    var p = carried.p;
    carried.motion.stop();
    carried.stage.end(0);
    carried = null;
    page().removeEventListener('wheel', wheeled);
    if (stream && now() - stream.at > GAP_MS) stream = null;
    p.n.classList.remove('is-carried');
    p.media.style.transform = p.media.style.clipPath = '';
    watch.detail.undraw();
  }

  /* What it shows lies on under the page's words: a trailer that plays until the person goes on,
     a picture until the page's own has come. */
  function keepOver(p, foot) {
    held = p;
    p.n.classList.add('is-held');
    page().style.setProperty('--ttl-hero', foot - 1 + 'px');
    page().classList.add('is-film');
    p.heldTimer = setTimeout(function () { if (!playing(p)) melt(); }, HOLD_MS);
    p.whenShown = function () { if (held === p && !playing(p)) melt(); };
    if (page().querySelector('.ttl__art.is-loaded, .ttl__fill.is-loaded')) p.whenShown();
    else page().querySelector('.ttl__hero').addEventListener('load', p.whenShown, true);
  }

  function playing(p) { return p.n.classList.contains('is-playing'); }

  /* The page's own picture comes over it. */
  function melt() {
    var p = held;
    if (!p || p.melting) return;
    p.melting = true;
    tell(p, 'mute');
    page().classList.add('is-melting');
    later(p, core.tokenMs('--dur-slow', 250), function () { if (held === p) letGo(); });
  }

  function letGo() {
    if (carried) putDown();
    var p = held;
    if (!p) return;
    held = null;
    glideTo = null;
    clearTimeout(p.heldTimer);
    page().querySelector('.ttl__hero').removeEventListener('load', p.whenShown, true);
    page().classList.remove('is-film', 'is-melting');
    page().style.removeProperty('--ttl-hero');
    remove(p);
  }

  var layerNode = document.body.appendChild(el('div', 'pv-layer'));
  makeMenu();

  window.addEventListener('wheel', heard, { passive: true, capture: true });
  // Not passive only over a preview, and over the page while one becomes it.
  layerNode.addEventListener('wheel', wheeled, { passive: false });
  if (page()) {
    page().addEventListener('scroll', function () {
      if (!held) return;
      held.n.style.transform = 'translateY(' + -page().scrollTop + 'px) var(--pv-to)';
      if (page().scrollTop > 8) melt();
    }, { passive: true });
    page().addEventListener('pointerdown', melt);
  }

  /* A press anywhere takes it the rest of the way. */
  document.addEventListener('pointerdown', function (e) {
    swallow = !!carried && e.button === 0;
    if (swallow) carried.motion.go(1);
  }, true);
  document.addEventListener('click', function (e) {
    if (!swallow || !e.detail) return;
    swallow = false;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  document.addEventListener('pointermove', onMove);
  document.documentElement.addEventListener('pointerleave', leave);
  document.addEventListener('pointerdown', function (e) {
    if (!(live && live.n.contains(e.target))) disarm();
  });
  document.addEventListener('dragstart', function () {
    disarm();
    if (live) close(live);
  });
  document.addEventListener('scroll', function (e) {
    if (carried || (e.target.closest && e.target.closest('.watch-keep-menu, .play-on'))) return;
    scrolledAt = now();
    fadeLive();
  }, { capture: true, passive: true });
  window.addEventListener('resize', function () {
    fadeLive();
    letGo();
  });
  window.addEventListener('popstate', function () {
    viewChanged();
    core.toArray(document.querySelectorAll('.pv__held')).forEach(function (n) { n.remove(); });
  });
  document.addEventListener('input', function (e) {
    if (!e.target.closest('.play-on')) fadeLive();
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) return;
    cut();
  });
  window.addEventListener('pagehide', cut);

  function cut() {
    disarm();
    if (live) close(live, 'cut');
    letGo();
  }

  /* Esc is the preview's before the search box's, the grid's or the panel's under it, but not
     before that of a menu or a dialog over it. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.isComposing || !live) return;
    if (carried) {
      e.preventDefault();
      e.stopPropagation();
      if (stream) stream.owner = 'over';
      carried.motion.go(0);
      return;
    }
    if (covered()) return;
    e.preventDefault();
    e.stopPropagation();
    disarm();
    close(live);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.isComposing || detailOpen()) return;
    if (e.key !== 'Escape' && e.key !== 'Tab') return;
    disarm();
    if (live) close(live);
  });

  /* The menu lines up with the preview's own edge, and starts at its first item
     (a menu starts at its ticked one). */
  document.addEventListener('click', function (e) {
    var trigger = menu.trigger();
    if (!trigger || !trigger.contains(e.target)) return;
    menuNode.style.left = trigger.getBoundingClientRect().right - menuNode.offsetWidth + 'px';
    menuNode.querySelector('.watch-keep-menu__item').focus({ preventScroll: true });
  });

  /* A card pressed, with its preview open or not: the detail opened with it. */
  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('.watch-card__link');
    if (!link) return;
    disarm();
    if (!live || !detailOpen()) return;
    if (live.link === link) handOver(live);
    else close(live);
  });

  /* A day count chosen in the Keep menu takes the card away: not under its preview. */
  document.addEventListener('click', function (e) {
    var days = e.target.closest && e.target.closest('#watch-keep-menu [data-days]');
    if (!days || !live || !live.keep || document.activeElement !== live.keep) return;
    live.keep.blur(); // keep.js moves a focus that is nowhere on from the card that goes
    close(live);
  });

  document.addEventListener('mm:title-changed', function (e) {
    var d = e.detail || {};
    unkeep(d.ref);
    if (flight) flight.stale = true;
    if (!live || live.card.ref !== d.ref || !live.data || !live.data.download) return;
    live.data.download.state = d.download_state;
    live.data.download.pct = d.download_pct;
    actions(live);
    core.fadeIn(live.actions.querySelector('.pv__first') || live.actions);
  });

  document.addEventListener('mm:rating', function (e) {
    if (!live || !live.rating || e.detail.refs.indexOf(live.card.ref) < 0) return;
    var p = live;
    if (e.detail.phase === 'pending' && e.detail.from === p.rating.node) {
      clearTimeout(p.lowering);
      p.lowering = setTimeout(function () { lower(p); }, watch.cardTools.SHOW_MS);
    }
    faceRate(p);
  });

  document.addEventListener('mm:watch-kept', function (e) { keptOrUndone(e.detail.id); });

  document.addEventListener('mm:watch-admin', function (e) {
    if (e.detail.phase === 'done') keptOrUndone(e.detail.id);
  });

  /* A scan, or deletions cleared: no title is named. */
  document.addEventListener('mm:watch-scheduled', function () { keptOrUndone(''); });

  document.addEventListener('mm:watch-marked', function (e) { marked(e.detail.ref); });

  watch.preview = { close: viewChanged, letGo: letGo };
})();
