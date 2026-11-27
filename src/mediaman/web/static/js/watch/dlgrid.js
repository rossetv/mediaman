/* Watch: Downloading, grown out of its row at /?dl=1. Every download gets the row's card, with
 * what the row has no room for under it: the size, what Radarr or Sonarr says is wrong and what
 * comes of it, the last search, its episodes and an admin's buttons. Groups: Needs attention,
 * Downloading, Looking for a copy, Coming soon. While open it reads /api/downloads every 2 s and
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
  /* An answer this late is told; one that outlasts what the server itself gives a service that
     hangs will never come, and another is asked for. */
  var LATE_MS = 20000;
  var HUNG_MS = 120000;
  var BACKOFF_MAX = 30000;
  var FALL_UP = 28; // px the rows above the row go up by as it grows, and come back from
  var FALL_DOWN = 44; // and those below it, down
  var RISE = 26; // px a grid card that was not in the row rises from
  // What the address of anything else that holds the page says; opening Downloading clears it.
  var OTHERS = { all: null, view: null, q: null, genre: null, type: null, where: null, decade: null, person: null, page: null, per_page: null, sort: null };
  var GROUPS = [['attention', 'Needs attention'], ['coming', 'Downloading'], ['looking', 'Looking for a copy'], ['soon', 'Coming soon']];
  var ETA = /^~(?:(\d+) hr )?(\d+) (min|sec) remaining$/;
  var TOP_STATES = ['downloading', 'almost_ready', 'needs_attention', 'finding_copy', 'queued', 'searching'];

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
  var timer = null;
  var asked = 0; // which asking is the current one
  var opener = null;
  var opens = 0; // which opening is the current one
  var savedY = 0;
  var lefts = []; // how far each row was scrolled when the grid took the page
  var moving = null; // ends the motion under way

  /* ── The layer ── */

  function build() {
    if (dom) return;
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

  function minutesLeft(eta) {
    var found = ETA.exec(eta || '');
    if (!found) return null;
    return found[3] === 'sec' ? 1 : (Number(found[1]) || 0) * 60 + Number(found[2]);
  }

  /* What the row's card is drawn from, made of what the downloads say of *item*. Why it needs
     attention and what comes of it are said whole under the card. */
  function topOf(item, items) {
    var row = rowItemOf(item, items);
    var state = TOP_STATES.indexOf(item.state) >= 0 ? item.state : 'searching';
    var active = state === 'downloading' || state === 'almost_ready';
    var percent = Math.max(0, Math.min(100, Number(item.progress) || 0));
    return {
      key: String(item.id), title: String(item.title || ''), kind: item.media_type === 'movie' ? 'movie' : 'show', state: state,
      percent: active ? percent : 0,
      minutes_left: state === 'downloading' ? minutesLeft(item.eta) : null,
      line: state === 'almost_ready' && item.state_label === 'Waiting to import' ? item.state_label : '',
      held: false, ready_seconds: null, playable: false,
      ref: row ? row.ref : null, href: row ? row.href : '',
      poster_url: item.poster_url || (row && row.poster_url) || '', art_url: row ? row.art_url : '',
      attention: '', attention_hint: '', retrying: false
    };
  }

  /* Only a page of Radarr's or Sonarr's own, on the web. */
  function outside(link) { return typeof link === 'string' && /^https?:\/\/[^\s]+$/i.test(link) ? link : ''; }

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

  function band(node) {
    var more = node.querySelector('.dwx__more');
    if (more) return more;
    more = node.appendChild(el('div', 'dwx__more'));
    ['dwx__facts', 'dwx__why', 'dwx__fix', 'dwx__seen'].forEach(function (name) { more.appendChild(el('p', name)).hidden = true; });
    var why = more.querySelector('.dwx__why');
    why.appendChild(el('span', 'dwx__why-by'));
    why.appendChild(el('span'));
    var fix = more.querySelector('.dwx__fix');
    fix.appendChild(icon('i-triangle-exclamation'));
    fix.appendChild(el('span'));
    more.appendChild(el('div', 'dwx__acts'));
    var wrap = more.appendChild(el('div', 'dwx__eps-wrap'));
    wrap.hidden = true;
    wrap.appendChild(el('ul', 'dwx__eps'));
    return more;
  }

  function show(node, on) { node.hidden = !on; return on; }

  function facts(p, item) {
    if (['downloading', 'almost_ready', 'queued'].indexOf(item.state) < 0) return show(p, false);
    var parts = [];
    if (item.state === 'queued') parts.push('Waiting its turn');
    var sized = item.state !== 'queued' && item.size_done && item.size_total;
    if (item.state === 'queued' && item.size_total) parts.push(item.size_total);
    if (item.episode_summary && item.state !== 'queued') parts.push(item.episode_summary);
    if (item.state === 'almost_ready') parts.push('Post-processing…');
    var said = (sized ? item.size_done + ' of ' + item.size_total + '|' : '') + parts.join(' · ');
    if (p.getAttribute('data-said') === said) return show(p, !!said);
    p.setAttribute('data-said', said);
    p.textContent = '';
    if (sized) {
      p.appendChild(el('b', '', item.size_done));
      p.appendChild(document.createTextNode(' of ' + item.size_total + (parts.length ? ' · ' : '')));
    }
    if (parts.length) p.appendChild(document.createTextNode(parts.join(' · ')));
    return show(p, !!said);
  }

  /* A control about to go gives the focus to its card's next one, else to the card's first. */
  function letGoOf(node, card) {
    if (!node.contains(document.activeElement)) return;
    var rest = toArray(card.querySelectorAll('a[href], button')).filter(function (n) { return !node.contains(n); });
    var to = rest[0] || nextControl(card);
    if (to) to.focus({ preventScroll: true });
  }

  function pill(acts, name, make, card) {
    var node = acts.querySelector('[data-act="' + name + '"]');
    if (!make) {
      if (node) { letGoOf(node, card); node.remove(); }
      return null;
    }
    if (!node) {
      node = make();
      node.setAttribute('data-act', name);
      var after = { arr: ['eps'], abandon: ['eps', 'arr'] }[name] || [];
      var before = toArray(acts.children).filter(function (n) { return after.indexOf(n.getAttribute('data-act')) < 0; })[0] || null;
      acts.insertBefore(node, before);
    }
    return node;
  }

  function button(className, glyph, words) {
    var node = el('button', 'dwx__act' + (className ? ' ' + className : ''));
    node.setAttribute('type', 'button');
    if (glyph) node.appendChild(icon(glyph));
    node.appendChild(el('span', '', words));
    return node;
  }

  /* As the old page's abandon button: dl-abandon.js reads these. */
  function arm(node, item, upcoming) {
    var label = upcoming ? 'Stop tracking' : 'Abandon search';
    node.setAttribute('data-abandon-trigger', '');
    if (upcoming) node.setAttribute('data-abandon-upcoming', '1');
    node.setAttribute('data-dl-id', String(item.id));
    node.setAttribute('data-arr-id', String(item.arr_id || 0));
    node.setAttribute('data-kind', item.media_type === 'movie' ? 'movie' : 'series');
    node.setAttribute('data-title', item.title || '');
    node.setAttribute('data-stuck-seasons', JSON.stringify(item.stuck_seasons || []));
    node.setAttribute('aria-label', item.title ? label + ': ' + item.title : label);
  }

  function fillBand(card, item) {
    var more = band(card);
    var held = item.state === 'needs_attention';
    facts(more.querySelector('.dwx__facts'), item);
    // What Radarr or Sonarr says is wrong, and where to fix it, are an admin's alone.
    var why = more.querySelector('.dwx__why');
    if (show(why, admin && held && !!item.attention)) {
      setText(why.children[0], (item.arr_source || 'Radarr or Sonarr') + ' says');
      setText(why.children[1], item.attention);
    }
    var fix = more.querySelector('.dwx__fix');
    if (show(fix, held)) {
      var ok = !!item.retrying;
      fix.classList.toggle('is-ok', ok);
      fix.classList.toggle('is-act', !ok);
      window.MM.dom.setIcon(fix.querySelector('.ico'), ok ? 'i-rotate-right' : 'i-triangle-exclamation');
      setText(fix.children[1], ok ? 'mediaman will look for another copy'
        : (admin && item.attention_hint) || 'Won’t finish until an admin sorts it out');
    }
    var seen = more.querySelector('.dwx__seen');
    if (show(seen, (item.state === 'searching' || item.state === 'finding_copy') && !!item.search_hint)) setText(seen, item.search_hint);
    var acts = more.querySelector('.dwx__acts');
    var episodes = item.episodes || [];
    var eps = pill(acts, 'eps', episodes.length ? function () {
      var node = button('', 'i-chevron-right', '');
      node.setAttribute('aria-expanded', 'false');
      node.setAttribute('data-dl-eps', '');
      return node;
    } : null, card);
    if (eps) setText(eps.querySelector('span'), episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '') + (item.has_pack ? ' · pack download' : ''));
    var link = admin && item.state !== 'downloading' && item.state !== 'almost_ready' ? outside(item.arr_link) : '';
    var arr = pill(acts, 'arr', link ? function () {
      var node = el('a', 'dwx__act dwx__act--quiet');
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
      return node;
    } : null, card);
    if (arr) {
      arr.setAttribute('href', link);
      setText(arr, 'View in ' + (item.arr_source || 'Radarr/Sonarr') + ' ↗');
    }
    var abandon = pill(acts, 'abandon', admin && item.abandon_visible ? function () {
      return button('dwx__act--quiet dwx__act--danger', 'i-xmark', 'Abandon search');
    } : null, card);
    if (abandon) arm(abandon, item, false);
    acts.hidden = !acts.children.length;
    var wrap = more.querySelector('.dwx__eps-wrap');
    if (!episodes.length) wrap.hidden = true;
    var list = wrap.querySelector('.dwx__eps');
    list.textContent = '';
    episodes.forEach(function (ep) {
      var row = list.appendChild(el('li', 'dwx__ep' + (ep.state === 'ready' ? ' is-ready' : '') + (ep.is_pack_episode ? ' is-pack' : '')));
      row.appendChild(el('span', 'dwx__ep-n', ep.label || ''));
      row.appendChild(el('span', 'dwx__ep-t', ep.title || ''));
      var bar = row.appendChild(el('span', 'dwx__ep-bar'));
      bar.setAttribute('aria-hidden', 'true');
      bar.style.setProperty('--p', String(Math.max(0, Math.min(100, Number(ep.progress) || 0))));
      row.appendChild(el('span', 'dwx__ep-s', episodeWords(ep)));
    });
  }

  function cardOf(item, items) {
    var key = 'dl:' + item.id;
    var top = topOf(item, items);
    var node = cards[key];
    if (!node) {
      node = cards[key] = watch.downloading.card(top);
      node.classList.remove('watch-card');
      node.classList.add('dwx');
      node.setAttribute('data-dl-card', '');
    } else watch.downloading.paint(node, top, false);
    node.setAttribute('data-dl-id', String(item.id));
    // One with no title of its own is no link here: it would only open this again.
    var link = node.querySelector('.dw__link');
    if (link.hasAttribute('data-dl-open')) {
      link.removeAttribute('href');
      link.removeAttribute('data-dl-open');
    }
    fillBand(node, item);
    return node;
  }

  function soonCardOf(item) {
    var key = 'soon:' + item.id;
    var node = cards[key];
    if (!node) {
      node = cards[key] = el('article', 'dwx dwx--small');
      node.setAttribute('data-dl-card', '');
      var pic = node.appendChild(el('span', 'dwx__pic'));
      var img = pic.appendChild(el('img'));
      img.setAttribute('alt', '');
      img.setAttribute('data-broken-on-error', '');
      var text = node.appendChild(el('span', 'dwx__small-text'));
      text.appendChild(el('span', 'dw__title'));
      text.appendChild(el('span', 'dwx__small-line')).appendChild(el('span'));
    }
    node.setAttribute('data-dl-id', String(item.id));
    var img2 = node.querySelector('img');
    var src = core.safePath(item.poster_url);
    if (src && img2.getAttribute('src') !== src) img2.setAttribute('src', src);
    else if (!src) img2.removeAttribute('src');
    setText(node.querySelector('.dw__title'), item.title || '');
    setText(node.querySelector('.dwx__small-line span'), item.release_label || '');
    var stop = pill(node, 'stop', admin ? function () { return button('dwx__act--quiet', 'i-xmark', 'Stop tracking'); } : null, node);
    if (stop) {
      if (node.lastElementChild !== stop) node.appendChild(stop);
      arm(stop, item, true);
    }
    return node;
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
      return list.reduce(function (all, kid) { return all.concat(toArray(kid.querySelectorAll('a[href], button'))); }, []);
    }
    var before = controls(kids.slice(0, Math.max(0, at)));
    return controls(kids.slice(at + 1))[0] || before[before.length - 1] || dom.title;
  }

  /* ── Drawing ── */

  function curly(text) { return String(text).replace(/'/g, '’'); }

  function wordsOf(data, items) {
    if (!setUpIn(data)) return '';
    var soon = (data.upcoming || []).length;
    var bits = [];
    if (items.length) bits.push(items.length + ' in progress');
    // In an outage nobody can tell that nothing downloads: an admin is told what does not
    // answer, a viewer only that some may be missing, or nothing where nothing is shown.
    else if (!data.outage) bits.push('Nothing downloading');
    else if (admin) bits.push(curly(data.outage));
    if (soon) bits.push(soon + ' coming soon');
    if (!admin && data.outage && items.length) bits.push('some may be missing');
    return bits.join(' · ');
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
    if (items.length) return null;
    if (!data && !lost) return ['i-download', 'Checking downloads…', ''];
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

  function draw() {
    build();
    var data = last;
    var items = data ? (data.hero ? [data.hero] : []).concat(data.queue || []) : [];
    var upcoming = data && setUpIn(data) ? data.upcoming || [] : [];
    dom.body.setAttribute('aria-busy', data || lost ? 'false' : 'true');
    setText(dom.count, data ? wordsOf(data, items) : '');
    var note = noteOf(data, items);
    if (note) setText(dom.noteText, note);
    dom.note.hidden = !note;
    drawEmpty(emptyOf(data, items));
    var groups = { attention: [], coming: [], looking: [], soon: [] };
    items.forEach(function (item) { groups[groupOf(item)].push(cardOf(item, items)); });
    upcoming.forEach(function (item) { groups.soon.push(soonCardOf(item)); });
    var want = [];
    GROUPS.forEach(function (g) {
      if (groups[g[0]].length) want.push(headOf(g[0], g[1], groups[g[0]].length));
      want = want.concat(groups[g[0]]);
    });
    place(want);
  }

  /* The grid's nodes in the order of *want*, moving only those out of place; those gone pass
     their focus on. */
  function place(want) {
    var grid = dom.grid;
    var active = document.activeElement;
    var next = grid.firstElementChild;
    want.forEach(function (node) {
      if (node === next) next = next.nextElementSibling;
      else grid.insertBefore(node, next);
    });
    toArray(grid.children).forEach(function (node) {
      if (want.indexOf(node) >= 0) return;
      if (node.contains(document.activeElement)) {
        var to = nextControl(node);
        if (to) to.focus({ preventScroll: true });
      }
      node.remove();
    });
    Object.keys(cards).forEach(function (key) { if (!cards[key].parentNode) delete cards[key]; });
    Object.keys(heads).forEach(function (key) { if (!heads[key].parentNode) delete heads[key]; });
    if (active && document.activeElement !== active && grid.contains(active)) active.focus({ preventScroll: true });
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

  function gridCards() { return byTitle(toArray(dom.grid.querySelectorAll('.dwx:not(.dwx--small)'))); }

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
    var topAt = browse.getBoundingClientRect().top + window.scrollY;
    dom.section.hidden = false;
    pin(topAt);
    var anims = [];
    var hidden = [];
    var fall = { duration: D, easing: E, fill: 'forwards' };
    sides.up.forEach(function (n) { anims.push(animate(n, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-' + FALL_UP + 'px)' }], fall)); });
    sides.down.forEach(function (n) { anims.push(animate(n, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(' + FALL_DOWN + 'px)' }], fall)); });
    var to = gridCards();
    var paired = {};
    Object.keys(to).forEach(function (k) { if (from[k]) paired[k] = true; });
    if (sides.slot) {
      var rest = toArray(sides.slot.querySelectorAll('.dlx__count, .row-open__cue')).concat(toArray(sides.slot.querySelectorAll('.dw')).filter(function (n) {
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
        anims.push(animate(n.querySelector('.dwx__more'), [{ opacity: 0 }, { opacity: 0, offset: 0.3 }, { opacity: 1 }], { duration: D, easing: 'linear' }));
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
    document.documentElement.classList.add('is-dl-moving');
    play(anims, D, function () {
      hidden.forEach(function (n) { n.style.opacity = ''; });
      toArray(dom.grid.querySelectorAll('.is-flyer')).forEach(function (n) { n.classList.remove('is-flyer'); });
      rowsAway(true);
      unpin();
      document.documentElement.classList.remove('is-dl-moving');
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
      focusTitle(false);
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
      anims.push(animate(at[k].node.querySelector('.dwx__more'), [{ opacity: 1 }, { opacity: 0, offset: 0.6 }, { opacity: 0 }], { duration: D, fill: 'forwards' }));
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
      toArray(sides.slot.querySelectorAll('.dlx__count, .row-open__cue')).concat(toArray(sides.slot.querySelectorAll('.dw')).filter(function (n) {
        return hidden.indexOf(n) < 0;
      })).forEach(function (n) { anims.push(animate(n, [{ opacity: 0 }, { opacity: 1 }], { duration: D, easing: E })); });
    }
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
    focusTitle(how === 'start');
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
    failures = 0;
    opener = from || null;
    savedY = window.scrollY;
    var growing = how === 'push' && inRow(from) && !core.reduceMotion();
    if (!growing) {
      fadeIn(how);
      draw();
      ask();
      return;
    }
    var mine = ++opens;
    var go = function () {
      // A fold and an open since: that open grows it, once.
      if (!isOpen || mine !== opens) return;
      draw();
      grow();
    };
    // The grid is laid out from what the downloads say, so it grows once they have said it.
    if (last) {
      go();
      ask();
    } else ask().then(go);
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
    var wrap = btn.closest('.dwx__more').querySelector('.dwx__eps-wrap');
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
    wrap.hidden = !on;
  }

  /* Open under a title or a pane: back down to it. */
  function toGrid() {
    var over = address.top();
    if (!isOpen) return;
    if (over && over !== 'dl') address.back(over, toGrid);
    else focusTitle(false);
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var link = e.target.closest('[data-dl-open]');
    if (link && plainClick(e) && (link.tagName !== 'A' || link.hasAttribute('href'))) {
      e.preventDefault();
      link.focus({ preventScroll: true }); // as Safari does not on a click, for the way back
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
    if (eps) toggleEpisodes(eps, eps.getAttribute('aria-expanded') !== 'true');
  });

  window.addEventListener('scroll', stuck, { passive: true });

  // On window, so that a dialog or a menu has had its Escape first.
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.defaultPrevented || !isOpen || address.top() !== 'dl') return;
    var modal = core.byId('abandon-modal');
    if (modal && !modal.hidden) return;
    e.preventDefault();
    var card = document.activeElement && document.activeElement.closest && document.activeElement.closest('.dwx');
    var eps = card && card.querySelector('[data-dl-eps][aria-expanded="true"]');
    if (eps) {
      toggleEpisodes(eps, false);
      eps.focus({ preventScroll: true });
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
