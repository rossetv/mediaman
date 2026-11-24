/* Watch: the person's own Watched marks and Dismiss. One More menu serves every
 * card, a toggle every film, episode, season and show that has one, and each
 * change can be undone from its toast. What a change means for the rest of the
 * page is read from the server's own page. Needs watch/core.js and
 * watch/keep.js; on window.MMWatch.marks.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var keep = window.MMWatch.keep;
  var byId = core.byId;
  var el = core.el;
  var toArray = core.toArray;

  var WATCHED_URL = '/api/watch/watched/';
  var DISMISSED_URL = '/api/watch/dismissed/';
  var TITLE_URL = '/api/watch/title/';
  var SEARCH_URL = '/api/watch/search?q=';
  var LEFT_RE = / · [^·]* left$/;

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
    var mores = moresOf(continueCards(document, ref));
    var all = moresOf(done.card ? [done.card] : []).concat(done.fromBanner ? buttons.concat(mores) : mores.concat(buttons));
    if (any) all = all.concat(moresOf(cardsOf(function (card) { return refOf(card) === ref; })));
    return all.filter(canFocus)[0];
  }

  function moresOf(cards) {
    return cards.map(function (card) { return card.querySelector('[data-watch-more]'); }).filter(Boolean);
  }

  function canFocus(node) { return node.getClientRects().length > 0 && !node.closest('[inert]'); }

  /* ── One change at a time ──
     The buttons stay focusable (aria-disabled, not disabled) so focus never drops. */

  var busy = {};
  var afterBusy = {}; // an Undo pressed meanwhile, run once it is not

  function buttonsOf(key) {
    var found = withAttr(document, 'data-mark-toggle', key).concat(withAttr(document, 'data-watch-dismiss', key));
    withAttr(document, 'data-mark', key).concat(withAttr(document, 'data-dismiss-ref', key)).forEach(function (card) {
      var more = card.querySelector('[data-watch-more]');
      if (more) found.push(more);
    });
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

  function setToggle(button, watched) {
    button.setAttribute('aria-pressed', watched ? 'true' : 'false');
    if (button.hasAttribute('title')) button.setAttribute('title', core.markAction(watched));
  }

  /* A card's mark, progress bar and caption, as they are now. Returns what puts them back. */
  function snapshot(card) {
    var poster = card.querySelector('.watch-card__poster');
    var sub = card.querySelector('.watch-card__sub');
    var badge = poster.querySelector('.watch-badge--watched');
    var bar = poster.querySelector('.watch-bar');
    var text = sub.textContent;
    return function () { draw(card, badge, bar, text); };
  }

  function draw(card, badge, bar, text) {
    var poster = card.querySelector('.watch-card__poster');
    [['.watch-badge--watched', badge], ['.watch-bar', bar]].forEach(function (p) {
      var old = poster.querySelector(p[0]);
      if (old === p[1]) return;
      if (old) old.remove();
      if (p[1]) poster.appendChild(p[1]);
    });
    card.querySelector('.watch-card__sub').textContent = text;
  }

  /* A watched title has nothing left to watch. */
  function setCard(card, watched) {
    var back = snapshot(card);
    var bar = card.querySelector('.watch-bar');
    var text = card.querySelector('.watch-card__sub').textContent;
    var badge = card.querySelector('.watch-badge--watched') || core.watchedBadge();
    if (watched) draw(card, badge, null, text.replace(LEFT_RE, ''));
    else draw(card, null, bar, text);
    return back;
  }

  function cardsOf(test) {
    return toArray(document.querySelectorAll('.watch-card[data-mark]')).filter(function (card) {
      return !isContinue(card) && test(card);
    });
  }

  /* Show *mark* as it now is wherever it is drawn. Returns what puts the cards back. */
  function paint(mark, watched) {
    withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, watched); });
    return cardsOf(function (card) { return card.getAttribute('data-mark') === mark; })
      .map(function (card) { return setCard(card, watched); });
  }

  /* A show's change is every season's. */
  function paintSeasons(ref, watched) {
    return cardsOf(function (card) { return /^season\//.test(card.getAttribute('data-mark')) && refOf(card) === ref; })
      .map(function (card) { return setCard(card, watched); });
  }

  /* ── Leaving Continue watching ── */

  /* Whether the focus goes with *node*, or has gone already. Not while Details
     is open over the page: it keeps its own. */
  function focusLost(node) {
    var active = document.activeElement;
    if (document.documentElement.classList.contains('has-ttl')) return false;
    return !active || active === document.body || active.tagName === 'MAIN' || !document.body.contains(active) ||
      !!(node && node.contains(active)) || !!active.closest('[inert], .ui-toast');
  }

  /* A card that goes takes its open menu with it. */
  function closeMenu(card) {
    var trigger = moreMenu && moreMenu.trigger();
    if (trigger && card.contains(trigger)) moreMenu.close(false);
  }

  /* An Undo puts the old nodes back only where they still belong: what another
     change has replaced or taken out since is drawn from the server's page. */

  function leaveContinue(ref, done) {
    continueCards(document, ref).forEach(function (card) {
      closeMenu(card);
      var lost = focusLost(card);
      var row = card.closest('.watch-row');
      var grid = row ? null : card.parentNode; // the row opened as a grid holds one too
      var taken = keep.fromRow(card, row, card.getClientRects().length > 0, '[data-watch-more]');
      done.rows = true;
      done.undos.push(function () {
        if (grid) {
          if (!continueCards(grid, ref).length) taken.undo();
          return;
        }
        var now = continueRow(document);
        if (!now || (now === row && !continueCards(row, ref).length)) taken.undo();
      });
      if (lost && taken.focus) core.focusSoftly(taken.focus);
    });
  }

  /* The card moves on to what the server now has in its place. */
  function replaceCard(card, fresh, done) {
    closeMenu(card);
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
    if (lost) fresh.querySelector('[data-watch-more]').focus({ preventScroll: true });
  }

  /* The server's card that the page lacks, in its place in the row, or with its row, and in that row's open grid. */
  function enterContinue(now) {
    var row = continueRow(document);
    var fresh = document.importNode(row ? now : now.closest('.watch-row'), true);
    if (row) {
      var next = null;
      for (var n = now.nextElementSibling; n && !next; n = n.nextElementSibling) {
        next = continueCards(row, n.getAttribute('data-dismiss-ref'))[0];
      }
      row.querySelector('.watch-row__scroll').insertBefore(fresh, next || null);
    } else {
      var rows = document.querySelector('.watch-rows');
      rows.insertBefore(fresh, rows.children[0] || null);
      window.MMWatch.rows.init(fresh.querySelector('.watch-row__viewport'));
      if (window.MMWatch.expand) window.MMWatch.expand.row(fresh);
    }
    core.fadeIn(fresh);
    var grid = window.MMWatch.grid;
    if (grid && grid.bring) grid.bring(row ? fresh : continueCards(fresh, now.getAttribute('data-dismiss-ref'))[0]);
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
    var to = fresh ? fresh.querySelector('.watch-btn--play') : byId('watch-search');
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
    fetchPage('/watch').then(function (doc) {
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
    if (now && !here.length) enterContinue(now);
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

  /* Details, when it shows the title, loads it again. */
  function announce(ref) {
    document.dispatchEvent(new CustomEvent('mm:watch-marked', { detail: { ref: ref } }));
  }

  /* ── Mark as watched, or not ── */

  function send(mark, watched, query) {
    return MM.api[watched ? 'post' : 'delete'](WATCHED_URL + mark + (query || ''));
  }

  function toastFailed(err, name) {
    var why = err && err.status === 404 ? name + ' is not in the library any more.'
      : err && err.status === 503 ? 'Couldn’t reach Plex to mark ' + name + '. Try again in a moment.'
      : 'Couldn’t mark ' + name + '. Try again.';
    core.toast(MM.api.messageOf(err, why), { variant: 'error' });
  }

  function refOfMark(mark, given) {
    var own = /^(movie|show)\/([0-9]+)$/.exec(mark);
    return own ? own[1] + '-' + own[2] : given || '';
  }

  function change(mark, watched, name, ref, card) {
    if (busy[mark]) return;
    // undos puts the page back; rows: Continue watching or the banner changed with it; redraw: it may come back there.
    var done = { undos: [], part: /^(season|episode)\//.test(mark), rows: false, redraw: !watched, card: card };
    ref = refOfMark(mark, ref);
    setBusy(mark, true);
    send(mark, watched).then(function (data) {
      changes += 1;
      done.undos = paint(mark, watched);
      if (/^show\//.test(mark)) done.undos = done.undos.concat(paintSeasons(ref, watched));
      if (watched && !done.part) leaveContinue(ref, done);
      var opts = { variant: 'success' };
      if (data.changed) opts.action = { label: 'Undo', onClick: function () { undoChange(mark, watched, name, ref, done); } };
      core.toast('Marked ' + name + ' as ' + (watched ? 'watched.' : 'not watched.'), opts);
      setBusy(mark, false);
      announce(ref);
      follow(ref, done);
    }, function (err) {
      toastFailed(err, name);
      setBusy(mark, false);
    });
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
    if (focusLost(null)) refocus(controlOf(ref, done, !done.rows));
    return { undos: [], part: part, redraw: done.rows, fromBanner: done.fromBanner, card: done.card, seek: focusLost(null) };
  }

  function undoChange(mark, watched, name, ref, done) {
    whenIdle(mark, function () {
      refocus(toggleOf(mark));
      setBusy(mark, true);
      send(mark, !watched, '?undo=1').then(function (data) {
        var after = putBack(ref, done, !/^movie\//.test(mark));
        withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, data.watched); });
        core.toast('Undone for ' + name + '.', { variant: 'info' });
        setBusy(mark, false);
        announce(ref);
        follow(ref, after);
      }, function (err) {
        toastFailed(err, name);
        setBusy(mark, false);
      });
    });
  }

  /* Where an undone mark is now: in Details while that is open over the page. */
  function toggleOf(mark) {
    var over = document.documentElement.classList.contains('has-ttl');
    return withAttr(document, 'data-mark-toggle', mark).filter(function (b) {
      return b.getClientRects().length && (!over || b.closest('#ttl'));
    })[0];
  }

  /* The toast takes the focus with it when it goes. */
  function refocus(node) {
    if (node && node.getClientRects().length) node.focus({ preventScroll: true });
  }

  /* ── Dismiss ── */

  function dismiss(ref, name, fromBanner) {
    if (busy[ref]) return;
    var done = { undos: [], part: false, rows: true, fromBanner: fromBanner };
    setBusy(ref, true);
    MM.api.post(DISMISSED_URL + ref).then(function () {
      changes += 1;
      leaveContinue(ref, done);
      core.toast('Dismissed ' + name + ' from Continue watching.', {
        variant: 'success',
        action: { label: 'Undo', onClick: function () { undoDismiss(ref, name, done); } }
      });
      setBusy(ref, false);
      followRows(ref, done);
    }, function (err) {
      core.toast(MM.api.messageOf(err, err && err.status === 404 ? name + ' is not in the library any more.'
        : 'Couldn’t dismiss ' + name + '. Try again.'), { variant: 'error' });
      setBusy(ref, false);
    });
  }

  function undoDismiss(ref, name, done) {
    whenIdle(ref, function () {
      setBusy(ref, true);
      MM.api.delete(DISMISSED_URL + ref).then(function () {
        var after = putBack(ref, done, false);
        core.toast(name + ' is back in Continue watching.', { variant: 'info' });
        setBusy(ref, false);
        followRows(ref, after);
      }, function (err) {
        core.toast(MM.api.messageOf(err, 'Couldn’t undo for ' + name + '. It stays dismissed.'), { variant: 'error' });
        setBusy(ref, false);
      });
    });
  }

  /* ── The More menu ── */

  var WHAT = { movie: '', episode: 'episode ', season: 'season ', show: 'show ' };
  var menuNode = null;
  var moreMenu = null;

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
    var kind = card.getAttribute('data-mark').split('/')[0];
    menuNode.textContent = '';
    menuNode.setAttribute('aria-label', 'More for ' + titleOf(card));
    menuNode.appendChild(menuItem('mark', 'i-eye', 'Mark ' + WHAT[kind] + 'as ' + (hasBadge(card) ? 'not watched' : 'watched')));
    if (isContinue(card)) menuNode.appendChild(menuItem('dismiss', 'i-xmark', 'Dismiss'));
  }

  function choose(trigger, item) {
    var card = trigger.closest('.watch-card');
    if (item.getAttribute('data-act') === 'dismiss') {
      dismiss(card.getAttribute('data-dismiss-ref'), titleOf(card));
    } else {
      change(card.getAttribute('data-mark'), !hasBadge(card), nameOf(card), refOf(card), card);
    }
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
      spare: function () { return byId('watch-search'); }
    });
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var toggle = e.target.closest('[data-mark-toggle]');
    if (toggle) {
      change(toggle.getAttribute('data-mark-toggle'), toggle.getAttribute('aria-pressed') !== 'true',
        toggle.getAttribute('data-mark-name'), toggle.getAttribute('data-mark-ref'));
      return;
    }
    var button = e.target.closest('[data-watch-dismiss]');
    if (button) dismiss(button.getAttribute('data-watch-dismiss'), button.getAttribute('data-mark-name'), !!button.closest('.watch-banner'));
  });

  window.MMWatch.marks = { paint: paint };
})();
