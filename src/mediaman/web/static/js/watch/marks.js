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

  function continueCards(root, ref) {
    return withAttr(root, 'data-dismiss-ref', ref).filter(function (n) {
      return n.classList.contains('watch-card') && !n.classList.contains('is-kept');
    });
  }

  function bannerOf(ref) {
    var banner = document.querySelector('.watch-banner[data-dismiss-ref]');
    return banner && banner.getAttribute('data-dismiss-ref') === ref ? banner : null;
  }

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
      !!(node && node.contains(active)) ||
      !!(menuNode && menuNode.contains(active)) || !!active.closest('[inert], .ui-toast');
  }

  function leaveContinue(ref, done) {
    continueCards(document, ref).forEach(function (card) {
      var lost = focusLost(card);
      var taken = keep.fromRow(card, card.closest('.watch-row'), card.getClientRects().length > 0, '[data-watch-more]');
      done.undos.push(taken.undo);
      if (lost && taken.focus) core.focusSoftly(taken.focus);
    });
  }

  /* The card moves on to what the server now has in its place. */
  function replaceCard(card, fresh, done) {
    var lost = focusLost(card);
    card.replaceWith(fresh);
    core.fadeIn(fresh);
    done.undos.push(function () {
      if (!fresh.parentNode) return null;
      fresh.replaceWith(card);
      core.fadeIn(card);
      return card.querySelector('[data-watch-more]');
    });
    if (lost) fresh.querySelector('[data-watch-more]').focus({ preventScroll: true });
  }

  function playOf(banner) { return banner.querySelector('.watch-btn--play').getAttribute('href'); }

  function replaceBanner(banner, fresh, done) {
    if (fresh && playOf(fresh) === playOf(banner)) return;
    var lost = focusLost(banner);
    if (fresh) {
      fresh = document.importNode(fresh, true);
      core.swap(banner, fresh);
      done.undos.push(function () { core.swap(fresh, banner); });
    } else {
      done.undos.push(keep.foldAway(banner));
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

  /* The Watch page's cards and banner of the title *ref*, after a change of it. */
  function followRows(ref, done) {
    if (!bannerOf(ref) && !continueCards(document, ref).length && !done.part) return;
    fetchPage('/watch').then(function (doc) {
      if (done.off) return;
      var banner = bannerOf(ref);
      var now = continueCards(doc, ref)[0];
      continueCards(document, ref).forEach(function (card) {
        if (!now) leaveContinue(ref, done);
        else if (now.getAttribute('data-mark') !== card.getAttribute('data-mark')) {
          replaceCard(card, document.importNode(now, true), done);
        }
      });
      if (banner) replaceBanner(banner, doc.querySelector('.watch-banner'), done);
      if (done.part) followShow(ref, doc, done);
    }).catch(function () {
      // The mark is saved; the rows show the rest on the next load.
    });
  }

  /* A season or an episode changes what its show's and seasons' cards say; the
     Watch page has some of them, the rest say no more how much is left. */
  function followShow(ref, doc, done) {
    cardsOf(function (card) { return refOf(card) === ref; }).forEach(function (card) {
      var mark = card.getAttribute('data-mark');
      var fresh = withAttr(doc, 'data-mark', mark).filter(function (n) { return !isContinue(n); })[0];
      var watched = fresh ? hasBadge(fresh) : hasBadge(card);
      var said = card.querySelector('.watch-card__sub').textContent.replace(LEFT_RE, '');
      var now = fresh ? fresh.querySelector('.watch-card__sub').textContent : said;
      done.undos.push(snapshot(card));
      // A Discover card's caption is its year: only the same caption is brought up to date.
      draw(card, watched ? card.querySelector('.watch-badge--watched') || core.watchedBadge() : null,
        card.querySelector('.watch-bar'), now.replace(LEFT_RE, '') === said ? now : said);
      withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, watched); });
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
    fetchPage(href).then(function (doc) {
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

  function change(mark, watched, name, ref) {
    if (busy[mark]) return;
    // undos puts the page back; off: undone, so what is still on its way is dropped.
    var done = { undos: [], off: false, part: /^(season|episode)\//.test(mark) };
    ref = refOfMark(mark, ref);
    setBusy(mark, true);
    send(mark, watched).then(function (data) {
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

  function putBack(done) {
    done.off = true;
    var back = null;
    done.undos.splice(0).reverse().forEach(function (fn) { back = fn() || back; });
    return back;
  }

  function undoChange(mark, watched, name, ref, done) {
    whenIdle(mark, function () {
      refocus(toggleOf(mark));
      setBusy(mark, true);
      send(mark, !watched, '?undo=1').then(function (data) {
        var lost = focusLost(null);
        var back = putBack(done);
        withAttr(document, 'data-mark-toggle', mark).forEach(function (b) { setToggle(b, data.watched); });
        if (lost) refocus(back);
        core.toast('Undone for ' + name + '.', { variant: 'info' });
        setBusy(mark, false);
        announce(ref);
        if (!byId('watch-browse')) followTitle();
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

  function dismiss(ref, name) {
    if (busy[ref]) return;
    var done = { undos: [], off: false, part: false };
    setBusy(ref, true);
    MM.api.post(DISMISSED_URL + ref).then(function () {
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
        var lost = focusLost(null);
        var back = putBack(done);
        if (lost) refocus(back);
        core.toast(name + ' is back in Continue watching.', { variant: 'info' });
        setBusy(ref, false);
      }, function (err) {
        core.toast(MM.api.messageOf(err, 'Couldn’t undo for ' + name + '. It stays dismissed.'), { variant: 'error' });
        setBusy(ref, false);
      });
    });
  }

  /* ── The More menu ── */

  var WHAT = { movie: '', episode: 'episode ', season: 'season ', show: 'show ' };
  var menuNode = null;

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
      change(card.getAttribute('data-mark'), !hasBadge(card), nameOf(card), refOf(card));
    }
  }

  if (byId('watch-browse')) {
    menuNode = el('div', 'watch-keep-menu watch-more-menu');
    menuNode.setAttribute('id', 'watch-more-menu');
    menuNode.setAttribute('role', 'menu');
    menuNode.hidden = true;
    page.appendChild(menuNode);
    keep.menu(menuNode, {
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
    if (button) dismiss(button.getAttribute('data-watch-dismiss'), button.getAttribute('data-mark-name'));
  });

  window.MMWatch.marks = { paint: paint };
})();
