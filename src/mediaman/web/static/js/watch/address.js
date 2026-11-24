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
 * push and replace move the address for a layer and do not call it back; every
 * other layer is brought into line. open and close may return true: the layer
 * places the page's scroll itself. watch.js calls start() last.
 */
(function () {
  'use strict';

  var KEY = 'mm:watch:places';
  var KEPT = 50; // entries whose scroll outlives the page
  var LATE_MS = 1000; // a step through the history this late is not coming
  var GROW_MS = 3000; // how long a page may take to grow back to its scroll

  var layers = []; // lowest first
  var values = {}; // what each open layer is open at, by name
  var started = false;
  var leaving = null; // the step asked of the browser and not yet made: { close, timer, then }
  var places = {}; // each entry's scroll, by id
  var focused = {}; // the node each entry had the focus on, by id
  var placing = null; // the scroll being put back

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
    if (!mark()) write('replaceState', { id: newId(), at: 0, entry: null, stack: [] }, currentUrl());
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
    var at = (was ? was.at : 0) + 1;
    var stack = was ? was.stack : [];
    var open = stack.some(function (s) { return s.name === name; });
    write('pushState', {
      id: newId(), at: at, entry: name,
      stack: name === null || open ? stack : stack.concat({ name: name, at: at })
    }, to);
    restack();
    stopPlacing();
    current = mark().id;
    places[current] = window.scrollY;
    if (started) route('push', name);
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
     step the browser never makes (its history was lost) is done without it. */
  function step(by, close) {
    leaving = { close: close, then: null, timer: setTimeout(function () {
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
    if (value(name) !== null) replace(null, without(named(name)));
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

  function back(name) {
    if (leaving) { leaving.then = function () { back(name); }; return; }
    if (value(name) === null) return;
    var m = mark();
    if (m && m.entry === name) step(-1, null);
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
    var placed = route('pop', null);
    if (placed || saved === undefined) stopPlacing();
    else putBack(saved);
    refocus();
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

  function putBack(y) {
    var mine = placing;
    function go() {
      if (placing !== mine) return;
      window.scrollTo({ left: 0, top: y, behavior: 'instant' });
      if (Math.abs(window.scrollY - y) < 2) stopPlacing();
    }
    ['wheel', 'touchstart', 'pointerdown', 'keydown'].forEach(function (type) {
      window.addEventListener(type, stopPlacing, { passive: true, capture: true });
      mine.stop.push(function () { window.removeEventListener(type, stopPlacing, { capture: true }); });
    });
    var timer = setTimeout(stopPlacing, GROW_MS);
    mine.stop.push(function () { clearTimeout(timer); });
    // A page still filling is too short for it yet.
    if ('ResizeObserver' in window) {
      var grows = new ResizeObserver(go);
      grows.observe(document.body);
      mine.stop.push(function () { grows.disconnect(); });
    }
    go();
  }

  function onScroll() {
    if (!placing && current) places[current] = window.scrollY;
  }

  function seen(node) {
    return !!node && node !== document.body && document.body.contains(node) &&
      node.getClientRects().length > 0 && !node.closest('[inert]');
  }

  function refocus() {
    if (seen(document.activeElement)) return;
    var node = focused[current];
    if (seen(node)) node.focus({ preventScroll: true });
  }

  /* A private window may refuse the storage; the page then opens at its top. */
  function remember() {
    if (current && !placing) places[current] = window.scrollY;
    var ids = Object.keys(places).slice(-KEPT);
    var kept = {};
    ids.forEach(function (id) { kept[id] = places[id]; });
    try { window.sessionStorage.setItem(KEY, JSON.stringify(kept)); } catch (e) { /* kept in memory only */ }
  }

  function recall() {
    try {
      var kept = JSON.parse(window.sessionStorage.getItem(KEY)) || {};
      Object.keys(kept).forEach(function (id) { if (typeof kept[id] === 'number') places[id] = kept[id]; });
    } catch (e) { /* nothing kept */ }
  }

  /* ── Start ── */

  function start() {
    if (started) return;
    started = true;
    if ('scrollRestoration' in window.history) window.history.scrollRestoration = 'manual';
    recall();
    stamp();
    current = mark().id;
    window.addEventListener('popstate', onPop);
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', remember);
    var saved = places[current];
    startPlacing();
    if (route('start', null) || saved === undefined) stopPlacing();
    else putBack(saved);
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
    value: value
  };
})();
