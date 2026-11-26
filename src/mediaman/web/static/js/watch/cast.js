/* Watch: the people of a title, in its detail, on MMWatch.cast. Resting on one,
 * or coming to one by the keyboard, opens a card out of their photo; a press
 * opens their page (watch/explore.js), to which the photo flies, and from which
 * it flies home. What a card says is asked for once a person, two at a time.
 * Needs watch/core.js; used by watch/detail.js, which holds the overlay.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch && watch.core;
  var root = document.getElementById('ttl');
  if (!core || !root) return;

  var el = core.el;
  var toArray = core.toArray;

  var SHOWN = 12;
  /* How long the pointer or the focus rests on a person before their card opens,
     while none is open and while one is or just was. */
  var INTENT_MS = 280;
  var WARM_MS = 70;
  var WARM_FOR_MS = 400;
  /* Passing along an open card, a person is asked about only if rested on. */
  var ASK_MS = 140;
  var GRACE_MS = 160;
  var QUIET_MS = 200; // after a scroll, people come under a pointer that does not move
  var FAILED_MS = 60000;
  var EDGE = 12;
  var PHOTO = 88; // the card's photo, and what stands around it
  var PAD = 18;

  var people = [];    // those on show: { id, key, name, role, photo, item, avatar, face }
  var live = null;    // whose card is open
  var folding = null; // whose card is closing
  var armed = null;
  var armTimer = null;
  var closeTimer = null;
  var askTimer = null;
  var warmUntil = 0;
  var quietUntil = 0;
  var byKeys = false;

  function now() { return Date.now(); }

  /* Run *fn* once what moves on *nodes* has ended. */
  function whenStill(nodes, fn) {
    var moving = nodes.reduce(function (all, n) { return all.concat(n.getAnimations ? n.getAnimations() : []); }, []);
    Promise.all(moving.map(function (a) { return a.finished.catch(function () {}); })).then(fn);
  }

  /* ── The strip ── */

  function initials(name) {
    return (name || '?').split(/\s+/).map(function (w) { return w.charAt(0); }).slice(0, 2).join('');
  }

  /* A round photo, or the initials where there is none or it does not load. */
  function face(name, url, lazy) {
    var node = el('span', 'cast-face');
    var src = core.safePath(url);
    if (src) {
      var img = el('img');
      img.alt = '';
      if (lazy) img.loading = 'lazy';
      img.setAttribute('data-broken-on-error', '');
      img.setAttribute('src', src);
      node.appendChild(img);
    }
    node.appendChild(el('span', 'cast-face__letters', initials(name)));
    return node;
  }

  /* The section of *cast*, as a title's reply lists them. */
  function section(cast) {
    drop();
    var sec = el('section', 'ttl__castsec');
    sec.appendChild(el('h3', 'ttl__sec-title', 'Cast'));
    var list = el('ul', 'ttl-cast');
    list.setAttribute('aria-label', 'Cast');
    people = cast.slice(0, SHOWN).map(function (c, i) { return person(c, 'cast-' + i); });
    people.forEach(function (p) {
      var li = el('li');
      li.appendChild(p.item);
      list.appendChild(li);
    });
    sec.appendChild(list);
    return sec;
  }

  function person(c, key) {
    var id = /^[0-9]{1,18}$/.test(String(c.person_id)) ? String(c.person_id) : null;
    var p = { id: id, key: key, name: c.name || '', role: c.character || '', photo: c.profile_url };
    p.item = el(id ? 'button' : 'div', 'ttl-cast__item');
    p.avatar = el('span', 'ttl-cast__avatar');
    p.avatar.setAttribute('aria-hidden', 'true');
    p.face = face(p.name, p.photo, true);
    p.avatar.appendChild(p.face);
    p.item.appendChild(p.avatar);
    p.item.appendChild(el('span', 'ttl-cast__name', p.name));
    if (p.role) p.item.appendChild(el('span', 'ttl-cast__char', p.role));
    if (!id) return p;
    p.item.type = 'button';
    p.item.setAttribute('data-ttl-act', key);
    p.item.addEventListener('click', function () { openPerson(p); });
    if (flight && flight.home === key) p.item.classList.add('is-out', 'is-away');
    return p;
  }

  function personAt(node) {
    var item = node && node.closest ? node.closest('.ttl-cast__item') : null;
    return item ? people.filter(function (p) { return p.item === item && p.id; })[0] || null : null;
  }

  /* ── What is known of them ── */

  var known = {};  // by id: the reply
  var failed = {}; // by id: { until, gone }, a failure that is not asked again before
  var asking = new Set(); // whose answers are on their way
  var ASKING_MOST = 2;

  function stateOf(p) {
    if (known[p.id]) return 'full';
    return failed[p.id] && now() < failed[p.id].until ? 'failed' : 'loading';
  }

  function askIn(ms) {
    clearTimeout(askTimer);
    askTimer = setTimeout(ask, ms);
  }

  function dontAsk() {
    clearTimeout(askTimer);
    askTimer = null;
  }

  /* Ask about whoever's card is open, unless too many answers are on their way:
     the one whose card is open by the next is asked about then. */
  function ask() {
    dontAsk();
    if (!live || stateOf(live) !== 'loading' || asking.has(live.id) || asking.size >= ASKING_MOST) return;
    var id = live.id;
    asking.add(id);
    watch.explore.person(id).then(function (reply) {
      if (reply && reply.person) known[id] = reply;
      else failed[id] = { until: Infinity, gone: true };
    }, function (err) {
      var gone = !!err && err.status === 404;
      failed[id] = { until: gone ? Infinity : now() + FAILED_MS, gone: gone };
    }).then(function () {
      asking.delete(id);
      if (!live) return;
      if (live.id === id) draw(live);
      else ask();
    });
  }

  function born(p) { return watch.explore.born(known[p.id].person); }

  /* ── The card: one for the whole strip, in the overlay's scroller so that it
     scrolls with it. Laid out at its full size; closed, it is moved, shrunk and
     clipped until its photo is the one in the strip. ── */

  var card = el('div', 'cast-card');
  var shadow = el('div', 'cast-card__shadow');
  var shell = el('div', 'cast-card__shell');
  // Its buttons are shortcuts for the mouse; a person read aloud has the words of it.
  card.setAttribute('aria-hidden', 'true');
  card.hidden = true;
  card.appendChild(shadow);
  card.appendChild(shell);
  var words = el('p', 'sr-only');
  words.id = 'ttl-cast-words';
  words.setAttribute('role', 'tooltip');
  root.appendChild(card);
  root.appendChild(words);

  function shortcut(cls, text) {
    var b = el('button', cls, text);
    b.type = 'button';
    b.tabIndex = -1;
    return b;
  }

  function line(width) {
    var s = el('span', 'skel skel--line');
    s.style.width = width;
    return s;
  }

  function biography(p, state) {
    var f = state === 'full' ? known[p.id].person : null;
    var node = el('p', 'cast-card__bio in in--2', f ? f.biography || 'No biography yet.' : '');
    if (f && !f.biography) node.classList.add('is-none');
    if (!f) ['100%', '72%'].forEach(function (w) { node.appendChild(line(w)); });
    return node;
  }

  function knownFor(p, state) {
    var node = el('div', 'cast-card__known in in--3');
    node.appendChild(el('p', 'cast-card__label', 'Known for'));
    var row = el('div', 'cast-card__posters');
    var titles = state === 'full' ? (known[p.id].known_for || []).slice(0, 4) : [];
    if (state !== 'full') for (var i = 0; i < 4; i++) row.appendChild(el('span', 'cast-card__poster skel'));
    titles.forEach(function (t) {
      var b = shortcut('cast-card__poster');
      b.title = t.title;
      b.setAttribute('aria-label', t.title);
      var src = core.safePath(t.poster_url);
      if (src) {
        var img = el('img');
        img.alt = '';
        img.setAttribute('data-broken-on-error', '');
        img.setAttribute('src', src);
        b.appendChild(img);
      }
      b.appendChild(el('span', 'cast-card__poster-name', t.title));
      b.addEventListener('click', function () {
        drop();
        watch.detail.open(t.library_ref || t.ref, { title: t.title, kind: t.kind });
      });
      row.appendChild(b);
    });
    if (state === 'full' && !titles.length) {
      node.classList.add('is-none');
      row.appendChild(el('p', '', 'No titles listed yet.'));
    }
    node.appendChild(row);
    return node;
  }

  function sorry(p) {
    var node = el('div', 'cast-card__failed in in--2');
    node.appendChild(el('p', '', 'Couldn’t load more about ' + (p.name.split(' ')[0] || 'them') + '.'));
    if (failed[p.id].gone) return node;
    var again = shortcut('cast-card__again', 'Try again');
    again.addEventListener('click', function () {
      delete failed[p.id];
      draw(p);
      ask();
    });
    node.appendChild(again);
    return node;
  }

  function pane(p, state) {
    var node = el('div', 'cast-card__pane');
    var head = el('div', 'cast-card__head');
    head.appendChild(face(p.name, p.photo));
    var text = el('div', 'cast-card__text in');
    text.appendChild(el('p', 'cast-card__name', p.name));
    if (p.role) text.appendChild(el('p', 'cast-card__role', 'as ' + p.role));
    var years = el('p', 'cast-card__born', state === 'full' ? born(p) : '');
    if (state === 'loading') years.appendChild(line('110px'));
    text.appendChild(years);
    head.appendChild(text);
    node.appendChild(head);
    if (state === 'failed') {
      node.appendChild(sorry(p));
    } else {
      node.appendChild(biography(p, state));
      node.appendChild(knownFor(p, state));
    }
    node.appendChild(shortcut('btn watch-btn--grey cast-card__all in in--3', 'See all titles'));
    node.addEventListener('mousedown', function (e) { e.preventDefault(); }); // the focus stays
    node.addEventListener('click', function (e) {
      if (!e.target.closest('button.cast-card__poster, .cast-card__again')) openPerson(p);
    });
    return node;
  }

  var shown = null; // the pane on show: { node, of, state }

  /* The card's contents for *p*. The same person's details arriving: the photo
     and the name stay and the rest is replaced under them. Another person's
     fade in where the last one's fade out. */
  function draw(p) {
    var state = stateOf(p);
    var fresh = pane(p, state);
    words.textContent = [
      p.role && 'as ' + p.role, state === 'full' && born(p), state === 'full' && known[p.id].person.biography
    ].filter(Boolean).map(function (t) { return /[.!?]$/.test(t) ? t : t + '.'; }).join(' ');
    if (shown && shown.of === p) {
      var node = shown.node;
      node.querySelector('.cast-card__born').replaceWith(fresh.querySelector('.cast-card__born'));
      toArray(node.children).slice(1, -1).forEach(function (n) { n.remove(); });
      toArray(fresh.children).slice(1, -1).forEach(function (n) {
        n.classList.remove('in');
        n.classList.add('is-arriving');
        node.insertBefore(n, node.lastElementChild);
      });
      shown.state = state;
      return;
    }
    var olds = toArray(shell.children);
    if (isShown()) {
      fresh.classList.add('is-new');
      olds.forEach(function (o) {
        o.classList.add('is-old');
        o.inert = true;
      });
      void shell.offsetWidth;
      whenStill(olds, function () { olds.forEach(function (o) { o.remove(); }); });
    } else {
      olds.forEach(function (o) { o.remove(); });
    }
    shell.appendChild(fresh);
    shown = { node: fresh, of: p, state: state };
  }

  function isShown() { return card.classList.contains('is-open'); }

  /* The box of *node*, in the scroller's own measure. */
  function box(node) {
    var r = node.getBoundingClientRect();
    return { left: r.left, top: r.top + root.scrollTop, width: r.width, height: r.height };
  }

  /* Above the photo, so that the row stays free to move along; under the person
     where there is no room. */
  function place(p) {
    var a = box(p.avatar);
    var below = box(p.item);
    var w = card.offsetWidth;
    var h = shell.offsetHeight;
    var top = root.scrollTop + EDGE;
    var bottom = root.scrollTop + root.clientHeight - EDGE;
    var x = Math.max(EDGE, Math.min(root.clientWidth - EDGE - w, a.left + a.width / 2 - w / 2));
    var y = a.top - EDGE - h;
    if (y < top) {
      y = below.top + below.height + 8;
      if (y + h > bottom) y = Math.max(top, Math.min(bottom - h, a.top - EDGE - h));
    }
    card.style.setProperty('--to', 'translate(' + Math.round(x) + 'px,' + Math.round(y) + 'px)');
    foldTo(p);
  }

  /* The closed look for *p*: the card's photo exactly on the strip's. */
  function foldTo(p) {
    var a = box(p.avatar);
    var s = p.avatar.offsetWidth / PHOTO;
    var middle = PAD + PHOTO / 2;
    card.style.setProperty('--from', 'translate(' + (a.left + a.width / 2 - middle * s) + 'px,' +
      (a.top + a.height / 2 - middle * s) + 'px) scale(' + s + ')');
    card.style.setProperty('--clip-right', card.offsetWidth - PAD - PHOTO + 'px');
    card.style.setProperty('--clip-bottom', shell.offsetHeight - PAD - PHOTO + 'px');
  }

  function open(p) {
    clearTimeout(closeTimer);
    closeTimer = null;
    if (live === p) return;
    var was = live;
    var outgoing = folding;
    folding = null;
    if (was) was.item.removeAttribute('aria-describedby');
    live = p;
    card.hidden = false;
    if (!shown || shown.of !== p || shown.state !== stateOf(p)) draw(p);
    if (was) {
      card.classList.add('is-passing');
      was.item.classList.remove('is-out');
      place(p);
    } else {
      if (outgoing) outgoing.item.classList.remove('is-out');
      still(function () {
        card.classList.remove('is-passing');
        place(p);
      });
      // Out of another photo than the one it was closing to: its parts start unseen.
      if (outgoing && outgoing !== p) {
        [shadow].concat(toArray(card.querySelectorAll('.in'))).forEach(function (n) {
          (n.getAnimations ? n.getAnimations() : []).forEach(function (a) { a.finish(); });
        });
      }
      card.classList.add('is-open');
    }
    p.item.classList.add('is-out');
    p.item.setAttribute('aria-describedby', words.id);
    askIn(was ? ASK_MS : 0);
  }

  /* Make a *change* to the closed card that is not to be seen moving. */
  function still(change) {
    card.style.transition = shell.style.transition = 'none';
    change();
    void card.offsetWidth;
    card.style.transition = shell.style.transition = '';
    void card.offsetWidth;
  }

  function close() {
    if (!live) return;
    var p = live;
    live = null;
    folding = p;
    warmUntil = now() + WARM_FOR_MS;
    dontAsk();
    p.item.removeAttribute('aria-describedby');
    foldTo(p);
    card.classList.remove('is-open', 'is-passing');
    whenStill([card, shell], function () {
      if (folding !== p) return;
      folding = null;
      p.item.classList.remove('is-out');
      card.hidden = true;
      // What arrived late opens with the rest from now on.
      toArray(card.querySelectorAll('.is-arriving')).forEach(function (n) {
        n.classList.remove('is-arriving');
        n.classList.add('in');
      });
    });
  }

  /* Gone at once: what it stands on is drawn again or goes. */
  function drop() {
    disarm();
    clearTimeout(closeTimer);
    closeTimer = null;
    dontAsk();
    [live, folding].forEach(function (p) {
      if (!p) return;
      p.item.classList.remove('is-out');
      p.item.removeAttribute('aria-describedby');
    });
    live = folding = shown = null;
    if (!card.hidden) still(function () { card.classList.remove('is-open', 'is-passing'); });
    card.hidden = true;
    shell.textContent = '';
  }

  /* Close an open card. Says whether there was one. */
  function dismiss() {
    if (!live) return false;
    disarm();
    close();
    return true;
  }

  /* ── Pointer and keyboard ── */

  function arm(p) {
    clearTimeout(armTimer);
    armed = p;
    armTimer = setTimeout(function () {
      // On a busy page the moves that left the person may still be waiting.
      requestAnimationFrame(function () {
        if (armed !== p) return;
        armed = null;
        if (root.contains(p.item)) open(p);
      });
    }, live || now() < warmUntil ? WARM_MS : INTENT_MS);
  }

  function disarm() {
    clearTimeout(armTimer);
    armed = null;
  }

  function closeSoon() {
    dontAsk();
    if (!live || closeTimer) return;
    closeTimer = setTimeout(function () {
      closeTimer = null;
      close();
    }, GRACE_MS);
  }

  root.addEventListener('pointermove', function (e) {
    if (e.pointerType !== 'mouse') return;
    byKeys = false;
    var p = personAt(e.target);
    if (p || card.contains(e.target)) {
      clearTimeout(closeTimer);
      closeTimer = null;
      if (!p || p === live) {
        disarm();
        if (!askTimer) askIn(ASK_MS);
      } else if (p !== armed && now() > quietUntil) arm(p);
      return;
    }
    disarm();
    closeSoon();
  });
  root.addEventListener('pointerleave', function () {
    disarm();
    closeSoon();
  });
  root.addEventListener('scroll', function () {
    quietUntil = Math.max(quietUntil, now() + QUIET_MS);
    if (byKeys) return;
    disarm();
    close();
  }, { passive: true });
  window.addEventListener('resize', function () {
    disarm();
    close();
  });
  root.addEventListener('focusin', function (e) {
    var p = personAt(e.target);
    if (!p || now() < quietUntil || !p.item.matches(':focus-visible')) return;
    byKeys = true;
    arm(p);
  });
  root.addEventListener('focusout', function (e) {
    if (!personAt(e.target) || personAt(e.relatedTarget)) return;
    disarm();
    close();
  });

  /* ── The photo flies to the person's page, and home from it ── */

  var flight = null; // the photo in the air: { node, home, turn }

  function at(r) { return 'translate(' + r.left + 'px,' + r.top + 'px) scale(' + r.width / PHOTO + ')'; }

  function inSight(r) { return r.width > 0 && r.top + r.height > 0 && r.top < window.innerHeight; }

  /* From the box *from* to the box *to*, or from wherever it is in the air. */
  function fly(p, from, to, home, done) {
    if (core.reduceMotion() || !inSight(to) || !(flight || inSight(from))) {
      if (flight) flight.node.remove();
      flight = null;
      done();
      return;
    }
    if (flight) {
      flight.home = home;
      flight.done();
    } else {
      flight = { node: p.face.cloneNode(true), home: home, turn: 0 };
      flight.node.classList.add('cast-fly');
      flight.node.setAttribute('aria-hidden', 'true');
      flight.node.style.transition = 'none';
      flight.node.style.transform = at(from);
      document.body.appendChild(flight.node);
      void flight.node.offsetWidth;
      flight.node.style.transition = '';
    }
    var mine = flight;
    var turn = ++mine.turn;
    mine.done = done;
    mine.node.style.transform = at(to);
    whenStill([mine.node], function () {
      if (flight !== mine || mine.turn !== turn) return;
      flight = null;
      mine.node.remove();
      done();
    });
  }

  function openPerson(p) {
    if (!watch.explore) return;
    var held = live === p && shown ? shown.node.querySelector('.cast-face') : p.face;
    var from = held.getBoundingClientRect();
    var facts = known[p.id] && known[p.id].person;
    drop();
    // What Back will find the focus on; Safari leaves a pressed button without it.
    quietUntil = now() + QUIET_MS;
    p.item.focus({ preventScroll: true, focusVisible: false });
    watch.explore.openPerson({ person_id: p.id, name: p.name, job: (facts && facts.job) || null, profile_url: p.photo }, null);
    var photo = document.querySelector('#watch-results .explore-photo');
    if (!photo) return;
    photo.style.visibility = 'hidden';
    fly(p, from, photo.getBoundingClientRect(), null, function () { photo.style.visibility = ''; });
  }

  /* Back in the detail from a page that *key* opened: their photo comes home from it. */
  function land(key) {
    quietUntil = now() + core.tokenMs('--dur-fast', 150) * 2 + QUIET_MS;
    var p = people.filter(function (one) { return one.key === key; })[0];
    if (!p) return;
    var page = document.querySelector('.is-leaving .explore-title');
    var photo = page && page.textContent === p.name && document.querySelector('.is-leaving .explore-photo');
    if (!photo && !flight) return;
    var from = photo && photo.getBoundingClientRect();
    p.item.classList.add('is-out', 'is-away');
    fly(p, from, homeOf(p), key, function () {
      people.forEach(function (one) {
        if (one.key !== key) return;
        one.item.classList.remove('is-away');
        if (one !== live) one.item.classList.remove('is-out');
      });
    });
    if (photo && flight) photo.style.visibility = 'hidden';
  }

  /* Where the strip's photo will be once the sheet is back: no transform moves
     what layout says. */
  function homeOf(p) {
    var r = { left: 0, top: -root.scrollTop, width: p.avatar.offsetWidth, height: p.avatar.offsetHeight };
    for (var n = p.avatar; n && n !== root; n = n.offsetParent) {
      r.left += n.offsetLeft;
      r.top += n.offsetTop;
      if (n !== p.avatar) {
        r.left += n.clientLeft;
        r.top += n.clientTop;
      }
    }
    return r;
  }

  watch.cast = { section: section, drop: drop, dismiss: dismiss, land: land };
})();
