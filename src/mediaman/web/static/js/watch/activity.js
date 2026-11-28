/* Watch: the Storage panel's Activity tab, for admins: what the server is busy with, the line
 * down and up, CPU and memory by app, now and over a period. Asks /api/activity every two
 * seconds while the tab is on show and nothing when it is not. Every graph is drawn here, in
 * SVG; one grows into the panel to be read closely. Needs watch/core.js and watch/space.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var byId = core.byId;
  var el = core.el;
  var toArray = core.toArray;
  var MM = window.MM;

  var pane = byId('space-activity');
  var root = byId('space');
  if (!pane || !root) return;
  var panel = root.querySelector('.space__panel');
  var foot = root.querySelector('.act-foot');
  var SVG = 'http://www.w3.org/2000/svg';

  var EVERY = 2000;
  var COUNT_MS = 700;
  var MORPH_MS = 520;
  var PERIODS = [
    { id: 'live', label: 'Live', span: 60, note: 'the last minute', every: EVERY },
    { id: '1h', label: '1 h', span: 3600, note: 'the last hour', every: 15000 },
    { id: '24h', label: '24 h', span: 86400, note: 'the last 24 hours', every: 60000 },
    { id: '7d', label: '7 d', span: 7 * 86400, note: 'the last 7 days', every: 300000 },
    { id: '30d', label: '30 d', span: 30 * 86400, note: 'the last 30 days', every: 900000 }
  ];
  var SLOTS = 160; // points a period is drawn with; Live has one every two seconds
  var LIVE_SLOTS = 31;
  /* One colour per app, the same in the line, the bars, the keys and the graphs. */
  var APPS = [
    { id: 'plex', name: 'Plex' },
    { id: 'nzbget', name: 'NZBGet' },
    { id: 'sonarr', name: 'Sonarr' },
    { id: 'radarr', name: 'Radarr' },
    { id: 'mediaman', name: 'mediaman' }
  ];
  var KIB = 1024;
  var GIB = KIB * KIB * KIB;

  var on = false; // the tab is on show
  var data = null; // the newest answer
  var failed = false;
  var period = PERIODS[0];
  var hist = {}; // by period id: { at, points, since }
  var timer = null;
  var histTimer = null;
  var gen = 0;
  var built = false;
  var N = {}; // the nodes that are filled on every answer
  var charts = {}; // by name
  var big = null; // the graph grown into the panel

  /* ── Small things ── */

  function reduce() { return core.reduceMotion(); }
  function add(node) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) node.append(arguments[i]);
    return node;
  }
  function icon(name) { return MM.dom.icon(name); }
  function svg(tag, attrs) {
    var n = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function known(v) { return typeof v === 'number' && isFinite(v); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many || one + 's'); }

  function rate(b) {
    if (!known(b)) return '—';
    var mb = b / KIB / KIB;
    if (mb >= 1000) return (mb / KIB).toFixed(2) + ' GB/s';
    if (mb >= 100) return Math.round(mb) + ' MB/s';
    if (mb >= 1) return mb.toFixed(1).replace(/\.0$/, '') + ' MB/s';
    return Math.round(b / KIB) + ' KB/s';
  }
  function bytes(b) {
    if (!known(b)) return '—';
    var gb = b / KIB / KIB / KIB;
    if (gb >= 100) return Math.round(gb) + ' GB';
    if (gb >= 1) return gb.toFixed(1) + ' GB';
    return Math.round(b / KIB / KIB) + ' MB';
  }
  function gbOnly(b) { return known(b) ? (b / KIB / KIB / KIB).toFixed(1) : '—'; }
  function pct(n) { return !known(n) ? '—' : n < 1 && n > 0 ? n.toFixed(1) + '%' : Math.round(n) + '%'; }
  function mbps(b) { return Math.round(b * 8 / 1e6) + ' Mbps'; }

  function clock(at, withDay) {
    var d = new Date(at * 1000);
    var t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return withDay ? d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }) + ', ' + t : t;
  }
  function clockSec(at) { return new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function ago(at, now) {
    var s = Math.max(0, now - at);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400 * 2) return plural(Math.round(s / 3600), 'hour') + ' ago';
    return plural(Math.round(s / 86400), 'day') + ' ago';
  }
  /* A time under a grown graph: short, so five fit across a phone. */
  function tickLabel(at, i) {
    var left = period.span * (4 - i) / 4;
    if (period.span <= 60) return left + '\u00a0s ago';
    if (period.span <= 3600) return (left >= 3600 ? '1\u00a0h' : Math.round(left / 60) + '\u00a0min') + ' ago';
    var d = new Date(at * 1000);
    if (period.span <= 86400) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return d.toLocaleDateString([], period.span <= 7 * 86400 ? { weekday: 'short', day: 'numeric' } : { day: 'numeric', month: 'short' });
  }

  function whenLabel(at) {
    if (period.span <= 60) return clockSec(at);
    var today = new Date(at * 1000).toDateString() === new Date().toDateString();
    return clock(at, period.span > 86400 || !today);
  }

  /* ── Numbers that count to their new value ── */

  function count(node, value, fmt) {
    var was = node._v;
    node._v = value;
    if (node._raf) cancelAnimationFrame(node._raf);
    if (!known(value) || !known(was) || was === value || reduce() || !on) { node.textContent = fmt(value); return; }
    var t0 = performance.now();
    function frame(t) {
      // A frame's time can come before t0: it is when the frame began.
      var k = Math.max(0, Math.min(1, (t - t0) / COUNT_MS));
      var e = 1 - Math.pow(1 - k, 3);
      node.textContent = fmt(was + (value - was) * e);
      node._raf = k < 1 ? requestAnimationFrame(frame) : 0;
    }
    node._raf = requestAnimationFrame(frame);
  }

  function lower(text) { return text.charAt(0).toLowerCase() + text.slice(1); }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }

  /* ── The figures of a point ── */

  /* The server's memory in bytes, when it is set in Settings. Plex gives the memory in use,
     its own too, only as a share: without the size memory is shown in percent, and an app
     measured in bytes has no share of it. */
  function memTotal() { return data && known(data.mem_total) && data.mem_total > 0 ? data.mem_total : null; }
  function share(v) { var total = memTotal(); return !known(v) ? null : total ? v / 100 * total : v; }
  function memFmt() { return memTotal() ? bytes : pct; }

  function appsOf(p) { return (p && p.apps) || {}; }
  /* An app's CPU (i 0) or memory (i 1). */
  function app(p, id, i) {
    if (i === 1 && id === 'plex' && known(p.plex_mem_pct)) return share(p.plex_mem_pct);
    if (i === 1 && !memTotal()) return null;
    var a = appsOf(p)[id];
    return a && known(a[i]) ? a[i] : null;
  }
  /* The whole server's CPU in percent, or its memory. */
  function whole(p, i) { return i === 0 ? (known(p.cpu) ? p.cpu : null) : share(p.mem_pct); }
  function knownSum(p, i) { return APPS.reduce(function (s, a) { return s + (app(p, a.id, i) || 0); }, 0); }
  /* What the host does that no app named here does. */
  function elseOf(p, i) {
    var all = whole(p, i);
    return all !== null ? Math.max(0, all - knownSum(p, i)) : null;
  }

  /* ── Graphs ── */

  /* The values of *get* over [start, end] in *n* even steps, from the points either side;
     a gap longer than *gap* seconds, or a time before the first point, is null. */
  function slots(points, start, end, n, get, gap) {
    var out = new Array(n);
    var j = 0;
    for (var i = 0; i < n; i++) {
      var t = start + (end - start) * i / (n - 1);
      while (j < points.length - 1 && points[j + 1].at <= t) j++;
      var a = points[j];
      var b = points[j + 1];
      var va = a ? get(a) : null;
      if (!a || a.at > t + gap / 2) { out[i] = null; continue; }
      if (!b || b.at - a.at > gap) { out[i] = t - a.at <= gap ? va : null; continue; }
      var vb = get(b);
      if (!known(va) || !known(vb)) { out[i] = known(va) ? va : null; continue; }
      // Before the first point its value is held: going on back along the slope can go below 0.
      out[i] = Math.max(0, va + (vb - va) * Math.max(0, t - a.at) / (b.at - a.at));
    }
    return out;
  }

  function niceMax(v, steps) {
    for (var i = 0; i < steps.length; i++) if (v * 1.1 <= steps[i]) return steps[i];
    return steps[steps.length - 1] * Math.ceil(v * 1.1 / steps[steps.length - 1]);
  }
  var MB = KIB * KIB;
  var RATE_STEPS = [0.5, 1, 2, 5, 10, 25, 50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000].map(function (x) { return x * MB; });
  var UP_STEPS = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500].map(function (x) { return x * MB; });

  /* A path through the known values, broken where one is not known; an area closes down to
     *base* (a number or, stacked, the values under it). */
  function pathOf(xs, ys, area, base) {
    var d = '';
    var run = [];
    function close() {
      if (!run.length) return;
      if (run.length === 1) run.push([run[0][0] + 0.01, run[0][1], run[0][2]]);
      d += 'M' + run.map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join('L');
      if (area) {
        for (var k = run.length - 1; k >= 0; k--) d += 'L' + run[k][0].toFixed(1) + ' ' + run[k][2].toFixed(1);
        d += 'Z';
      }
      run = [];
    }
    for (var i = 0; i < xs.length; i++) {
      if (ys[i] === null) { close(); continue; }
      run.push([xs[i], ys[i], !area ? 0 : typeof base === 'number' ? base : base[i]]);
    }
    close();
    return d;
  }

  /* A graph: an SVG sized to its box, the series drawn from *model()*, and a crosshair that
     follows the pointer or a finger dragged along it. */
  function Chart(name, kind, opts) {
    var self = this;
    this.name = name;
    this.kind = kind; // 'net' or 'stack'
    this.opts = opts || {};
    this.box = el('div', 'act-chart act-chart--' + kind + (this.opts.big ? ' act-chart--big' : ''));
    this.svg = svg('svg', { class: 'act-svg', 'aria-hidden': 'true', focusable: 'false' });
    this.g = svg('g', { class: 'act-g' });
    if (kind === 'net') {
      this.fill = 'act-fill-' + name;
      var grad = svg('linearGradient', { id: this.fill, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.append(svg('stop', { offset: 0, class: 'act-fill-top' }), svg('stop', { offset: 1, class: 'act-fill-end' }));
      this.svg.append(add(svg('defs'), grad));
    }
    this.svg.append(this.g);
    this.hair = el('div', 'act-hair');
    this.hair.hidden = true;
    this.tip = el('div', 'act-tip');
    this.tip.hidden = true;
    this.dots = el('div', 'act-hair__dots');
    this.hair.append(this.dots);
    this.empty = el('p', 'act-chart__empty');
    this.empty.hidden = true;
    add(this.box, this.svg, this.hair, this.tip, this.empty);
    this.vals = null; // what is drawn: { layers: [[values]], max, ... }
    this.at = null; // the time of each slot
    this.w = 0;
    this.h = 0;
    this.scrub = -1;
    var down = null;
    this.box.addEventListener('pointermove', function (e) {
      if (down && Math.abs(e.clientX - down.x) > 6) down.moved = true;
      if (e.pointerType === 'mouse' || down) self.point(e.clientX);
    });
    this.box.addEventListener('pointerdown', function (e) {
      down = { x: e.clientX, moved: false };
      if (e.pointerType !== 'mouse') self.point(e.clientX);
    });
    function up(e) {
      var was = down;
      down = null;
      if (e.type === 'pointerup' && was && !was.moved && self.opts.onTap) self.opts.onTap();
      if (e.pointerType !== 'mouse' || e.type === 'pointerleave') self.point(null);
    }
    this.box.addEventListener('pointerup', up);
    this.box.addEventListener('pointercancel', up);
    this.box.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') { down = null; self.point(null); } });
  }

  Chart.prototype.size = function () {
    var w = this.box.clientWidth;
    var h = this.box.clientHeight;
    if (w === this.w && h === this.h) return false;
    this.w = w;
    this.h = h;
    this.svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    return true;
  };

  /* *m*: { at: [times], layers: [{ cls, vals, fill }], max, up } — net graphs mirror *up*
     below their line. */
  Chart.prototype.draw = function (m) {
    this.size();
    this.m = m;
    var w = this.w;
    var h = this.h;
    if (!w || !h) return;
    var n = m.at.length;
    var xs = m.at.map(function (_, i) { return n > 1 ? i * w / (n - 1) : 0; });
    var g = this.g;
    var parts = [];
    if (this.kind === 'net') {
      var mid = Math.round(h * 0.69);
      var room = mid - 4;
      var below = h - mid - 4;
      var downY = m.layers[0].vals.map(function (v) { return v === null ? null : mid - Math.min(1, v / m.max) * room; });
      var upY = m.layers[1].vals.map(function (v) { return v === null ? null : mid + 1 + Math.min(1, v / m.upMax) * below; });
      parts.push(['path', { class: 'act-area act-area--down', d: pathOf(xs, downY, true, mid), fill: 'url(#' + this.fill + ')' }]);
      parts.push(['path', { class: 'act-area act-area--up', d: pathOf(xs, upY, true, mid + 1) }]);
      parts.push(['path', { class: 'act-ln act-ln--down', d: pathOf(xs, downY, false) }]);
      parts.push(['path', { class: 'act-ln act-ln--up', d: pathOf(xs, upY, false) }]);
      parts.push(['line', { class: 'act-mid', x1: 0, x2: w, y1: mid + 0.5, y2: mid + 0.5 }]);
      this.ys = [downY, upY];
    } else {
      var top = 3;
      var under = xs.map(function () { return 0; });
      var self = this;
      this.ys = [];
      m.layers.forEach(function (layer) {
        var known0 = layer.vals.some(function (v) { return v !== null; });
        var lo = under.slice();
        var hi = layer.vals.map(function (v, i) { return v === null ? lo[i] : lo[i] + v; });
        under = hi;
        if (!known0) { self.ys.push(null); return; }
        var y = function (v) { return h - Math.min(1, v / m.max) * (h - top); };
        var yHi = hi.map(function (v, i) { return layer.vals[i] === null ? null : y(v); });
        var yLo = lo.map(y);
        parts.push(['path', { class: 'act-stack ' + layer.cls, d: pathOf(xs, yHi, true, yLo) }]);
        self.ys.push(yHi);
      });
      var total = m.total ? m.total.map(function (v) { return v === null ? null : h - Math.min(1, v / m.max) * (h - top); }) : null;
      if (total) parts.push(['path', { class: 'act-ln act-ln--total', d: pathOf(xs, total, false) }]);
      this.totalY = total;
    }
    var frag = document.createDocumentFragment();
    parts.forEach(function (p) { frag.append(svg(p[0], p[1])); });
    g.textContent = '';
    g.append(frag);
    this.xs = xs;
    if (this.scrub >= 0) this.show(this.scrub);
  };

  /* Slide what was drawn one slot to the left over the next two seconds, as Live moves on. */
  Chart.prototype.slide = function () {
    if (!this.g.animate || reduce() || !this.xs || this.xs.length < 2) return;
    var dx = this.xs[1] - this.xs[0];
    this.g.animate([{ transform: 'translateX(' + dx + 'px)' }, { transform: 'none' }], { duration: EVERY, easing: 'linear' });
  };

  Chart.prototype.point = function (clientX) {
    if (clientX === null || !this.xs) { this.scrub = -1; this.hair.hidden = true; this.tip.hidden = true; this.box.classList.remove('is-scrub'); return; }
    var r = this.box.getBoundingClientRect();
    var x = clientX - r.left;
    var n = this.xs.length;
    var i = Math.max(0, Math.min(n - 1, Math.round(x / Math.max(1, this.w) * (n - 1))));
    this.show(i);
  };

  Chart.prototype.step = function (by) {
    var n = this.xs ? this.xs.length : 0;
    if (!n) return;
    var i = this.scrub < 0 ? n - 1 : Math.max(0, Math.min(n - 1, this.scrub + by));
    this.show(i);
  };

  Chart.prototype.show = function (i) {
    var m = this.m;
    if (!m || !this.xs) return;
    this.scrub = i;
    var x = this.xs[i];
    var lines = this.opts.tip(m, i);
    this.box.classList.add('is-scrub');
    this.hair.hidden = false;
    this.hair.style.transform = 'translateX(' + x.toFixed(1) + 'px)';
    this.dots.textContent = '';
    var self = this;
    (this.kind === 'net' ? this.ys : [this.totalY]).forEach(function (ys, k) {
      if (!ys || ys[i] === null || ys[i] === undefined) return;
      var d = el('span', 'act-hair__dot' + (self.kind === 'net' ? (k ? ' is-up' : ' is-down') : ''));
      d.style.transform = 'translateY(' + ys[i].toFixed(1) + 'px)';
      self.dots.append(d);
    });
    this.tip.textContent = '';
    add(this.tip, el('b', 'act-tip__when', lines.when));
    lines.rows.forEach(function (row) {
      var line = el('span', 'act-tip__row ' + (row.cls || ''));
      add(line, el('span', 'space-dot'), el('span', 'act-tip__name', row.name), el('span', 'act-tip__v', row.value));
      self.tip.append(line);
    });
    this.tip.hidden = false;
    var tw = this.tip.offsetWidth;
    var left = x + 12 + tw > this.w ? x - 12 - tw : x + 12;
    this.tip.style.transform = 'translateX(' + Math.max(0, left).toFixed(1) + 'px)';
    if (this.opts.said) this.opts.said(lines);
  };

  /* ── What each graph draws, for the period on show ── */

  function windowOf() {
    var p = points();
    var end = period.id === 'live' ? (p.length ? p[p.length - 1].at : Date.now() / 1000) : (hist[period.id] ? hist[period.id].now : Date.now() / 1000);
    return { start: end - period.span, end: end, n: period.id === 'live' ? LIVE_SLOTS : SLOTS };
  }

  function points() {
    if (period.id === 'live') return data ? data.live : [];
    return hist[period.id] ? hist[period.id].points : [];
  }

  function gapOf() { return period.id === 'live' ? 20 : Math.max(45, period.span / 60); }

  function times(wnd) {
    var out = [];
    for (var i = 0; i < wnd.n; i++) out.push(wnd.start + (wnd.end - wnd.start) * i / (wnd.n - 1));
    return out;
  }

  function netModel() {
    var wnd = windowOf();
    var p = points();
    var g = gapOf();
    var down = slots(p, wnd.start, wnd.end, wnd.n, function (x) { return x.down; }, g);
    var up = slots(p, wnd.start, wnd.end, wnd.n, function (x) { return x.up; }, g);
    var hiDown = Math.max.apply(null, down.map(function (v) { return v || 0; }).concat([0]));
    var hiUp = Math.max.apply(null, up.map(function (v) { return v || 0; }).concat([0]));
    return {
      at: times(wnd),
      layers: [{ vals: down }, { vals: up }],
      max: niceMax(hiDown, RATE_STEPS),
      upMax: niceMax(hiUp, UP_STEPS)
    };
  }

  /* CPU (i 0) or memory (i 1), stacked by app with what else runs on top. */
  function stackModel(i) {
    var wnd = windowOf();
    var p = points();
    var g = gapOf();
    var layers = APPS.map(function (a) {
      return { id: a.id, cls: 'act-c--' + a.id, vals: slots(p, wnd.start, wnd.end, wnd.n, function (x) { return app(x, a.id, i); }, g) };
    });
    layers.push({ id: 'else', cls: 'act-c--other', vals: slots(p, wnd.start, wnd.end, wnd.n, function (x) { return elseOf(x, i); }, g) });
    var total = slots(p, wnd.start, wnd.end, wnd.n, function (x) { return whole(x, i); }, g);
    var max = i === 0 ? 100 : memTotal() || 100;
    return { at: times(wnd), layers: layers, total: total, max: max, i: i };
  }

  /* From the graph drawn to the next: each value moves from where it was. */
  function morph(chart, next) {
    var was = chart.m;
    if (!was || reduce() || was.at.length !== next.at.length || was.layers.length !== next.layers.length) { chart.draw(next); return; }
    if (chart._raf) cancelAnimationFrame(chart._raf);
    var t0 = performance.now();
    function mix(a, b, k) {
      return b.map(function (v, i) {
        var f = a[i];
        if (v === null && f === null) return null;
        return (f === null ? 0 : f) + ((v === null ? 0 : v) - (f === null ? 0 : f)) * k;
      });
    }
    var from = JSON.parse(JSON.stringify(was));
    function frame(t) {
      var k = Math.max(0, Math.min(1, (t - t0) / MORPH_MS));
      var e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
      var m = JSON.parse(JSON.stringify(next));
      m.layers.forEach(function (layer, j) { layer.vals = k < 1 ? mix(from.layers[j].vals, next.layers[j].vals, e) : next.layers[j].vals; });
      if (next.total) m.total = k < 1 ? mix(from.total || [], next.total, e) : next.total;
      if (from.max && next.max) m.max = from.max + (next.max - from.max) * e;
      if (from.upMax && next.upMax) m.upMax = from.upMax + (next.upMax - from.upMax) * e;
      chart.draw(m);
      chart._raf = k < 1 ? requestAnimationFrame(frame) : 0;
    }
    chart._raf = requestAnimationFrame(frame);
  }

  function netTip(m, i) {
    return {
      when: whenLabel(m.at[i]),
      rows: [
        { cls: 'act-c--dl', name: 'Down', value: rate(m.layers[0].vals[i]) },
        { cls: 'act-c--up', name: 'Up', value: rate(m.layers[1].vals[i]) }
      ]
    };
  }

  function stackTip(m, i) {
    var fmt = m.i === 0 ? pct : memFmt();
    var rows = [{ cls: 'act-c--total', name: m.i === 0 ? 'All of the CPU' : 'All in use', value: fmt(m.total[i]) }];
    m.layers.forEach(function (layer) {
      var v = layer.vals[i];
      if (v === null || v === undefined) return;
      var a = APPS.filter(function (x) { return x.id === layer.id; })[0];
      rows.push({ cls: layer.cls, name: a ? a.name : 'Everything else', value: fmt(v) });
    });
    return { when: whenLabel(m.at[i]), rows: rows };
  }

  /* ── Drawing the tab ── */

  function heading(text, note) {
    var h = el('h3', 'space-sub act-h');
    var n = el('span', 'act-h__note', note || '');
    add(h, el('span', 'act-h__name', text), n);
    return { node: h, note: n };
  }

  function tile(name) {
    var t = el('div', 'act-tile');
    var n = el('span', 'act-tile__n');
    var s = el('small', 'act-tile__sub');
    add(t, el('span', 'act-tile__name', name), n, s);
    return { node: t, n: n, sub: s };
  }

  function periodControl() {
    var box = el('div', 'act-periods');
    box.setAttribute('role', 'radiogroup');
    box.setAttribute('aria-label', 'Period');
    var ink = el('span', 'act-periods__ink');
    ink.setAttribute('aria-hidden', 'true');
    box.append(ink);
    PERIODS.forEach(function (p) {
      var b = el('button', 'act-period', p.label);
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.setAttribute('data-period', p.id);
      b.setAttribute('aria-label', p.id === 'live' ? 'Live, the last minute' : p.note.replace(/^the last/, 'The last'));
      box.append(b);
    });
    box.addEventListener('click', function (e) {
      var b = e.target.closest('[data-period]');
      if (b) choose(b.getAttribute('data-period'));
    });
    box.addEventListener('keydown', function (e) {
      var i = PERIODS.indexOf(period);
      var to = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? i + 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? PERIODS.length - 1 : null;
      if (to === null) return;
      e.preventDefault();
      var p = PERIODS[(to + PERIODS.length) % PERIODS.length];
      choose(p.id);
      box.querySelector('[data-period="' + p.id + '"]').focus({ preventScroll: true });
    });
    return box;
  }

  function paintPeriods(move) {
    toArray(document.querySelectorAll('.act-periods')).forEach(function (box) {
      var picked = null;
      toArray(box.querySelectorAll('[data-period]')).forEach(function (b) {
        var yes = b.getAttribute('data-period') === period.id;
        b.setAttribute('aria-checked', yes ? 'true' : 'false');
        b.tabIndex = yes ? 0 : -1;
        if (yes) picked = b;
      });
      var ink = box.querySelector('.act-periods__ink');
      if (!picked || !picked.offsetWidth) return;
      var to = 'translateX(' + picked.offsetLeft + 'px) scaleX(' + picked.offsetWidth / 100 + ')';
      var was = ink.style.transform;
      ink.style.transform = to;
      if (move && was && was !== to && ink.animate && !reduce()) {
        ink.animate([{ transform: was }, { transform: to }], { duration: 260, easing: 'cubic-bezier(.32,.72,0,1)' });
      }
    });
  }

  /* A graph that grows when it is pressed, or by Enter. */
  function expandable(chart, label) {
    var wrap = el('div', 'act-grow');
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'button');
    wrap.setAttribute('aria-label', label + ', ' + period.note + '. Show larger');
    var hint = el('span', 'act-grow__hint');
    hint.setAttribute('aria-hidden', 'true');
    hint.append(icon('i-expand'));
    add(wrap, chart.box, hint);
    chart.opts.onTap = function () { grow(chart.name); };
    wrap.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); grow(chart.name); }
    });
    wrap.addEventListener('focus', function () { if (wrap.matches(':focus-visible')) chart.step(0); });
    wrap.addEventListener('blur', function () { chart.point(null); });
    wrap._label = label;
    return wrap;
  }

  function row(cls, name) {
    var li = el('li', 'act-row ' + cls);
    var dot = el('span', 'space-dot');
    dot.setAttribute('aria-hidden', 'true');
    var text = el('span', 'act-row__text');
    var b = el('b', '', name);
    var sub = el('small', 'act-row__sub');
    add(text, b, sub);
    var val = el('span', 'act-row__val');
    var subs = el('ul', 'act-subs');
    add(li, dot, text, val, subs);
    return { node: li, sub: sub, val: val, subs: subs };
  }

  function resCard(name, i) {
    var card = el('div', 'act-res');
    var head = el('div', 'act-res__head');
    var n = el('span', 'act-res__n');
    var fig = el('span', 'act-res__fig');
    var of = el('small', 'act-res__of');
    add(n, fig, of);
    add(head, el('span', 'act-res__name', name), n);
    var chart = new Chart(i === 0 ? 'cpu' : 'mem', 'stack', { tip: stackTip, said: say });
    var grow0 = expandable(chart, name + ' by app');
    var bar = el('div', 'act-bar');
    bar.setAttribute('aria-hidden', 'true');
    var keys = el('ul', 'act-keys');
    add(card, head, grow0, bar, keys);
    charts[chart.name] = chart;
    return { node: card, fig: fig, of: of, bar: bar, keys: keys, grow: grow0 };
  }

  function build() {
    pane.textContent = '';
    pane.classList.add('act-pane');
    var body = el('div', 'act-body');
    N.say = el('p', 'act-say');
    N.say.setAttribute('aria-live', 'polite');
    var tiles = el('div', 'act-tiles');
    N.tDown = tile('Network down');
    N.tCpu = tile('CPU');
    N.tMem = tile('Memory');
    add(tiles, N.tDown.node, N.tCpu.node, N.tMem.node);
    N.periods = periodControl();

    var net = heading('Network', period.note);
    N.netNote = net.note;
    var netChart = new Chart('net', 'net', { tip: netTip, said: say });
    charts.net = netChart;
    var netBox = el('div', 'act-net');
    N.netMax = el('span', 'act-scale act-scale--top');
    N.upMax = el('span', 'act-scale act-scale--up');
    var axis = el('div', 'act-axis');
    N.axisFrom = el('span', 'act-axis__from');
    var keysLine = el('span', 'act-axis__keys');
    add(keysLine, el('span', 'act-k act-k--down', 'Down'), el('span', 'act-k act-k--up', 'Up'));
    add(axis, N.axisFrom, keysLine, el('span', '', 'Now'));
    N.netGrow = expandable(netChart, 'Network');
    add(netBox, N.netMax, N.upMax, N.netGrow, axis);

    var rows = el('ul', 'act-rows');
    rows.setAttribute('aria-label', 'What’s using the network');
    N.rDl = row('act-c--dl', 'Downloads');
    N.rWatch = row('act-c--plex', 'Watching');
    N.rMm = row('act-c--mediaman', 'mediaman');
    add(rows, N.rDl.node, N.rWatch.node, N.rMm.node);

    var res = heading('CPU and memory', '');
    N.resNote = res.note;
    N.resSay = el('p', 'act-quiet');
    N.cpu = resCard('CPU', 0);
    N.mem = resCard('Memory', 1);

    var work = heading('Working on', '');
    N.works = el('ul', 'act-works');

    var parts = [N.say, tiles, N.periods, net.node, netBox, rows, res.node, N.resSay, N.cpu.node, N.mem.node, work.node, N.works];
    parts.forEach(function (p, i) { p.style.setProperty('--i', Math.min(i, 8)); body.append(p); });
    pane.append(body);
    N.body = body;
    built = true;
    if (!reduce()) body.classList.add('is-arriving');
    setTimeout(function () { body.classList.remove('is-arriving'); }, 900);
  }

  /* The words of the line at the top. */
  function sayLine(d) {
    var s = d.sources;
    var watching = d.sessions.length;
    var people = watching === 1 ? '1 person is watching' : watching + ' people are watching';
    var dl = d.downloads && d.downloads.rate > 0;
    if (s.nzbget === 'down') return { warn: true, text: 'NZBGet isn’t answering, so downloads aren’t counted.' + (s.plex === 'ok' ? ' ' + (watching ? people : 'Nobody is watching') + '.' : '') };
    if (s.plex === 'down') return { warn: true, text: 'Plex isn’t answering, so watching and the server’s CPU aren’t counted.' + (dl ? ' Downloading.' : '') };
    if (dl && watching) return { text: 'Downloading, and ' + people + '.' };
    if (dl) return { text: 'Downloading. Nobody is watching.' };
    if (watching) return { text: people.charAt(0).toUpperCase() + people.slice(1) + '.' };
    return { text: 'All quiet: nothing downloading, nobody watching.' };
  }

  function sub(o) {
    var li = el('li', 'act-sub' + (o.icon ? ' has-icon' : ''));
    if (o.icon) { var ic = el('span', 'act-sub__ic'); ic.append(icon(o.icon)); li.append(ic); }
    var text = el('span', 'act-sub__text');
    var name = el('span', 'act-sub__name');
    name.append(el('span', 'act-sub__title', o.name));
    if (o.tag) name.append(el('span', 'act-tag' + (o.away ? ' act-tag--away' : ''), o.tag));
    add(text, name, el('small', '', o.sub));
    var val = el('span', 'act-sub__val' + (o.valCls ? ' ' + o.valCls : ''));
    add(li, text, val);
    if (o.bar !== undefined) {
      var line = el('span', 'act-line');
      line.setAttribute('aria-hidden', 'true');
      var fill = el('span', 'act-line__fill');
      fill.style.setProperty('--w', o.bar);
      line.append(fill);
      li.append(line);
    }
    return { node: li, val: val };
  }

  /* Rows under a row, kept by key so what stays counts on rather than being drawn again. */
  function fillSubs(list, items) {
    var keep = {};
    toArray(list.children).forEach(function (li) { keep[li._key] = li; });
    var frag = [];
    items.forEach(function (it) {
      var made = sub(it);
      var old = keep[it.key];
      if (old) {
        old.replaceChildren.apply(old, toArray(made.node.childNodes));
        old.className = made.node.className;
        made.node = old;
        made.val = old.querySelector('.act-sub__val');
        made.val._v = old._v;
      } else {
        made.node._key = it.key;
        if (built && N.body && !reduce()) made.node.classList.add('is-new');
      }
      if (it.num !== undefined) { made.val._v = made.node._v; count(made.val, it.num, it.fmt); made.node._v = it.num; } else made.val.textContent = it.val || '';
      frag.push(made.node);
    });
    list.replaceChildren.apply(list, frag);
  }

  var KIND_ICON = { tv: 'i-tv', phone: 'i-phone', tablet: 'i-tablet', web: 'i-laptop', computer: 'i-desktop' };
  var IMPORTER = { sonarr: 'Sonarr', radarr: 'Radarr' };
  var STATE_WORD = { downloading: 'Downloading', queued: 'Queued', paused: 'Paused', repairing: 'Repairing', unpacking: 'Unpacking', finishing: 'Finishing' };

  function minutesLeft(left, speed) {
    if (!(speed > 0)) return '';
    var s = left / speed;
    if (s < 90) return 'a minute left';
    if (s < 3600) return Math.round(s / 60) + ' min left';
    return Math.floor(s / 3600) + ' h ' + Math.round(s % 3600 / 60) + ' min left';
  }

  function paintRows(d) {
    var s = d.sources;
    var p = d.point;
    // Downloads
    var dl = N.rDl;
    dl.node.classList.toggle('act-row--warn', s.nzbget === 'down');
    dl.node.classList.toggle('act-row--off', s.nzbget === 'off');
    if (s.nzbget === 'down') {
      dl.sub.replaceChildren(icon('i-triangle-exclamation'), document.createTextNode('NZBGet isn’t answering. Speeds will show when it’s back.'));
      dl.val.textContent = '';
      dl.val._v = null;
      fillSubs(dl.subs, []);
    } else if (s.nzbget === 'off') {
      setText(dl.sub, 'NZBGet isn’t set up');
      dl.val.textContent = '';
      fillSubs(dl.subs, []);
    } else {
      var q = d.downloads;
      setText(dl.sub, q.count ? 'NZBGet · ' + q.count + ' in the queue · ' + bytes(q.left).replace(/\.\d/, '') + ' to go' : 'NZBGet · Queue empty');
      count(dl.val, q.rate, rate);
      fillSubs(dl.subs, q.items.map(function (it) {
        var o = { key: 'dl' + it.title, name: it.title };
        var share = it.size > 0 ? it.done / it.size : 0;
        if (it.state === 'downloading') {
          o.sub = bytes(it.done) + ' of ' + bytes(it.size) + (minutesLeft(it.size - it.done, q.rate) ? ' · ' + minutesLeft(it.size - it.done, q.rate) : '');
          o.num = share * 100;
          o.fmt = function (v) { return Math.round(v) + '%'; };
          o.valCls = 'is-num';
          o.bar = share;
        } else if (it.state === 'queued' || it.state === 'paused') {
          o.sub = (it.state === 'paused' ? 'Paused' : 'Next in the queue') + ' · ' + bytes(it.size);
          o.val = STATE_WORD[it.state];
        } else {
          o.sub = bytes(it.size) + ' · ' + (IMPORTER[it.by] ? IMPORTER[it.by] + ' imports it next' : 'imported once it’s done');
          o.val = STATE_WORD[it.state];
        }
        return o;
      }));
    }
    // Watching
    var w = N.rWatch;
    w.node.classList.toggle('act-row--warn', s.plex === 'down');
    w.node.classList.toggle('act-row--off', s.plex === 'off');
    if (s.plex === 'down') {
      w.sub.replaceChildren(icon('i-triangle-exclamation'), document.createTextNode('Plex isn’t answering. Who’s watching will show when it’s back.'));
      w.val.textContent = '';
      w.val._v = null;
      fillSubs(w.subs, []);
    } else if (s.plex === 'off') {
      setText(w.sub, 'Plex isn’t set up');
      w.val.textContent = '';
      fillSubs(w.subs, []);
    } else {
      var n = d.sessions.length;
      setText(w.sub, 'Plex · ' + (n ? plural(n, 'person', 'people') + ' watching' : 'Nobody watching'));
      count(w.val, p.watch || 0, rate);
      fillSubs(w.subs, d.sessions.map(function (st, i) {
        return {
          key: 'w' + st.user + st.device + i,
          icon: KIND_ICON[st.kind] || 'i-tv',
          name: st.user + ' · ' + st.device,
          tag: st.local ? 'Home' : 'Away',
          away: !st.local,
          sub: st.title + ' · ' + (st.transcode ? 'Transcoding' : 'Direct play') + ' · ' + mbps(st.rate),
          num: st.rate,
          fmt: rate
        };
      }));
    }
    // mediaman
    var mm = N.rMm;
    var busy = d.work.scanning || (d.work.ai && d.work.ai.running);
    setText(mm.sub, d.work.scanning ? 'Scanning the library' : busy ? 'Asking the AI' : 'Posters, searches and this page');
    if (known(p.mm)) count(mm.val, p.mm, rate); else setText(mm.val, '');
  }

  function bar(box, segs) {
    if (box.children.length !== segs.length) {
      box.textContent = '';
      segs.forEach(function (sg) { var b = el('span', 'act-seg ' + sg.cls); b.title = sg.name; box.append(b); });
    }
    segs.forEach(function (sg, i) {
      var b = box.children[i];
      b.style.setProperty('--v', Math.max(0, sg.v || 0));
      b.classList.toggle('is-none', !(sg.v > 0));
    });
  }

  function keys(list, items) {
    var sig = items.map(function (k) { return k.cls; }).join();
    if (list._sig !== sig) {
      list.textContent = '';
      items.forEach(function (k) {
        var li = el('li', 'act-key ' + k.cls);
        var dot = el('span', 'space-dot');
        dot.setAttribute('aria-hidden', 'true');
        var name = el('span', 'act-key__name', k.name);
        name.append(el('small', 'act-key__note'));
        add(li, dot, name, el('span', 'act-key__val'));
        list.append(li);
      });
      list._sig = sig;
    }
    items.forEach(function (k, i) {
      var li = list.children[i];
      var note = li.querySelector('.act-key__note');
      setText(note, k.note || '');
      note.hidden = !k.note;
      count(li.querySelector('.act-key__val'), k.v, k.fmt);
    });
  }

  /* The server as set in Settings: "Server · 10 cores · 16 GB", or nothing. */
  function serverNote(d) {
    var parts = [];
    if (known(d.cores)) parts.push(plural(d.cores, 'core'));
    if (memTotal()) parts.push(Math.round(memTotal() / GIB) + ' GB');
    return parts.length ? ['Server'].concat(parts).join(' · ') : '';
  }

  function paintRes(d) {
    var p = d.point;
    var s = d.sources;
    var total = memTotal();
    setText(N.resNote, serverNote(d));
    var quiet = [];
    if (s.plex === 'down') quiet.push({ warn: true, text: 'Plex isn’t answering, so the server’s CPU and memory aren’t known. The apps below are.' });
    else if (s.plex === 'off') quiet.push({ text: 'Set up Plex to see the server’s CPU and memory.' });
    if (s.docker === 'down') quiet.push({ warn: true, text: 'The Docker proxy isn’t answering, so apps in containers count as everything else.' });
    else if (s.docker === 'off') quiet.push({ text: 'Add a read-only Docker proxy in Settings to tell Sonarr, Radarr and others apart.' });
    if (s.plex !== 'off' && !total) quiet.push({ text: 'Plex gives memory as a share. Set the server’s memory in Settings to see it, and each app’s, in GB.' });
    N.resSay.textContent = '';
    quiet.forEach(function (q) {
      var line = el('span', 'act-quiet__line' + (q.warn ? ' is-warn' : ''));
      if (q.warn) line.append(icon('i-triangle-exclamation'));
      line.append(document.createTextNode(q.text));
      N.resSay.append(line);
    });
    N.resSay.hidden = !quiet.length;
    var listed = APPS.filter(function (a) { return appsOf(p)[a.id]; });
    var transcoding = d.sessions.filter(function (x) { return x.transcode; }).length;
    var unpacking = d.downloads && d.downloads.items.some(function (x) { return x.state === 'unpacking'; });
    function noteOf(id) {
      if (id === 'plex' && transcoding) return 'transcoding ' + plural(transcoding, 'stream');
      if (id === 'nzbget' && unpacking) return 'unpacking';
      return '';
    }
    var elseName = s.docker === 'ok' ? 'Everything else' : 'Other apps';
    [0, 1].forEach(function (i) {
      var card = i === 0 ? N.cpu : N.mem;
      var fmt = i === 0 ? pct : memFmt();
      var all = whole(p, i);
      count(card.fig, all, i === 0 || !total ? pct : gbOnly);
      setText(card.of, i === 1 && all !== null && total ? ' of ' + Math.round(total / GIB) + ' GB' : '');
      // In percent, only Plex has a share of the memory to show.
      var shown = i === 1 && !total ? listed.filter(function (a) { return app(p, a.id, 1) !== null; }) : listed;
      var segs = shown.map(function (a) { return { cls: 'act-c--' + a.id, name: a.name, v: app(p, a.id, i) }; });
      var rest = elseOf(p, i);
      var cap = i === 0 ? 100 : total || 100;
      if (all !== null) {
        segs.push({ cls: 'act-c--other', name: elseName, v: rest });
        segs.push({ cls: 'act-c--free', name: i === 0 ? 'Idle' : 'Free', v: Math.max(0, cap - all) });
      } else {
        segs.push({ cls: 'act-c--free', name: 'Not known', v: Math.max(0, cap - knownSum(p, i)) });
      }
      bar(card.bar, segs);
      var items = shown.map(function (a) { return { cls: 'act-c--' + a.id, name: a.name, v: app(p, a.id, i), fmt: fmt, note: i === 0 ? noteOf(a.id) : '' }; });
      if (all !== null) items.push({ cls: 'act-c--other', name: elseName, v: rest, fmt: fmt });
      if (i === 1 && all !== null) items.push({ cls: 'act-c--free', name: 'Free', v: Math.max(0, cap - all), fmt: fmt });
      keys(card.keys, items);
    });
  }

  function work(o) {
    var li = el('li', 'act-work' + (o.cls ? ' ' + o.cls : ''));
    var ic = el('span', 'act-work__ic');
    ic.append(icon(o.icon));
    var text = el('span', 'act-work__text');
    add(text, el('b', '', o.name), el('small', '', o.sub));
    var state = el('span', 'act-work__state' + (o.stateCls ? ' ' + o.stateCls : ''), o.state || '');
    add(li, ic, text, state);
    if (o.bar !== undefined) {
      var line = el('span', 'act-line');
      line.setAttribute('aria-hidden', 'true');
      var fill = el('span', 'act-line__fill');
      fill.style.setProperty('--w', o.bar);
      line.append(fill);
      li.append(line);
    }
    li._key = o.key;
    return li;
  }

  function paintWork(d) {
    var list = [];
    var wk = d.work;
    var q = d.downloads;
    var going = q && q.items.filter(function (x) { return x.state === 'downloading'; })[0];
    if (going) {
      list.push({ key: 'dl', icon: 'i-download', name: going.title, sub: 'Downloading · NZBGet · ' + rate(q.rate),
        state: Math.round(going.done / Math.max(1, going.size) * 100) + '%', stateCls: 'is-num', bar: going.done / Math.max(1, going.size) });
    }
    if (d.sources.nzbget === 'down') {
      list.push({ key: 'nzbdown', icon: 'i-triangle-exclamation', cls: 'act-work--warn', name: 'Downloads', sub: 'Can’t see them while NZBGet isn’t answering.' });
    }
    if (wk.fixing) {
      list.push({ key: 'fix', icon: 'i-folder-tree', name: 'Fixing ' + plural(wk.fixing, 'download') + ' that wouldn’t import',
        sub: 'mediaman tries again until Radarr or Sonarr takes ' + (wk.fixing === 1 ? 'it' : 'them'), state: 'Working', stateCls: 'is-busy' });
    }
    var searching = Object.keys(wk.searching || {});
    var found = searching.reduce(function (s, k) { return s + wk.searching[k]; }, 0);
    if (found) {
      list.push({ key: 'search', icon: 'i-magnifying-glass', name: 'Searching for ' + plural(found, 'title'),
        sub: searching.map(function (k) { return k.charAt(0).toUpperCase() + k.slice(1) + ' ' + wk.searching[k]; }).join(' · '), state: 'Searching', stateCls: 'is-busy' });
    }
    if (wk.scanning) list.push({ key: 'scan', icon: 'i-rotate-right', name: 'Scanning the library', sub: 'Looking for titles to clear', state: 'Scanning', stateCls: 'is-busy' });
    if (!list.length) list.push({ key: 'calm', icon: 'i-circle-check', cls: 'act-work--calm', name: 'Nothing to do right now', sub: 'Downloads, fixes and searches show here as they start' });
    if (wk.ai) {
      list.push({ key: 'ai', icon: 'i-sparkles', name: 'AI search', sub: wk.ai.running ? 'Answering a search now' : wk.ai.at ? 'Last asked ' + ago(wk.ai.at, d.at) : 'Not asked yet',
        state: wk.ai.running ? 'Thinking' : 'Idle', stateCls: wk.ai.running ? 'is-busy' : 'is-quiet' });
    }
    var sig = JSON.stringify(list);
    if (N.works._sig === sig) return;
    N.works._sig = sig;
    var had = {};
    toArray(N.works.children).forEach(function (li) { had[li._key] = true; });
    N.works.replaceChildren.apply(N.works, list.map(function (o) {
      var li = work(o);
      if (!had[o.key] && N.works._drawn && !reduce()) li.classList.add('is-new');
      return li;
    }));
    N.works._drawn = true;
  }

  function paintTiles(d) {
    var p = d.point;
    count(N.tDown.n, p.down, rate);
    setText(N.tDown.sub, 'Up ' + rate(p.up));
    count(N.tCpu.n, p.cpu, pct);
    setText(N.tCpu.sub, known(d.cores) ? 'of ' + plural(d.cores, 'core') : 'of the server');
    count(N.tMem.n, whole(p, 1), memFmt());
    setText(N.tMem.sub, memTotal() ? 'of ' + Math.round(memTotal() / GIB) + ' GB' : 'of the server');
    N.tCpu.node.classList.toggle('is-unknown', !known(p.cpu));
    N.tMem.node.classList.toggle('is-unknown', whole(p, 1) === null);
  }

  /* Whether the history has come as far back as the period goes. */
  function collecting() {
    if (period.id === 'live') return null;
    var since = data && data.since;
    var h = hist[period.id];
    var start = (h ? h.now : Date.now() / 1000) - period.span;
    if (since && since <= start + period.span / 40) return null;
    var today = new Date(since * 1000).toDateString() === new Date().toDateString();
    return since ? 'Collecting since ' + clock(since, !today) : 'Collecting from now on';
  }

  function paintCharts(slide) {
    var c = collecting();
    var tooSoon = period.id !== 'live' && !hist[period.id];
    setText(N.netNote, period.note);
    setText(N.axisFrom, period.id === 'live' ? '1 min ago' : period.note.replace(/^the last /, '').replace(/^hour$/, '1 hour') + ' ago');
    Object.keys(charts).forEach(function (k) {
      var ch = charts[k];
      var m = k === 'net' ? netModel() : stackModel(k === 'cpu' ? 0 : 1);
      if (tooSoon) return;
      if (slide && period.id === 'live') { ch.draw(m); ch.slide(); } else morph(ch, m);
      ch.empty.hidden = !c;
      ch.empty.textContent = c || '';
      ch.box.classList.toggle('is-collecting', !!c);
      ch.collecting = c;
    });
    var nm = charts.net.m;
    if (nm) { setText(N.netMax, rate(nm.max)); setText(N.upMax, rate(nm.upMax)); }
    N.netGrow.setAttribute('aria-label', 'Network, ' + period.note + (c ? ', ' + lower(c) : '') + '. Show larger');
    [N.cpu, N.mem].forEach(function (card) { card.grow.setAttribute('aria-label', card.grow._label + ', ' + period.note + '. Show larger'); });
    if (big) big.paint();
  }

  function paint(slide) {
    if (!data) return;
    if (!built) build();
    var line = sayLine(data);
    N.say.classList.toggle('act-say--warn', !!line.warn);
    setText(N.say, line.text);
    paintTiles(data);
    paintRows(data);
    paintRes(data);
    paintWork(data);
    paintPeriods(false);
    paintCharts(slide);
  }

  function paintFoot() {
    if (!foot) return;
    var sayNode = foot.querySelector('.act-foot__say');
    foot.classList.toggle('is-stale', failed);
    setText(sayNode, failed ? 'Can’t reach mediaman · trying again' : 'Updated live · every 2 s');
  }

  function failure() {
    pane.textContent = '';
    var s = el('div', 'space-say');
    s.setAttribute('role', 'alert');
    var again = el('button', 'btn btn--secondary', 'Try again');
    again.type = 'button';
    again.prepend(icon('i-rotate-right'));
    again.addEventListener('click', function () { tick(); });
    add(s, el('b', '', 'Activity could not be loaded'), el('span', '', 'Check the connection and try again.'), again);
    pane.append(s);
    built = false;
  }

  /* ── Asking ── */

  function tick() {
    clearTimeout(timer);
    if (!on) return;
    var mine = gen;
    MM.api.get('/api/activity').then(function (d) {
      if (mine !== gen || !on) return;
      var first = !data;
      data = d;
      failed = false;
      paint(!first);
      paintFoot();
    }, function () {
      if (mine !== gen || !on) return;
      failed = true;
      paintFoot();
      if (!data) failure();
    }).then(function () {
      if (mine === gen && on) timer = setTimeout(tick, EVERY);
    });
  }

  function loadHistory(p) {
    clearTimeout(histTimer);
    if (!on || p.id === 'live') return;
    var mine = gen;
    MM.api.get('/api/activity/history?period=' + p.id).then(function (h) {
      if (mine !== gen) return;
      hist[p.id] = h;
      if (period === p && data) paintCharts(false);
    }, function () {}).then(function () {
      if (mine === gen && on && period === p) histTimer = setTimeout(function () { loadHistory(p); }, p.every);
    });
  }

  function choose(id) {
    var p = PERIODS.filter(function (x) { return x.id === id; })[0];
    if (!p || p === period) return;
    period = p;
    paintPeriods(true);
    if (p.id === 'live' || hist[p.id]) paintCharts(false);
    else setText(N.netNote, p.note);
    loadHistory(p);
  }

  function start() {
    if (on) return;
    on = true;
    gen++;
    if (!data) {
      if (!pane.querySelector('.space-skel')) skeleton();
    } else {
      // Back on show: the graphs are measured again, then counted on from where they were.
      requestAnimationFrame(function () { Object.keys(charts).forEach(function (k) { charts[k].w = 0; }); paint(false); });
    }
    tick();
    loadHistory(period);
  }

  function stop() {
    if (!on) return;
    on = false;
    gen++;
    clearTimeout(timer);
    clearTimeout(histTimer);
    if (big) shrink(false);
  }

  function skeleton() {
    pane.textContent = '';
    var box = el('div', 'space-skel');
    box.setAttribute('aria-busy', 'true');
    var sk = function (cls) { return el('span', 'skel ' + cls); };
    add(box, sk('space-skel__line'), sk('act-skel__tiles'), sk('act-skel__graph'));
    for (var i = 0; i < 3; i++) box.append(sk('space-skel__row'));
    box.append(el('span', 'sr-only', 'Loading'));
    pane.append(box);
  }

  document.addEventListener('visibilitychange', function () {
    if (!on) return;
    // A new generation, so an answer still on its way does not ask again while hidden.
    gen++;
    clearTimeout(timer);
    clearTimeout(histTimer);
    if (!document.hidden) { tick(); loadHistory(period); }
  });

  /* ── One graph, grown into the panel ── */

  var GROWN = {
    net: { title: 'Network', legend: [['act-c--dl', 'Down'], ['act-c--up', 'Up']] },
    cpu: { title: 'CPU by app' },
    mem: { title: 'Memory by app' }
  };

  /* A crosshair's values, said once the finger or the keys rest. */
  var sayTimer = null;
  function say(lines) {
    if (!big) return;
    clearTimeout(sayTimer);
    sayTimer = setTimeout(function () {
      if (!big) return;
      big.live.textContent = lines.when + ': ' + lines.rows.map(function (r) { return r.name + ' ' + r.value; }).join(', ');
    }, 250);
  }

  /* Memory up the side of a grown graph, all in GB so its foot reads 0 GB. */
  function gbTick(b) { var g = b / GIB; return (g >= 10 || g === 0 ? Math.round(g) : g.toFixed(1)) + '\u00a0GB'; }

  function axisTicks(max, fmt, n) {
    var out = [];
    for (var i = n; i >= 0; i--) out.push(fmt(max * i / n));
    return out;
  }

  function grow(name) {
    if (big || !charts[name]) return;
    var from = charts[name];
    var spec = GROWN[name];
    var layer = el('div', 'act-big');
    layer.setAttribute('role', 'group');
    layer.setAttribute('aria-label', spec.title + ', larger');
    var head = el('div', 'act-big__head');
    var title = el('h3', 'act-big__title', spec.title);
    var note = el('span', 'act-big__note');
    var close = el('button', 'act-big__close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Show smaller');
    close.append(icon('i-compress'));
    close.addEventListener('click', function () { shrink(true); });
    add(head, add(el('div', 'act-big__titles'), title, note), close);
    var periods = periodControl();
    var chart = new Chart(name + '-big', from.kind, { tip: from.opts.tip, said: say, big: true });
    chart.box.tabIndex = 0;
    chart.box.setAttribute('role', 'img');
    var plot = el('div', 'act-big__plot');
    var yAxis = el('div', 'act-big__y');
    yAxis.setAttribute('aria-hidden', 'true');
    var grid = el('div', 'act-big__grid');
    grid.setAttribute('aria-hidden', 'true');
    var xAxis = el('div', 'act-big__x');
    xAxis.setAttribute('aria-hidden', 'true');
    add(plot, grid, chart.box, yAxis);
    var legend = el('ul', 'act-big__legend');
    var live = el('p', 'sr-only');
    live.setAttribute('aria-live', 'polite');
    var help = el('p', 'act-big__help', 'Drag along the graph, or use the arrow keys, to read it.');
    add(layer, head, periods, plot, xAxis, legend, help, live);
    chart.box.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); chart.step((e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 10 : 1)); }
      else if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); chart.show(e.key === 'Home' ? 0 : chart.xs.length - 1); }
    });
    chart.box.addEventListener('blur', function () { chart.point(null); });

    var lift = toArray(panel.querySelectorAll(':scope > .space__head, :scope > .space__panes, :scope > .space__foot'));
    lift.forEach(function (n) { n.inert = true; });
    panel.append(layer);
    panel.classList.add('has-big');
    big = {
      name: name,
      layer: layer,
      chart: chart,
      from: from,
      live: live,
      lift: lift,
      paint: function () {
        var m = name === 'net' ? netModel() : stackModel(name === 'cpu' ? 0 : 1);
        if (chart.m && period.id === 'live' && chart.m.at.length === m.at.length && !chart._raf) { chart.draw(m); chart.slide(); } else morph(chart, m);
        var c = collecting();
        setText(note, period.note + (c ? ' · ' + lower(c) : ''));
        chart.empty.hidden = !c;
        chart.empty.textContent = c || '';
        chart.box.setAttribute('aria-label', spec.title + ', ' + period.note + '. Use the arrow keys to read it.');
        var fmt = name === 'net' ? rate : name === 'cpu' ? pct : memFmt();
        var ticks = name === 'net' ? [rate(m.max), rate(m.max / 2), '0', rate(m.upMax)]
          : axisTicks(m.max, name === 'mem' && memTotal() ? gbTick : fmt, 4);
        yAxis.replaceChildren.apply(yAxis, ticks.map(function (t, i) { var s = el('span', 'act-big__tick', t); s.style.setProperty('--at', name === 'net' ? [0, 0.345, 0.69, 1][i] : i / 4); return s; }));
        if (!grid.children.length) {
          (name === 'net' ? [0, 0.345, 0.69, 1] : [0, 0.25, 0.5, 0.75, 1]).forEach(function (f) { var l = el('span', 'act-big__line'); l.style.setProperty('--at', f); grid.append(l); });
        }
        var wnd = windowOf();
        var xs = [];
        for (var i = 0; i <= 4; i++) xs.push(i === 4 ? 'Now' : tickLabel(wnd.start + (wnd.end - wnd.start) * i / 4, i));
        xAxis.replaceChildren.apply(xAxis, xs.map(function (t) { return el('span', '', t); }));
        var items = name === 'net'
          ? [['act-c--dl', 'Down', rate(data.point.down)], ['act-c--up', 'Up', rate(data.point.up)]]
          : APPS.filter(function (a) { return appsOf(data.point)[a.id] && (name === 'cpu' || memTotal() || app(data.point, a.id, 1) !== null); }).map(function (a) { return ['act-c--' + a.id, a.name, fmt(app(data.point, a.id, name === 'cpu' ? 0 : 1))]; })
            .concat(whole(data.point, name === 'cpu' ? 0 : 1) !== null ? [['act-c--other', data.sources.docker === 'ok' ? 'Everything else' : 'Other apps', fmt(elseOf(data.point, name === 'cpu' ? 0 : 1))]] : []);
        legend.replaceChildren.apply(legend, items.map(function (it) {
          var li = el('li', 'act-key ' + it[0]);
          var dot = el('span', 'space-dot');
          dot.setAttribute('aria-hidden', 'true');
          add(li, dot, el('span', 'act-key__name', it[1]), el('span', 'act-key__val', it[2]));
          return li;
        }));
      }
    };
    paintPeriods(false);
    requestAnimationFrame(function () {
      big.paint();
      paintPeriods(false);
      chart.box.focus({ preventScroll: true });
      if (reduce() || !layer.animate) return;
      var a = from.box.getBoundingClientRect();
      var b = chart.box.getBoundingClientRect();
      var sx = a.width / Math.max(1, b.width);
      var sy = a.height / Math.max(1, b.height);
      var dx = a.left - b.left;
      var dy = a.top - b.top;
      chart.box.animate([
        { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + sx + ',' + sy + ')' },
        { transform: 'none' }
      ], { duration: 420, easing: 'cubic-bezier(.32,.72,0,1)' });
      layer.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: 'ease-out' });
      toArray(layer.children).forEach(function (n, i) {
        if (n === plot) return;
        n.animate([{ opacity: 0, transform: 'translateY(' + (8 + i * 2) + 'px)' }, { opacity: 1, transform: 'none' }], { duration: 360, delay: 60 + i * 25, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards' });
      });
    });
  }

  function shrink(focusBack) {
    if (!big) return;
    var was = big;
    big = null;
    was.lift.forEach(function (n) { n.inert = false; });
    // The tab's panes but the one on show stay out of reach, as space.js keeps them.
    toArray(root.querySelectorAll('.space-pane:not(.is-on)')).forEach(function (n) { n.inert = true; });
    panel.classList.remove('has-big');
    var target = was.from.box.parentNode;
    function gone() { was.layer.remove(); }
    if (reduce() || !was.layer.animate) gone();
    else {
      var a = target.getBoundingClientRect();
      var b = was.chart.box.getBoundingClientRect();
      was.chart.box.animate([
        { transform: 'none' },
        { transform: 'translate(' + (a.left - b.left) + 'px,' + (a.top - b.top) + 'px) scale(' + a.width / Math.max(1, b.width) + ',' + a.height / Math.max(1, b.height) + ')' }
      ], { duration: 320, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'forwards' });
      was.layer.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 320, easing: 'ease-in', fill: 'forwards' }).onfinish = gone;
    }
    if (focusBack) target.focus({ preventScroll: true });
  }

  // Escape puts a grown graph back before it closes the panel (space.js leaves a handled one).
  root.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && big && !e.defaultPrevented) { e.preventDefault(); shrink(true); }
  });

  window.addEventListener('resize', function () {
    if (!on || !data) return;
    Object.keys(charts).forEach(function (k) { charts[k].w = 0; });
    paintPeriods(false);
    paintCharts(false);
  });

  // Opened at Activity by its address before this file ran.
  var tabNow = byId('space-tab-activity');
  if (root.classList.contains('is-open') && tabNow && tabNow.getAttribute('aria-selected') === 'true') start();

  watch.activity = { shown: function (yes) { if (yes) start(); else stop(); }, periods: PERIODS, slots: slots, choose: choose, grow: grow, shrink: shrink, isGrown: function () { return !!big; } };
})();
