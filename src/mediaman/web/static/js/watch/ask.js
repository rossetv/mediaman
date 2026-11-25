/* Watch: Ask AI. What the search panel knows of the AI (whether there is one, the person's
 * recent searches and phrases), their questions, and a question's place on the page: the row
 * that offers it, the wait, the titles that answer it and the ways to narrow them.
 * A question belongs to the page, not to what shows it: it goes on while another is looked at
 * or the bar is closed, and is drawn where it is asked for again. One is asked at a time; a
 * newer one stops the one before. Nothing is built with innerHTML: every word comes from the
 * person, TMDB or a model.
 * On window.MMWatch.ask, which watch/search.js works. Needs watch/core.js; watch/search.js
 * when something is drawn.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;
  var safePath = core.safePath;

  var URL = '/api/search/assist';
  var FRESH_MS = 60000; // how long what the panel knows is not asked for again
  var POLL_MS = 1200;
  var SLOW_MS = 12000;
  var STOPPING_MS = 1500; // between tries while the call of a stopped question ends
  var STOPPING_TRIES = 60;
  var POLL_TRIES = 5; // polls in a row that may fail
  var KEPT = 30; // questions this page remembers
  var RECENT = 6;
  var WAITING = 3; // cards of the outline
  var RUN = 'mm.watch.ask.run';
  var LONGEST = 200; // of a question, as the server takes it

  function tidy(text) { return String(text || '').replace(/\s+/g, ' ').trim().slice(0, LONGEST).trim(); }

  function wordCount(text) {
    return tidy(text).split(' ').filter(function (word) { return /[\p{L}\p{N}]/u.test(word); }).length;
  }

  /* ── What the panel knows ── */

  var state = null; // { available, history, recent, phrases, min_words }
  var asked = 0; // when it was asked for
  var loading = null;
  var wipes = 0; // times the searches were forgotten

  function load() {
    if (loading) return loading;
    if (state && Date.now() - asked < FRESH_MS) return Promise.resolve();
    asked = Date.now();
    var mine = wipes;
    loading = MM.api.get(URL).then(function (data) {
      if (mine !== wipes) return;
      var was = available();
      state = {
        available: data.available === true,
        history: data.history === true,
        recent: data.recent || [],
        phrases: data.personal && data.phrases ? data.phrases : [],
        min_words: data.min_words || 3
      };
      if (available() !== was) sayAvailable();
    }).catch(function () { /* nothing of it is offered */ }).then(function () { loading = null; });
    return loading;
  }

  function sayAvailable() {
    document.dispatchEvent(new CustomEvent('mm:search-assist', { detail: { available: available() } }));
  }

  function available() { return !!state && state.available; }

  function fits(text) { return available() && wordCount(text) >= state.min_words; }

  function same(a, b) { return tidy(a).toLowerCase() === tidy(b).toLowerCase(); }

  /* Keep a search the person made, where their history is used. */
  function record(text) {
    var phrase = tidy(text);
    if (!state || !state.history || !phrase || (state.recent[0] && same(state.recent[0], phrase))) return;
    var mine = wipes;
    MM.api.post(URL + '/searches', { q: phrase }).then(function (data) {
      if (!data.recorded || mine !== wipes) return;
      var rest = state.recent.filter(function (r) { return !same(r, phrase); });
      state = Object.assign({}, state, { recent: [phrase].concat(rest).slice(0, RECENT) });
    }).catch(function () { /* the search itself is not held up by it */ });
  }

  /* Forget them, with the phrases and the answers made from them. */
  function forget() {
    return MM.api.delete(URL + '/searches').then(function () {
      wipes++;
      state = Object.assign({}, state, { recent: [], phrases: [] });
    });
  }

  /* ── Questions ── */

  var items = new Map(); // by key, the one asked last at the end
  var going = null; // the one being asked: { item, left, timer, posted, run }
  var views = [];

  /* The question of *kind* ("ask" or "like") about *value*: the words, or a title's ref. It is
     remembered from when it is asked. */
  function question(kind, value, name) {
    var key = kind + ':' + (kind === 'ask' ? tidy(value).toLowerCase() : value);
    var item = items.get(key) || {
      key: key, kind: kind, value: kind === 'ask' ? tidy(value) : value,
      state: 'new', // new, thinking, answer, nothing or refused
      chip: '', pressed: '', // the one the titles shown are narrowed by, and the one asked for
      narrowing: false, slow: false, waiting: false, note: '', refused: null,
      base: null, shown: null, chips: [], instead: []
    };
    if (name) item.name = name;
    return item;
  }

  function remember(item) {
    items.delete(item.key);
    items.set(item.key, item);
    if (items.size > KEPT) items.delete(items.keys().next().value);
  }

  function changed(item) {
    views = views.filter(function (v) { return v.host.isConnected; });
    views.forEach(function (v) { if (v.item === item) v.draw(); });
  }

  /* Give up what is being asked. Returns once the server has been told. */
  function leave() {
    var g = going;
    if (!g) return Promise.resolve();
    going = null;
    g.left = true;
    clearTimeout(g.timer);
    clearTimeout(g.slow);
    if (g.wake) g.wake();
    rest(g.item);
    changed(g.item);
    return g.posted.then(function (run) {
      return run ? MM.api.delete(URL + '/runs/' + run).catch(function () { /* it ends by itself */ }) : null;
    });
  }

  /* Back to what it was before it was asked. */
  function rest(item) {
    item.narrowing = item.slow = item.waiting = false;
    item.pressed = item.chip;
    if (item.state === 'thinking') item.state = 'new';
  }

  function stop(item) { if (going && going.item === item) leave(); }

  function pause(g, ms) {
    return new Promise(function (resolve) {
      g.wake = resolve;
      g.timer = setTimeout(resolve, ms);
    });
  }

  /* Ask it, or for it narrowed by *chip*, which is words of its own answer. */
  function start(item, chip) {
    chip = chip || '';
    remember(item);
    var before = leave();
    var g = going = { item: item, chip: chip, left: false, timer: null, slow: null, wake: null };
    item.pressed = chip;
    item.note = '';
    item.refused = null;
    if (chip) item.narrowing = true;
    else item.state = 'thinking';
    changed(item);
    g.slow = setTimeout(function () {
      item.slow = true;
      changed(item);
    }, SLOW_MS);
    g.posted = before.then(function () { return post(g, STOPPING_TRIES); });
  }

  /* The run begun last in this tab, which another page of it may have left going. */
  function keepRun(run) {
    try { window.sessionStorage.setItem(RUN, String(run)); } catch (e) { /* storage blocked */ }
  }

  function keptRun() {
    try { return Number(window.sessionStorage.getItem(RUN)); } catch (e) { return 0; }
  }

  /* Returns the run to poll, or null: it was answered, refused or given up. */
  function post(g, tries) {
    if (g.left) return null;
    var item = g.item;
    var body = item.kind === 'like' ? { ref: item.value } : { q: item.value };
    if (g.chip) body.refine = g.chip;
    return MM.api.post(URL + '/' + item.kind, body).then(function (data) {
      if (data.status !== 'running') {
        if (!g.left) done(g, data);
        return null;
      }
      keepRun(data.run_id);
      if (!g.left) poll(g, data.run_id, POLL_TRIES);
      return data.run_id;
    }, function (err) {
      if (g.left) return null;
      var left = err && err.error === 'running' && err.data && err.data.run_id;
      if (left && left === keptRun()) {
        keepRun(0);
        return MM.api.delete(URL + '/runs/' + left).catch(function () { /* it has ended */ }).then(function () { return post(g, tries); });
      }
      if (err && err.error === 'stopping' && tries > 0) {
        if (!item.waiting) {
          item.waiting = true;
          changed(item);
        }
        return pause(g, STOPPING_MS).then(function () { return post(g, tries - 1); });
      }
      refuse(g, err);
      return null;
    });
  }

  function poll(g, run, tries) {
    if (g.item.waiting) {
      g.item.waiting = false;
      changed(g.item);
    }
    g.timer = setTimeout(function () {
      MM.api.get(URL + '/runs/' + run).then(function (data) {
        if (g.left) return;
        if (data.status === 'done') done(g, data);
        else if (data.status === 'running') poll(g, run, POLL_TRIES);
        else if (data.status === 'cancelled') {
          ended(g);
          changed(g.item);
        } else refuse(g, null);
      }, function (err) {
        if (g.left) return;
        var gone = err && err.status >= 400 && err.status < 500 && err.status !== 429;
        if (gone || tries <= 1) refuse(g, err && err.error === 'unavailable' ? err : null);
        else poll(g, run, tries - 1);
      });
    }, POLL_MS);
  }

  function ended(g) {
    clearTimeout(g.slow);
    if (going === g) going = null;
    rest(g.item);
  }

  function done(g, data) {
    var item = g.item;
    ended(g);
    item.chip = item.pressed = g.chip;
    item.shown = { owned: data.owned || [], others: data.others || [] };
    item.chips = data.chips || [];
    if (!g.chip) {
      item.base = item.shown;
      item.instead = data.instead || [];
    }
    item.state = g.chip || titles(item).length ? 'answer' : 'nothing';
    changed(item);
  }

  /* Narrow the answer by *chip*; by the one it is narrowed by already, back to all of it. */
  function narrow(item, chip) {
    if (item.pressed !== chip) { start(item, chip); return; }
    if (going && going.item === item) leave();
    item.chip = item.pressed = '';
    item.note = '';
    item.shown = item.base;
    changed(item);
  }

  function titles(item) { return item.shown ? item.shown.owned.concat(item.shown.others) : []; }

  /* ── Refusals, in words ── */

  function clock(at) { return at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); }

  /* "after midnight", or the person's own time for the server's. */
  function resets(iso) {
    var at = new Date(iso || NaN);
    if (isNaN(at)) return '';
    if (at.getHours() + at.getMinutes() === 0) return ' after midnight';
    return ' after ' + clock(at) + (at.toDateString() === new Date().toDateString() ? '' : ' tomorrow');
  }

  /* What the person is told: { title, text, again }, *again* the button that asks once more. */
  function reason(err) {
    var code = err && err.error;
    var data = (err && err.data) || {};
    var limit = typeof data.limit === 'number' ? data.limit : null;
    if (code === 'unavailable' || ((code === 'limit' || code === 'limit_everyone') && limit === 0)) {
      return { off: true, title: 'AI isn’t available right now', text: 'Search works as always.' };
    }
    if (code === 'limit') {
      return {
        title: limit === null ? 'You’ve asked all your questions for today'
          : 'You’ve asked ' + limit + (limit === 1 ? ' question today' : ' questions today'),
        text: 'You can ask again' + (resets(data.resets_at) || ' tomorrow') + '. Search works as always.'
      };
    }
    if (code === 'limit_everyone') {
      return {
        title: 'AI is done for today on this server',
        text: 'The daily limit for everyone' + (limit === null ? '' : ', ' + limit + ',') + ' is reached. It starts again' +
          (resets(data.resets_at) || ' tomorrow') + '.'
      };
    }
    if (code === 'running') {
      return {
        title: 'Another question of yours is being answered', text: 'It was asked in another tab or on another device.',
        again: 'Stop it and ask this', run: data.run_id
      };
    }
    if (code === 'busy' || code === 'stopping') {
      return { title: 'AI is busy just now', text: 'Your question is still here. Try again in a moment.', again: 'Try again' };
    }
    if (code === 'too_many_requests') {
      return { title: 'That was a lot at once', text: 'Wait a minute, then ask again.', again: 'Try again' };
    }
    if (code === 'too_short') return { title: 'Ask with a few more words', text: 'Three or more say what you want.' };
    return { title: 'The answer didn’t arrive', text: 'Your question is still here.', again: 'Try again' };
  }

  function refuse(g, err) {
    var item = g.item;
    var why = reason(err);
    ended(g);
    if (why.off && state) {
      state = Object.assign({}, state, { available: false });
      sayAvailable();
    }
    if (g.chip) {
      item.note = err && err.error === 'not_found' ? 'This answer is too old to narrow. Ask again for a new one.'
        : why.title + '. ' + why.text;
    } else {
      item.state = 'refused';
      item.refused = why;
    }
    changed(item);
  }

  function again(item) {
    var run = item.refused && item.refused.run;
    if (!run) { start(item); return; }
    item.state = 'thinking';
    changed(item);
    MM.api.delete(URL + '/runs/' + run).catch(function () { /* it has ended */ }).then(function () {
      if (item.state === 'thinking' && !(going && going.item === item)) start(item);
    });
  }

  /* ── Pieces ── */

  function button(className, text) {
    var b = el('button', className, text);
    b.type = 'button';
    return b;
  }

  function dur(minutes) {
    if (minutes < 60) return minutes + ' min';
    return Math.floor(minutes / 60) + ' h' + (minutes % 60 ? ' ' + minutes % 60 + ' min' : '');
  }

  function mark() {
    var node = el('span', 'ask__mark');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(MM.dom.icon('i-sparkles'));
    return node;
  }

  function words(title, text) {
    var node = el('span', 'ask__words');
    node.appendChild(el('b', '', title));
    node.appendChild(el('small', '', text));
    return node;
  }

  /* What is asked, or what became of it, over the answer. */
  function head(title, text, after) {
    var node = el('div', 'ask__row');
    node.tabIndex = -1;
    node.appendChild(mark());
    var said = words(title, text);
    said.classList.add('ask__swapin');
    node.appendChild(said);
    if (after) node.appendChild(after);
    return node;
  }

  function outline() {
    var node = el('div', 'ask__body');
    node.setAttribute('aria-hidden', 'true');
    var grid = node.appendChild(el('div', 'ask__grid'));
    grid.appendChild(el('div', 'ask__group')).appendChild(el('span', 'skel ask__skel-title'));
    for (var i = 0; i < WAITING; i++) {
      var card = grid.appendChild(el('div', 'ans ans--skel'));
      card.appendChild(el('span', 'skel ans__p'));
      var text = card.appendChild(el('div', 'ans__t'));
      for (var n = 0; n < 4; n++) text.appendChild(el('span', 'skel'));
    }
    return node;
  }

  function count(item) {
    var all = titles(item).length;
    var held = item.shown.owned.length;
    return all + (all === 1 ? ' title' : ' titles') + (all ? ', ' + (held === all ? 'all' : held || 'none') + ' already here' : '');
  }

  /* ── A title of the answer ── */

  function facts(t) {
    var long = t.kind === 'show'
      ? t.season_count && t.season_count + (t.season_count === 1 ? ' season' : ' seasons')
      : t.minutes && dur(t.minutes);
    return [t.kind === 'show' ? 'TV' : 'Movie', t.year, long].filter(Boolean).join(' · ');
  }

  function refOf(t) { return (t.in_library && t.library_ref) || t.ref || ''; }

  function openDetail(t, from) {
    window.MMWatch.detail.open(refOf(t), { origin: from, title: t.title, kind: t.kind });
  }

  /* Play a held title: a film at once, a show where the person is in it. */
  function playButton(t) {
    var a = el('a', 'ans__go');
    var film = safePath(t.play_href);
    a.href = film || safePath(t.href) || '/watch';
    a.setAttribute('aria-label', 'Play ' + t.title);
    a.appendChild(MM.dom.icon('i-play'));
    if (film) return a;
    var ready = false;
    a.addEventListener('click', function (e) {
      if (ready || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      if (a.hasAttribute('aria-busy')) return;
      a.setAttribute('aria-busy', 'true');
      MM.api.get('/api/watch/title/' + encodeURIComponent(refOf(t))).then(function (data) {
        a.removeAttribute('aria-busy');
        var to = safePath(data.play && data.play.href);
        if (!to) { openDetail(t, a); return; }
        ready = true;
        a.href = to;
        a.click();
      }).catch(function () {
        a.removeAttribute('aria-busy');
        openDetail(t, a);
      });
    });
    return a;
  }

  /* What a title not held is doing: *icon* and words, or null where it may be downloaded. */
  function course(t) {
    if (t.in_library || /^(in_library|partial)$/.test(t.download_state || '')) return ['i-check', 'In library'];
    if (t.download_state === 'queued') return ['i-clock', 'Queued'];
    if (t.download_state !== 'downloading') return null;
    return ['i-clock', 'Downloading' + (typeof t.download_pct === 'number' ? ' ' + t.download_pct + '%' : '')];
  }

  function downloadButton(t) {
    var at = course(t);
    var b = button('ans__go ' + (at ? 'ans__go--ok' : 'ans__go--dl'));
    b.appendChild(MM.dom.icon(at ? at[0] : 'i-download'));
    b.setAttribute('aria-label', at ? t.title + ': ' + at[1] : 'Download ' + t.title);
    b.setAttribute('title', at ? at[1] : 'Download');
    b.addEventListener('click', function () {
      // Which seasons of a show, or how far a download is, is in its detail.
      if (at || t.kind === 'show') openDetail(t, b);
      else download(t, b);
    });
    return b;
  }

  function download(t, b) {
    if (b.hasAttribute('aria-busy')) return;
    b.setAttribute('aria-busy', 'true');
    MM.api.get('/api/watch/title/' + encodeURIComponent(t.ref)).then(function (data) {
      var request = data.download && data.download.request;
      var url = request && safePath(request.url);
      if (!url) {
        b.removeAttribute('aria-busy');
        openDetail(t, b);
        return;
      }
      return MM.api.post(url, request.body || undefined).then(function () {
        core.toast(t.title + ' is queued.', { variant: 'success' });
        tellQueued(t, data);
      });
    }).catch(function (err) {
      b.removeAttribute('aria-busy');
      var there = err && err.status === 409;
      core.toast(MM.api.messageOf(err, 'Couldn’t add ' + t.title + '. Try again.'), { variant: there ? 'info' : 'error' });
      if (there) tellQueued(t, {});
    });
  }

  function tellQueued(t, data) {
    document.dispatchEvent(new CustomEvent('mm:title-changed', {
      detail: { ref: t.ref, download_state: 'queued', download_pct: null, title: t.title, kind: t.kind, year: data.year || t.year }
    }));
  }

  document.addEventListener('mm:title-changed', function (e) {
    items.forEach(function (item) {
      function now(set) {
        if (!set || !set.others.some(function (t) { return t.ref === e.detail.ref; })) return set;
        return {
          owned: set.owned,
          others: set.others.map(function (t) {
            if (t.ref !== e.detail.ref) return t;
            return Object.assign({}, t, { download_state: e.detail.download_state, download_pct: e.detail.download_pct });
          })
        };
      }
      var all = item.shown === item.base;
      item.base = now(item.base);
      item.shown = all ? item.base : now(item.shown);
      changed(item);
    });
  });

  function card(t) {
    var node = el('article', 'ans');
    var link = el('a', 'ans__p');
    link.setAttribute('data-title-ref', refOf(t));
    link.setAttribute('data-title', t.title);
    link.setAttribute('aria-label', t.title);
    if (t.kind === 'movie' || t.kind === 'show') link.setAttribute('data-kind', t.kind);
    if (t.in_library) link.setAttribute('data-in-library', '');
    link.href = safePath(t.href) || '#title=' + encodeURIComponent(refOf(t));
    var art = link.appendChild(el('span', 'ans__art'));
    var poster = safePath(t.poster_url);
    if (poster) {
      var img = el('img');
      img.src = poster;
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('data-broken-on-error', '');
      art.appendChild(img);
    }
    node.appendChild(link);
    var text = node.appendChild(el('div', 'ans__t'));
    var name = text.appendChild(el('b', '', t.title));
    // The link says it.
    name.setAttribute('aria-hidden', 'true');
    text.appendChild(el('small', '', facts(t)));
    text.appendChild(el('p', '', t.reason));
    node.appendChild(t.in_library ? playButton(t) : downloadButton(t));
    return node;
  }

  function group(key, title, list) {
    if (!list.length) return [];
    return [{
      key: 'head-' + key,
      make: function () {
        var node = el('div', 'ask__group');
        node.appendChild(el('h2', '', title));
        node.appendChild(el('small'));
        return node;
      },
      update: function (node) { node.querySelector('small').textContent = list.length; }
    }].concat(list.map(function (t) {
      return { key: t.ref, sig: JSON.stringify(t), make: function () { return card(t); } };
    }));
  }

  /* ── The place of a question ── */

  /* *host* shows a question. *opts*: say(text) reads it out, ask(text) asks another,
     stopped(item), and around(change) makes a change of height. */
  function view(host, opts) {
    var status = host.appendChild(el('p', 'sr-only'));
    status.setAttribute('role', 'status');
    var inside = host.appendChild(el('div', 'ask__in'));
    var v = { host: host, item: null, draw: draw, row: row, show: show, hide: hide, pick: pick, offered: offered };
    var drawn = null; // what *inside* holds: { item, phase, ... }
    views.push(v);

    function fill(item, phase, nodes) {
      var had = inside.contains(document.activeElement);
      inside.textContent = '';
      nodes.forEach(function (node) { if (node) inside.appendChild(node); });
      host.hidden = !nodes.length;
      drawn = nodes.length ? { item: item, phase: phase } : null;
      host.classList.toggle('is-thinking', phase === 'thinking');
      host.setAttribute('aria-busy', phase === 'thinking' ? 'true' : 'false');
      if (had && nodes.length) nodes[0].focus({ preventScroll: true });
    }

    function say(text) { if (status.textContent !== text) status.textContent = text; }

    function hide() {
      v.item = null;
      say('');
      if (drawn) opts.around(function () { fill(null, '', []); });
    }

    /* Show *item* where it stands; a new one as the row that asks it. */
    function show(item) {
      if (views.indexOf(v) < 0) views.push(v);
      v.item = item;
      draw();
    }

    function row(text) { show(question('ask', text)); }

    /* Whether Return would ask what is shown. */
    function offered() { return !!v.item && v.item.state === 'new'; }

    /* The row is what Return does, until the arrow keys are on something else. */
    function pick(on) {
      var node = drawn && drawn.phase === 'new' ? inside.firstChild : null;
      if (node) node.classList.toggle('is-picked', on);
    }

    function draw() {
      var item = v.item;
      if (!item) return;
      var phase = item.state;
      var here = drawn && drawn.phase === phase ? drawn : null;
      if (here && phase === 'new') {
        inside.querySelector('b').textContent = rowTitle(item);
        drawn.item = item;
        return;
      }
      if (here && here.item === item && phase === 'answer') { update(item); return; }
      if (here && here.item === item && phase === 'thinking') { wait(item); return; }
      opts.around(function () {
        if (phase === 'new') fill(item, phase, [offer(item)]);
        else if (phase === 'thinking') thinking(item);
        else if (phase === 'answer') answer(item);
        else if (phase === 'nothing') nothing(item);
        else refused(item);
      });
    }

    function rowTitle(item) {
      return item.kind === 'like' ? 'Find more like ' + item.name : 'Ask AI: “' + item.value + '”';
    }

    function offer(item) {
      var b = button('ask__row is-picked');
      b.appendChild(mark());
      b.appendChild(words(rowTitle(item), 'Real titles that fit, yours first'));
      var key = b.appendChild(el('kbd', 'ask__key', 'return'));
      key.setAttribute('aria-hidden', 'true');
      b.addEventListener('click', function () {
        if (v.item.kind === 'like') start(v.item);
        else opts.ask(v.item.value);
      });
      say('');
      return b;
    }

    function heading(item) { return item.kind === 'like' ? 'More like ' + item.name : '“' + item.value + '”'; }

    function waitText(item) {
      return item.waiting ? 'Finishing your last question first'
        : item.slow ? 'Still looking. This one is taking longer than usual.' : 'Looking for titles that fit';
    }

    function wait(item) {
      inside.querySelector('small').textContent = waitText(item);
      say(waitText(item));
    }

    function thinking(item) {
      var b = button('txt ask__stop', 'Stop');
      b.addEventListener('click', function () {
        stop(item);
        opts.stopped(item);
      });
      fill(item, 'thinking', [head(heading(item), waitText(item), b), outline()]);
      say(waitText(item));
    }

    function chip(text) { return button('chip', text); }

    function answer(item) {
      var body = el('div', 'ask__body');
      var grid = body.appendChild(el('div', 'ask__grid'));
      var refine = el('div', 'refine');
      refine.setAttribute('role', 'group');
      refine.setAttribute('aria-label', 'Narrow it');
      var note = el('p', 'ask__note');
      note.hidden = true;
      fill(item, 'answer', [head(heading(item), ''), body, note, refine, el('p', 'ask__foot', 'Chosen by AI, each checked against TMDB.')]);
      drawn.grid = grid;
      drawn.refine = refine;
      drawn.note = note;
      drawChips(item);
      drawTitles(item);
    }

    function drawTitles(item) {
      var rows = group('owned', 'In your library', item.shown.owned).concat(group('others', 'Not in your library', item.shown.others));
      if (!rows.length) {
        rows = [{ key: 'none', sig: item.chip, make: function () { return el('p', 'ask__none', 'Nothing here fits “' + item.chip + '”'); } }];
      }
      drawn.shown = item.shown;
      window.MMWatch.search.glide(drawn.grid, rows);
      sayCount(item);
    }

    function sayCount(item) {
      var text = item.narrowing ? 'Narrowing' : count(item);
      inside.querySelector('small').textContent = text;
      drawn.note.hidden = !item.note;
      drawn.note.textContent = item.note;
      say(item.note || text);
    }

    function update(item) {
      host.classList.toggle('is-refining', item.narrowing);
      drawChips(item);
      if (drawn.shown === item.shown) { sayCount(item); return; }
      opts.around(function () { drawTitles(item); }, [drawn.refine, drawn.refine.nextElementSibling]);
    }

    function drawChips(item) {
      var refine = drawn.refine;
      refine.hidden = !item.chips.length;
      if (drawn.chips !== item.chips) {
        drawn.chips = item.chips;
        refine.textContent = '';
        refine.appendChild(el('small', '', 'Narrow it'));
        item.chips.forEach(function (text) {
          var b = chip(text);
          b.setAttribute('data-chip', text);
          b.addEventListener('click', function () { narrow(item, text); });
          refine.appendChild(b);
        });
      }
      core.toArray(refine.querySelectorAll('.chip')).forEach(function (b) {
        var on = b.getAttribute('data-chip') === item.pressed;
        if (b.getAttribute('aria-pressed') === String(on)) return;
        b.setAttribute('aria-pressed', String(on));
        var tick = b.querySelector('.ico');
        if (tick) tick.remove();
        if (on) b.insertBefore(MM.dom.icon('i-check'), b.firstChild);
      });
    }

    function nothing(item) {
      var told = item.kind === 'like' ? 'Nothing real is like “' + item.name + '”.' : 'Nothing real matches “' + item.value + '”.';
      var less = null;
      if (item.instead.length) {
        less = el('div', 'refine is-wrap');
        item.instead.forEach(function (text) {
          var b = less.appendChild(chip(text));
          b.addEventListener('click', function () { opts.ask(text); });
        });
      }
      fill(item, 'nothing', [head('No title fits all of that', 'Nothing was made up to fill the gap'),
        el('p', 'ask__note', told + (less ? ' Dropping one part may be enough:' : '')), less]);
      say('No title fits all of that');
    }

    function refused(item) {
      var why = item.refused;
      var b = null;
      if (why.again) {
        b = chip(why.again);
        b.addEventListener('click', function () { again(item); });
      }
      fill(item, 'refused', [head(why.title, why.text, b)]);
      say(why.title + '. ' + why.text);
    }

    host.hidden = true;
    return v;
  }

  window.MMWatch.ask = {
    load: load,
    state: function () { return state; },
    available: available,
    fits: fits,
    record: record,
    forget: forget,
    question: question,
    start: start,
    view: view,
    dur: dur
  };
})();
