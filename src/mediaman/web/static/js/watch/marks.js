/* Watch: the person's own Watched marks and Dismiss, and what their rating does to a pick.
 * A toggle serves every film, episode, season and show that has one, in the library or not,
 * and each change can be undone from its toast. What a change means for the rest of the
 * page is read from the server's own page. Needs watch/core.js, watch/keep.js, ratings.js and
 * watch/card-tools.js, and for an admin's All titles watch/library.js; on window.MMWatch.marks.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var keep = window.MMWatch.keep;
  var byId = core.byId;
  var el = core.el;
  var toArray = core.toArray;

  var WATCHED_URL = '/api/watch/watched/';
  var SEEN_URL = '/api/watch/seen/';
  var DISMISSED_URL = '/api/watch/dismissed/';
  var TITLE_URL = '/api/watch/title/';
  var SEARCH_URL = '/api/watch/search?q=';
  var LEFT_RE = / · [^·]* left$/;
  var EYE = '.card-tool--eye';
  var RATED_MS = 900; // how long a rating is looked at, and comes down, before what it was given in goes
  var RATED_STILL_MS = 650; // with nothing moving
  var MARKED_MS = 600; // how long a mark is looked at before its card leaves Continue watching or moves on there
  var STRONG_NO = -2;

  var page = byId('watch-page');
  if (!page) return;

  function withAttr(root, name, value) {
    return toArray(root.querySelectorAll('[' + name + ']')).filter(function (n) {
      return n.getAttribute(name) === value;
    });
  }

  /* ── What is on the page ── */

  function isContinue(card) { return card.hasAttribute('data-dismiss-ref'); }

  /* The title a card is of: movie-<key> or show-<key>. */
  function refOf(card) {
    var mark = /^(movie|show)\/([0-9]+)$/.exec(card.getAttribute('data-mark') || '');
    if (mark) return mark[1] + '-' + mark[2];
    var link = card.querySelector('[data-title-ref]');
    return card.getAttribute('data-dismiss-ref') || (link && link.getAttribute('data-title-ref')) || '';
  }

  function isPick(card) { return !!card.querySelector('[data-pick-ref]'); }

  function titleOf(card) { return card.querySelector('.watch-card__title').textContent.trim(); }

  /* "Brightwater", "Brightwater, S2 · E4" or "Brightwater, Season 2", for the toasts. */
  function nameOf(card) {
    var which = /^(S\d+ · E\d+|Season \d+)/.exec(card.querySelector('.watch-card__sub').textContent);
    return titleOf(card) + (which ? ', ' + which[1] : '');
  }

  function hasBadge(card) { return !!card.querySelector('.watch-badge--watched'); }

  /* Not one on its way out of its row (is-kept) or of the grid (is-gone). */
  function continueCards(root, ref) {
    return withAttr(root, 'data-dismiss-ref', ref).filter(function (n) {
      return n.classList.contains('watch-card') && !n.classList.contains('is-kept') && !n.classList.contains('is-gone');
    });
  }

  function continues(ref) { return continueCards(document, ref).length > 0; }

  /* The row of *root* that Continue watching is in. */
  function continueRow(root) {
    var card = toArray(root.querySelectorAll('.watch-card[data-dismiss-ref]'))
      .filter(function (n) { return !n.classList.contains('is-kept'); })[0];
    return card ? card.closest('.watch-row') : null;
  }

  /* The banner shown: not one on its way out. */
  function bannerNow() {
    return toArray(document.querySelectorAll('.watch-banner')).filter(function (n) { return !n.hasAttribute('inert'); })[0] || null;
  }

  function isBannerOf(banner, ref) { return !!banner && banner.getAttribute('data-dismiss-ref') === ref; }

  function bannerOf(ref) {
    var banner = bannerNow();
    return isBannerOf(banner, ref) ? banner : null;
  }

  /* What the title of *done* can be changed from, for the focus: the card it was
     changed from, else its Continue watching card or its banner's Dismiss, by where
     the change came from, else (*any*) another card of it. The rows stand hidden
     behind an open grid: only what is shown. */
  function controlOf(ref, done, any) {
    var banner = bannerOf(ref);
    var buttons = banner ? [banner.querySelector('[data-watch-dismiss]')] : [];
    var cards = controlsOf(continueCards(document, ref));
    var all = controlsOf(done.card ? [done.card] : []).concat(done.fromBanner ? buttons.concat(cards) : cards.concat(buttons));
    if (any) all = all.concat(controlsOf(cardsOf(function (card) { return refOf(card) === ref; })));
    return all.filter(canFocus)[0];
  }

  function controlsOf(cards) {
    return cards.map(function (card) { return core.controlIn(card, EYE); }).filter(Boolean);
  }

  function canFocus(node) { return node.getClientRects().length > 0 && !node.closest('[inert]'); }

  /* ── One change at a time ──
     The buttons stay focusable (aria-disabled, not disabled) so focus never drops. */

  var busy = {};
  var afterBusy = {}; // an Undo pressed meanwhile, run once it is not

  function buttonsOf(key) {
    var found = withAttr(document, 'data-mark-toggle', key).concat(withAttr(document, 'data-watch-dismiss', key));
    return found;
  }

  function setBusy(key, on) {
    busy[key] = on;
    buttonsOf(key).forEach(function (b) {
      if (on) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
      b.setAttribute('aria-busy', on ? 'true' : 'false');
    });
    var next = !on && afterBusy[key];
    if (next) {
      delete afterBusy[key];
      next();
    }
  }

  /* ── Drawing a mark ── */

  /* Each page's style is worked out again whenever it is read after a change, which costs as many
     cards as the page has: what the drawing of a mark needs to know is read before it changes anything. */

  var shut = new WeakMap(); // of an eye that waits for its answer: how far it has shut, from 0 to 1
  var sight = null; // while a mark is drawn: whether each of its cards is in sight

  /* Read what drawing the mark *key* of the title *ref* needs, then draw it with *fn*. */
  function readFirst(key, ref, fn) {
    buttonsOf(key).forEach(function (button) {
      var drawn = /^matrix\((?:[^,]+,){3}([^,]+)/.exec(getComputedStyle(button.querySelector('.ico') || button).transform);
      shut.set(button, drawn ? 1 - Math.min(1, Math.max(0, Number(drawn[1]))) : 0);
    });
    sight = new Map();
    cardsOfMark(key).concat(continueCards(document, ref)).forEach(function (card) { sight.set(card, card.getClientRects().length > 0); });
    try { fn(); } finally { sight = null; }
  }

  /* Whether *card* is in sight, with motion. */
  function moving(card) {
    return !core.reduceMotion() && (sight && sight.has(card) ? sight.get(card) : card.getClientRects().length > 0);
  }

  function setToggle(button, watched) {
    var now = watched ? 'true' : 'false';
    if (button.getAttribute('aria-pressed') !== now && button.matches('.card-tool, .card-over__b, .pv__eye')) {
      button.style.setProperty('--shut', String(shut.get(button) || 0));
      shut.delete(button);
      // One still blinking is made to blink again; one done blinking is told so below.
      if (button.classList.contains('is-blink')) {
        button.classList.remove('is-blink');
        void button.offsetWidth;
      }
      button.classList.add('is-blink');
    }
    button.setAttribute('aria-pressed', now);
    if (button.hasAttribute('title')) button.setAttribute('title', core.markAction(watched));
  }

  document.addEventListener('animationend', function (e) {
    if (/^eye-(shuts|opens)$/.test(e.animationName)) e.target.classList.remove('is-blink');
  });

  /* A card's caption; null while it says what an admin's change is doing. */
  function captionOf(card) {
    return card.classList.contains('is-working') ? null : card.querySelector('.watch-card__sub').textContent;
  }

  /* A card's mark, progress bar and caption, as they are now. Returns what puts them back. */
  function snapshot(card) {
    var poster = card.querySelector('.watch-card__poster');
    var badge = poster.querySelector('.watch-badge--watched');
    var bar = poster.querySelector('.watch-bar');
    var text = captionOf(card);
    return function () { draw(card, badge, bar, text); };
  }

  function draw(card, badge, bar, text) {
    var poster = card.querySelector('.watch-card__poster');
    var moves = moving(card);
    [['.watch-badge--watched', badge], ['.watch-bar', bar]].forEach(function (p) {
      var old = poster.querySelector(p[0]);
      if (old === p[1]) return;
      if (old) {
        if (moves) (p[0] === '.watch-bar' ? endBar : liftBadge)(poster, old);
        old.remove();
      }
      if (p[1]) poster.appendChild(p[1]);
    });
    toArray(card.querySelectorAll('[data-mark-toggle]')).forEach(function (b) { setToggle(b, !!badge); });
    if (text !== null && captionOf(card) !== null) core.caption(card.querySelector('.watch-card__sub'), text);
  }

  /* What is left on *poster* for a moment, for the motion alone. */
  function ghost(poster, node, ms) {
    node.setAttribute('aria-hidden', 'true');
    poster.appendChild(node);
    setTimeout(function () { node.remove(); }, ms);
  }

  function liftBadge(poster, badge) {
    var copy = badge.cloneNode(true);
    copy.className = 'watch-badge watch-badge--unwatched';
    copy.style.top = badge.offsetTop + 'px';
    copy.style.left = badge.offsetLeft + 'px';
    copy.style.right = 'auto';
    ghost(poster, copy, 170);
  }

  /* The bar runs to its end. */
  function endBar(poster, bar) {
    var rail = el('span', 'card-barend');
    rail.appendChild(el('i')).style.setProperty('--from', String((Number(bar.value) || 0) / 100));
    ghost(poster, rail, 560);
  }

  /* The lights go down and come up. */
  function lights(card) {
    if (!moving(card)) return;
    ghost(card.querySelector('.watch-card__poster'), el('span', 'card-lights'), 660);
  }

  /* A watched title has nothing left to watch. */
  function setCard(card, watched) {
    var back = snapshot(card);
    var bar = card.querySelector('.watch-bar');
    var text = captionOf(card);
    var badge = card.querySelector('.watch-badge--watched') || core.watchedBadge();
    if (watched && !hasBadge(card)) {
      badge.classList.add('is-new');
      lights(card);
    }
    if (watched) draw(card, badge, null, text && text.replace(LEFT_RE, ''));
    else draw(card, null, bar, text);
    return back;
  }

  function cardsOf(test) {
    return toArray(document.querySelectorAll('.watch-card[data-mark]')).filter(function (card) {
      return !isContinue(card) && test(card);
    });
  }

  var WHOLE_RE = /^(movie|show)\/|^tmdb-/; // a film or a whole show, by its mark

  function markOf(card) { return card.getAttribute('data-mark') || ''; }

  function isWhole(card) { return WHOLE_RE.test(markOf(card)) && !isContinue(card); }

  /* The titles of TMDB that *mark*, of a film or a whole show, is of, as far as the page knows. */
  function tmdbRefsOf(mark) {
    if (/^tmdb-/.test(mark)) return [mark];
    if (!WHOLE_RE.test(mark)) return [];
    return withAttr(document, 'data-mark', mark).map(function (card) { return card.getAttribute('data-tmdb-ref'); })
      .filter(function (ref, i, all) { return ref && all.indexOf(ref) === i; });
  }

  /* What a title was marked as from this page, for a card of it drawn from an older answer. */
  var said = {}; // by its TMDB ref: { watched, at }
  var drawnAt = new WeakMap();
  var LATE_MS = 10000; // how long an answer asked for before a mark may be on its way

  function note(mark, watched) {
    tmdbRefsOf(mark).forEach(function (ref) { said[ref] = { watched: watched, at: Date.now() }; });
  }

  /* A card that has come onto the page. */
  function adopt(card) {
    var ref = card.getAttribute('data-tmdb-ref');
    var told = ref && said[ref];
    var from = drawnAt.get(card) || Date.now();
    drawnAt.set(card, Date.now());
    if (told && told.at > from - LATE_MS && isWhole(card) && hasBadge(card) !== told.watched) setCard(card, told.watched);
  }

  /* The cards that *mark* is drawn on: of the same title, in the library or not. */
  function cardsOfMark(mark) {
    var refs = tmdbRefsOf(mark);
    return cardsOf(function (card) {
      return markOf(card) === mark || (isWhole(card) && refs.indexOf(card.getAttribute('data-tmdb-ref')) >= 0);
    });
  }

  /* Show *mark* as it now is wherever it is drawn. Returns what puts the cards back. */
  function paint(mark, watched) {
    note(mark, watched);
    var backs = cardsOfMark(mark).map(function (card) { return setCard(card, watched); });
    withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, watched); });
    return backs;
  }

  /* A show's change is every season's. */
  function paintSeasons(ref, watched) {
    return cardsOf(function (card) { return /^season\//.test(card.getAttribute('data-mark')) && refOf(card) === ref; })
      .map(function (card) { return setCard(card, watched); });
  }

  /* ── Leaving Continue watching ── */

  /* Details, while it is open over the page. */
  function detailsShown() {
    return document.documentElement.classList.contains('has-ttl') ? byId('ttl') : null;
  }

  /* Whether the focus goes with *node*, or has gone already. Not while Details
     is open over the page: it keeps its own. */
  function focusLost(node) {
    var active = document.activeElement;
    if (detailsShown()) return false;
    return !active || active === document.body || active.tagName === 'MAIN' || !document.body.contains(active) ||
      !!(node && node.contains(active)) || !!active.closest('[inert], .ui-toast');
  }

  /* A card that goes takes its open menu with it. */
  function closeMenu(card) {
    window.MMWatch.cardTools.close(card);
    var trigger = moreMenu && moreMenu.trigger();
    if (trigger && card.contains(trigger)) moreMenu.close(false);
  }

  var SHOW_MS = 1000; // longer than a card's entrance and the scroll to it

  /* Run *putBack*, which puts *card* in *row*, and scroll the row to it if it is out of sight.
     A row that snaps would stay on the card it stood on, so meanwhile it does not. */
  function showInRow(row, card, putBack) {
    var scroller = row.querySelector('.watch-row__scroll');
    scroller.style.scrollSnapType = 'none';
    putBack();
    setTimeout(function () { scroller.style.scrollSnapType = ''; }, SHOW_MS);
    if (!card.getClientRects().length) return;
    var pad = parseFloat(getComputedStyle(scroller).scrollPaddingLeft) || 0;
    var box = scroller.getBoundingClientRect();
    var start = card.getBoundingClientRect().left;
    var before = start - box.left - pad;
    // Its slot may still be opening, but what is in it has its own width.
    var past = start + card.children[0].offsetWidth - (box.right - pad);
    var by = before < 0 ? before : Math.max(0, Math.min(before, past));
    if (by) scroller.scrollBy({ left: by, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* An Undo puts the old nodes back only where they still belong: what another
     change has replaced or taken out since is drawn from the server's page. */

  function tellContinue(ref) {
    document.dispatchEvent(new CustomEvent('mm:watch-continue', { detail: { ref: ref } }));
  }

  function leaveContinue(ref, done) {
    var cards = continueCards(document, ref);
    cards.forEach(function (card) {
      closeMenu(card);
      var lost = focusLost(card);
      var row = card.closest('.watch-row');
      var grid = row ? null : card.parentNode; // the row opened as a grid holds one too
      var taken = keep.fromRow(card, row, card.getClientRects().length > 0, '.card-tool');
      done.rows = true;
      done.undos.push(function () {
        if (grid) {
          if (!continueCards(grid, ref).length) taken.undo();
          return;
        }
        var now = continueRow(document);
        if (now && (now !== row || continueCards(row, ref).length)) return;
        showInRow(row, card, taken.undo);
      });
      // From the banner, the one that takes its place gets the focus.
      if (lost && taken.focus && !done.fromBanner) core.focusSoftly(taken.focus);
      done.beside = taken.focus;
    });
    if (cards.length) tellContinue(ref);
  }

  /* A card of Continue watching shows that it was watched. Returns whether one does. */
  function showWatched(ref, mark, done) {
    return continueCards(document, ref).filter(function (card) {
      if ((done.part && markOf(card) !== mark) || !moving(card)) return false;
      var poster = card.querySelector('.watch-card__poster');
      var bar = poster.querySelector('.watch-bar');
      var own = markOf(card);
      lights(card);
      if (!bar) return true;
      endBar(poster, bar);
      bar.remove();
      done.undos.push(function () {
        if (markOf(card) === own && !poster.querySelector('.watch-bar')) poster.appendChild(bar);
      });
      return true;
    }).length > 0;
  }

  function pictureOf(card) {
    var img = card.querySelector('.watch-card__poster img');
    return img ? img.getAttribute('src') : '';
  }

  var OWN_ATTRS = ['class', 'style', 'tabindex', 'aria-pressed', 'data-level']; // of the page, not of what is drawn

  function syncAttrs(node, to) {
    to.getAttributeNames().forEach(function (name) {
      if (OWN_ATTRS.indexOf(name) < 0) node.setAttribute(name, to.getAttribute(name));
    });
  }

  /* Draw *card* as *to* is, with its picture and its buttons where they are. */
  function redraw(card, to) {
    card.getAttributeNames().forEach(function (name) {
      if (/^data-(keep|leaving)-/.test(name) && !to.hasAttribute(name)) card.removeAttribute(name);
    });
    syncAttrs(card, to);
    syncAttrs(card.querySelector('.watch-card__link'), to.querySelector('.watch-card__link'));
    replacePart(card.querySelector('.watch-card__poster'), to.querySelector('.watch-card__poster'), '.watch-badge--leaving');
    replacePart(card.querySelector('.watch-card__poster'), to.querySelector('.watch-card__poster'), '.watch-bar');
    replacePart(card.querySelector('.watch-card__link'), to.querySelector('.watch-card__link'), '.watch-card__sub');
    core.fadeIn(card.querySelector('.watch-card__sub'));
    var tools = to.querySelectorAll('.card-tool');
    toArray(card.querySelectorAll('.card-tool')).forEach(function (b, i) { syncAttrs(b, tools[i]); });
  }

  /* The card moves on to what the server now has in its place: where it stands, if the picture is the same. */
  function replaceCard(card, fresh, done) {
    closeMenu(card);
    if (pictureOf(card) === pictureOf(fresh) && card.querySelectorAll('.card-tool').length === fresh.querySelectorAll('.card-tool').length) {
      var was = card.cloneNode(true);
      redraw(card, fresh);
      var eye = card.querySelector(EYE);
      if (eye) setToggle(eye, fresh.querySelector(EYE).getAttribute('aria-pressed') === 'true');
      done.rows = true;
      done.undos.push(function () {
        if (!document.body.contains(card) || card.classList.contains('is-kept') || markOf(card) !== markOf(fresh)) return;
        closeMenu(card);
        redraw(card, was);
      });
      return;
    }
    var lost = focusLost(card);
    card.replaceWith(fresh);
    core.fadeIn(fresh);
    done.rows = true;
    done.undos.push(function () {
      if (!document.body.contains(fresh) || fresh.classList.contains('is-kept')) return;
      closeMenu(fresh);
      fresh.replaceWith(card);
      core.fadeIn(card);
    });
    if (lost) refocus(core.controlIn(fresh, EYE));
  }

  /* The server's card that the page lacks, in its place in the row, or with its row, and in that row's open grid. */
  function enterContinue(now, undone) {
    var row = continueRow(document);
    var fresh = document.importNode(row ? now : now.closest('.watch-row'), true);
    if (row) {
      var next = null;
      for (var n = now.nextElementSibling; n && !next; n = n.nextElementSibling) {
        next = continueCards(row, n.getAttribute('data-dismiss-ref'))[0];
      }
      var join = function () { row.querySelector('.watch-row__scroll').insertBefore(fresh, next || null); };
      if (undone) showInRow(row, fresh, join); else join();
    } else {
      var rows = document.querySelector('.watch-rows');
      rows.insertBefore(fresh, rows.children[0] || null);
      window.MMWatch.rows.init(fresh.querySelector('.watch-row__viewport'));
      if (window.MMWatch.expand) window.MMWatch.expand.row(fresh);
    }
    core.fadeIn(fresh);
    var grid = window.MMWatch.grid;
    if (grid && grid.bring) grid.bring(row ? fresh : continueCards(fresh, now.getAttribute('data-dismiss-ref'))[0]);
    tellContinue(now.getAttribute('data-dismiss-ref'));
  }

  function playOf(banner) { return banner.querySelector('.watch-btn--play').getAttribute('href'); }

  function replaceBanner(banner, fresh, done) {
    if (fresh && banner && playOf(fresh) === playOf(banner)) return;
    var lost = banner && focusLost(banner);
    if (fresh) fresh = document.importNode(fresh, true);
    if (!banner) {
      var browse = byId('watch-browse');
      browse.insertBefore(fresh, browse.children[0] || null);
      core.fadeIn(fresh);
      return;
    }
    done.rows = true;
    if (fresh) {
      core.swap(banner, fresh);
      done.undos.push(function () { if (bannerNow() === fresh) core.swap(fresh, banner); });
    } else {
      var unfold = keep.foldAway(banner);
      banner.setAttribute('inert', '');
      done.undos.push(function () {
        if (bannerNow()) return;
        banner.removeAttribute('inert');
        unfold();
      });
    }
    if (!lost) return;
    var to = fresh ? fresh.querySelector('.watch-btn--play') : core.spareFocus();
    if (to) to.focus({ preventScroll: true });
  }

  /* ── Reading the server's page ── */

  function fetchPage(href) {
    return fetch(href, { credentials: 'same-origin', headers: { Accept: 'text/html' } }).then(function (resp) {
      if (!resp.ok || resp.redirected) throw new Error('page ' + resp.status);
      return resp.text();
    }).then(function (html) { return new DOMParser().parseFromString(html, 'text/html'); });
  }

  var changes = 0; // every change and Undo that is in: a page asked for before the latest is out of date
  var latest = {}; // each title's latest mark or Undo of one
  var owed = {}; // the titles changed since the rows last followed: their latest change, and the one to follow the show's cards for

  /* The Watch page's cards and banner of the title *ref*, after a change of it, and of any
     other whose page was dropped. */
  function followRows(ref, done) {
    if (bannerOf(ref) || continueCards(document, ref).length || done.part || done.redraw) {
      owed[ref] = { done: done, part: done.part ? done : owed[ref] && owed[ref].part };
    }
    if (!Object.keys(owed).length) return;
    var asked = changes;
    fetchPage('/').then(function (doc) {
      if (asked !== changes) return;
      var due = owed;
      owed = {};
      Object.keys(due).forEach(function (one) {
        followRef(one, doc, due[one].done);
        if (due[one].part) followShow(one, doc, due[one].part);
      });
    }).catch(function () {
      // The mark is saved; the rows show the rest on the next load.
    });
  }

  function followRef(ref, doc, done) {
    var now = continueCards(doc, ref)[0];
    var here = continueCards(document, ref);
    // Without Plex the server's page lacks what it could not ask Plex about.
    var whole = !doc.querySelector('.watch-notice');
    here.forEach(function (card) {
      if (!now) {
        if (whole) leaveContinue(ref, done);
      } else if (now.getAttribute('data-mark') !== card.getAttribute('data-mark')) {
        replaceCard(card, document.importNode(now, true), done);
      }
    });
    if (now && !here.length) enterContinue(now, done.undone);
    var banner = bannerNow();
    var fresh = doc.querySelector('.watch-banner');
    var own = isBannerOf(banner, ref) && (whole || now || !here.length);
    if (own || isBannerOf(fresh, ref)) replaceBanner(banner, fresh, done);
    if (done.seek && focusLost(null)) refocus(controlOf(ref, done, true));
  }

  /* Bring one card up to date with what the server now says of its title. */
  function bringUp(card, watched, now, done) {
    var said = card.querySelector('.watch-card__sub').textContent.replace(LEFT_RE, '');
    done.undos.push(snapshot(card));
    // A Discover card's caption is its year: only the same caption is brought up to date.
    draw(card, watched ? card.querySelector('.watch-badge--watched') || core.watchedBadge() : null,
      card.querySelector('.watch-bar'), now.replace(LEFT_RE, '') === said ? now : said);
    withAttr(document, 'data-mark-toggle', card.getAttribute('data-mark')).forEach(function (b) { setToggle(b, watched); });
  }

  function tryGet(url) { return MM.api.get(url).catch(function () { return null; }); }

  /* A season or an episode changes what its show's and seasons' cards say. The
     Watch page has the first cards of each row. For one further on, the show's
     details say whether it is watched and its search result how much is left:
     without that result the card says no more how much is. */
  function followShow(ref, doc, done) {
    var past = cardsOf(function (card) { return refOf(card) === ref; }).filter(function (card) {
      var fresh = withAttr(doc, 'data-mark', card.getAttribute('data-mark')).filter(function (n) { return !isContinue(n); })[0];
      if (fresh) bringUp(card, hasBadge(fresh), fresh.querySelector('.watch-card__sub').textContent, done);
      return !fresh;
    });
    if (!past.length) return;
    var asked = latest[ref];
    Promise.all([tryGet(TITLE_URL + ref), tryGet(SEARCH_URL + encodeURIComponent(titleOf(past[0])))]).then(function (got) {
      if (latest[ref] !== asked) return;
      var found = ((got[1] && got[1].items) || []).filter(function (item) { return item.ref === ref; })[0];
      past.forEach(function (card) {
        var mark = card.getAttribute('data-mark');
        var own = /^show\//.test(mark);
        var title = got[0] || {};
        var now = own ? title : (title.seasons || []).filter(function (n) { return mark === 'season/' + n.key; })[0] || {};
        bringUp(card, 'watched' in now ? now.watched : hasBadge(card), (own && found && found.subtitle) || '', done);
      });
    });
  }

  function replacePart(old, fresh, selector) {
    var a = old.querySelector(selector);
    var b = fresh.querySelector(selector);
    if (a && b) a.replaceWith(document.importNode(b, true));
    else if (a) a.remove();
    else if (b) old.appendChild(document.importNode(b, true));
  }

  function syncEpisode(link, fresh) {
    link.className = fresh.className;
    replacePart(link, fresh, '.watch-ep__text');
    replacePart(link, fresh, '.watch-ep__state');
    replacePart(link.querySelector('.watch-ep__thumb'), fresh.querySelector('.watch-ep__thumb'), '.watch-bar');
  }

  /* The film's or show's own page: its Play button, its season tabs, its episodes
     and every toggle, changed where they are. */
  function followTitle() {
    var tab = document.querySelector('.watch-seasons a.watch-pill.on');
    var href = tab ? tab.getAttribute('href') : window.location.pathname + window.location.search;
    var asked = changes;
    fetchPage(href).then(function (doc) {
      if (asked !== changes) return;
      var on = document.querySelector('.watch-seasons a.watch-pill.on');
      var list = document.querySelector('.watch-episodes');
      // Another season was picked meanwhile: it comes with its own marks.
      if (on !== tab || (list && list.getAttribute('aria-busy') === 'true')) return;
      var info = document.querySelector('.watch-hero__info');
      var freshInfo = doc.querySelector('.watch-hero__info');
      if (!info || !freshInfo) return;
      var play = info.querySelector('.watch-btn--play');
      if (play !== document.activeElement) replacePart(info, freshInfo, '.watch-btn--play');
      var tabs = toArray(doc.querySelectorAll('.watch-seasons a.watch-pill'));
      toArray(document.querySelectorAll('.watch-seasons a.watch-pill')).forEach(function (pill) {
        var fresh = tabs.filter(function (t) { return t.getAttribute('href') === pill.getAttribute('href'); })[0];
        if (!fresh) return;
        replacePart(pill, fresh, '.watch-pill__mark');
        if (fresh.hasAttribute('aria-label')) pill.setAttribute('aria-label', fresh.getAttribute('aria-label'));
        else pill.removeAttribute('aria-label');
      });
      toArray(document.querySelectorAll('[data-mark-toggle]')).forEach(function (b) {
        var fresh = withAttr(doc, 'data-mark-toggle', b.getAttribute('data-mark-toggle'))[0];
        if (fresh && !busy[b.getAttribute('data-mark-toggle')]) setToggle(b, fresh.getAttribute('aria-pressed') === 'true');
      });
      var eps = toArray(doc.querySelectorAll('.watch-ep'));
      toArray(document.querySelectorAll('.watch-ep')).forEach(function (link, i) {
        if (eps[i] && eps[i].getAttribute('data-ep') === link.getAttribute('data-ep')) syncEpisode(link, eps[i]);
      });
    }).catch(function () {
      // The mark is saved; the page shows the rest on the next load.
    });
  }

  function follow(ref, done) {
    latest[ref] = done;
    if (byId('watch-browse')) followRows(ref, done);
    else followTitle();
  }

  /* Told as `mm:watch-marked`, with detail { ref, refs }: the title's refs, and of them the library's, if it has one. */
  function announce(mark, ref) {
    var refs = [ref];
    tmdbRefsOf(mark).forEach(function (tmdb) {
      refs.push(tmdb);
      withAttr(document, 'data-tmdb-ref', tmdb).forEach(function (card) {
        if (!isContinue(card)) refs.push(refOf(card));
      });
    });
    refs = refs.filter(function (one, i) { return one && refs.indexOf(one) === i; });
    if (!refs.length) return;
    var held = refs.filter(function (one) { return /^(movie|show)-/.test(one); })[0];
    document.dispatchEvent(new CustomEvent('mm:watch-marked', { detail: { ref: held || refs[0], refs: refs } }));
  }

  /* ── Mark as watched, or not ── */

  function send(mark, watched, query) {
    if (/^tmdb-/.test(mark)) return MM.api[watched ? 'put' : 'delete'](SEEN_URL + mark + (query || ''));
    return MM.api[watched ? 'post' : 'delete'](WATCHED_URL + mark + (query || ''));
  }

  /* The library has come to hold the title *mark*: its cards are marked by that from now on. */
  function nowHeld(mark, held) {
    ['data-mark', 'data-mark-toggle'].forEach(function (name) {
      withAttr(document, name, mark).forEach(function (n) { n.setAttribute(name, held); });
    });
  }

  /* TMDB has no such title: nothing can be marked of it. */
  function noEye(mark) {
    toArray(document.querySelectorAll('.watch-card')).forEach(function (card) {
      if (markOf(card) !== mark) return;
      toArray(card.querySelectorAll('[data-mark-toggle]')).forEach(function (b) { b.hidden = true; });
      window.MMWatch.cardTools.moveStop(card);
    });
  }

  /* The toast of each mark's or Dismiss's last change, taken away as the next one starts:
     it would say what is no longer so, and its Undo would undo the wrong thing. */
  var toasts = {};

  function tell(key, message, opts) {
    toasts[key] = window.UIFeedback.toast(message, opts);
  }

  function retire(key) {
    if (toasts[key]) window.UIFeedback.dismiss(toasts[key]);
    delete toasts[key];
  }

  /* An older Undo of a title is of no use once the whole of it, or a part, is marked again. */
  var offered = {}; // what each mark's Undo is of: { ref, whole }

  function retireUndos(mark, ref) {
    var whole = WHOLE_RE.test(mark);
    Object.keys(offered).forEach(function (key) {
      if (key === mark || !ref || offered[key].ref !== ref || !(whole || offered[key].whole)) return;
      retire(key);
      delete offered[key];
    });
  }

  function started(key) {
    retire(key);
    setBusy(key, true);
  }

  function toastFailed(mark, err, name) {
    var why = err && err.status === 404 ? name + (/^tmdb-/.test(mark) ? ' can’t be found any more.' : ' is not in the library any more.')
      : err && err.status === 503 ? 'Couldn’t reach Plex to mark ' + name + '. Try again in a moment.'
      : 'Couldn’t mark ' + name + '. Try again.';
    tell(mark, MM.api.messageOf(err, why), { variant: 'error' });
  }

  function refOfMark(mark, given) {
    var own = /^(movie|show)\/([0-9]+)$/.exec(mark);
    return own ? own[1] + '-' + own[2] : given || '';
  }

  /* *inPanel*: from a touch screen's panel, which stays open until the answer, as its eye alone can show the wait. */
  function change(mark, watched, name, ref, card, inPanel) {
    if (busy[mark]) return;
    // undos puts the page back; rows: Continue watching or the banner changed with it; redraw: it may come back there.
    var done = { undos: [], part: /^(season|episode)\//.test(mark), rows: false, redraw: !watched, card: card };
    var tmdb = /^tmdb-/.test(mark); // not in the library: nothing of it is in Continue watching
    ref = refOfMark(mark, ref);
    if (card && !inPanel) window.MMWatch.cardTools.close(card);
    started(mark);
    retireUndos(mark, ref);
    send(mark, watched).then(function (data) { readFirst(mark, ref, function () {
      changes += 1;
      if (inPanel) window.MMWatch.cardTools.close(card);
      if (done.part) tmdbOf(ref).forEach(function (tmdb) { delete said[tmdb]; });
      done.undos = paint(mark, watched);
      if (/^show\//.test(mark)) done.undos = done.undos.concat(paintSeasons(ref, watched));
      var shown = watched && showWatched(ref, mark, done);
      latest[ref] = done;
      var opts = { variant: 'success', dialog: detailsShown() };
      if (data.changed) {
        offered[mark] = { ref: ref, whole: !done.part };
        opts.action = { label: 'Undo', onClick: function () { undoChange(mark, watched, name, ref, done); } };
      }
      tell(mark, 'Marked ' + name + ' as ' + (watched ? 'watched.' : 'not watched.'), opts);
      setBusy(mark, false);
      announce(mark, ref);
      var goOn = function () {
        if (latest[ref] !== done) return; // a later change of it says what stands there
        if (watched && !done.part) leaveContinue(ref, done);
        if (!tmdb) follow(ref, done);
      };
      if (shown) setTimeout(goOn, MARKED_MS); else goOn();
    }); }, function (err) {
      setBusy(mark, false);
      var held = err && err.error === 'in_library' && err.data ? err.data.mark_kind + '/' + err.data.mark_key : '';
      if (tmdb && /^(movie|show)\/[0-9]+$/.test(held)) {
        nowHeld(mark, held);
        change(held, watched, name, null, card, inPanel);
        return;
      }
      if (err && err.error === 'no_tmdb_id') noEye(mark);
      toastFailed(mark, err, name);
    });
  }

  /* The TMDB refs of the cards of the title *ref*. */
  function tmdbOf(ref) {
    return toArray(document.querySelectorAll('[data-tmdb-ref]')).filter(function (card) { return refOf(card) === ref; })
      .map(function (card) { return card.getAttribute('data-tmdb-ref'); });
  }

  /* Run *fn* now, or once the change of *key* on its way is in. */
  function whenIdle(key, fn) {
    if (busy[key]) afterBusy[key] = fn; else fn();
  }

  /* Put the page back, and the focus on what came back if it went with what was
     there. Returns the change to follow the rows with: seek, where the focus still has to go. */
  function putBack(ref, done, part) {
    changes += 1;
    done.undos.splice(0).reverse().forEach(function (fn) { fn(); });
    if (done.rows) tellContinue(ref);
    if (focusLost(null)) refocus(controlOf(ref, done, !done.rows));
    return { undos: [], part: part, undone: true, redraw: done.rows, fromBanner: done.fromBanner, card: done.card, seek: focusLost(null) };
  }

  /* An Undo that was refused has taken its toast, and the focus in it, with it. */
  function keepFocus(ref, done) {
    if (!focusLost(null)) return;
    refocus(controlOf(ref, done, true) || (done.beside && canFocus(done.beside) ? done.beside : core.spareFocus()));
  }

  function undoChange(mark, watched, name, ref, done) {
    whenIdle(mark, function () {
      refocus(toggleOf(mark, done.card));
      started(mark);
      send(mark, !watched, '?undo=1').then(function (data) { readFirst(mark, ref, function () {
        var after = putBack(ref, done, !/^movie\//.test(mark));
        // A card that came meanwhile was not there to be put back.
        note(mark, data.watched);
        cardsOfMark(mark).forEach(function (card) { if (hasBadge(card) !== data.watched) setCard(card, data.watched); });
        withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, data.watched); });
        tell(mark, 'Undone for ' + name + '.', { variant: 'info' });
        setBusy(mark, false);
        announce(mark, ref);
        if (!/^tmdb-/.test(mark)) follow(ref, after);
      }); }, function (err) {
        toastFailed(mark, err, name);
        setBusy(mark, false);
        keepFocus(ref, done);
      });
    });
  }

  /* Where an undone mark is now: in Details while that is open over the page. */
  function toggleOf(mark, card) {
    var over = detailsShown();
    var own = card && card.isConnected && (!over || over.contains(card)) && core.controlIn(card, EYE);
    if (own) return own;
    return withAttr(document, 'data-mark-toggle', mark).filter(function (b) {
      return b.getClientRects().length && (over ? over.contains(b) : !b.closest('.watch-card'));
    })[0];
  }

  /* The toast takes the focus with it when it goes. */
  function refocus(node) {
    if (!node || !node.getClientRects().length) return;
    core.drawTools(node);
    node.focus({ preventScroll: true });
  }

  /* ── Dismiss ── */

  function dismiss(ref, name, fromBanner) {
    if (busy[ref]) return;
    var done = { undos: [], part: false, rows: true, fromBanner: fromBanner };
    started(ref);
    MM.api.post(DISMISSED_URL + ref).then(function () {
      changes += 1;
      leaveContinue(ref, done);
      tell(ref, 'Dismissed ' + name + ' from Continue watching.', {
        variant: 'success',
        action: { label: 'Undo', onClick: function () { undoDismiss(ref, name, done); } }
      });
      setBusy(ref, false);
      followRows(ref, done);
    }, function (err) {
      tell(ref, MM.api.messageOf(err, err && err.status === 404 ? name + ' is not in the library any more.'
        : 'Couldn’t dismiss ' + name + '. Try again.'), { variant: 'error' });
      setBusy(ref, false);
    });
  }

  function undoDismiss(ref, name, done) {
    whenIdle(ref, function () {
      started(ref);
      MM.api.delete(DISMISSED_URL + ref).then(function () {
        var after = putBack(ref, done, false);
        tell(ref, name + ' is back in Continue watching.', { variant: 'info' });
        setBusy(ref, false);
        followRows(ref, after);
      }, function (err) {
        tell(ref, MM.api.messageOf(err, 'Couldn’t undo for ' + name + '. It stays dismissed.'), { variant: 'error' });
        setBusy(ref, false);
        keepFocus(ref, done);
      });
    });
  }

  /* ── A strong no takes the title out of the picks ── */

  var goneTitles = []; // { title, undos, toast }: each title that has left them, to come back if rated otherwise
  var noes = []; // each strong no given, as it was told
  var restored = []; // the titles brought back by a change not yet saved

  /* Whether *a* and *b*, each with refs, mediaType and tmdbId, are one title. */
  function sameTitle(a, b) {
    return (!!a.tmdbId && a.tmdbId === b.tmdbId && a.mediaType === b.mediaType) ||
      a.refs.some(function (ref) { return b.refs.indexOf(ref) >= 0; });
  }

  function pickOf(card) {
    var link = card.querySelector('[data-pick-ref]');
    return {
      refs: [link.getAttribute('data-title-ref'), link.getAttribute('data-pick-ref')],
      mediaType: link.getAttribute('data-kind') === 'movie' ? 'movie' : 'tv',
      tmdbId: Number(link.getAttribute('data-tmdb-id'))
    };
  }

  /* The cards of *title* among the picks, the open grid's last, so that an Undo has it back first. */
  function pickCards(title) {
    return toArray(document.querySelectorAll('.watch-card')).filter(function (card) {
      return isPick(card) && !card.classList.contains('is-kept') &&
        !card.classList.contains('is-gone') && sameTitle(pickOf(card), title);
    }).sort(function (a, b) { return (a.closest('.watch-row') ? 0 : 1) - (b.closest('.watch-row') ? 0 : 1); });
  }

  /* Details gives the focus back to the card it opened from: *node* has it if that has gone. */
  function focusAfter(details, node) {
    var watcher = new MutationObserver(function () {
      var shut = details.classList.contains('is-leaving');
      if (!shut && !details.classList.contains('is-aside')) return;
      watcher.disconnect();
      var active = document.activeElement;
      // Over See all its heading has been given it already.
      if (shut && (active === document.body || active.id === 'watch-all-title' || details.contains(active))) refocus(node);
    });
    watcher.observe(details, { attributes: true, attributeFilter: ['class'] });
  }

  /* Returns what puts it back, or null: it is not in the picks shown. */
  function leavePicks(title) {
    var grid = window.MMWatch.grid;
    var preview = window.MMWatch.preview;
    var left = { title: title, name: '', undos: [], toast: null };
    var focus = null;
    var next = null;
    pickCards(title).forEach(function (card) {
      var row = card.closest('.watch-row');
      if (!row && !(grid && grid.isOpen())) return; // a grid on its way out takes it along
      left.name = titleOf(card);
      closeMenu(card);
      if (preview && card.querySelector('.is-previewed')) preview.close();
      var lost = card.contains(document.activeElement);
      var taken = keep.fromRow(card, row, card.getClientRects().length > 0, '.card-tool');
      if (lost) focus = taken.focus;
      next = taken.focus;
      left.undos.push(function () {
        var control = null;
        function putBack() { control = taken.undo(); }
        if (row) showInRow(row, card, putBack);
        else putBack();
        return (control && control.closest('.watch-card')) || card; // the row's grid may have one of its own
      });
    });
    if (detailsShown()) focusAfter(detailsShown(), next);
    else if (focus) core.focusSoftly(focus);
    if (!left.undos.length) return null;
    goneTitles.push(left);
    return left;
  }

  function of(list, title) { return list.filter(function (t) { return sameTitle(t, title); })[0]; }

  function without(list, title) { return list.filter(function (t) { return !sameTitle(t, title); }); }

  /* Returns whether it had left them. */
  function backInPicks(title) {
    var mine = goneTitles.filter(function (left) { return sameTitle(left.title, title); });
    mine.forEach(function (left) {
      goneTitles.splice(goneTitles.indexOf(left), 1);
      if (left.toast) window.UIFeedback.dismiss(left.toast);
      var cards = left.undos.reverse().map(function (fn) { return fn(); });
      if (!focusLost(null)) return;
      var shown = cards.filter(function (card) { return canFocus(card); })[0];
      if (shown) core.focusSoftly(core.controlIn(shown, '.card-tool--thumb'));
    });
    return mine.length > 0;
  }

  function saidNo(title) {
    if (MM.ratings.get(title.ref) !== STRONG_NO) return;
    var left = leavePicks(title);
    if (!left) return;
    left.toast = window.UIFeedback.toast(left.name + ' is gone from your picks. Search still finds it.', {
      variant: 'info',
      dialog: detailsShown(),
      // With no level known to go back to, there is none.
      action: title.was === undefined ? null : { label: 'Undo', onClick: function () { undoNo(title); } }
    });
  }

  /* The title Details shows, as far as its address and the cards on the page say. */
  function titleShown() {
    var ref = decodeURIComponent((/^#title=(.+)$/.exec(window.location.hash || '') || ['', ''])[1]);
    var tmdb = /^tmdb-(movie|tv)-0*([1-9][0-9]*)$/.exec(ref);
    var shown = { refs: [ref], mediaType: tmdb && tmdb[1], tmdbId: tmdb && Number(tmdb[2]) };
    if (!ref || tmdb) return shown;
    var card = toArray(document.querySelectorAll('.watch-card')).filter(function (c) {
      return isPick(c) && pickOf(c).refs.indexOf(ref) >= 0;
    })[0];
    return card ? pickOf(card) : shown;
  }

  function undoNo(title) {
    var details = detailsShown();
    var heading = byId('ttl-title');
    var active = document.activeElement;
    var lost = !active || active === document.body || active === heading || active.closest('.ui-toast');
    MM.ratings.set(title.ref, title.was); // its cards are back, for titleShown
    if (!details || !lost) return;
    var thumb = sameTitle(titleShown(), title) &&
      details.querySelector('[data-ttl-act="rate-' + (title.was || STRONG_NO) + '"]');
    if (thumb) refocus(thumb);
    // Once Details has put it on its first button, which acts.
    else setTimeout(function () { refocus(heading); }, 0);
  }

  document.addEventListener('mm:rating', function (e) {
    var d = e.detail;
    if (d.phase === 'pending') {
      if (d.level === STRONG_NO) {
        if (!of(noes, d)) noes.push(d); // it may be saved and its answer lost
      } else if (backInPicks(d)) {
        restored.push(d);
      }
    } else if (d.phase === 'saved') {
      restored = without(restored, d);
      noes = without(noes, d);
      if (d.level === STRONG_NO) {
        noes.push(d);
        setTimeout(function () { saidNo(d); }, core.reduceMotion() ? RATED_STILL_MS : RATED_MS);
      } else {
        backInPicks(d);
      }
    } else if (d.phase === 'read') {
      // Asked after a save of which nothing was heard.
      if (d.level !== STRONG_NO) {
        noes = without(noes, d);
        backInPicks(d);
      } else if (of(noes, d)) {
        saidNo(of(noes, d));
      }
    } else if (d.phase === 'failed') {
      var again = d.level === STRONG_NO && of(restored, d) ? leavePicks(d) : null;
      restored = without(restored, d);
      // The strong no still stands, and may have been passed over while another level was shown.
      if (!again && d.level === STRONG_NO && of(noes, d)) saidNo(of(noes, d));
      var where = d.from && d.from.closest('.pv');
      if (again) again.toast = window.UIFeedback.toast('Couldn’t bring ' + again.name + ' back. It stays out of your picks.', { variant: 'error' });
      // Over a card nothing says so, and the preview that would may have gone.
      else if (!d.from || (d.from.matches('.rate--lift') &&
          !(where && document.body.contains(where) && where.matches('.is-open:not(.is-fading):not(.is-handed)')))) {
        core.toast('Your rating wasn’t saved. Try again.', { variant: 'error' });
      }
    }
  });

  /* Whether the title *ref* of the card's *link* can be rated, or may be: a pick with no TMDB title cannot. */
  function canRate(ref, link) {
    return !!MM.ratings && !!ref && MM.ratings.canRate(ref) !== false &&
      !(/^(rec|pick)-/.test(ref) && !link.hasAttribute('data-tmdb-id'));
  }

  /* What to find more like the title *ref* of the card's *link* by, or '': a pick goes by its TMDB id. */
  function likeRef(ref, link) {
    var search = window.MMWatch.search || {};
    var id = /^(rec|pick)-/.test(ref) && link.getAttribute('data-tmdb-id');
    if (id) ref = (link.getAttribute('data-kind') === 'movie' ? 'tmdb-movie-' : 'tmdb-tv-') + id;
    return search.canLike && search.canLike(ref) ? ref : '';
  }

  /* ── The More menu: what an admin can do of a title of All titles ── */

  var menuNode = null;
  var moreMenu = null;
  var keeping = null; // the card whose menu is opened at how long to keep it

  function menuItem(act, iconName, label) {
    var item = el('button', 'watch-keep-menu__item');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.setAttribute('tabindex', '-1');
    item.setAttribute('data-act', act);
    item.appendChild(MM.dom.icon(iconName));
    item.appendChild(document.createTextNode(label));
    return item;
  }

  function fillMenu(trigger) {
    var card = trigger.closest('.watch-card');
    var library = window.MMWatch.library;
    menuNode.textContent = '';
    if (!library || library.durations(card, keeping === card ? menuNode : null, menuItem)) return;
    menuNode.setAttribute('aria-label', 'More for ' + titleOf(card));
    library.menu(card, menuNode, menuItem);
  }

  function choose(trigger, item) {
    var card = trigger.closest('.watch-card');
    var act = item.getAttribute('data-act');
    if (act === 'keep-for') {
      keeping = card;
      moreMenu.open(trigger);
      keeping = null;
      return;
    }
    window.MMWatch.cardTools.close(card);
    window.MMWatch.library.act(card, act, item);
  }

  if (byId('watch-browse')) {
    menuNode = el('div', 'watch-keep-menu watch-more-menu');
    menuNode.setAttribute('id', 'watch-more-menu');
    menuNode.setAttribute('role', 'menu');
    menuNode.hidden = true;
    page.appendChild(menuNode);
    moreMenu = keep.menu(menuNode, {
      trigger: '[data-watch-more]',
      canOpen: function (trigger) { return trigger.getAttribute('aria-disabled') !== 'true'; },
      fill: fillMenu,
      choose: choose,
      spare: core.spareFocus
    });
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var toggle = e.target.closest('[data-mark-toggle]');
    var of = toggle && toggle.closest('.watch-card');
    if (of) {
      change(toggle.getAttribute('data-mark-toggle'), toggle.getAttribute('aria-pressed') !== 'true', nameOf(of), refOf(of), of, !!toggle.closest('.card-over'));
      return;
    }
    if (toggle) {
      change(toggle.getAttribute('data-mark-toggle'), toggle.getAttribute('aria-pressed') !== 'true',
        toggle.getAttribute('data-mark-name'), toggle.getAttribute('data-mark-ref'));
      return;
    }
    var button = e.target.closest('[data-watch-dismiss]');
    if (button) dismiss(button.getAttribute('data-watch-dismiss'), button.getAttribute('data-mark-name'), !!button.closest('.watch-banner'));
  });

  window.MMWatch.marks = { paint: paint, adopt: adopt, continues: continues, showInRow: showInRow, canRate: canRate, likeRef: likeRef };
})();
