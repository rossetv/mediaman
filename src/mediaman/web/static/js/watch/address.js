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
  var HOLD_MS = 120; // how long a row stays where it was put before it is left there
  var TAKES = ['wheel', 'touchstart', 'pointerdown', 'keydown']; // the person takes over

  var layers = []; // lowest first
  var values = {}; // what each open layer is open at, by name
  var started = false;
  var leaving = null; // the step asked of the browser and not yet made: { close, timer, then, land }
  var places = {}; // each entry's scroll, by id
  var focused = {}; // the node each entry had the focus on, by id, while it is on the page
  var ats = {}; // how far along each entry seen stands, by id
  var placing = null; // the scroll being put back
  var rowsById = {}; // how far each entry's rows were scrolled when its page was left, by id
  var rowsNow = {}; // how far each row is scrolled, by its id
  var rowsBack = {}; // how far each row not put back yet was scrolled

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
      if (ats[id] >= at) { delete ats[id]; delete places[id]; delete focused[id]; }
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
    var placed = route('pop', null);
    if (placed || saved === undefined) stopPlacing();
    else putBack(saved);
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

  function putBack(y) {
    var mine = placing;
    function go() {
      if (placing !== mine) return;
      window.scrollTo({ left: 0, top: y, behavior: 'instant' });
      if (Math.abs(window.scrollY - y) < 2) stopPlacing();
    }
    TAKES.forEach(function (type) {
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

  /* ── The rows' scroll ── */

  function onRowScroll(e) {
    var row = e.target.closest && e.target.matches('.watch-row__scroll') && e.target.closest('[data-row-id]');
    var id = row && row.getAttribute('data-row-id');
    if (id && e.target.clientWidth && rowsBack[id] === undefined) rowsNow[id] = e.target.scrollLeft;
  }

  /* Put each row in *root* back to where it was when this entry's page was left. */
  function putRowsBack(root) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-row-id] .watch-row__scroll'), function (scroller) {
      var row = scroller.closest('[data-row-id]');
      var id = row.getAttribute('data-row-id');
      if (rowsBack[id] > 0) putRowBack(row, id, scroller);
    });
  }

  /* A row that is hidden has no scroll yet, and one still filling is too short for it. */
  function putRowBack(row, id, scroller) {
    var left = rowsBack[id];
    var timer = null;
    var watching = [];
    function done() {
      if (!watching) return;
      clearTimeout(timer);
      watching.forEach(function (o) { o.disconnect(); });
      watching = null;
      TAKES.forEach(function (type) { row.removeEventListener(type, done, { capture: true }); });
      delete rowsBack[id];
      rowsNow[id] = scroller.scrollLeft;
      scroller.style.scrollSnapType = '';
    }
    function there() { return Math.abs(scroller.scrollLeft - left) < 2; }
    function held() {
      if (there()) done();
      else go();
    }
    function go() {
      if (!watching || !scroller.clientWidth) return;
      scroller.style.scrollSnapType = 'none';
      if (!there()) scroller.scrollTo({ left: left, behavior: 'instant' });
      clearTimeout(timer);
      // Safari may take a scroll back a frame later while the row fills.
      timer = there() ? setTimeout(held, HOLD_MS) : setTimeout(done, GROW_MS);
    }
    TAKES.forEach(function (type) { row.addEventListener(type, done, { passive: true, capture: true }); });
    if ('ResizeObserver' in window) {
      watching.push(new ResizeObserver(go));
      watching[0].observe(scroller);
    }
    if ('MutationObserver' in window) {
      var grows = new MutationObserver(go);
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
    if (seen(node)) node.focus({ preventScroll: true });
  }

  /* A private window may refuse the storage; the page then opens at its top. */
  function remember() {
    if (current && !placing) places[current] = window.scrollY;
    var ids = Object.keys(places).slice(-KEPT);
    var kept = {};
    ids.forEach(function (id) { kept[id] = places[id]; });
    Object.keys(ats).forEach(function (id) { rowsById[id] = rowsNow; });
    var rows = {};
    Object.keys(rowsById).slice(-KEPT).forEach(function (id) { rows[id] = rowsById[id]; });
    try {
      window.sessionStorage.setItem(KEY, JSON.stringify(kept));
      window.sessionStorage.setItem(ROWS_KEY, JSON.stringify(rows));
    } catch (e) { /* kept in memory only */ }
  }

  function recall() {
    try {
      var kept = JSON.parse(window.sessionStorage.getItem(KEY)) || {};
      Object.keys(kept).forEach(function (id) { if (typeof kept[id] === 'number') places[id] = kept[id]; });
      rowsById = JSON.parse(window.sessionStorage.getItem(ROWS_KEY)) || {};
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
    Object.assign(rowsBack, rowsById[current]);
    Object.assign(rowsNow, rowsBack);
    Object.keys(rowsBack).forEach(function (id) { if (!(rowsBack[id] > 0)) delete rowsBack[id]; });
    window.addEventListener('popstate', onPop);
    document.addEventListener('scroll', onRowScroll, { passive: true, capture: true });
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', remember);
    var saved = places[current];
    startPlacing();
    if (route('start', null) || saved === undefined) stopPlacing();
    else putBack(saved);
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
    putRowsBack: putRowsBack
  };
})();
