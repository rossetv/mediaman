/* Watch page: how a card's preview becomes its title's page under a scroll, on
 * MMWatch.carry. One number, the place, is 0 at the preview and 1 at the page:
 * motion() moves it with the scroll, stage() draws it. All is measured once; from
 * then on it is transforms, opacity and clips, so that it can follow a finger.
 * The preview's media is never moved in the page, as its trailer would load anew:
 * it is drawn over the page's hero where it stands.
 * Needs watch/core.js; watch/preview.js says when, and has the page drawn (watch/detail.js).
 */
(function () {
  'use strict';

  var core = window.MMWatch && window.MMWatch.core;
  var root = document.getElementById('ttl');
  if (!core || !root) return;

  var el = core.el;
  var toArray = core.toArray;

  var FULL = 400;       // px of scrolling for the whole way, were it all done by hand
  var OPENS = 140;      // px after which it goes the rest of it by itself
  var FOLLOW = 34;      // the spring, by hand and by itself (1/s)
  var SETTLE = 18;
  var NEAR = 0.0016;    // of the way: it is there
  var LONGEST = 0.05;   // s: a frame a busy page gave late

  function now() { return performance.now(); }
  function mix(a, b, at) { return a + (b - a) * at; }
  function ramp(at, a, b) { return Math.max(0, Math.min(1, (at - a) / (b - a))); }

  /* ── The place ──
     A critically damped spring after the hand, or to one of the ends. *draw* is told each place,
     *end* that it stands at 0 or 1. */
  function motion(draw, end) {
    var place = 0;
    var speed = 0;
    var pushed = 0;   // px
    var target = 0;
    var byHand = true;
    var down = true;
    var quiet = 0;    // s since the last push, a frame a busy page gave late counted short
    var still = 0;    // s without a push that are the end of it
    var raf = 0;
    var last = 0;

    function tick(t) {
      raf = 0;
      var dt = Math.min(LONGEST, Math.max(0.001, (t - last) / 1000));
      last = t;
      quiet += dt;
      if (byHand && quiet > still) go(pushed >= OPENS && down ? 1 : 0);
      var w = byHand ? FOLLOW : SETTLE;
      var off = place - target;
      // Never past its end, however hard it was thrown.
      if (Math.abs(speed) > w * Math.abs(off) && speed * off < 0) speed = -w * off;
      var fall = Math.exp(-w * dt);
      var b = speed + w * off;
      place = target + (off + b * dt) * fall;
      speed = (speed - w * b * dt) * fall;
      if (place < 0 || place > 1) {
        place = place < 0 ? 0 : 1;
        speed = 0;
      }
      if (!byHand && Math.abs(place - target) < NEAR) {
        draw(target);
        end(target);
        return;
      }
      draw(place);
      wake();
    }

    function wake() {
      if (raf) return;
      raf = window.requestAnimationFrame(tick);
    }

    function go(to) {
      target = to;
      byHand = false;
    }

    function start() {
      if (!raf) last = now();
      wake();
    }

    return {
      /* *dy* px more of a scroll that is over once nothing has come for *rest* ms. */
      push: function (dy, rest) {
        if (!byHand) {
          if (target === 1 && dy > 0) return;
          byHand = true;
          pushed = place * FULL;
        }
        quiet = 0;
        still = rest / 1000;
        if (dy) down = dy > 0;
        pushed = Math.max(0, Math.min(FULL, pushed + dy));
        target = pushed / FULL;
        if (dy > 0 && pushed >= OPENS) go(1);
        start();
      },
      go: function (to) {
        go(to);
        start();
      },
      stop: function () {
        window.cancelAnimationFrame(raf);
        raf = 0;
      }
    };
  }

  /* ── The drawing ── */

  function lineHeight(n) { return parseFloat(getComputedStyle(n).lineHeight) || n.getBoundingClientRect().height; }

  function fontSize(n) { return parseFloat(getComputedStyle(n).fontSize); }

  /* The box of what *n* holds. */
  function insideOf(n) {
    var range = document.createRange();
    range.selectNodeContents(n);
    return range.getBoundingClientRect();
  }

  /* The first of *n*'s words that are not blank, or null. */
  function firstWords(n) {
    var walk = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
    for (var t = walk.nextNode(); t; t = walk.nextNode()) if (/\S/.test(t.data)) return t;
    return null;
  }

  /* Where the words of *n* start, past an icon and the room beside it. */
  function wordsLeft(n) {
    var t = firstWords(n);
    if (!t) return insideOf(n).left;
    var range = document.createRange();
    range.setStart(t, t.data.search(/\S/));
    range.setEnd(t, t.data.length);
    return range.getBoundingClientRect().left;
  }

  /* Something that stands on the first line of *n*. */
  function markLine(n) { return n.insertBefore(el('span', 'cry-mark'), n.firstChild); }

  /* How far below its top that line is. */
  function baseline(n) { return n.firstChild.getBoundingClientRect().bottom - n.getBoundingClientRect().top; }

  /* Each word of *n* in a box of its own; *cls*, if given, for one that stays in the line. */
  function splitWords(n, cls) {
    var out = [];
    var parts = n.textContent.split(/(\s+)/);
    n.textContent = '';
    parts.forEach(function (s) {
      if (/^\s*$/.test(s)) n.appendChild(document.createTextNode(s));
      else out.push(n.appendChild(el('span', cls || 'cry-word', s)));
    });
    return out;
  }

  /* Where word *i* of the boxes *pb* stands at the card, scaled by *s*: as far from the word before it
     on its line as on the page, else where its line is, *home*. *at*: where those before it stand. */
  function after(pb, onB, at, i, s, home) {
    var j = i - 1;
    if (j < 0 || onB[j] !== onB[i]) return home;
    return { x: pb[j].x + at[j].x + (pb[i].x - pb[j].x) * s - pb[i].x, y: at[j].y + pb[j].y - pb[i].y };
  }

  /* The line each box is on. */
  function linesOf(boxes) {
    var line = -1;
    var y = null;
    return boxes.map(function (r) {
      if (y === null || r.y > y + 4) {
        line++;
        y = r.y;
      }
      return line;
    });
  }

  /* The preview *p* and the page drawn for it, made ready to be drawn anywhere between the two.
     Returns draw(place) and end(place), which leaves the page as it is drawn without one. */
  function stage(p) {
    var sheet = root.querySelector('.ttl__sheet');
    var hero = root.querySelector('.ttl__hero');
    var dim = document.querySelector('.ttl__dim');
    var o = null;
    var moves = [];
    var undo = [];
    var late = []; // the page's pieces under its title, whose dots come after them
    var hushed = []; // the card's pieces of it, whose words go at once

    function rect(n) {
      var r = n.getBoundingClientRect();
      return { x: r.left - o.left, y: r.top - o.top, w: r.width, h: r.height };
    }
    function shown(n) { return n && n.getClientRects().length ? n : null; }
    function page(sel) { return shown(root.querySelector(sel)); }

    // Everything is put in its place first, and then read: on a page of many cards each layout
    // after a change of what is in it costs as much as all of them.

    // The preview's words go along in the sheet, as a copy, for what is under them to come through.
    var ghost = p.body.cloneNode(true);
    ghost.classList.add('cry-ghost');
    if (p.n.classList.contains('pv--reason')) ghost.classList.add('pv--reason');
    if (p.hint) ghost.appendChild(p.hint.cloneNode(true));
    root.classList.add('is-carried');
    sheet.appendChild(ghost);
    function own(sel) { return shown(ghost.querySelector(sel)); }

    var plate = el('div', 'cry-plate');
    dim.after(plate);
    var shade = hero.insertBefore(el('div', 'cry-shade'), hero.firstChild);
    var veil = shade.appendChild(el('i'));
    var low = veil.appendChild(el('i'));
    var lines = [[ghost.querySelector('.pv__title'), root.querySelector('.ttl__title')], [ghost.querySelector('.pv__meta'), root.querySelector('.ttl__meta')]];
    lines.forEach(function (q) { q.push(q[0].textContent === q[1].textContent); });
    var titleWords = null;
    var titleSaid = null;
    if (lines[0][2]) {
      var named = lines[0][1].textContent;
      undo.push(function () { lines[0][1].textContent = named; });
      titleWords = splitWords(lines[0][1]);
      titleSaid = splitWords(lines[0][0], 'cry-said'); // inline, so that the card's ellipsis stays
    }
    var marks = titleWords ? [] : lines[0].slice(0, 2).map(markLine);
    // Each piece of what the card says under its title in a box of its own, to be found on the page by its words.
    var metas = toArray(ghost.querySelectorAll('.pv__meta'));
    metas.forEach(function (line) {
      toArray(line.querySelectorAll('.pv__grow')).forEach(function (n) {
        var said = n.textContent.split(' · ');
        n.textContent = '';
        said.forEach(function (words, i) {
          if (i) n.appendChild(el('span', 'cry-piece', ' · '));
          n.appendChild(el('span', 'cry-piece', words));
        });
      });
    });
    var texts = [['.pv__reason > p', '.ttl__reason > p:not(.ttl__reason-head, .ttl__because)'], ['.pv__text', '.ttl__overview']].map(function (sels) {
      var a = ghost.querySelector(sels[0]);
      var b = root.querySelector(sels[1]);
      var t = { a: a, b: b, same: !!a && !!b && a.textContent === b.textContent };
      if (!t.same) return t;
      t.real = p.body.querySelector(sels[0]);
      var text = b.textContent;
      undo.push(function () { b.textContent = text; });
      t.wa = splitWords(a);
      t.wb = splitWords(b);
      return t;
    });
    var box = { a: ghost.querySelector('.pv__reason'), b: root.querySelector('.ttl__reason') };
    var under = box.a && box.b ? { n: sheet.appendChild(el('div', 'cry-under')) } : null;
    var underNode = under && under.n;

    // The one read of it all.
    var words = p.body.getBoundingClientRect();
    o = sheet.getBoundingClientRect();
    texts.forEach(function (t) { t.fits = t.same && t.real.scrollHeight <= t.real.clientHeight + 1; });
    Object.assign(ghost.style, { left: words.left - o.left - 1 + 'px', top: words.top - o.top - 1 + 'px', width: words.width + 'px', height: words.height + 'px' });
    // Words in boxes of their own count as more than fits, for Safari, where they just do.
    texts.forEach(function (t) { if (t.fits) t.a.style.display = 'block'; });

    var media = rect(p.media);
    var card = rect(p.shell);
    // What lies over the hero's picture, its crop going from the card's to the hero's.
    var h = rect(hero);
    var ph = Math.max(h.w * media.h / media.w, h.h);
    var pw = ph * media.w / media.h;
    var hx = h.x - (pw - h.w) / 2;
    var hy = h.y - (ph - h.h) * 0.25;
    var lowH = h.y + h.h - rect(page('.ttl__head')).y + 72;
    var endH = Math.min(o.height, window.innerHeight - o.top + 14);

    /* *n* from {x, y, sx, sy} at the card to {x1, y1, s1x, s1y} at the page; fade: the places it
       comes or goes between, or 'gone'; cut: px off the top, sides and foot of a pill at the card, all of it there at the place open. */
    function move(n, d) {
      if (n) moves.push(Object.assign({ n: n, x: 0, y: 0, sx: 1, sy: 1, x1: 0, y1: 0, s1x: 1, s1y: 1, fade: null, cut: null, shut: 0, open: 0.6, via: null, full: 1 }, d, { full: d.fade && !d.word ? Number(getComputedStyle(n).opacity) : 1 }));
    }

    /* A round button into the page's on another row, as its icon: on its own row, where it stands among
       the rest, until the other row comes; then down into it, and open only once there. */
    function travelPill(a, b) {
      var ra = rect(a);
      var rb = rect(b);
      var ia = rect(a.querySelector('.ico'));
      var ib = rect(b.querySelector(b.getAttribute('aria-pressed') === 'true' ? '.watch-mark-btn__on .ico' : '.watch-mark-btn__off .ico') || b);
      var s = ra.h / rb.h;
      var x = ia.x + ia.w / 2 - rb.x - (ib.x + ib.w / 2 - rb.x) * s;
      var y = ia.y + ia.h / 2 - rb.y - (ib.y + ib.h / 2 - rb.y) * s;
      // Each row goes as the first button does: where its own place on the first row will be, and
      // where its row below is on the way.
      var sr = spot.from.h / spot.to.h;
      var via = {
        x: spot.to.x + (rb.x + x - spot.from.x) / sr - rb.x, y: spot.to.y + (rb.y + y - spot.from.y) / sr - rb.y,
        x2: spot.from.x + (rb.x - spot.to.x) * sr - rb.x, y2: spot.from.y + (rb.y - spot.to.y) * sr - rb.y
      };
      move(b, { x: x, y: y, sx: s, sy: s, cut: cutTo(ra, rb, x, y, s), shut: 0.65, open: 0.9, via: via, tint: tintOf(a, b, [0.6, 0.9]) });
      move(a, { fade: 'gone' });
    }

    /* The colours of a button *b* that looks as *a* does until the place *from*, and as itself by *to*. */
    function tintOf(a, b, when) {
      function look(n) { var c = getComputedStyle(n); return [c.backgroundColor, c.color, c.borderTopColor]; }
      return { from: look(a), to: look(b), when: when };
    }

    /* It goes between the places *to* and *from*. */
    function out(n, from, to, d) { move(n, Object.assign({ fade: [from, to] }, d)); }

    /* Where the line *b*, *inset* px in, stands at the card: on the line *a*, at its size; by
       their middles for a row of pieces, else by their first lines. */
    function lineTo(a, b, inset, row) {
      var s = fontSize(a) / fontSize(b);
      var ra = rect(a);
      var rb = rect(b);
      var y = row ? ra.y + ra.h / 2 - rb.y - rb.h * s / 2 : ra.y + baseline(a) - baseline(b) * s - rb.y;
      return { x: ra.x - rb.x - inset * s, y: y, s: s };
    }

    /* A line of text into the page's that says something else: one after the other. */
    function pairText(a, b) {
      var at = lineTo(a, b, 0);
      move(b, { x: at.x, y: at.y, sx: at.s, sy: at.s, fade: [0.12, 0.32] });
      move(a, { x1: -at.x, y1: -at.y, s1x: 1 / at.s, s1y: 1 / at.s, fade: [0.12, 0] });
      return at;
    }

    /* The title, word by word: those the card shows go where they are on the page at once; one it
       cuts short comes after, and a line the page has more comes once what is under it has gone by. */
    function pairWords(a, wa, b, wb) {
      var frame = rect(a);
      var s = fontSize(a) / fontSize(b);
      var pa = wa.map(rect);
      var pb = wb.map(rect);
      var onB = linesOf(pb);
      var lhA = lineHeight(a);
      var lhB = lineHeight(b);
      var dx = pa[0].x - pb[0].x;
      var dy = pa[0].y + (pa[0].h - pb[0].h * s) / 2 - pb[0].y;
      var meta = { from: rect(metas[0]).y, to: rect(lines[1][1]).y };
      var at = [];
      var cut = false;
      pb.forEach(function (r, i) {
        var q = pa[i];
        if (q && !onB[i] && q.x + q.w <= frame.x + frame.w + 1) {
          at[i] = { x: q.x - r.x, y: q.y + (q.h - r.h * s) / 2 - r.y };
          move(wb[i], { x: at[i].x, y: at[i].y, sx: q.w / r.w, sy: s });
          move(wa[i], { fade: 'gone', word: true });
          return;
        }
        if (wa[i]) out(wa[i], 0.08, 0, { word: true });
        cut = true;
        at[i] = after(pb, onB, at, i, s, { x: dx + (r.x - pb[0].x) * (s - 1), y: dy + onB[i] * (lhA - lhB) });
        var y = at[i].y;
        var wait = 0.24;
        // Not on what is under the title, wherever that is on its way.
        for (var t = 0; onB[i] && t < 1; t += 0.02) {
          wait = t + 0.04;
          if (mix(r.y + y + lhA, r.y + r.h, t) <= mix(meta.from, meta.to, t)) break;
        }
        move(wb[i], { x: at[i].x, y: y, sx: s, sy: s, fade: [wait, wait + 0.2], word: true });
      });
      // Its ellipsis is the line's.
      if (cut) out(a, 0.08, 0, { x1: pb[0].x - pa[0].x, y1: pb[0].y - pa[0].y, s1x: 1 / s, s1y: 1 / s });
      return { x: dx, y: dy };
    }

    /* What is said under the title, piece by piece: a piece the card says too comes from where it
       says it, in its place; the rest of the card's go, and the rest of the page's come after them. */
    function pairPieces(b, line) {
      var had = [];
      metas.forEach(function (m) {
        toArray(m.children).forEach(function (n) {
          if (n.classList.contains('pv__grow')) had = had.concat(toArray(n.children));
          else had.push(n);
        });
      });
      had = had.filter(shown);
      var pieces = toArray(b.children).filter(shown);
      var rows = linesOf(pieces.map(rect));
      var next = 0;
      pieces.forEach(function (n, k) {
        var words = n.textContent.trim();
        // In the card's order on the same row, from the first: one out of it would cross the rest on its way.
        var a = next >= 0 ? had.filter(function (h, i) { return i >= next && h.textContent.trim() === words && words && metas.indexOf(h.closest('.pv__meta')) === rows[k]; })[0] : null;
        if (words && !a) next = -1;
        if (!a) {
          move(n, { x: line.x, y: line.y, fade: [0.3, 0.6] });
          return;
        }
        next = had.indexOf(a);
        had.splice(next, 1);
        late.push(n);
        var ia = insideOf(a);
        var ib = insideOf(n);
        var rb = rect(n);
        var s = fontSize(a) / fontSize(n);
        var sx = ia.width / ib.width; // its words as wide as the card's, so that none moves at the first step
        var ax = ia.left - o.left;
        var bx = ib.left - o.left;
        var ay = ia.top - o.top + ia.height / 2;
        var by = ib.top - o.top + ib.height / 2;
        move(n, { x: ax - rb.x - (bx - rb.x) * sx, y: ay - rb.y - (by - rb.y) * s, sx: sx, sy: s });
        // Its words go at once, its dot as the page's come.
        hushed.push(a);
        out(a, 0.15, 0);
      });
      had.forEach(function (n) { out(n, n.textContent === ' · ' ? 0.15 : 0.03, 0); });
    }

    /* How far inside *b*, moved by *x* and *y* and scaled, *a* stands: top, right, foot and left. */
    function cutTo(a, b, x, y, s) {
      return [a.y - b.y - y, b.x + x + b.w * s - a.x - a.w, b.y + y + b.h * s - a.y - a.h, a.x - b.x - x].map(function (d) { return Math.max(0, d / s); });
    }

    /* A button into the one whose words start with its own: their words lie on each other, and the
       rest of them is uncovered. */
    function growPill(a, b) {
      var ra = rect(a);
      var rb = rect(b);
      var inA = wordsLeft(a) - o.left;
      var inB = wordsLeft(b) - o.left;
      var s = fontSize(a) / fontSize(b);
      var x = inA - rb.x - (inB - rb.x) * s;
      var y = ra.y + ra.h / 2 - rb.y - rb.h * s / 2;
      move(b, { x: x, y: y, sx: s, sy: s, cut: cutTo(ra, rb, x, y, s), open: 0.5, tint: tintOf(a, b, [0.15, 0.5]) });
      move(a, { fade: 'gone' });
      return { x: x, y: y };
    }

    /* A button into the page's: round their middles, so that its words are never squeezed. */
    function pairPill(a, b, fadeA, fadeB, open) {
      if (!a || !b) return null;
      var ra = rect(a);
      var rb = rect(b);
      var s = ra.h / rb.h;
      var x = ra.x + ra.w / 2 - rb.x - rb.w * s / 2;
      var y = ra.y - rb.y;
      move(b, { x: x, y: y, sx: s, sy: s, cut: cutTo(ra, rb, x, y, s), fade: fadeB, open: open || 0.6, tint: tintOf(a, b, [0.2, 0.6]) });
      // Gone before the page's comes, it stays where it is, in nobody's way.
      if (fadeA && fadeB && fadeA[0] <= fadeB[0]) move(a, { fade: fadeA });
      else move(a, { x1: rb.x + rb.w / 2 - ra.x - ra.w / s / 2, y1: rb.y - ra.y, s1x: 1 / s, s1y: 1 / s, fade: fadeA });
      return { x: x, y: y };
    }

    var line = lineTo(lines[1][0], lines[1][1], 17, true);
    var title = titleWords ? pairWords(lines[0][0], titleSaid, lines[0][1], titleWords) : pairText(lines[0][0], lines[0][1]);
    move(page('.ttl__kind'), { x: title.x, y: title.y, fade: [0.4, 0.85] });
    pairPieces(lines[1][1], line);
    move(page('.ttl__tagline'), { x: line.x + 17, y: line.y, fade: [0.65, 0.95] });

    // With no wide picture, the poster: the card's, on a blur of itself, is the page's, beside the title.
    var poster = page('.ttl__poster.is-loaded');
    var whole = p.media.querySelector('.pv__whole');
    if (poster && whole) {
      var rp = rect(poster);
      var rw = rect(whole);
      var sp = rw.h / rp.h;
      move(poster, { x: rw.x + (rw.w - rp.w * sp) / 2 - rp.x, y: rw.y - rp.y, sx: sp, sy: sp });
      move(whole, { fade: 'gone' });
    }

    // Buttons
    var acts = toArray(root.querySelectorAll('.ttl__actions > *')).filter(shown);
    var paired = [];
    function pair(sel, b, fadeA, fadeB) {
      var a = own(sel);
      var by = pairPill(a, b, fadeA, fadeB);
      if (by) paired.push(b);
      else if (a) out(a, 0.04, 0); // gone before the page's pass over it
      return by;
    }
    function act(name) { return page('.ttl__actions > [data-ttl-act="' + name + '"]'); }
    var main = act('primary') || (own('.pv__waiting') && acts[0]);
    var go = own('.pv__first');
    var solid = go && !go.matches('.watch-btn--grey, .skel'); // what is under it does not show
    var first;
    if (go && main && !go.matches('.skel') && main.textContent.trim().indexOf(go.textContent.trim()) === 0) {
      paired.push(main);
      first = growPill(go, main);
    } else first = pair('.pv__first', main, solid ? [0.2, 0.1] : [0.25, 0.05], solid ? null : [0.05, 0.25]) || line;
    // Those on its row come out of the card's; the rest keep their places by it.
    var spot = main && go ? { to: rect(main), from: rect(go) } : null;
    function level(b) { return b && (!spot || Math.abs(rect(b).y - spot.to.y) < spot.to.h / 2) ? b : null; }
    // One after the other: the page's words are never under the card's button.
    // The TV button keeps its place by the first, whose words grow beside it.
    out(own('[data-play-on]'), 0.04, 0);
    // The page's Trailer, by the first button too: on the way there it would cross the eye.
    out(own('.pv__trailerbtn'), 0.04, 0);
    // The eye says whether it is watched, so it goes to the page's on whichever row that is.
    var eye = own('.pv__eye');
    var mark = act('mark');
    if (eye && mark && spot && !level(mark)) {
      paired.push(mark);
      travelPill(eye, mark);
    } else pair('.pv__eye', level(mark), [0.12, 0.02], [0.3, 0.5]);
    acts.forEach(function (b) {
      if (paired.indexOf(b) >= 0) return;
      var r = rect(b);
      var s = spot ? spot.from.h / spot.to.h : 0.9;
      move(b, {
        x: spot ? spot.from.x + (r.x - spot.to.x) * s - r.x : first.x, y: spot ? spot.from.y + (r.y - spot.to.y) * s - r.y : first.y,
        sx: s, sy: s, fade: level(b) ? [0.9, 1] : [0.45, 0.8] // on its row, once those coming by it have passed
      });
    });
    var set = page('.rate > .rate__set') || page('.ttl__rate-skel > .skel');
    // One after the other, the page's whole when it is seen: its thumbs are never cut.
    var rated = pairPill(own('.pv__rate'), set, [0.06, 0], [0.3, 0.5], 0.3);
    if (!rated) {
      out(own('.pv__rate'), 0.22, 0);
      move(set, { y: 20, fade: [0.5, 0.85] });
    }
    move(page('.ttl__main .rate__line'), { x: rated ? rated.x : 0, y: rated ? rated.y : 20, fade: [0.5, 0.85] });
    out(own('[data-preview-more]'), 0.04, 0); // gone before the rating passes over it
    out(own('.pv__said'), 0.22, 0, { x1: -first.x, y1: -first.y });
    out(own('.pv__progress'), 0.22, 0);
    out(own('.pv__hint'), 0.1, 0, { y1: 10 });

    // Reading
    box = { a: shown(box.a), b: shown(box.b) };
    if (under && !(box.a && box.b)) {
      under.n.style.display = 'none';
      under = null;
    }
    if (under) {
      var ba = rect(box.a);
      var bb = rect(box.b);
      under.a = ba;
      under.b = bb;
      var la = box.a.querySelector('.ai-label');
      var lb = box.b.querySelector('.ai-label');
      var ra = rect(la);
      var rb = rect(lb);
      move(lb, { x: ra.x - rb.x, y: ra.y - rb.y, sx: ra.w / rb.w, sy: ra.h / rb.h });
      move(la, { fade: 'gone' });
      toArray(box.b.querySelectorAll('.ttl__reason-head > :not(.ai-label), .ttl__because, .skel')).forEach(function (n) {
        move(n, { x: ra.x - rb.x, y: ra.y - rb.y, fade: [0.4, 0.8] });
      });
      out(box.a.querySelector('.pv__skels'), 0.2, 0);
    } else {
      out(box.a, 0.25, 0);
      move(box.b, { y: 40, fade: [0.4, 0.8] });
    }

    var later = [];
    var by = null; // how far the words of the title are from where they will be
    texts.forEach(function (t) {
      var a = shown(t.a);
      var b = shown(t.b);
      if (!t.same || !a || !b) {
        out(a, 0.25, 0);
        move(b, { y: 40, fade: [0.35, 0.75] });
        return;
      }
      var frame = rect(a);
      var wa = t.wa;
      var wb = t.wb;
      var s = fontSize(a) / fontSize(b);
      var pa = wa.map(rect);
      var pb = wb.map(rect);
      if (!pa.length) return;
      var onA = linesOf(pa);
      var onB = linesOf(pb);
      var lhA = lineHeight(a);
      var lhB = lineHeight(b);
      var dx = pa[0].x - pb[0].x;
      var dy = pa[0].y + (pa[0].h - pb[0].h * s) / 2 - pb[0].y;
      by = { x: dx, y: dy };
      var end = frame.y + frame.h + 2;
      var at = [];
      pb.forEach(function (r, i) {
        function inSight(k) { return pa[k].y + pa[k].h <= end; }
        var seen = inSight(i);
        var cut = seen && !t.fits && (i === pa.length - 1 || !inSight(i + 1)); // the ellipsis is on it
        if (seen && !cut && onA[i] === onB[i] && Math.abs(pa[i].x - pa[0].x - (r.x - pb[0].x) * s) < 30) {
          at[i] = { x: pa[i].x - r.x, y: pa[i].y + (pa[i].h - r.h * s) / 2 - r.y };
          later.push([wb[i], { x: at[i].x, y: at[i].y, sx: pa[i].w / r.w, sy: s }]);
          later.push([wa[i], { fade: 'gone', word: true }]);
          return;
        }
        // It changes line: it goes where it was and comes where it will be.
        var wait = seen ? 0.24 : 0.2 + Math.min(0.2, onB[i] * 0.05);
        at[i] = after(pb, onB, at, i, s, { x: dx + (r.x - pb[0].x) * (s - 1), y: dy + onB[i] * (lhA - lhB) });
        later.push([wb[i], { x: at[i].x, y: at[i].y, sx: s, sy: s, fade: [wait, wait + (seen ? 0.26 : 0.4)], word: true }]);
      });
      later.push([a, { x1: pb[0].x - pa[0].x, y1: pb[0].y - pa[0].y, fade: [0.34, 0.1] }]);
    });
    later.forEach(function (q) { move(q[0], q[1]); });
    toArray(ghost.children).forEach(function (n) { if (n.classList.contains('pv__skels')) out(n, 0.25, 0); });

    // What only the page has
    var rise = by || { x: title.x, y: 60 };
    // Between the buttons and what is read, in what room there is between them.
    var foot = acts.reduce(function (y, b) { var r = rect(b); return Math.max(y, r.y + r.h); }, 0);
    var read = page('.ttl__reason') || page('.ttl__overview') || page('.ttl-skel-lines');
    ['.ttl__under', '.ttl__main > .ttl__ratings'].forEach(function (sel) {
      var n = page(sel);
      if (!n) return;
      var r = rect(n);
      var y = rise.y * 0.6;
      if (read && rect(read).y > foot) {
        var from = foot + first.y;
        var to = rect(read).y + rise.y;
        y = from + (to - from) * (r.y - foot) / (rect(read).y - foot) - r.y;
      }
      // Seen once it is clear of what is read, wherever that is on its way.
      var wait = 0.55;
      for (var t = wait; read && t < 1; t += 0.02) {
        wait = t;
        if (r.y + r.h + (1 - t) * y <= rect(read).y + (1 - t) * rise.y - 2) break;
      }
      move(n, { x: rise.x, y: y, fade: [wait, Math.max(wait + 0.2, 0.9)] });
    });
    move(page('.ttl__main > .ttl-skel-lines'), { x: rise.x, y: rise.y, fade: [0.35, 0.75] });
    move(page('.ttl__credit'), { x: rise.x, y: rise.y + 40, fade: [0.5, 0.85] });
    move(page('.ttl__lead > .ttl-facts'), { x: 40, y: rise.y * 0.6, fade: [0.45, 0.85] });
    toArray(root.querySelectorAll('.ttl__body > *')).slice(1).filter(shown).slice(0, 4).forEach(function (n, i) {
      move(n, { x: rise.x * 0.6, y: rise.y * 0.6 + 60 + 30 * i, fade: [0.42 + 0.08 * i, 0.85 + 0.05 * i] });
    });
    move(page('.ttl__bar'), { fade: [0.6, 1] });

    function draw(at) {
      var x = mix(card.x, 0, at);
      var y = mix(card.y, 0, at);
      var w = mix(card.w, o.width, at);
      var ht = mix(card.h, endH, at);
      sheet.style.setProperty('--cry-clip', 'inset(' + y + 'px ' + (o.width - x - w) + 'px ' + (o.height - y - ht) + 'px ' + x + 'px round 12px)');
      Object.assign(plate.style, { width: w + 'px', height: ht + 'px', transform: 'translate(' + (o.left + x) + 'px,' + (o.top + y) + 'px)' });
      plate.style.setProperty('--cry-at', at);
      late.forEach(function (n) { n.style.setProperty('--cry-dot', ramp(at, 0.15, 0.35)); });
      dim.style.opacity = at;
      veil.style.opacity = ramp(at, 0.1, 0.9);

      var s = mix(media.w / pw, 1, at);
      var mx = mix(media.x, hx, at);
      var my = mix(media.y, hy, at);
      var cutT = mix(0, h.y - hy, at);
      var cutX = mix(0, h.x - hx, at);
      var cutB = mix(0, hy + ph - h.y - h.h, at);
      shade.style.transform = 'translate(' + (mx - h.x) + 'px,' + (my - h.y) + 'px) scale(' + s + ')';
      shade.style.clipPath = 'inset(' + cutT / s + 'px ' + cutX / s + 'px ' + cutB / s + 'px)';
      var k = s * pw / media.w;
      var r = 12 / k;
      p.media.style.transform = 'translate(' + (mx - media.x) + 'px,' + (my - media.y) + 'px) scale(' + k + ')';
      p.media.style.clipPath = 'inset(' + cutT / k + 'px ' + cutX / k + 'px ' + cutB / k + 'px round ' + r + 'px ' + r + 'px 0 0)';

      if (under) {
        Object.assign(under.n.style, {
          width: mix(under.a.w, under.b.w, at) + 'px', height: mix(under.a.h, under.b.h, at) + 'px', borderRadius: mix(6, 12, at) + 'px',
          transform: 'translate(' + mix(under.a.x, under.b.x, at) + 'px,' + mix(under.a.y, under.b.y, at) + 'px)'
        });
      }
      moves.forEach(function (q) {
        var st = q.n.style;
        var tx = mix(q.x, q.x1, at);
        var ty = mix(q.y, q.y1, at);
        if (q.via) {
          // With the first row until the second comes, then down into the second.
          // Straight down, clear of the button beside it, and only then along.
          tx = mix(mix(q.x, q.via.x, at), mix(q.via.x2, q.x1, at), ramp(at, 0.56, 0.75));
          ty = mix(mix(q.y, q.via.y, at), mix(q.via.y2, q.y1, at), ramp(at, 0.5, 0.62));
          q.n.classList.toggle('cry-hush', at < q.shut);
        }
        st.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + mix(q.sx, q.s1x, at) + ',' + mix(q.sy, q.s1y, at) + ')';
        if (q.cut) st.clipPath = 'inset(' + q.cut.map(function (px) { return px * (1 - ramp(at, q.shut, q.open)) + 'px'; }).join(' ') + ' round 999px)';
        if (q.tint) {
          var k = Math.round(ramp(at, q.tint.when[0], q.tint.when[1]) * 100);
          ['backgroundColor', 'color', 'borderColor'].forEach(function (prop, i) {
            st[prop] = k >= 100 ? '' : 'color-mix(in srgb, ' + q.tint.to[i] + ' ' + k + '%, ' + q.tint.from[i] + ')';
          });
        }
        if (q.fade === 'gone') st.opacity = 0;
        else if (q.fade) st.opacity = q.full * (q.fade[0] < q.fade[1] ? ramp(at, q.fade[0], q.fade[1]) : 1 - ramp(at, q.fade[1], q.fade[0]));
      });
    }

    /* *at*: where it ended. */
    function end(at) {
      root.classList.remove('is-carried');
      sheet.style.removeProperty('--cry-clip');
      late.forEach(function (n) { n.style.removeProperty('--cry-dot'); });
      dim.style.opacity = '';
      [ghost, plate, shade].concat(marks, underNode || []).forEach(function (n) { n.remove(); });
      moves.forEach(function (q) {
        q.n.classList.remove('cry-move');
        if (!at || q.n !== whole) q.n.style.transform = q.n.style.opacity = q.n.style.clipPath = '';
        if (q.tint) q.n.style.backgroundColor = q.n.style.color = q.n.style.borderColor = '';
        if (q.via) q.n.classList.remove('cry-hush');
      });
      hushed.forEach(function (n) { n.classList.remove('cry-hush'); });
      undo.forEach(function (fn) { fn(); });
    }

    // Read; from here on it is written alone.
    Object.assign(shade.style, { width: pw + 'px', height: ph + 'px' });
    Object.assign(veil.style, { left: h.x - hx + 'px', top: h.y - hy + 'px', width: h.w + 'px', height: h.h + 'px' });
    low.style.height = lowH + 'px';
    if (under) {
      box.a.style.background = box.b.style.background = 'none';
      undo.push(function () { box.b.style.background = ''; });
    }
    moves.forEach(function (q) { q.n.classList.add('cry-move'); });
    hushed.forEach(function (n) { n.classList.add('cry-hush'); });
    p.n.classList.add('is-carried');
    draw(0);
    return { draw: draw, end: end, heroFoot: h.y + h.h };
  }

  window.MMWatch.carry = { OPENS: OPENS, motion: motion, stage: stage };
})();
