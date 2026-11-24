/* Watch: the one owner of the page's address and of Back, on MMWatch.address.
 * The address says what is open. Each thing that opens because of it is a
 * layer, declared once:
 *
 *   MMWatch.address.layer({
 *     name, depth,     // a layer stands on those of lower depth
 *     params, hash,    // its keys of the query; true when the hash is its own
 *     read(here),      // its value (a string) from here.params, here.hash and
 *                      // here.entry, the layer the entry was pushed for; null: not open
 *     open(value, how), close(how)   // how: 'start', 'pop', 'push' or 'replace'
 *   });
 *
 * A link to the player takes the whole address along, so it can lead back here.
 *
 * push and replace move the address for a layer and do not call it back; every
 * other layer is brought into line. open and close may return true: the layer
 * places the page's scroll itself. watch.js calls start() last.
 */
(function () {
  'use strict';

  var KEY = 'mm:watch:places';
  var KEPT = 50; // entries whose scroll outlives the page
  var LATE_MS = 1000; // a step through the history this late is not coming
  var GROW_MS = 3000; // how long a page, or a row, may go without growing on its way back to its scroll
  var ROWS_KEY = 'mm:watch:rows';
  var TITLES_KEY = 'mm:watch:titles';
  var HEADINGS = { grid: '.watch-all__title', explore: '.explore-title', search: '.results-group__title' }; // of the layers that are views
  var HOLD_MS = 1000; // how long a page, or a row, stays where it was put before it is left there
  var MOST_MS = 5000; // how long the page is held at most, from when it last grew on its way
  var STILL_FRAMES = 3; // frames of standing still that are rest; the first can be the one it moved in
  var RUN_FRAMES = 3; // frames of moving the same way that are the person's
  var KEY_MS = 500; // focus this soon after a key was moved by it
  var TAKES = ['wheel', 'touchstart', 'pointerdown', 'mousedown', 'click', 'keydown']; // the person takes over

  var layers = []; // lowest first
  var values = {}; // what each open layer is open at, by name
  var started = false;
  var leaving = null; // the step asked of the browser and not yet made: { close, timer, then, land }
  var places = {}; // each entry's scroll, by id
  var focused = {}; // the node each entry had the focus on, by id, while it is on the page
  var openedOn = {}; // the title opened from each entry, as its card had it, by id
  var ats = {}; // how far along each entry seen stands, by id
  var placing = null; // the scroll being put back
  var arriving = null; // the entry a step through the history leads to, while its layers open and close
  var rowsById = {}; // how far each entry's rows were scrolled when its page was left, by id
  var rowsNow = {}; // how far each row is scrolled, by its id

  /* ── Entries ── */

  /* This entry's own mark: its id, how many entries the page has pushed before
     it, the layer it was pushed for and, for each layer opened by a push, the
     number of the entry that did. */
  function mark() {
    var state = window.history.state;
    var m = state && state.mm;
    return m && typeof m.at === 'number' && Array.isArray(m.stack) ? m : null;
  }

  function newId() { return Math.random().toString(36).slice(2, 10); }

  function write(how, m, url) {
    window.history[how](Object.assign({}, how === 'replaceState' ? window.history.state : null, { mm: m }), '', url);
  }

  /* An entry the page did not push: a fresh load, or a link followed in it. */
  function stamp() {
    if (!mark()) write('replaceState', { id: newId(), at: current ? ats[current] + 1 : 0, entry: null, stack: [] }, currentUrl());
    ats[mark().id] = mark().at;
  }

  function currentUrl() { return window.location.pathname + window.location.search + window.location.hash; }

  function url(params, hash) {
    var query = new URLSearchParams(window.location.search);
    Object.keys(params || {}).forEach(function (key) {
      if (params[key] === null || params[key] === undefined) query.delete(key);
      else query.set(key, params[key]);
    });
    var text = query.toString();
    var tail = hash === undefined ? window.location.hash : hash ? '#' + hash : '';
    return window.location.pathname + (text ? '?' + text : '') + tail;
  }

  /* ── Layers ── */

  function named(name) {
    return layers.filter(function (l) { return l.name === name; })[0] || null;
  }

  function value(name) { return values[name] === undefined ? null : values[name]; }

  function readNow(l) {
    var got = l.read({
      params: new URLSearchParams(window.location.search),
      hash: (window.location.hash || '').replace(/^#/, ''),
      entry: (mark() || {}).entry || null
    });
    return got === null || got === undefined ? null : String(got);
  }

  /* One layer that fails must not keep the others from following the address. */
  function call(l, what, args) {
    try {
      return l[what].apply(l, args) === true;
    } catch (err) {
      setTimeout(function () { throw err; });
      return false;
    }
  }

  /* Bring every layer but *except* into line with the address: the ones that
     are gone close from the top down, then the rest open from the bottom up.
     Says whether one of them places the scroll. */
  function route(how, except) {
    var placed = false;
    layers.slice().reverse().forEach(function (l) {
      if (value(l.name) === null || readNow(l) !== null) return;
      values[l.name] = null;
      if (l.name !== except && call(l, 'close', [how])) placed = true;
    });
    layers.slice().forEach(function (l) {
      var now = readNow(l);
      if (now === null || now === value(l.name)) return;
      values[l.name] = now;
      if (l.name !== except && call(l, 'open', [now, how])) placed = true;
    });
    return placed;
  }

  function layer(def) {
    layers.push(def);
    layers.sort(function (a, b) { return a.depth - b.depth; });
    if (started) route('start', null);
  }

  function top() {
    var open = layers.filter(function (l) { return value(l.name) !== null; });
    return open.length ? open[open.length - 1].name : null;
  }

  /* ── Moving on ── */

  /* The layers of *stack* the address still opens. */
  function standing(stack) {
    return stack.filter(function (s) {
      var l = named(s.name);
      return l && readNow(l) !== null;
    });
  }

  function push(name, to) {
    var was = mark();
    if (was && was.entry === name && name !== null && to === currentUrl()) return;
    // A title's grows out of the card's preview, which closes itself then.
    var preview = window.MMWatch.preview;
    if (name !== 'title' && preview && preview.close) preview.close();
    keepPlace();
    var ref = /#title=([a-z0-9-]+)$/.exec(to);
    if (was && ref) openedOn[was.id] = ref[1];
    var at = (was ? was.at : 0) + 1;
    var stack = was ? was.stack : [];
    var open = stack.some(function (s) { return s.name === name; });
    write('pushState', {
      id: newId(), at: at, entry: name,
      stack: name === null || open ? stack : stack.concat({ name: name, at: at })
    }, to);
    restack();
    stopPlacing();
    Object.keys(ats).forEach(function (id) {
      // Back and Forward no longer lead to it.
      if (ats[id] >= at) { delete ats[id]; delete places[id]; delete focused[id]; delete openedOn[id]; }
    });
    current = mark().id;
    ats[current] = at;
    places[current] = window.scrollY;
    if (started) route('push', name);
    letGo();
  }

  /* Drop from the entry's stack the layers its address no longer opens. Only
     when there are any: Safari refuses an address changed too often. */
  function restack() {
    var m = mark();
    var left = standing(m.stack);
    if (left.length !== m.stack.length) write('replaceState', Object.assign({}, m, { stack: left }), currentUrl());
  }

  function moveTo(to) {
    write('replaceState', mark(), to);
    restack();
  }

  function replace(name, to) {
    stamp();
    moveTo(to);
    if (started) route('replace', name);
  }

  /* ── Back ── */

  /* Step *by* entries. *close*: a layer that must be shut at the end of it. A
     step the browser never makes (its history was lost) is done without it.
     *land* is called once the page stands where the step leads. */
  function step(by, close, land) {
    leaving = { close: close, then: null, land: land, timer: setTimeout(function () {
      if (!leaving) return;
      leaving = null;
      if (close) shut(close);
    }, LATE_MS) };
    window.history.go(by);
  }

  /* The address without the layer's own part. */
  function without(l) {
    var params = {};
    (l.params || []).forEach(function (key) { params[key] = null; });
    return url(params, l.hash ? '' : undefined);
  }

  function shut(name) {
    if (value(name) === null) return;
    replace(null, without(named(name)));
    refocus();
  }

  function close(name) {
    var l = named(name);
    if (!l) return;
    if (leaving) { leaving.then = function () { close(name); }; return; }
    if (value(name) === null) return;
    var m = mark();
    var stack = m ? m.stack : [];
    var own = stack.filter(function (s) { return s.name === name; })[0];
    var from = own ? own.at : stack.reduce(function (least, s) {
      return named(s.name).depth > l.depth ? Math.min(least, s.at) : least;
    }, Infinity);
    if (from === Infinity) shut(name);
    else step(from - 1 - m.at, name);
  }

  function back(name, land) {
    if (leaving) { leaving.then = function () { back(name, land); }; return; }
    if (value(name) === null) return;
    var m = mark();
    if (m && m.entry === name) step(-1, null, land);
    else close(name);
  }

  var current = null; // the id of the entry the page stands at

  function onPop() {
    var asked = leaving;
    leaving = null;
    if (asked) clearTimeout(asked.timer);
    if (current) focused[current] = document.activeElement;
    stamp();
    current = mark().id;
    // What scrolls from here on is not the person.
    var saved = places[current];
    startPlacing();
    var shutting = asked && asked.close ? named(asked.close) : null;
    if (shutting && readNow(shutting) !== null) moveTo(without(shutting));
    arriving = { y: saved, rows: rowsNow };
    var placed = route('pop', null);
    arriving = null;
    if (placed || saved === undefined) stopPlacing();
    else putBackTo(saved);
    refocus();
    if (asked && asked.land) asked.land();
    letGo();
    if (asked && asked.then) asked.then();
  }

  /* ── Scroll and focus ── */

  function keepPlace() {
    var m = mark();
    if (!m) return;
    places[m.id] = window.scrollY;
    focused[m.id] = document.activeElement;
  }

  /* Until it stands where it should, what the page scrolls by is not kept. */
  function startPlacing() {
    stopPlacing();
    placing = { stop: [] };
  }

  function stopPlacing() {
    if (!placing) return;
    placing.stop.forEach(function (fn) { fn(); });
    placing = null;
    current = (mark() || {}).id || null;
  }

  /* The rows that load after the page are still on their way. */
  function filling() { return !!document.querySelector('#watch-discover-slot[aria-busy="true"]'); }

  /* Watches what stands at read(): once it has moved and come to rest, rest() is called; moving
     the same way frame after frame, it is the person, whatever they scroll with, and theirs(). */
  function follower(read, rest, theirs) {
    var at = read();
    var run = 0; // frames it moved the same way; back is negative
    var still = 0; // frames it stood still
    var busy = false;
    function frame() {
      var now = read();
      var by = now - at;
      at = now;
      still = by ? 0 : still + 1;
      if (still === STILL_FRAMES) {
        run = 0;
        busy = false;
        rest();
        return;
      }
      if (by) run = by > 0 ? Math.max(0, run) + 1 : Math.min(0, run) - 1;
      if (Math.abs(run) >= RUN_FRAMES) {
        busy = false;
        theirs();
        return;
      }
      window.requestAnimationFrame(frame);
    }
    return {
      moved: function () {
        if (busy) return;
        busy = true;
        still = 0;
        frame();
      },
      put: function () { at = read(); },
      busy: function () { return busy; }
    };
  }

  /* Put the page back to where *target* says, and hold it there until what fills it has settled. */
  function putBack(target) {
    var mine = placing;
    var timer = null;
    var end = null;
    var furthest = 0; // where the page was too short to go on
    function there() { return Math.abs(window.scrollY - target()) < 2; }
    function go() {
      if (placing !== mine) return;
      wait();
      if (there()) return;
      window.scrollTo({ left: 0, top: target(), behavior: 'instant' });
      steps.put();
      if (!there() && window.scrollY > furthest) {
        furthest = window.scrollY;
        bound();
      }
    }
    // Safari moves a page that is put back while what fills it takes the place of its skeletons.
    function wait() {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (filling()) wait();
        else stopPlacing();
      }, there() ? HOLD_MS : GROW_MS);
    }
    function bound() {
      clearTimeout(end);
      end = setTimeout(stopPlacing, MOST_MS);
    }
    function theirs() {
      if (placing !== mine) return;
      stopPlacing();
      onScroll();
    }
    function grown() { if (!steps.busy()) go(); }
    var steps = follower(function () { return window.scrollY; }, go, theirs);
    TAKES.forEach(function (type) {
      window.addEventListener(type, theirs, { passive: true, capture: true });
      mine.stop.push(function () { window.removeEventListener(type, theirs, { capture: true }); });
    });
    bound();
    mine.stop.push(function () { clearTimeout(timer); clearTimeout(end); });
    window.addEventListener('scroll', steps.moved, { passive: true });
    mine.stop.push(function () { window.removeEventListener('scroll', steps.moved); });
    // A page still filling is too short for it yet.
    if ('ResizeObserver' in window) {
      var grows = new ResizeObserver(grown);
      grows.observe(document.body);
      mine.stop.push(function () { grows.disconnect(); });
    }
    go();
  }

  function putBackTo(y) {
    var anchor = document.documentElement.style;
    anchor.overflowAnchor = 'none';
    placing.stop.push(function () { anchor.overflowAnchor = ''; });
    putBack(function () { return y; });
  }

  /* What is in *slot* is about to be swapped: hold what the person sees. */
  function hold(slot) {
    if (placing || !started || window.scrollY === 0) return;
    var y = window.scrollY;
    var row = Array.prototype.filter.call(document.querySelectorAll('[data-row-id]'), function (n) {
      var r = n.getBoundingClientRect();
      return !slot.contains(n) && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
    })[0];
    var top = row ? row.getBoundingClientRect().top : 0;
    startPlacing();
    putBack(function () { return row ? Math.max(0, window.scrollY + row.getBoundingClientRect().top - top) : y; });
  }

  function onScroll() {
    if (!placing && current) places[current] = window.scrollY;
  }

  /* ── The rows' scroll ── */

  var touched = {}; // the rows the person has scrolled, by id; Safari scrolls others by itself
  var keyAt = 0; // when a key was last pressed
  var finger = null; // where a touch began

  /* Whether *e* is the person scrolling *row* sideways, not the page past it. */
  function scrollsRow(e, row) {
    if (e.type === 'wheel') return e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY);
    if (e.type === 'touchmove') {
      var t = e.touches[0];
      return !!finger && Math.abs(t.clientX - finger.x) > Math.abs(t.clientY - finger.y);
    }
    if (e.type === 'mousedown') return e.target.classList.contains('watch-row__scroll');
    if (e.type === 'click') return !!e.target.closest('.watch-row__arrow') && row.contains(e.target);
    if (e.type === 'focusin') return Date.now() - keyAt < KEY_MS;
    return e.type === 'keydown';
  }

  var ROW_TAKES = ['wheel', 'touchmove', 'mousedown', 'click', 'keydown', 'focusin'];

  function onRowTaken(e) {
    if (e.type === 'keydown') keyAt = Date.now();
    var row = e.target && e.target.closest && e.target.closest('[data-row-id]');
    if (row && scrollsRow(e, row)) takeRow(row.getAttribute('data-row-id'));
  }

  function takeRow(id) {
    touched[id] = true;
    if (rowHolds[id]) rowHolds[id]();
  }

  function onRowScroll(e) {
    var row = e.target.closest && e.target.matches('.watch-row__scroll') && e.target.closest('[data-row-id]');
    var id = row && row.getAttribute('data-row-id');
    if (id && e.target.clientWidth && touched[id]) rowsNow[id] = e.target.scrollLeft;
  }

  var settled = {}; // the rows that stood still where they were put, by id
  var rowHolds = {}; // what ends each row's hold, by id

  /* Put each row in *root* back to where it was when this entry's page was left, or hold it at its start. */
  function putRowsBack(root) {
    if (!started) return;
    Array.prototype.forEach.call(root.querySelectorAll('[data-row-id] .watch-row__scroll'), function (scroller) {
      var id = scroller.closest('[data-row-id]').getAttribute('data-row-id');
      if (!touched[id] && !settled[id]) putRowBack(id, scroller);
    });
  }

  /* A row that is hidden has no scroll yet, and one still filling is too short for it. */
  function putRowBack(id, scroller) {
    var left = rowsNow[id] || 0;
    var timer = null;
    var watching = [];
    if (rowHolds[id]) rowHolds[id]();
    function done() {
      if (!watching) return;
      clearTimeout(timer);
      watching.forEach(function (o) { o.disconnect(); });
      watching = null;
      scroller.removeEventListener('scroll', steps.moved);
      delete rowHolds[id];
      scroller.style.scrollSnapType = '';
      if (touched[id]) rowsNow[id] = scroller.scrollLeft;
    }
    function there() { return Math.abs(scroller.scrollLeft - left) < 2; }
    function held() {
      if (there()) { settled[id] = true; done(); }
      else go();
    }
    function go() {
      if (!watching || !scroller.clientWidth) return;
      scroller.style.scrollSnapType = 'none';
      if (!there()) {
        scroller.scrollTo({ left: left, behavior: 'instant' });
        steps.put();
      }
      clearTimeout(timer);
      // Safari may take a scroll back a frame later while the row fills.
      if (there()) timer = setTimeout(held, HOLD_MS);
      // Off the screen it loads nothing more, however long it takes to come there.
      else if (!scroller.parentNode.hasAttribute('data-row-more')) timer = setTimeout(done, GROW_MS);
    }
    var steps = follower(function () { return scroller.scrollLeft; }, go, function () { takeRow(id); });
    function grown() { if (!steps.busy()) go(); }
    rowHolds[id] = done;
    scroller.addEventListener('scroll', steps.moved, { passive: true });
    if ('ResizeObserver' in window) {
      watching.push(new ResizeObserver(grown));
      watching[0].observe(scroller);
    }
    if ('MutationObserver' in window) {
      var grows = new MutationObserver(grown);
      grows.observe(scroller, { childList: true });
      watching.push(grows);
    }
    go();
  }

  function seen(node) {
    return !!node && node !== document.body && document.body.contains(node) &&
      node.getClientRects().length > 0 && !node.closest('[inert]');
  }

  /* A node off the page takes no focus, and would keep all it stood in. */
  function letGo() {
    Object.keys(focused).forEach(function (id) {
      if (!document.body.contains(focused[id])) delete focused[id];
    });
  }

  function refocus() {
    if (seen(document.activeElement)) return;
    var node = focused[current];
    // A page that was loaded again has neither the node nor what opened its title.
    if (!seen(node)) node = cardOf(openedOn[current]) || firstSeen(HEADINGS[top()]);
    if (!node) return;
    node.focus({ preventScroll: true });
  }

  function firstSeen(selector) {
    return !selector ? null : Array.prototype.filter.call(document.querySelectorAll(selector), seen)[0] || null;
  }

  function cardOf(ref) { return ref ? firstSeen('[data-title-ref="' + ref + '"]') : null; }

  /* A private window may refuse the storage; the page then opens at its top. */
  function remember() {
    if (current && !placing) places[current] = window.scrollY;
    var ids = Object.keys(places).slice(-KEPT);
    var kept = {};
    ids.forEach(function (id) { kept[id] = places[id]; });
    Object.keys(ats).forEach(function (id) { rowsById[id] = rowsNow; });
    var rows = {};
    Object.keys(rowsById).slice(-KEPT).forEach(function (id) { rows[id] = rowsById[id]; });
    var titles = {};
    Object.keys(openedOn).slice(-KEPT).forEach(function (id) { titles[id] = openedOn[id]; });
    try {
      window.sessionStorage.setItem(KEY, JSON.stringify(kept));
      window.sessionStorage.setItem(ROWS_KEY, JSON.stringify(rows));
      window.sessionStorage.setItem(TITLES_KEY, JSON.stringify(titles));
    } catch (e) { /* kept in memory only */ }
  }

  function recall() {
    try {
      var kept = JSON.parse(window.sessionStorage.getItem(KEY)) || {};
      Object.keys(kept).forEach(function (id) { if (typeof kept[id] === 'number') places[id] = kept[id]; });
      rowsById = JSON.parse(window.sessionStorage.getItem(ROWS_KEY)) || {};
      openedOn = JSON.parse(window.sessionStorage.getItem(TITLES_KEY)) || {};
    } catch (e) { /* nothing kept */ }
  }

  /* ── To the player ── */

  function onPlayLink(e) {
    var link = e.target.closest && e.target.closest('a');
    var to = link && link.getAttribute('href');
    if (!to || to.indexOf('/watch/play/') !== 0) return;
    var href = to.split('?');
    var query = new URLSearchParams(href[1]);
    var hash = window.location.hash;
    query.set('from', window.location.pathname + window.location.search + (/^#title=/.test(hash) ? hash : ''));
    link.setAttribute('href', href[0] + '?' + query);
  }

  // Before the browser follows it, in this tab or in another.
  ['click', 'auxclick', 'contextmenu'].forEach(function (type) {
    document.addEventListener(type, onPlayLink, true);
  });

  /* ── Start ── */

  function start() {
    if (started) return;
    started = true;
    if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'manual';
    recall();
    stamp();
    current = mark().id;
    Object.assign(rowsNow, rowsById[current]);
    window.addEventListener('popstate', onPop);
    document.addEventListener('scroll', onRowScroll, { passive: true, capture: true });
    document.addEventListener('touchstart', function (e) {
      finger = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    }, { passive: true, capture: true });
    ROW_TAKES.forEach(function (type) { document.addEventListener(type, onRowTaken, { passive: true, capture: true }); });
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', remember);
    var saved = places[current];
    startPlacing();
    if (route('start', null) || saved === undefined) stopPlacing();
    else putBackTo(saved);
    putRowsBack(document);
  }

  window.MMWatch = window.MMWatch || {};
  window.MMWatch.address = {
    layer: layer,
    start: start,
    url: url,
    push: push,
    replace: replace,
    back: back,
    close: close,
    top: top,
    value: value,
    putRowsBack: putRowsBack,
    hold: hold,
    arriving: function () { return arriving; }
  };
})();
