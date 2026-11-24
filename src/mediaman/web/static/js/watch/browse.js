/* Watch page: the Discover rows (Recommended for you, Trending this week,
 * Popular movies, Popular TV) that load after the page in place of their
 * skeleton, the badges on their posters, the menu of earlier picks and, for
 * admins, Refresh. Needs watch/core.js and watch/keep.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var byId = core.byId;
  var toArray = core.toArray;
  var safePath = core.safePath;

  var DISCOVER_URL = '/api/watch/discover';
  var RECOMMENDED_URL = '/api/watch/recommended';
  var REFRESH_URL = '/api/recommended/refresh';
  var STATUS_URL = '/api/recommended/refresh/status';
  var SETTINGS_HREF = '/settings#setg-general';
  var REC_TITLE = 'Recommended for you';
  var DISCOVER_ROWS = [
    ['trending', 'Trending this week'],
    ['popular_movies', 'Popular movies'],
    ['popular_tv', 'Popular TV']
  ];
  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^rec-[0-9]{1,10}$/;
  var POLL_MS = 3000;
  var POLL_MAX_MS = 30000;
  var MAX_FAILURES = 8;
  var IDLE_LIMIT = 2; // "idle" twice running: the worker died without a result
  var SWAP_FALLBACK_MS = 400; // past the slowest motion token, for an animation that never runs

  function toLogin() { window.location.href = '/login'; }

  /* A 401: the session ran out, so the login page, not a "Couldn't load". */
  function loggedOut(err) {
    if (!(err && err.status === 401)) return false;
    toLogin();
    return true;
  }

  /* What a load gives while the browser leaves for the login page: nothing,
     so the skeleton stays until it does. */
  function never() { return new Promise(function () {}); }

  /* ── Cards ── */

  /* The download state on a poster, or null when the title can be downloaded. */
  function badge(state, pct, inLibrary) {
    var node;
    if (state === 'downloading') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--downloading');
      node.appendChild(icon('i-arrow-down'));
      if (typeof pct === 'number') {
        node.appendChild(el('span', 'sr-only', 'Downloading '));
        node.appendChild(document.createTextNode(pct + '%'));
      } else {
        node.appendChild(document.createTextNode('Downloading'));
      }
    } else if (state === 'queued') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--queued');
      node.appendChild(icon('i-clock'));
      node.appendChild(document.createTextNode('Queued'));
    } else if (inLibrary || state === 'in_library' || state === 'partial') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--in');
      node.appendChild(icon('i-check'));
      node.appendChild(document.createTextNode('In library'));
    }
    return node || null;
  }

  /* One poster card, the markup of _watch_card.html's card(). The link
     keeps a real address for a new tab; a plain click opens the detail. */
  function card(item) {
    var ref = item && typeof item.ref === 'string' && REF_RE.test(item.ref) ? item.ref : '';
    if (!ref) return null;
    var article = el('article', 'watch-card');
    var link = el('a', 'watch-card__link');
    link.setAttribute('href', safePath(item.href) || '/watch#title=' + ref);
    link.setAttribute('data-title-ref', ref);
    if (item.kind === 'movie' || item.kind === 'show') link.setAttribute('data-kind', item.kind);
    if (item.in_library) link.setAttribute('data-in-library', '');
    var poster = el('span', 'watch-card__poster');
    var src = safePath(item.poster_url);
    if (src) {
      var img = el('img');
      img.setAttribute('src', src);
      img.setAttribute('alt', '');
      img.setAttribute('loading', 'lazy');
      img.setAttribute('data-broken-on-error', '');
      poster.appendChild(img);
    }
    poster.appendChild(el('span', 'watch-card__title', String(item.title || '')));
    var state = badge(item.download_state, item.download_pct, item.in_library);
    if (state) poster.appendChild(state);
    link.appendChild(poster);
    var kind = item.kind === 'movie' ? 'Movie' : 'TV';
    link.appendChild(el('span', 'watch-card__sub', typeof item.year === 'number' ? kind + ' · ' + item.year : kind));
    article.appendChild(link);
    return article;
  }

  /* A card of the title under another ref (a pick, a result): it prints the
     same title, kind and year. */
  function sameTitle(link, detail) {
    if (!detail.title || !detail.kind) return false;
    var title = link.querySelector('.watch-card__title');
    var sub = link.querySelector('.watch-card__sub');
    if (!title || !sub || title.textContent !== detail.title) return false;
    return sub.textContent === (detail.kind === 'movie' ? 'Movie' : 'TV') + (detail.year ? ' · ' + detail.year : '');
  }

  /* A title's state changed (the detail queued it, or a download moved on):
     every card of it on the page shows the new badge. */
  function updateBadges(detail) {
    if (!detail || typeof detail.ref !== 'string') return;
    toArray(document.querySelectorAll('[data-title-ref]')).forEach(function (link) {
      if (link.getAttribute('data-title-ref') !== detail.ref && !sameTitle(link, detail)) return;
      var poster = link.querySelector('.watch-card__poster');
      if (!poster) return;
      var old = poster.querySelector('.watch-badge--state');
      var fresh = badge(detail.download_state, detail.download_pct, link.hasAttribute('data-in-library'));
      if (old && fresh) old.replaceWith(fresh);
      else if (old) old.remove();
      else if (fresh) poster.appendChild(fresh);
    });
  }

  /* ── Rows ── */

  function rowSection(id, title) {
    var section = el('section', 'watch-row');
    section.setAttribute('data-discover-row', id);
    var head = el('div', 'watch-row__head');
    head.appendChild(el('h2', 'watch-row__title', title));
    section.appendChild(head);
    return section;
  }

  /* The scrolling strip of cards, or null when none of *items* can be shown. */
  function strip(title, items) {
    var cards = (items || []).map(card).filter(Boolean);
    if (!cards.length) return null;
    var viewport = el('div', 'watch-row__viewport');
    var scroller = el('div', 'watch-row__scroll');
    scroller.setAttribute('tabindex', '0');
    scroller.setAttribute('role', 'region');
    scroller.setAttribute('aria-label', title);
    cards.forEach(function (c) { scroller.appendChild(c); });
    viewport.appendChild(scroller);
    return viewport;
  }

  /* A quiet line in a row's place: nothing to show, or it did not load. */
  function note(text, extra) {
    var p = el('p', 'watch-row__note');
    p.appendChild(el('span', null, text));
    if (extra) p.appendChild(extra);
    return p;
  }

  function failedNote(title, retry) {
    var button = el('button', 'watch-row__retry', 'Try again');
    button.type = 'button';
    button.setAttribute('data-discover-retry', retry);
    var p = note('Couldn’t load ' + title + '.', button);
    p.classList.add('watch-row__note--failed');
    p.setAttribute('role', 'alert');
    return p;
  }

  function startRows(root) {
    if (!watch.rows) return; // watch-rows.js starts every row it finds when it loads
    toArray(root.querySelectorAll('.watch-row__viewport')).forEach(watch.rows.init);
  }

  /* Swap a row's body (its strip or note) for another in place. */
  function swapBody(oldBody, newBody) {
    var done = core.swap(oldBody, newBody);
    startRows(newBody.parentNode || newBody);
    return done;
  }

  /* ── Discover rows ── */

  function discoverRow(id, title, items) {
    var body = strip(title, items);
    if (!body) return null;
    var section = rowSection(id, title);
    section.appendChild(body);
    return section;
  }

  function discoverFailed(err) {
    if (loggedOut(err)) return never();
    return DISCOVER_ROWS.map(function (r) {
      var section = rowSection(r[0], r[1]);
      section.appendChild(failedNote(r[1], 'discover'));
      return section;
    });
  }

  /* The three TMDB rows, as sections; a failure gives each its Try again. */
  function loadDiscover() {
    return MM.api.get(DISCOVER_URL).then(function (data) {
      var titles = {};
      DISCOVER_ROWS.forEach(function (r) { titles[r[0]] = r[1]; });
      return (data.rows || []).map(function (row) {
        return titles[row.id] ? discoverRow(row.id, titles[row.id], row.items) : null;
      }).filter(Boolean);
    }, discoverFailed);
  }

  function retryDiscover(button) {
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
    var failed = toArray(document.querySelectorAll('[data-discover-row]')).filter(function (s) {
      return s.querySelector('[data-discover-retry="discover"]');
    });
    loadDiscover().then(function (fresh) {
      var stillFailed = fresh.some(function (s) { return s.querySelector('.watch-row__note--failed'); });
      if (stillFailed) {
        button.removeAttribute('aria-disabled');
        button.setAttribute('aria-busy', 'false');
        core.toast('Still couldn’t load them. Try again in a moment.', { variant: 'error' });
        return;
      }
      // Read before the swap: a row fading out is inert, so it loses the focus.
      var focused = document.activeElement === button;
      var focusNext = null;
      failed.forEach(function (old) {
        var match = fresh.filter(function (s) {
          return s.getAttribute('data-discover-row') === old.getAttribute('data-discover-row');
        })[0];
        if (!match) { old.remove(); return; }
        core.swap(old, match);
        startRows(match);
        if (!focusNext || old.contains(button)) focusNext = match.querySelector('.watch-row__scroll');
      });
      if (focusNext && focused) core.focusSoftly(focusNext);
    });
  }

  /* ── Recommended for you ── */

  var isAdmin = false;
  var rec = null; // { section, tools, body, payload }
  var picksMenu = null;
  var picksMenuNode = null;
  var cooldownTimer = null;
  var polling = false;

  /* "14 h" or "25 min": how long until *iso*. */
  function waitText(iso) {
    var ms = new Date(iso).getTime() - Date.now();
    if (!(ms > 0)) return '';
    if (ms >= 3600000) return Math.ceil(ms / 3600000) + '\u00a0h';
    return Math.max(1, Math.ceil(ms / 60000)) + '\u00a0min';
  }

  function batchName(batch, first) {
    return first ? 'Latest · ' + batch.date_label : batch.date_label;
  }

  function picksButton(payload) {
    var batches = payload.batches || [];
    var chosen = batches.filter(function (b) { return b.id === payload.batch; })[0] || batches[0];
    var button = el('button', 'row-tool');
    button.type = 'button';
    button.setAttribute('data-picks-menu', '');
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'watch-picks-menu');
    button.setAttribute('aria-label', 'Picks: ' + chosen.label + ', ' + chosen.date_label + '. Choose another batch');
    button.appendChild(el('span', 'row-tool__long', chosen.label + ' · '));
    button.appendChild(document.createTextNode(chosen.date_label));
    button.appendChild(icon('i-chevron-down', 'row-tool__chev'));
    return button;
  }

  /* The one menu of batches, the Keep menu's component and look. */
  function fillPicksMenu(payload) {
    if (!picksMenuNode) {
      picksMenuNode = el('div', 'watch-keep-menu rec-menu');
      picksMenuNode.setAttribute('id', 'watch-picks-menu');
      picksMenuNode.setAttribute('role', 'menu');
      picksMenuNode.setAttribute('aria-label', 'Batches of picks');
      picksMenuNode.hidden = true;
      (byId('watch-page') || document.body).appendChild(picksMenuNode);
      picksMenu = watch.keep.menu(picksMenuNode, {
        trigger: '[data-picks-menu]',
        choose: function (trigger, item) { chooseBatch(item.getAttribute('data-batch')); },
        spare: function () { return byId('watch-search'); }
      });
    }
    picksMenuNode.textContent = '';
    (payload.batches || []).forEach(function (b, i) {
      var item = el('button', 'watch-keep-menu__item');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('tabindex', '-1');
      item.setAttribute('data-batch', b.id);
      item.setAttribute('aria-checked', b.id === payload.batch ? 'true' : 'false');
      item.appendChild(icon('i-check'));
      item.appendChild(document.createTextNode(batchName(b, i === 0)));
      picksMenuNode.appendChild(item);
    });
    var refresh = payload.refresh;
    if (refresh && !refresh.available && refresh.next_available_at && waitText(refresh.next_available_at)) {
      picksMenuNode.appendChild(el('p', 'rec-menu__note',
        'New picks can be fetched again in ' + waitText(refresh.next_available_at) + '.'));
    }
  }

  /* Refresh: ready, busy (spinner) or cooling down ("Refresh in 14 h"). */
  function refreshButton() {
    var button = el('button', 'row-tool row-tool--refresh');
    button.type = 'button';
    button.setAttribute('data-picks-refresh', '');
    return button;
  }

  function setRefresh(state, nextAt) {
    var button = rec && rec.tools.querySelector('[data-picks-refresh]');
    if (!button) return;
    clearTimeout(cooldownTimer);
    button.textContent = '';
    button.classList.toggle('is-busy', state === 'busy');
    if (state === 'ready') {
      button.removeAttribute('aria-disabled');
      button.setAttribute('aria-label', 'Refresh picks');
      button.appendChild(icon('i-rotate-right'));
      button.appendChild(el('span', 'row-tool__long', 'Refresh picks'));
      return;
    }
    button.setAttribute('aria-disabled', 'true');
    if (state === 'busy') {
      button.setAttribute('aria-label', 'Refreshing picks');
      button.appendChild(icon('i-rotate-right'));
      button.appendChild(el('span', 'row-tool__long', 'Refreshing…'));
      return;
    }
    var left = waitText(nextAt);
    if (!left) { setRefresh('ready'); return; }
    button.setAttribute('aria-label', 'Refresh picks, available in ' + left);
    button.appendChild(icon('i-clock-regular'));
    button.appendChild(el('span', 'row-tool__long', 'Refresh in '));
    button.appendChild(document.createTextNode(left));
    // The clock counts down by itself; once it runs out Refresh is back.
    cooldownTimer = setTimeout(function () { setRefresh('cooldown', nextAt); }, 60000);
  }

  function refreshState(refresh) {
    if (refresh.running) return 'busy';
    return !refresh.available && refresh.next_available_at ? 'cooldown' : 'ready';
  }

  /* An inline line under the row's heading; null clears it. */
  function setRecError(message) {
    if (!rec) return;
    var old = rec.section.querySelector('.watch-row__error');
    if (old) old.remove();
    if (!message) return;
    var line = el('p', 'watch-row__error', message);
    line.setAttribute('role', 'alert');
    var head = rec.section.querySelector('.watch-row__head');
    head.parentNode.insertBefore(line, head.nextSibling);
  }

  function recBody(payload) {
    var body = payload.enabled ? strip(REC_TITLE, payload.items) : null;
    if (body) return body;
    if (!payload.enabled) {
      var link = el('a', 'watch-row__link', 'Turn them on in Settings');
      link.setAttribute('href', SETTINGS_HREF);
      return note('Recommendations are switched off.', link);
    }
    var empty = note('No recommendations yet.');
    if (payload.refresh) empty.appendChild(el('span', 'watch-row__hint', 'Refresh picks to fetch the first ones.'));
    return empty;
  }

  function fillTools(tools, payload) {
    var keepRefresh = tools.querySelector('[data-picks-refresh]');
    tools.textContent = '';
    if (!payload.enabled) return;
    if ((payload.batches || []).length) {
      tools.appendChild(picksButton(payload));
      fillPicksMenu(payload);
    }
    if (payload.refresh) tools.appendChild(keepRefresh || refreshButton());
  }

  /* The Recommended row for *payload*; null when a viewer has it switched off. */
  function recSection(payload) {
    if (!payload.enabled && !payload.refresh) return null;
    var section = rowSection('recommended', REC_TITLE);
    var tools = el('div', 'watch-row__tools');
    section.querySelector('.watch-row__head').appendChild(tools);
    var body = recBody(payload);
    section.appendChild(body);
    rec = { section: section, tools: tools, body: body, payload: payload };
    fillTools(tools, payload);
    if (payload.refresh && payload.enabled) {
      setRefresh(refreshState(payload.refresh), payload.refresh.next_available_at);
      if (payload.refresh.running) poll();
    }
    return section;
  }

  function recFailed(err) {
    if (loggedOut(err)) return never();
    rec = null;
    var section = rowSection('recommended', REC_TITLE);
    section.appendChild(failedNote(REC_TITLE, 'recommended'));
    return section;
  }

  function fetchRec(batch) {
    return MM.api.get(RECOMMENDED_URL + (batch ? '?batch=' + encodeURIComponent(batch) : ''));
  }

  function loadRecommended() {
    return fetchRec(null).then(recSection, recFailed);
  }

  /* Put a newer answer into the row that is there, without moving it. */
  function updateRec(payload) {
    if (picksMenu) picksMenu.close(false);
    var refocus = rec.tools.contains(document.activeElement) && document.activeElement.hasAttribute('data-picks-menu');
    rec.payload = payload;
    fillTools(rec.tools, payload);
    if (refocus) {
      var pill = rec.tools.querySelector('[data-picks-menu]');
      if (pill) pill.focus({ preventScroll: true });
    }
    var body = recBody(payload);
    var old = rec.body;
    rec.body = body;
    old.removeAttribute('aria-busy');
    return swapBody(old, body);
  }

  function chooseBatch(id) {
    if (!rec || !id || id === rec.payload.batch) return;
    rec.body.setAttribute('aria-busy', 'true');
    fetchRec(id).then(updateRec, function (err) {
      if (loggedOut(err)) return;
      rec.body.removeAttribute('aria-busy');
      core.toast(MM.api.messageOf(err, 'Couldn’t load those picks. Try again.'), { variant: 'error' });
    });
  }

  function retryRec(button) {
    var old = button.closest('[data-discover-row]');
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
    fetchRec(null).then(function (payload) {
      var section = recSection(payload);
      if (!section) { old.remove(); return; }
      // Read before the swap: a row fading out is inert, so it loses the focus.
      var focused = document.activeElement === button;
      core.swap(old, section);
      startRows(section);
      var focus = section.querySelector('.watch-row__scroll, [data-picks-menu]');
      if (focus && focused) core.focusSoftly(focus);
    }, function (err) {
      if (loggedOut(err)) return;
      button.removeAttribute('aria-disabled');
      button.setAttribute('aria-busy', 'false');
      core.toast('Still couldn’t load your picks. Try again in a moment.', { variant: 'error' });
    });
  }


  function refreshFailed(message) {
    polling = false;
    setRefresh('ready');
    setRecError(message);
  }

  /* Ask for new picks; the server says no while the last refresh cools down. */
  function refresh(button) {
    if (polling || button.getAttribute('aria-disabled') === 'true') return;
    setRecError(null);
    setRefresh('busy');
    MM.api.post(REFRESH_URL).then(function (data) {
      if (data && (data.status === 'started' || data.status === 'already_running')) { poll(); return; }
      refreshFailed('Couldn’t refresh picks. Try again.');
    }).catch(function (err) {
      if (err && err.status === 401) { toLogin(); return; }
      if (err && (err.status === 429 || err.status === 409)) {
        var data = err.data || {};
        var next = data.next_available_at ||
          new Date(Date.now() + (data.cooldown_seconds || 0) * 1000).toISOString();
        rec.payload.refresh = { available: false, next_available_at: next, running: false };
        fillPicksMenu(rec.payload);
        setRefresh('cooldown', next);
        return;
      }
      refreshFailed(MM.api.messageOf(err, 'Couldn’t refresh picks. Try again.'));
    });
  }

  /* Follow a running refresh to its end, then show the new picks in place. */
  function poll() {
    if (polling) return;
    polling = true;
    setRefresh('busy');
    var failures = 0;
    var idle = 0;
    function later(ms) { setTimeout(tick, ms); }
    function tick() {
      if (!polling) return;
      if (document.hidden) { later(POLL_MS); return; }
      MM.api.get(STATUS_URL).then(function (st) {
        failures = 0;
        if (st.status === 'done') {
          if (!(st.result && st.result.ok)) {
            refreshFailed((st.result && st.result.error) || 'Couldn’t refresh picks. Try again.');
            return;
          }
          fetchRec(null).then(function (payload) {
            polling = false;
            updateRec(payload);
            setRefresh(payload.refresh ? refreshState(payload.refresh) : 'ready',
              payload.refresh && payload.refresh.next_available_at);
            core.toast('New picks are in.', { variant: 'success' });
          }, function () {
            refreshFailed('New picks are ready, but they didn’t load. Reload the page to see them.');
          });
          return;
        }
        if (st.status === 'idle') {
          idle += 1;
          if (idle >= IDLE_LIMIT) {
            refreshFailed('The refresh stopped before it finished. Try again.');
            return;
          }
        } else {
          idle = 0;
        }
        later(POLL_MS);
      }).catch(function (err) {
        if (err && (err.status === 401 || err.status === 403)) { polling = false; toLogin(); return; }
        failures += 1;
        if (failures >= MAX_FAILURES) {
          refreshFailed('Couldn’t reach the server. Check your connection and try again.');
          return;
        }
        later(Math.min(POLL_MS * Math.pow(2, failures), POLL_MAX_MS));
      });
    }
    later(POLL_MS);
  }

  /* ── Landing on Recommended ── */

  /* /recommended sends people here with ?row=recommended: a query, since a
     #fragment of the redirect's own would drop an old #recommendation-<id>.
     The marker leaves the address at once; says whether it was there. */
  function takeLanding() {
    var params = (window.location.search || '').replace(/^\?/, '').split('&').filter(Boolean);
    var rest = params.filter(function (p) { return p !== 'row=recommended'; });
    if (rest.length === params.length) return false;
    window.history.replaceState(window.history.state, '', window.location.pathname +
      (rest.length ? '?' + rest.join('&') : '') + (window.location.hash || ''));
    return true;
  }

  /* Bring the Recommended row up under the top bar, unless there is none
     (picks off) or the person has scrolled while it loaded. */
  function showRecommended() {
    var row = document.querySelector('[data-discover-row="recommended"]');
    if (!row || window.scrollY > 0) return;
    var by = row.getBoundingClientRect().top - core.clearArea().top - 12;
    if (by > 0) window.scrollBy({ top: by, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* ── The slot ── */

  /* Put *content* where the skeleton is: both share one grid cell while the
     skeleton fades out and the rows fade in, so nothing below jumps. */
  function fill(slot, content) {
    slot.removeAttribute('aria-busy');
    var skel = slot.querySelector('.watch-discover-skel');
    function settle() {
      slot.textContent = '';
      if (content) {
        slot.appendChild(content);
        content.classList.remove('is-swapping-in');
      }
    }
    if (!content || !skel || core.reduceMotion()) {
      settle();
      if (content) startRows(content);
      return;
    }
    var layer = el('div', 'swap-layer watch-discover-layer');
    skel.replaceWith(layer);
    content.classList.add('is-swapping-in');
    layer.appendChild(content);
    layer.appendChild(skel);
    startRows(content);
    skel.classList.add('swap-out');
    var done = false;
    function finish() { if (!done) { done = true; settle(); } }
    skel.addEventListener('animationend', function (e) { if (e.target === skel) finish(); });
    setTimeout(finish, SWAP_FALLBACK_MS);
  }

  function start(slot, data) {
    isAdmin = !!data.is_admin;
    var discover = data.tmdb_configured ? loadDiscover() : Promise.resolve([]);
    Promise.all([loadRecommended(), discover]).then(function (got) {
      var rows = (got[0] ? [got[0]] : []).concat(got[1]);
      if (!rows.length) { fill(slot, null); return; }
      var content = el('div', 'watch-discover');
      content.appendChild(el('p', 'watch-group-label', 'Discover'));
      rows.forEach(function (r) { content.appendChild(r); });
      fill(slot, content);
      if (landing) showRecommended();
    });
  }

  function pageData() {
    var island = byId('watch-page-data');
    try { return JSON.parse(island ? island.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  document.addEventListener('mm:title-changed', function (e) { updateBadges(e.detail); });

  document.addEventListener('click', function (e) {
    var target = e.target;
    if (!target.closest) return;
    var retry = target.closest('[data-discover-retry]');
    if (retry && retry.getAttribute('aria-disabled') !== 'true') {
      if (retry.getAttribute('data-discover-retry') === 'recommended') retryRec(retry); else retryDiscover(retry);
      return;
    }
    var button = target.closest('[data-picks-refresh]');
    if (button && isAdmin) refresh(button);
  });

  watch.browse = { card: card, badge: badge };

  var landing = takeLanding();
  var slot = byId('watch-discover-slot');
  if (slot) start(slot, pageData());
})();
