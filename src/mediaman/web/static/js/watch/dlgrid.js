/* Watch: Downloading, grown out of its row at /?dl=1. Every download gets the row's card, with
 * one line under it of what the row has no room for (its size, its last search, what is wrong),
 * its episodes, and an admin's menu of it; the whole card opens its title. Groups: Ready to watch (for
 * the minute the row says so), Needs attention, Downloading, Looking for a copy, Coming soon. While open it reads /api/downloads every 2 s and
 * patches each card in place.
 *
 * Every link to it is <a href="/?dl=1" data-dl-open>, which works without JavaScript too. One in
 * the row grows the row's cards into the grid; Escape, the chevron and Back fold it again.
 *
 * Needs watch/core.js, watch/address.js and watch/downloading.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var address = watch.address;
  var browse = core.byId('watch-browse');
  if (!browse || !address || !watch.downloading) return;

  var el = core.el;
  var toArray = core.toArray;
  var icon = window.MM.dom.icon;

  var URL_ALL = '/api/downloads';
  var POLL_MS = 2000;
  var PLAY_AGAIN_MS = 10000;
  /* An answer this late is told; one that outlasts what the server itself gives a service that
     hangs will never come, and another is asked for. */
  var LATE_MS = 20000;
  var HUNG_MS = 120000;
  var BACKOFF_MAX = 30000;
  var FALL_UP = 28; // px the rows above the row go up by as it grows, and come back from
  var FALL_DOWN = 44; // and those below it, down
  var RISE = 26; // px a grid card that was not in the row rises from
  var RACE_MS = 120; // how long a tap waits for a first answer before it grows into placeholders
  // What the address of anything else that holds the page says; opening Downloading clears it.
  var OTHERS = { all: null, view: null, q: null, genre: null, type: null, where: null, decade: null, person: null, page: null, per_page: null, sort: null };
  var GROUPS = [['ready', 'Ready to watch'], ['attention', 'Needs attention'], ['coming', 'Downloading'], ['looking', 'Looking for a copy'], ['soon', 'Coming soon']];
  var TOP_STATES = ['downloading', 'almost_ready', 'needs_attention', 'finding_copy', 'queued', 'searching'];
  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$/; // as watch/downloading.js checks a row's
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  var admin = false;
  var setUp = true;
  (function () {
    try {
      var data = JSON.parse(core.byId('watch-page-data').textContent);
      admin = data.is_admin === true;
      var can = data.can_download;
      if (can) setUp = !!(can.movie || can.show);
    } catch (e) { /* a viewer's page, set up */ }
  })();

  var dom = null; // { section, title, count, note, noteText, empty, grid }
  var isOpen = false;
  var last = null; // the last answer
  var lost = false; // mediaman did not answer, or not in time
  var failures = 0; // askings failed in a row
  var request = null; // the asking under way
  var cards = {}; // by key: the card node
  var heads = {}; // by group: its heading
  var known = {}; // by download id: what the last answer said of it
  var menu = null; // an admin's menu of a card (watch/keep.js)
  var skels = null; // { nodes, cards }: what holds the grid's place until the first answer
  var growing = false; // the grid is growing out of the row
  var timer = null;
  var asked = 0; // which asking is the current one
  var opener = null;
  var tapped = false; // opened by a tap or a click, not a key: its heading takes no ring
  var savedY = 0;
  var lefts = []; // how far each row was scrolled when the grid took the page
  var moving = null; // ends the motion under way

  /* ── The layer ── */

  function build() {
    if (dom) return;
    startMenu();
    var section = el('section', 'dlg');
    section.id = 'watch-dl';
    section.setAttribute('aria-labelledby', 'watch-dl-title');
    section.hidden = true;
    var head = section.appendChild(el('div', 'dlg__head'));
    head.appendChild(el('div', 'dlg__glass'));
    var back = head.appendChild(el('a', 'dlg__back'));
    back.setAttribute('href', '/');
    back.setAttribute('aria-label', 'Back to all rows');
    back.setAttribute('data-dl-back', '');
    back.appendChild(icon('i-chevron-left'));
    var names = head.appendChild(el('div', 'dlg__names'));
    var title = names.appendChild(el('h1', 'dlg__title', 'Downloading'));
    title.id = 'watch-dl-title';
    title.setAttribute('tabindex', '-1');
    title.setAttribute('data-dl-heading', '');
    title.addEventListener('blur', function () { title.classList.remove('is-quiet'); });
    var count = names.appendChild(el('span', 'dlg__count'));
    var body = section.appendChild(el('div', 'dlg__body'));
    var note = body.appendChild(el('p', 'dlg__note'));
    note.hidden = true;
    note.appendChild(icon('i-triangle-exclamation'));
    var noteText = note.appendChild(el('span'));
    var empty = body.appendChild(el('div', 'grid-empty dlg__empty'));
    empty.hidden = true;
    var grid = body.appendChild(el('div', 'dlg__grid'));
    // Last, so that the banner stays the first of the page and runs up under the bar.
    browse.appendChild(section);
    dom = { section: section, head: head, back: back, title: title, count: count, body: body, note: note, noteText: noteText, empty: empty, grid: grid };
  }

  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }

  /* ── What a card says ── */

  function groupOf(item) {
    if (item.state === 'needs_attention') return 'attention';
    if (item.state === 'searching' || item.state === 'finding_copy') return 'looking';
    return 'coming'; // downloading, waiting to import, and queued: it has a copy and waits its turn
  }

  function nameOf(text) { return String(text || '').trim().toLowerCase(); }

  /* The row's item of the same title, where only one is of it on each side: what the row knows
     of it, its title's page and its wide picture, the downloads do not say. */
  function rowItemOf(item, items) {
    var kind = item.media_type === 'movie' ? 'movie' : 'show';
    var name = nameOf(item.title);
    var same = function (other) { return nameOf(other.title) === name; };
    if (items.filter(same).length !== 1) return null;
    var found = watch.downloading.items().filter(function (row) { return row.kind === kind && same(row); });
    return found.length === 1 ? found[0] : null;
  }

  /* The minutes left the server gives, 0 under a minute; null where it gives none. */
  function minutesLeft(minutes) {
    return typeof minutes === 'number' && isFinite(minutes) && minutes >= 0 ? Math.round(minutes) : null;
  }

  /* The title a download opens, as the server names it; where the server does not say, the row's
     of the same name. */
  function refOf(item, row) {
    if (!('title_ref' in item)) return (row && row.ref) || null;
    return typeof item.title_ref === 'string' && REF_RE.test(item.title_ref) ? item.title_ref : null;
  }

  /* What critics scored it, as the server says, else as the row's card has it. */
  function scoresOf(item, row) {
    var s = item.scores && typeof item.scores === 'object' ? item.scores : row || {};
    return { rt: s.rt, imdb: s.imdb, rating: row ? row.rating : null };
  }

  /* What the row's card is drawn from, made of what the downloads say of *item*. Why it needs
     attention is said on the line under it, whole in its menu. */
  function topOf(item, items) {
    var row = rowItemOf(item, items);
    var paused = item.state === 'paused';
    // One paused waits as one queued does, and its line says why.
    var state = paused ? 'queued' : TOP_STATES.indexOf(item.state) >= 0 ? item.state : 'searching';
    var active = state === 'downloading' || state === 'almost_ready';
    var percent = Math.max(0, Math.min(100, Number(item.progress) || 0));
    var ref = refOf(item, row);
    var scores = scoresOf(item, row);
    return {
      key: String(item.id), title: String(item.title || ''), kind: item.media_type === 'movie' ? 'movie' : 'show', state: state,
      percent: active ? percent : 0,
      minutes_left: state === 'downloading' ? minutesLeft(item.minutes_left) : null,
      line: (state === 'almost_ready' && item.state_label === 'Waiting to import') || paused ? String(item.state_label || '')
        : state === 'needs_attention' ? String(item.stopped || '') : '',
      held: false, ready_seconds: null, playable: false,
      ref: ref, href: row && row.ref === ref ? row.href : ref ? '/#title=' + ref : '',
      poster_url: item.poster_url || (row && row.poster_url) || '', art_url: row ? row.art_url : '',
      rt: scores.rt, imdb: scores.imdb, rating: scores.rating,
      attention: '', attention_hint: '', retrying: false,
      fixing: state === 'needs_attention' ? String(item.fixing || '') : ''
    };
  }

  function episodeWords(ep) {
    if (ep.is_pack_episode) return 'In pack';
    var state = ep.state || 'searching';
    if (state === 'ready') return 'Ready';
    if (state === 'downloading') return (ep.progress || 0) + '%';
    if (state === 'queued') return 'Queued';
    if (state === 'needs_attention') return 'Stuck';
    return 'Searching';
  }

  /* ── Cards ── */

  /* The one line under a card, its controls at the line's end, and the episodes that open under it. */
  function footOf(card) {
    var foot = card.querySelector('.dwx__foot');
    if (foot) return foot;
    foot = card.appendChild(el('div', 'dwx__foot'));
    foot.appendChild(el('p', 'dwx__say'));
    card.appendChild(el('div', 'dwx__drawer')).appendChild(el('div')).appendChild(el('ul', 'dwx__list'));
    return foot;
  }

  // Only those not yet fetching link to Radarr or Sonarr, for an admin.
  function linksOut(state) { return admin && state !== 'downloading' && state !== 'almost_ready'; }

  function fixWords(item) {
    return item.fixing || item.stopped || (item.retrying ? 'mediaman will look for another copy'
      : (admin && item.attention_hint) || 'Won’t finish until an admin sorts it out');
  }

  /* [what stands out, the rest, what is kept when the line is cut]: what the row's card has no
     room for, in one line. */
  function sayOf(item) {
    var state = item.state;
    /* Of one mediaman is mending, its line says so. */
    // The card's line says it stopped; the foot says why, or what is left to do.
    if (state === 'needs_attention' && item.stopped) return ['', (admin && item.attention) || 'Won’t finish until an admin sorts it out'];
    if (state === 'needs_attention') return ['', (admin && item.attention) || (item.fixing ? 'Nothing for you to do' : fixWords(item))];
    if (state === 'paused') return ['', ['Waiting to be resumed', item.size_total].filter(Boolean).join(' · ')];
    if (state === 'finding_copy') return ['', 'Finding another copy'];
    if (state === 'searching') return ['', item.search_hint || 'Waiting for its first search'];
    if (state === 'queued') return ['', ['Waiting its turn', item.size_total].filter(Boolean).join(' · ')];
    var parts = [];
    if (item.episode_summary) parts.push(item.episode_summary);
    if (state === 'almost_ready') parts.push('Post-processing…');
    if (item.size_done && item.size_total) return [item.size_done, ' of ' + item.size_total, parts.length ? ' · ' + parts.join(' · ') : ''];
    return ['', parts.join(' · ') || item.state_label || 'Downloading'];
  }

  function setSay(p, words, whole) {
    var said = words.join('|');
    if (p.getAttribute('data-said') !== said) {
      p.setAttribute('data-said', said);
      p.textContent = '';
      var lead = words[2] ? p.appendChild(el('span', 'dwx__lead')) : p;
      if (words[0]) lead.appendChild(el('b', '', words[0]));
      if (words[1]) lead.appendChild(document.createTextNode(words[1]));
      if (words[2]) p.appendChild(el('span', 'dwx__keep', words[2]));
    }
    if (whole) p.setAttribute('title', words.join(''));
    else p.removeAttribute('title');
  }

  /* A control about to go gives the focus to its card's next one, else to the card's first. */
  function letGoOf(node, card) {
    if (!node.contains(document.activeElement)) return;
    var rest = toArray(card.querySelectorAll('a[href], button')).filter(function (n) { return !node.contains(n); });
    var to = rest[0] || nextControl(card);
    if (to) to.focus({ preventScroll: true });
  }

  /* The control *name* of *box*, made by *make*, or taken away where *make* is null. */
  function control(box, name, make, card) {
    var node = box.querySelector('[data-act="' + name + '"]');
    if (!make) {
      if (node) {
        if (menu && menu.trigger() === node) menu.close(false);
        letGoOf(node, card);
        node.remove();
      }
      return null;
    }
    if (!node) {
      node = box.appendChild(make());
      node.setAttribute('data-act', name);
    }
    return node;
  }

  function tool(glyph) {
    var node = el('button', 'dwx__tool');
    node.setAttribute('type', 'button');
    node.appendChild(icon(glyph));
    return node;
  }

  function dayOf(seconds) {
    var d = new Date(seconds * 1000);
    return d.getDate() + ' ' + MONTHS[d.getMonth()];
  }

  /* What the confirm says of it under its name. */
  function factOf(item, upcoming) {
    if (upcoming) return item.release_label || '';
    var n = Number(item.search_count) || 0;
    var since = Number(item.searching_since) || 0;
    if (!n) return item.search_hint || '';
    return 'Searched ' + (n === 1 ? 'once' : n + '×') + (since > 0 ? ' since ' + dayOf(since) : '');
  }

  /* What dl-abandon.js reads off the control that opens its confirm. */
  function arm(node, item, upcoming) {
    if (upcoming) node.setAttribute('data-abandon-upcoming', '1');
    node.setAttribute('data-dl-id', String(item.id));
    node.setAttribute('data-arr-id', String(item.arr_id || 0));
    node.setAttribute('data-kind', item.media_type === 'movie' ? 'movie' : 'series');
    node.setAttribute('data-title', item.title || '');
    node.setAttribute('data-stuck-seasons', JSON.stringify(item.stuck_seasons || []));
    node.setAttribute('data-poster', core.safePath(item.poster_url) || '');
    node.setAttribute('data-fact', factOf(item, upcoming));
  }

  /* An admin's way into Radarr or Sonarr: only a page of theirs, on the web. */
  function linkOf(item) {
    var link = linksOut(item.state) && typeof item.arr_link === 'string' ? item.arr_link : '';
    return /^https?:\/\/[^\s]+$/i.test(link) ? link : '';
  }

  /* What Radarr or Sonarr says is wrong, an admin's alone. */
  function whyOf(item) { return admin && item.state === 'needs_attention' && item.attention ? item.attention : ''; }

  /* What the server lets go of, but never once it has a copy coming. */
  function canAbandon(item) { return !!item.abandon_visible && item.state !== 'downloading' && item.state !== 'almost_ready'; }

  /* One paused in NZBGet by itself, which an admin may resume: never all of NZBGet. */
  function canResume(item) { return item.state === 'paused' && !!(item.paused_ids && item.paused_ids.length); }

  /* One mediaman stopped replacing, which an admin may have it try again. */
  function canTryAgain(item) { return item.state === 'needs_attention' && !!item.stopped; }

  function menuHolds(item) { return admin && !!(whyOf(item) || linkOf(item) || canAbandon(item) || canResume(item) || canTryAgain(item)); }

  function fillFoot(card, item) {
    var foot = footOf(card);
    setSay(foot.querySelector('.dwx__say'), sayOf(item), item.state === 'needs_attention');
    var episodes = item.episodes || [];
    var eps = control(foot, 'eps', episodes.length ? function () {
      var node = el('button', 'dwx__eps');
      node.setAttribute('type', 'button');
      node.setAttribute('aria-expanded', 'false');
      node.setAttribute('data-dl-eps', '');
      node.appendChild(el('span'));
      node.appendChild(icon('i-chevron-down'));
      return node;
    } : null, card);
    if (eps) setText(eps.querySelector('span'), episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '') + (item.has_pack ? ' · pack download' : ''));
    else card.classList.remove('is-open');
    var more = control(foot, 'more', menuHolds(item) ? function () {
      var node = tool('i-ellipsis');
      node.setAttribute('data-dl-more', '');
      node.setAttribute('aria-haspopup', 'menu');
      node.setAttribute('aria-expanded', 'false');
      node.setAttribute('title', 'More');
      return node;
    } : null, card);
    if (more) {
      // After the episodes, whichever came first.
      if (foot.lastElementChild !== more) foot.appendChild(more);
      more.setAttribute('aria-label', 'More for ' + (item.title || 'this download'));
      arm(more, item, false);
      // A menu open on what a poll has changed holds what is no longer so.
      var holds = JSON.stringify([item.state, canAbandon(item), linkOf(item), whyOf(item), canResume(item), canTryAgain(item)]);
      if (more.getAttribute('data-holds') !== holds) {
        if (menu && menu.trigger() === more) menu.close(false);
        more.setAttribute('data-holds', holds);
      }
    }
    var list = card.querySelector('.dwx__list');
    var said = JSON.stringify(episodes.map(function (ep) { return [ep.label, ep.title, ep.state, ep.progress, ep.is_pack_episode]; }));
    if (list.getAttribute('data-said') === said) return;
    list.setAttribute('data-said', said);
    list.textContent = '';
    episodes.forEach(function (ep, i) {
      var row = list.appendChild(el('li', 'dwx__ep' + (ep.state === 'ready' ? ' is-ready' : '') + (ep.is_pack_episode ? ' is-pack' : '')));
      row.style.setProperty('--i', String(i));
      row.appendChild(el('span', 'dwx__ep-n', ep.label || ''));
      row.appendChild(el('span', 'dwx__ep-t', ep.title || ''));
      var bar = row.appendChild(el('span', 'dwx__ep-bar'));
      bar.setAttribute('aria-hidden', 'true');
      bar.style.setProperty('--p', String(Math.max(0, Math.min(100, Number(ep.progress) || 0))));
      row.appendChild(el('span', 'dwx__ep-s', episodeWords(ep)));
    });
  }

  function bigCard(key, top) {
    var node = cards[key];
    if (!node) {
      node = cards[key] = watch.downloading.card(top);
      node.classList.remove('watch-card');
      node.classList.add('dwx');
      node.setAttribute('data-dl-card', '');
      // The row's Play is drawn in its link; a ready card here has one of its own (readyCardOf).
      var drawn = node.querySelector('.dw__link .dw__go');
      if (drawn) drawn.remove();
    } else watch.downloading.paint(node, top, false);
    return node;
  }

  function cardOf(item, items) {
    var node = bigCard('dl:' + item.id, topOf(item, items));
    node.setAttribute('data-dl-id', String(item.id));
    known[String(item.id)] = item;
    // One with no title of its own is no link here: it would only open this again.
    var link = node.querySelector('.dw__link');
    if (link.hasAttribute('data-dl-open')) {
      link.removeAttribute('href');
      link.removeAttribute('data-dl-open');
    }
    fillFoot(node, item);
    return node;
  }

  /* The server's format_bytes. */
  function sizeOf(n) {
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i && n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + units[i];
  }

  function agoOf(seconds) {
    var m = Math.floor(Math.max(0, Date.now() / 1000 - seconds) / 60);
    if (m < 1) return 'just now';
    if (m < 60) return m + ' min ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + ' hr ago';
    var d = Math.floor(h / 24);
    return d === 1 ? 'yesterday' : d + ' days ago';
  }

  function readyWords(item) {
    var at = Number(item.finished_at);
    var bytes = Number(item.size_bytes);
    var parts = [];
    if (item.finished_at != null && at > 0 && isFinite(at)) parts.push('Finished ' + agoOf(at));
    if (item.size_bytes != null && bytes > 0 && isFinite(bytes)) parts.push(sizeOf(bytes));
    return parts.join(' · ') || 'Now in your library';
  }

  /* Play goes where the title's own Play does, as its preview has it; with nowhere to go there is
     none, and the card opens the title. A card turns ready before the library has caught up, so
     one with no Play yet asks again, though not more than every PLAY_AGAIN_MS. */
  function askPlay(node, item) {
    if (!item.ref || node.playAsking || node.querySelector('.dwx__play')) return;
    if (node.playAskedAt && Date.now() - node.playAskedAt < PLAY_AGAIN_MS) return;
    node.playAsking = true;
    node.playAskedAt = Date.now();
    var title = item.title || '';
    window.MM.api.get('/api/watch/preview/' + encodeURIComponent(item.ref)).then(function (d) {
      node.playAsking = false;
      var href = d && d.in_library && d.play && core.safePath(d.play.href);
      if (!href || !node.isConnected || node.querySelector('.dwx__play')) return;
      var word = String(d.play.label || 'Play').split(' ')[0];
      var go = el('a', 'dwx__play');
      go.setAttribute('data-act', 'play');
      go.setAttribute('href', href);
      go.setAttribute('aria-label', word + ' ' + title);
      go.setAttribute('title', word);
      go.appendChild(icon('i-play'));
      node.insertBefore(go, node.querySelector('.dw__link').nextSibling);
    }, function () { node.playAsking = false; });
  }

  /* One that is done, as the row has it: the downloads no longer list it. */
  function readyCardOf(item) {
    var node = bigCard('ready:' + item.key, item);
    node.classList.add('dwx--ready');
    setSay(footOf(node).querySelector('.dwx__say'), ['', readyWords(item)], false);
    askPlay(node, item);
    return node;
  }

  function soonCardOf(item) {
    var key = 'soon:' + item.id;
    var node = cards[key];
    if (!node) {
      node = cards[key] = el('article', 'dwx dwx--small');
      node.setAttribute('data-dl-card', '');
      var open = node.appendChild(el('a', 'dwx__open dwx__small-link'));
      var img = open.appendChild(el('span', 'dwx__pic')).appendChild(el('img'));
      img.setAttribute('alt', '');
      img.setAttribute('data-broken-on-error', '');
      var text = open.appendChild(el('span', 'dwx__small-text'));
      text.appendChild(el('span', 'dw__title'));
      text.appendChild(el('span', 'dwx__small-line'));
    }
    node.setAttribute('data-dl-id', String(item.id));
    known[String(item.id)] = item;
    var opener = node.querySelector('.dwx__open');
    var ref = refOf(item, null);
    if (ref) {
      opener.setAttribute('href', '/#title=' + ref);
      opener.setAttribute('data-title-ref', ref);
      opener.setAttribute('data-title', item.title || '');
      opener.setAttribute('data-kind', item.media_type === 'movie' ? 'movie' : 'show');
    } else ['href', 'data-title-ref', 'data-title', 'data-kind'].forEach(function (name) { opener.removeAttribute(name); });
    var img2 = node.querySelector('img');
    var src = core.safePath(item.poster_url);
    if (src && img2.getAttribute('src') !== src) img2.setAttribute('src', src);
    else if (!src) img2.removeAttribute('src');
    var name = node.querySelector('.dw__title');
    setText(name, item.title || '');
    var had = node.querySelector('.watch-card__scores');
    var scores = item.scores && typeof item.scores === 'object' ? core.scores({ rt: item.scores.rt, imdb: item.scores.imdb }) : null;
    if ((had ? had.textContent : '') !== (scores ? scores.textContent : '')) {
      if (had) had.remove();
      if (scores) name.parentNode.insertBefore(scores, name);
    }
    setText(node.querySelector('.dwx__small-line'), item.release_label || '');
    var stop = control(node, 'stop', admin ? function () {
      var b = tool('i-xmark');
      b.setAttribute('data-abandon-trigger', '');
      b.setAttribute('title', 'Stop tracking');
      return b;
    } : null, node);
    if (stop) {
      arm(stop, item, true);
      stop.setAttribute('aria-label', 'Stop tracking ' + (item.title || 'this title'));
    }
    return node;
  }

  /* ── An admin's menu of a card: why it is stuck, its page in Radarr or Sonarr, Abandon ── */

  function menuItem(tag, glyph, words, className) {
    var node = menu.node.appendChild(el(tag, 'watch-keep-menu__item' + (className ? ' ' + className : '')));
    node.setAttribute('role', 'menuitem');
    node.setAttribute('tabindex', '-1');
    if (tag === 'button') node.setAttribute('type', 'button');
    node.appendChild(icon(glyph));
    if (words) node.appendChild(el('span', '', words));
    return node;
  }

  function fillMenu(trigger) {
    var box = menu.node;
    box.textContent = '';
    var card = trigger.closest('[data-dl-card]');
    var item = card && known[card.getAttribute('data-dl-id')];
    if (!item) return;
    box.setAttribute('aria-label', 'More for ' + (item.title || 'this download'));
    var why = whyOf(item);
    if (why) {
      var facts = menuItem('div', 'i-circle-info', '', 'dwx-menu__facts');
      facts.setAttribute('aria-disabled', 'true');
      var words = facts.appendChild(el('span', 'dwx-menu__words'));
      words.appendChild(el('span', 'dwx-menu__by', 'Details'));
      words.appendChild(el('span', 'dwx-menu__why', (item.arr_source ? item.arr_source + ' says: ' : '') + why));
      words.appendChild(el('span', 'dwx-menu__fix', fixWords(item)));
    }
    if (canResume(item)) menuItem('button', 'i-play', 'Resume download').setAttribute('data-dl-resume', '');
    if (canTryAgain(item)) menuItem('button', 'i-rotate-right', 'Try again').setAttribute('data-dl-try-again', '');
    var link = linkOf(item);
    if (link) {
      var a = menuItem('a', 'i-arrow-up-right-from-square', 'Open in ' + (item.arr_source || 'Radarr/Sonarr'));
      a.setAttribute('href', link);
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    }
    if (canAbandon(item)) {
      if (box.children.length) box.appendChild(el('div', 'dwx-menu__line')).setAttribute('role', 'separator');
      menuItem('button', 'i-xmark', 'Abandon search…', 'is-danger').setAttribute('data-dl-abandon', '');
    }
  }

  /* Resume a download paused in NZBGet, or have mediaman try a stopped one again. */
  function act(trigger, resume) {
    var card = trigger.isConnected && trigger.closest('[data-dl-card]:not(.is-going)');
    var item = card && known[card.getAttribute('data-dl-id')];
    if (!item || !(resume ? canResume(item) : canTryAgain(item))) return;
    var title = item.title || 'this download';
    var path = '/api/downloads/' + encodeURIComponent(String(item.id)) + (resume ? '/resume' : '/try-again');
    window.MM.api.post(path, { arr_id: item.arr_id || null }).then(function () {
      window.UIFeedback.toast(resume ? 'Resumed ' + title : 'mediaman will try ' + title + ' again within the hour', { variant: 'success' });
      document.dispatchEvent(new CustomEvent('mediaman:downloads:refresh'));
    }, function (err) {
      window.UIFeedback.error(window.MM.api.messageOf(err, resume ? 'Couldn’t resume it. Try again.' : 'Couldn’t try it again. Try again.'));
    });
  }

  function startMenu() {
    if (menu || !admin || !watch.keep || !watch.keep.menu) return;
    var node = el('div', 'watch-keep-menu watch-more-menu dwx-menu');
    node.id = 'watch-dl-menu';
    node.setAttribute('role', 'menu');
    node.hidden = true;
    (core.byId('watch-page') || document.body).appendChild(node);
    menu = watch.keep.menu(node, {
      trigger: '[data-dl-more]',
      fill: fillMenu,
      choose: function (trigger, item) {
        if (item.hasAttribute('data-dl-resume') || item.hasAttribute('data-dl-try-again')) {
          act(trigger, item.hasAttribute('data-dl-resume'));
          return;
        }
        if (!item.hasAttribute('data-dl-abandon') || !window.MM.dlAbandon) return;
        // Only what the last answer still lets go of, on a card still here.
        var card = trigger.isConnected && trigger.closest('[data-dl-card]:not(.is-going)');
        var now = card && known[card.getAttribute('data-dl-id')];
        if (now && canAbandon(now)) window.MM.dlAbandon.open(trigger);
      },
      spare: function () { return dom && dom.title; }
    });
  }

  function headOf(group, words, n) {
    var node = heads[group];
    if (!node) {
      node = heads[group] = el('p', 'grid-group');
      node.setAttribute('role', 'heading');
      node.setAttribute('aria-level', '2');
      node.appendChild(el('span', '', words));
      node.appendChild(el('span', 'dlg__n'));
    }
    setText(node.querySelector('.dlg__n'), ' · ' + n);
    return node;
  }

  /* The first control after *node* in the grid, else before it, else the heading. */
  function nextControl(node) {
    var kids = toArray(dom.grid.children);
    var at = kids.indexOf(node);
    function controls(list) {
      return list.reduce(function (all, kid) {
        return kid.classList.contains('is-going') ? all : all.concat(toArray(kid.querySelectorAll('a[href], button')));
      }, []);
    }
    var before = controls(kids.slice(0, Math.max(0, at)));
    return controls(kids.slice(at + 1))[0] || before[before.length - 1] || dom.title;
  }

  /* ── Drawing ── */

  function curly(text) { return String(text).replace(/'/g, '’'); }

  function wordsOf(data, items, ready) {
    if (!setUpIn(data)) return '';
    var soon = (data.upcoming || []).length;
    var bits = [];
    if (items.length) bits.push(items.length + '\u00a0in\u00a0progress');
    // In an outage nobody can tell that nothing downloads: an admin is told what does not
    // answer, a viewer only that some may be missing, or nothing where nothing is shown.
    else if (!data.outage) bits.push('Nothing downloading');
    else if (admin) bits.push(curly(data.outage));
    if (soon) bits.push(soon + '\u00a0coming\u00a0soon');
    if (ready) bits.push(ready + '\u00a0ready\u00a0to\u00a0watch');
    if (!admin && data.outage && items.length) bits.push('some may be missing');
    // A wrapped head never starts a line with the dot.
    return bits.join('\u00a0· ');
  }

  function setUpIn(data) {
    var said = data && data.downloads_set_up;
    return said ? !!(said.movies || said.shows) : setUp;
  }

  function noteOf(data, items) {
    if (lost) return 'Can’t reach mediaman right now.' + (data ? ' This is how it last was.' : '');
    if (!data || !data.outage || !items.length || !setUpIn(data)) return '';
    return curly(data.outage) + ' right now.';
  }

  function emptyOf(data, items) {
    if (items.length || (!data && !lost)) return null;
    if (data && !setUpIn(data)) {
      return admin ? ['i-download', 'Downloads aren’t set up', 'Connect Radarr or Sonarr in ', 'Settings']
        : ['i-download', 'Downloads aren’t set up', 'Ask whoever runs mediaman to set it up.'];
    }
    if (lost || data.outage) {
      var refused = admin && data && data.refused && data.refused.length ? data.refused : null;
      if (refused) {
        var names = refused.length > 1 ? refused.slice(0, -1).join(', ') + ' and ' + refused[refused.length - 1] : refused[0];
        return ['i-triangle-exclamation', 'Downloads can’t be checked right now', 'Check the details for ' + names + ' in ', 'Settings'];
      }
      return ['i-triangle-exclamation', 'Downloads can’t be checked right now', 'This page keeps trying.'];
    }
    return ['i-download', 'All caught up', 'Nothing downloading right now.'];
  }

  function drawEmpty(said) {
    var box = dom.empty;
    var key = said ? said.join('|') : '';
    if (box.getAttribute('data-said') === key) return;
    box.setAttribute('data-said', key);
    box.textContent = '';
    box.hidden = !said;
    if (!said) return;
    box.appendChild(icon(said[0]));
    box.appendChild(el('p', 'grid-empty__title', said[1]));
    if (!said[2]) return;
    var text = box.appendChild(el('p', 'grid-empty__text', said[2]));
    if (said[3]) {
      var link = text.appendChild(el('a', '', said[3]));
      link.setAttribute('href', '/settings#setg-integrations');
      text.appendChild(document.createTextNode('.'));
    }
  }

  function skeletonCard(item) {
    var card = el('div', 'dlg-skel');
    card.setAttribute('aria-hidden', 'true');
    var top = card.appendChild(el('div', 'dlg-skel__top'));
    top.appendChild(el('span', 'skel dlg-skel__poster'));
    var words = top.appendChild(el('span', 'dlg-skel__words'));
    words.appendChild(el('span', 'skel skel--line'));
    words.appendChild(el('span', 'skel skel--line'));
    // The card's one line, with the controls its card will have at its end: a show's episodes,
    // and an admin's menu where Radarr or Sonarr may be linked to.
    var foot = card.appendChild(el('div', 'dlg-skel__foot'));
    foot.appendChild(el('span', 'skel skel--line'));
    if (item.kind === 'show') foot.appendChild(el('span', 'skel dlg-skel__pill'));
    if (item.state && linksOut(item.state)) foot.appendChild(el('span', 'skel dlg-skel__tool'));
    return card;
  }

  /* The grid as the row says it will be: its groups' headings, and a card for each of the row's
     downloads with the room its band will take. Three cards where the row has said nothing. */
  function skeletons() {
    if (skels) return skels;
    var items = watch.downloading.items().slice(0, 6);
    if (!items.length) items = [{}, {}, {}];
    var groups = { attention: [], coming: [], looking: [] };
    items.forEach(function (item) { groups[groupOf(item)].push(item); });
    skels = { nodes: [], cards: {} };
    GROUPS.forEach(function (g) {
      var list = groups[g[0]];
      if (!list || !list.length) return;
      var head = el('p', 'grid-group dlg-skel-head');
      head.setAttribute('aria-hidden', 'true');
      head.appendChild(el('span', 'skel dlg-skel__label'));
      skels.nodes.push(head);
      list.forEach(function (item) {
        var card = skeletonCard(item);
        skels.nodes.push(card);
        if (item.title) skels.cards[nameOf(item.title)] = card;
      });
    });
    return skels;
  }

  /* What the row says of an outage, which the first answer will most likely say again. */
  function rowNote() {
    var slot = rowSlot();
    var note = slot && slot.querySelector('.dlx__notes.is-on .dlx__note');
    return note ? note.textContent.trim() : '';
  }

  function skeletonsHeld() { return !!skels && !!skels.nodes[0].parentNode; }

  /* The placeholders fade where they stood, over the answer's cards fading in. */
  function fadeAway(nodes, at) {
    var fade = { duration: core.tokenMs('--dur', 200), easing: ease(), fill: 'forwards' };
    nodes.forEach(function (n, i) {
      n.classList.add('is-going');
      n.style.left = at[i].left + 'px';
      n.style.top = at[i].top + 'px';
      n.style.width = at[i].width + 'px';
      n.style.height = at[i].height + 'px';
      dom.body.appendChild(n);
      var a = animate(n, [{ opacity: 1 }, { opacity: 0 }], fade);
      if (a) a.onfinish = function () { n.remove(); };
      else n.remove();
    });
  }

  function draw() {
    build();
    var data = last;
    var items = data ? (data.hero ? [data.hero] : []).concat(data.queue || []) : [];
    var upcoming = data && setUpIn(data) ? data.upcoming || [] : [];
    var ready = watch.downloading.items().filter(function (item) { return item.state === 'ready' && item.playable !== false; });
    var waiting = !data && !lost;
    var held = skeletonsHeld();
    // Placeholders still growing out of the row land before the answer takes their place.
    if (held && !waiting && growing) return;
    dom.body.setAttribute('aria-busy', waiting ? 'true' : 'false');
    var countHeld = dom.count.querySelector('.skel');
    if (waiting && !countHeld) dom.count.appendChild(el('span', 'skel dlg-skel__count')).setAttribute('aria-hidden', 'true');
    if (!waiting && countHeld) countHeld.remove();
    if (!waiting) setText(dom.count, data ? wordsOf(data, items, ready.length) : '');
    var note = noteOf(data, items) || (waiting ? rowNote() : '');
    if (note) setText(dom.noteText, note);
    dom.note.hidden = !note;
    drawEmpty(emptyOf(data, items.concat(ready)));
    known = {};
    var groups = { ready: ready.map(readyCardOf), attention: [], coming: [], looking: [], soon: [] };
    items.forEach(function (item) { groups[groupOf(item)].push(cardOf(item, items)); });
    upcoming.forEach(function (item) { groups.soon.push(soonCardOf(item)); });
    var want = waiting ? skeletons().nodes.slice() : [];
    GROUPS.forEach(function (g) {
      if (groups[g[0]].length) want.push(headOf(g[0], g[1], groups[g[0]].length));
      want = want.concat(groups[g[0]]);
    });
    var swap = held && !waiting && !core.reduceMotion();
    var leaving = swap ? skels.nodes.filter(function (n) { return n.parentNode; }) : [];
    var from = dom.body.getBoundingClientRect();
    var at = leaving.map(function (n) {
      var r = n.getBoundingClientRect();
      return { left: r.left - from.left, top: r.top - from.top, width: r.width, height: r.height };
    });
    place(want);
    // The header's count of what is ready leaves out what has been on show here.
    if (isOpen && ready.length) {
      document.dispatchEvent(new CustomEvent('mm:ready-shown', { detail: { keys: ready.map(function (item) { return item.key; }) } }));
    }
    if (!swap) return;
    var fade = { duration: core.tokenMs('--dur', 200), easing: ease() };
    toArray(dom.grid.children).concat(dom.empty.hidden ? [] : [dom.empty]).forEach(function (n) {
      animate(n, [{ opacity: 0 }, { opacity: 1 }], fade);
    });
    fadeAway(leaving, at);
  }

  /* The grid's nodes in the order of *want*, moving only those out of place; those gone pass
     their focus on. */
  function place(want) {
    var grid = dom.grid;
    var active = document.activeElement;
    // While it shows, a card or heading that goes fades where it stands, and is laid in among
    // those that stay until it has; placeholders go at once, as draw() fades them itself.
    var fades = isOpen && !dom.section.hidden;
    var order = want.slice();
    toArray(grid.children).forEach(function (node, i, all) {
      if (want.indexOf(node) >= 0) return;
      if (menu && menu.trigger() && node.contains(menu.trigger())) menu.close(false);
      if (node.contains(document.activeElement)) {
        var to = nextControl(node);
        if (to) to.focus({ preventScroll: true });
      }
      if (!fades || !(node.hasAttribute('data-dl-card') || node.hasAttribute('role'))) return;
      var before = all.slice(0, i).filter(function (n) { return order.indexOf(n) >= 0; }).pop();
      order.splice(before ? order.indexOf(before) + 1 : 0, 0, node);
      if (!node.classList.contains('is-going')) leave(node);
    });
    var next = grid.firstElementChild;
    order.forEach(function (node) {
      if (node === next) next = next.nextElementSibling;
      else grid.insertBefore(node, next);
    });
    toArray(grid.children).forEach(function (node) { if (order.indexOf(node) < 0) node.remove(); });
    // One going is never drawn again: a title that comes back has a card of its own.
    function stays(node) { return !!node.parentNode && !node.classList.contains('is-going'); }
    Object.keys(cards).forEach(function (key) { if (!stays(cards[key])) delete cards[key]; });
    Object.keys(heads).forEach(function (key) { if (!stays(heads[key])) delete heads[key]; });
    if (active && document.activeElement !== active && grid.contains(active) && !active.closest('.is-going')) active.focus({ preventScroll: true });
  }

  /* A card that goes fades and shrinks where it stands, then those after it slide into its place;
     with reduced motion it only fades, and the rest take their places at once. */
  function leave(node) {
    var still = core.reduceMotion();
    node.classList.add('is-going');
    var a = animate(node, still ? [{ opacity: 1 }, { opacity: 0 }] : [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.96)' }],
      { duration: core.tokenMs(still ? '--dur-fast' : '--dur', 200), easing: still ? 'linear' : core.token('--ease-exit', 'ease-in'), fill: 'forwards' });
    function gone() {
      if (!node.parentNode) return;
      var grid = node.parentNode;
      var was = still ? [] : toArray(grid.children).filter(function (n) { return n !== node; }).map(function (n) { return [n, n.getBoundingClientRect()]; });
      node.remove();
      was.forEach(function (w) {
        var r = w[0].getBoundingClientRect();
        var dx = w[1].left - r.left;
        var dy = w[1].top - r.top;
        if (dx || dy) animate(w[0], [{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }], { duration: core.tokenMs('--dur-slow', 250), easing: ease() });
      });
    }
    if (a) a.onfinish = gone;
    else gone();
  }

  /* ── Asking ── */

  function ask() {
    stopAsking();
    if (!isOpen || document.hidden) return Promise.resolve();
    var mine = asked;
    var ctl = request = new AbortController();
    var late = setTimeout(function () {
      if (mine !== asked) return;
      lost = true;
      draw();
    }, LATE_MS);
    var hung = setTimeout(function () { ctl.abort(); }, HUNG_MS);
    var wait = POLL_MS;
    return window.MM.api.get(URL_ALL, { signal: ctl.signal }).then(function (data) {
      if (mine !== asked || !isOpen) return;
      failures = 0;
      if (!data || typeof data !== 'object') return;
      last = data;
      lost = false;
      if (watch.downloading.hearAll) watch.downloading.hearAll(data);
      draw();
    }, function () {
      if (mine !== asked || !isOpen) return;
      failures += 1;
      wait = Math.min(POLL_MS * Math.pow(2, failures), BACKOFF_MAX);
      // One failed asking is no news while there is something to show: the next is close behind.
      if (failures > 1 || !last) {
        lost = true;
        draw();
      }
    }).then(function () {
      clearTimeout(late);
      clearTimeout(hung);
      if (request === ctl) request = null;
      if (mine === asked && isOpen) timer = setTimeout(ask, wait);
    });
  }

  /* An asking under way is given up: a phone put away may never hear back. */
  function stopAsking() {
    asked += 1;
    clearTimeout(timer);
    timer = null;
    if (request) request.abort();
    request = null;
  }

  /* ── Open and fold ── */

  function rowSlot() { return core.byId('watch-downloading'); }

  function rowHeading() {
    var slot = rowSlot();
    var link = slot && slot.querySelector('.row-open');
    return link && link.getClientRects().length ? link : null;
  }

  function scrollers() { return toArray(browse.querySelectorAll('.watch-row__scroll')); }

  function onScreen(r) { return r.width > 0 && r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth; }

  /* The row's cards on the screen and the grid's, by title: one whose title another shares
     is not paired with any. */
  function byTitle(nodes) {
    var found = {};
    var twice = {};
    nodes.forEach(function (node) {
      var title = node.querySelector('.dw__title');
      var r = node.getBoundingClientRect();
      if (!title || !onScreen(r)) return;
      var name = nameOf(title.textContent);
      if (found[name]) twice[name] = true;
      found[name] = { node: node, r: r };
    });
    Object.keys(twice).forEach(function (name) { delete found[name]; });
    return found;
  }

  function rowCards() {
    var slot = rowSlot();
    if (!slot) return {};
    return byTitle(toArray(slot.querySelectorAll('.dw')).filter(function (n) {
      return !n.classList.contains('is-leaving') && !n.classList.contains('dw--calm');
    }));
  }

  function gridCards() { return byTitle(toArray(dom.grid.querySelectorAll('.dwx:not(.dwx--small):not(.is-going)'))); }

  /* The page's other parts, above the row and below it. */
  function around() {
    var slot = rowSlot();
    var rows = slot && slot.parentNode;
    var up = [];
    var down = [];
    function seen(n) { return !n.hidden && n.getClientRects().length > 0; }
    toArray(browse.children).forEach(function (n) {
      if (n !== dom.section && n !== rows && seen(n)) up.push(n);
    });
    var below = false;
    if (rows && rows.parentNode) {
      toArray(rows.children).forEach(function (n) {
        if (n === slot) { below = true; return; }
        if (seen(n)) (below ? down : up).push(n);
      });
    }
    return { up: up, down: down, slot: slot };
  }

  function ease() { return core.token('--ease-out', 'ease-out'); }

  /* Where the grid will stand on the page once it has it alone. The first of the page (the
     banner) runs up under the bar by its top margin, which falls through to the page's top. */
  function landing() {
    var top = browse.getBoundingClientRect().top + window.scrollY;
    var first = toArray(browse.children).filter(function (n) { return n !== dom.section; })[0];
    if (!first || !first.getClientRects().length) return top;
    var cs = getComputedStyle(browse);
    if ((parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.borderTopWidth) || 0) > 0) return top;
    return top - (parseFloat(getComputedStyle(first).marginTop) || 0);
  }

  function pin(top) {
    dom.section.classList.add('is-moving');
    dom.section.style.top = top + 'px';
  }

  function unpin() {
    dom.section.classList.remove('is-moving');
    dom.section.style.top = '';
    dom.head.style.top = '';
  }

  /* The head takes its glass once the grid passes under the bar. */
  function stuck() {
    if (!dom) return;
    dom.head.classList.toggle('is-stuck', isOpen && !moving && dom.section.getBoundingClientRect().top < core.clearArea().top - 2);
  }

  function rowsAway(on) {
    if (on) lefts = scrollers().map(function (s) { return { node: s, left: s.scrollLeft }; });
    browse.classList.toggle('has-dl', on);
    if (!on) lefts.forEach(function (l) { if (l.node.scrollLeft !== l.left) l.node.scrollLeft = l.left; });
  }

  function finishMoving() {
    if (moving) moving();
  }

  /* Run *anims* for *ms*, then *done*; a motion begun before this one ends first. */
  function play(anims, ms, done) {
    var ended = false;
    var t = setTimeout(end, ms + 40);
    function end() {
      if (ended) return;
      ended = true;
      clearTimeout(t);
      if (moving === end) moving = null;
      done();
      anims.forEach(function (a) { if (a && a.cancel) a.cancel(); });
    }
    moving = end;
  }

  function animate(node, frames, opts) { return node && node.animate ? node.animate(frames, opts) : null; }

  /* At little height the bar is in the page's flow (_watch_search.css), out of sight once the
     page is scrolled *y* down, while the grid stands where the page's top will be: the bar comes
     down with the grid, and goes back up as it folds. Held where it ends until the page is
     scrolled to match. */
  function barMoves(y, coming, D, E) {
    if (y <= 0) return [];
    return toArray(document.querySelectorAll('.nav-glass')).filter(function (bar) {
      var cs = getComputedStyle(bar);
      return cs.display !== 'none' && cs.position !== 'sticky' && cs.position !== 'fixed';
    }).map(function (bar) {
      var h = bar.getBoundingClientRect().height;
      var away = { opacity: y >= h ? 0 : 1, transform: 'translateY(' + Math.max(0, y - h) + 'px)' };
      var here = { opacity: 1, transform: 'translateY(' + y + 'px)' };
      return animate(bar, coming ? [away, here] : [here, away], { duration: D, easing: E, fill: 'forwards' });
    });
  }

  function headingTo(title, from) {
    var fs = parseFloat(getComputedStyle(from).fontSize) || 19;
    var tfs = parseFloat(getComputedStyle(title).fontSize) || fs;
    var r = from.getBoundingClientRect();
    var t = title.getBoundingClientRect();
    var s = fs / tfs;
    return 'translate(' + (r.left - t.left) + 'px,' + (r.top - t.top) + 'px) scale(' + s + ')';
  }

  function clipTo(r, to) {
    return 'inset(0 ' + Math.max(0, to.width - r.width) + 'px ' + Math.max(0, to.height - r.height) + 'px 0 round 12px)';
  }

  /* From the row: its cards grow into their places in the grid as the rest of the page falls away. */
  function grow() {
    var D = core.tokenMs('--dur-slow', 250);
    var FAST = core.tokenMs('--dur-fast', 150);
    var E = ease();
    var from = rowCards();
    var sides = around();
    var name = sides.slot && sides.slot.querySelector('.dlx__name');
    var nameSeen = name && onScreen(name.getBoundingClientRect());
    var topAt = landing();
    dom.section.hidden = false;
    pin(topAt);
    var anims = [];
    var hidden = [];
    var fall = { duration: D, easing: E, fill: 'forwards' };
    sides.up.forEach(function (n) { anims.push(animate(n, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-' + FALL_UP + 'px)' }], fall)); });
    sides.down.forEach(function (n) { anims.push(animate(n, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(' + FALL_DOWN + 'px)' }], fall)); });
    var to = gridCards();
    // Placeholders have no titles: each grows out of the row's card in its place.
    if (skeletonsHeld()) {
      Object.keys(from).forEach(function (k) {
        var card = skels.cards[k];
        var r = card && card.getBoundingClientRect();
        if (r && onScreen(r)) to[k] = { node: card, r: r };
      });
    }
    var paired = {};
    Object.keys(to).forEach(function (k) { if (from[k]) paired[k] = true; });
    if (sides.slot) {
      var rest = toArray(sides.slot.querySelectorAll('.dlx__count, .row-open__cue, .watch-row__arrow')).concat(toArray(sides.slot.querySelectorAll('.dw')).filter(function (n) {
        return !Object.keys(paired).some(function (k) { return from[k].node === n; });
      }));
      rest.forEach(function (n) { anims.push(animate(n, [{ opacity: 1 }, { opacity: 0 }], { duration: FAST, easing: E, fill: 'forwards' })); });
    }
    var i = 0;
    toArray(dom.grid.children).concat(dom.empty.hidden ? [] : [dom.empty]).forEach(function (n) {
      var r = n.getBoundingClientRect();
      var hit = Object.keys(paired).filter(function (k) { return to[k].node === n; })[0];
      if (hit) {
        var f = from[hit].r;
        from[hit].node.style.opacity = '0';
        hidden.push(from[hit].node);
        n.classList.add('is-flyer');
        anims.push(animate(n, [
          { transform: 'translate(' + (f.left - r.left) + 'px,' + (f.top - r.top) + 'px)', clipPath: clipTo(f, r) },
          { transform: 'none', clipPath: 'inset(0 0 0 0 round 12px)' }
        ], { duration: D, easing: E }));
        anims.push(animate(n.querySelector('.dwx__foot'), [{ opacity: 0 }, { opacity: 0, offset: 0.3 }, { opacity: 1 }], { duration: D, easing: 'linear' }));
      } else if (onScreen(r)) {
        var delay = Math.min(48, i++ * 12);
        anims.push(animate(n, [{ opacity: 0, transform: 'translateY(' + RISE + 'px) scale(.96)' }, { opacity: 1, transform: 'none' }], { duration: D - delay, easing: E, delay: delay, fill: 'backwards' }));
      }
    });
    if (nameSeen) {
      anims.push(animate(dom.title, [{ transform: headingTo(dom.title, name) }, { transform: 'none' }], { duration: D, easing: E }));
      name.style.opacity = '0';
      hidden.push(name);
    } else anims.push(animate(dom.title, [{ opacity: 0 }, { opacity: 1 }], { duration: FAST, easing: E }));
    [dom.count, dom.back, dom.note].forEach(function (n) {
      anims.push(animate(n, [{ opacity: 0 }, { opacity: 1 }], { duration: FAST, delay: FAST * 0.6, easing: E, fill: 'backwards' }));
    });
    anims = anims.concat(barMoves(savedY, true, D, E));
    document.documentElement.classList.add('is-dl-moving');
    growing = true;
    play(anims, D, function () {
      growing = false;
      hidden.forEach(function (n) { n.style.opacity = ''; });
      toArray(dom.grid.querySelectorAll('.is-flyer')).forEach(function (n) { n.classList.remove('is-flyer'); });
      rowsAway(true);
      unpin();
      document.documentElement.classList.remove('is-dl-moving');
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
      focusTitle(tapped);
      draw(); // an answer that came while it grew
    });
  }

  /* Back to the row: the cards go back into it as the page comes back round it. */
  function fold(done) {
    var D = core.tokenMs('--dur', 200);
    var FAST = core.tokenMs('--dur-fast', 150);
    var E = ease();
    var at = gridCards();
    var headAt = dom.head.getBoundingClientRect().top;
    pin(dom.section.getBoundingClientRect().top);
    dom.head.style.top = headAt - dom.head.getBoundingClientRect().top + 'px';
    rowsAway(false);
    window.scrollTo({ top: savedY, left: 0, behavior: 'instant' });
    var to = rowCards();
    var sides = around();
    var anims = [];
    var hidden = [];
    var keep = { duration: D, easing: E, fill: 'forwards' };
    var gone = { duration: FAST, easing: E, fill: 'forwards' };
    var flown = [];
    Object.keys(at).forEach(function (k) {
      if (!to[k]) return;
      var r = at[k].r;
      var t = to[k].r;
      to[k].node.style.opacity = '0';
      hidden.push(to[k].node);
      flown.push(at[k].node);
      at[k].node.classList.add('is-flyer');
      anims.push(animate(at[k].node, [
        { transform: 'none', clipPath: 'inset(0 0 0 0 round 12px)' },
        { transform: 'translate(' + (t.left - r.left) + 'px,' + (t.top - r.top) + 'px)', clipPath: clipTo(t, r) }
      ], keep));
      anims.push(animate(at[k].node.querySelector('.dwx__foot'), [{ opacity: 1 }, { opacity: 0, offset: 0.6 }, { opacity: 0 }], { duration: D, fill: 'forwards' }));
    });
    toArray(dom.grid.children).concat([dom.empty, dom.note]).forEach(function (n) {
      if (flown.indexOf(n) < 0) anims.push(animate(n, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(16px) scale(.98)' }], gone));
    });
    [dom.count, dom.back].forEach(function (n) { anims.push(animate(n, [{ opacity: 1 }, { opacity: 0 }], gone)); });
    var name = sides.slot && sides.slot.querySelector('.dlx__name');
    if (name && onScreen(name.getBoundingClientRect())) {
      anims.push(animate(dom.title, [{ transform: 'none' }, { transform: headingTo(dom.title, name) }], keep));
      name.style.opacity = '0';
      hidden.push(name);
    } else anims.push(animate(dom.title, [{ opacity: 1 }, { opacity: 0 }], gone));
    sides.up.forEach(function (n) { anims.push(animate(n, [{ opacity: 0, transform: 'translateY(-' + FALL_UP + 'px)' }, { opacity: 1, transform: 'none' }], { duration: D, easing: E })); });
    sides.down.forEach(function (n) { anims.push(animate(n, [{ opacity: 0, transform: 'translateY(' + FALL_DOWN + 'px)' }, { opacity: 1, transform: 'none' }], { duration: D, easing: E })); });
    if (sides.slot) {
      toArray(sides.slot.querySelectorAll('.dlx__count, .row-open__cue, .watch-row__arrow')).concat(toArray(sides.slot.querySelectorAll('.dw')).filter(function (n) {
        return hidden.indexOf(n) < 0;
      })).forEach(function (n) { anims.push(animate(n, [{ opacity: 0 }, { opacity: 1 }], { duration: D, easing: E })); });
    }
    anims = anims.concat(barMoves(savedY, false, D, E));
    document.documentElement.classList.add('is-dl-moving');
    play(anims, D, function () {
      hidden.forEach(function (n) { n.style.opacity = ''; });
      flown.forEach(function (n) { n.classList.remove('is-flyer'); });
      dom.section.hidden = true;
      unpin();
      document.documentElement.classList.remove('is-dl-moving');
      done();
    });
  }

  /* Without the grow: with reduced motion, or from a link outside the row. Opacity alone. */
  function fadeIn(how) {
    rowsAway(true);
    dom.section.hidden = false;
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    // Opened with the page, nothing was pressed: no ring.
    focusTitle(how === 'start' || tapped);
    if (how === 'start') return;
    var D = core.tokenMs('--dur-fast', 150);
    play([animate(dom.section, [{ opacity: 0 }, { opacity: 1 }], { duration: D, easing: 'linear' })], D, function () {});
  }

  function fadeOut(fade, done) {
    dom.section.hidden = true;
    rowsAway(false);
    window.scrollTo({ top: savedY, left: 0, behavior: 'instant' });
    done();
    if (!fade) return;
    var D = core.tokenMs('--dur-fast', 150);
    var anims = toArray(browse.children).filter(function (n) { return n !== dom.section; }).map(function (n) {
      return animate(n, [{ opacity: 0 }, { opacity: 1 }], { duration: D, easing: 'linear' });
    });
    play(anims, D, function () {});
  }

  function focusTitle(quiet) {
    dom.title.classList.toggle('is-quiet', !!quiet);
    dom.title.focus({ preventScroll: true });
  }

  function inRow(node) {
    var slot = rowSlot();
    return !!node && !!slot && slot.contains(node) && !!rowHeading();
  }

  /* how: what opened it, as address.js says; *from*: the link pressed. */
  function open(how, from) {
    if (isOpen) return;
    finishMoving();
    build();
    isOpen = true;
    if (!from) tapped = false;
    failures = 0;
    opener = from || null;
    savedY = window.scrollY;
    if (!(how === 'push' && inRow(from) && !core.reduceMotion())) {
      fadeIn(how);
      draw();
      ask();
      return;
    }
    var grown = false;
    var go = function () {
      // A fold since aborted the asking, which ended this open's wait: a later open has its own.
      if (grown || !isOpen) return;
      grown = true;
      draw();
      grow();
    };
    if (last) {
      go();
      ask();
      return;
    }
    // An answer in time grows the row's own cards; a later one fills the placeholders grown instead.
    var late = setTimeout(go, RACE_MS);
    ask().then(function () {
      clearTimeout(late);
      go();
    });
  }

  function close(how) {
    if (!isOpen) return;
    finishMoving();
    isOpen = false;
    stuck();
    stopAsking();
    var back = opener && opener.isConnected ? opener : null;
    opener = null;
    function done() {
      var active = document.activeElement;
      var to = back && back.getClientRects().length ? back : rowHeading();
      if (to && (!active || active === document.body || dom.section.contains(active))) to.focus({ preventScroll: true });
    }
    // Something else that takes the page closed it: it gives way at once.
    if (how === 'start' || how === 'push') {
      fadeOut(false, done);
      return;
    }
    var slot = rowSlot();
    if (core.reduceMotion() || !slot || !slot.querySelector('.row-open')) {
      fadeOut(true, done);
      return;
    }
    fold(done);
  }

  /* ── The address ── */

  function elsewhere(params) {
    return !!(params.get('q') || '').trim() || !!params.get('genre') || !!params.get('person') || !!params.get('all');
  }

  // Only dl=1 opens it; anything else in its place is taken out of the address.
  (function () {
    var params = new URLSearchParams(window.location.search);
    if (!params.has('dl') || params.get('dl') === '1') return;
    params.delete('dl');
    var query = params.toString();
    window.history.replaceState(window.history.state, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
  })();

  address.layer({
    name: 'dl',
    depth: 12,
    params: ['dl'],
    under: true,
    read: function (here) { return here.params.get('dl') === '1' && !elsewhere(here.params) ? '1' : null; },
    open: function (_value, how) {
      open(how, null);
      return true;
    },
    close: function (how) {
      close(how);
      return true;
    }
  });

  /* ── What is pressed ── */

  function plainClick(e) {
    return !(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
  }

  function toggleEpisodes(btn, on) {
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    btn.closest('[data-dl-card]').classList.toggle('is-open', on);
  }

  /* Open under a title or a pane: back down to it. */
  function toGrid() {
    var over = address.top();
    if (!isOpen) return;
    if (over && over !== 'dl') address.back(over, toGrid);
    else focusTitle(tapped);
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var link = e.target.closest('[data-dl-open]');
    if (link && plainClick(e) && (link.tagName !== 'A' || link.hasAttribute('href'))) {
      e.preventDefault();
      link.focus({ preventScroll: true }); // as Safari does not on a click, for the way back
      tapped = byPointer;
      if (isOpen) { toGrid(); return; }
      address.push('dl', address.url(Object.assign({}, OTHERS, { dl: '1' }), ''));
      open('push', link);
      return;
    }
    if (!dom || !dom.section.contains(e.target)) return;
    if (e.target.closest('[data-dl-back]') && plainClick(e)) {
      e.preventDefault();
      address.back('dl');
      return;
    }
    var eps = e.target.closest('[data-dl-eps]');
    if (eps) {
      toggleEpisodes(eps, eps.getAttribute('aria-expanded') !== 'true');
      eps.focus({ preventScroll: true }); // as Safari does not on a click, for Escape to find it
      return;
    }
    // Anywhere on a card but its controls opens its title, as its own link does (watch/detail.js);
    // with its menu open, a press there only closes the menu.
    var card = e.target.closest('[data-dl-card]');
    if (!card || !plainClick(e) || e.target.closest('a[href], button, [data-title-ref]') || (menu && menu.trigger())) return;
    var opener = card.querySelector('[data-title-ref]');
    if (opener) opener.click();
  });

  /* What was last pressed: the Profile hub's row opens it with a click of its own, after a tap. */
  var byPointer = false;
  document.addEventListener('pointerdown', function () { byPointer = true; }, true);
  document.addEventListener('keydown', function () { byPointer = false; }, true);

  window.addEventListener('scroll', stuck, { passive: true });

  // A card lifts as its menu opens: the menu lines up with its button where the lift ends.
  document.addEventListener('transitionend', function (e) {
    var trigger = menu && menu.trigger();
    if (trigger && e.propertyName === 'transform' && e.target === trigger.closest('.dwx')) menu.place();
  });

  // On window, so that a dialog or a menu has had its Escape first.
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.defaultPrevented || !isOpen || address.top() !== 'dl') return;
    var modal = core.byId('abandon-modal');
    if (modal && !modal.hidden) return;
    e.preventDefault();
    // The focused card's open episodes, else any open: Escape never folds the grid over them.
    var card = document.activeElement && document.activeElement.closest && document.activeElement.closest('.dwx');
    var mine = card && card.querySelector('[data-dl-eps][aria-expanded="true"]');
    var open = mine ? [mine] : toArray(dom.grid.querySelectorAll('[data-dl-eps][aria-expanded="true"]'));
    if (open.length) {
      open.forEach(function (eps) { toggleEpisodes(eps, false); });
      open[0].focus({ preventScroll: true });
      return;
    }
    address.back('dl');
  });

  document.addEventListener('visibilitychange', function () {
    if (!isOpen) return;
    if (document.hidden) stopAsking();
    else ask();
  });

  // An abandon has just been made: what the grid shows is asked for at once.
  document.addEventListener('mediaman:downloads:refresh', function () { if (isOpen) ask(); });

  watch.dlgrid = { isOpen: function () { return isOpen; } };
})();
