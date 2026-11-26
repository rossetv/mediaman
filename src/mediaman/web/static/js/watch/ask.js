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
  var POLL_MS = 1200;
  var SLOW_MS = 12000;
  var STOPPING_MS = 1500; // between tries while the call of a stopped question ends
  var STOPPING_TRIES = 60;
  var LEAVING_MS = 8000; // how long a question waits at most for the server to be told of one given up
  var POLL_TRIES = 5; // polls in a row that may fail
  var KEPT = 30; // questions this page remembers
  var RECENT = 6;
  var STORE = 'mm.watch.ask'; // of this tab: what was asked and the run begun last, with whose they are
  var LIKES = 20; // pages of titles like one it remembers
  var LONGEST = 200; // of a question, as the server takes it

  function tidy(text) { return String(text || '').replace(/\s+/g, ' ').trim().slice(0, LONGEST).trim(); }

  function wordCount(text) {
    return tidy(text).split(' ').filter(function (word) { return /[\p{L}\p{N}]/u.test(word); }).length;
  }

  /* ── What the panel knows ── */

  var state = null; // { account, available, records, recent, phrases, min_words }
  var loading = null;
  var wipes = 0; // times the searches were forgotten
  var pageLeft = false; // the page is being left for the next person's

  /* Ask the server again: what it knows changes, and so may who is signed in. */
  function load() {
    if (loading || pageLeft) return loading || Promise.resolve();
    var mine = wipes;
    loading = MM.api.get(URL).then(function (data) {
      if (mine !== wipes) return;
      var was = available();
      if (state && state.account !== data.account) { leavePage(); return; }
      state = {
        account: data.account || '',
        available: data.available === true,
        records: data.records === true,
        recent: data.recent || [],
        phrases: data.personal && data.phrases ? data.phrases : [],
        min_words: data.min_words || 3,
        why: data.why || null // why there is no AI, where there is none
      };
      if (available() !== was) sayAvailable();
    }).catch(function () { /* nothing of it is offered */ }).then(function () { loading = null; });
    return loading;
  }

  /* Someone else is signed in now: nothing of the person before is left on the page, in the
     bar, the address or the tab. */
  function leavePage() {
    if (pageLeft) return;
    pageLeft = true;
    keep(null);
    leave();
    items.clear();
    views.forEach(function (v) { v.hide(); });
    state = null;
    core.byId('watch-sheet').textContent = '';
    core.byId('watch-search').value = '';
    window.location.replace('/');
  }

  /* Send what is the person's own, with whose page this is: the server refuses it for another. */
  function send(method, path, body) {
    var opts = { headers: { 'X-Assist-Account': state ? state.account : '' } };
    return (method === 'delete' ? MM.api.delete(URL + path, opts) : MM.api.post(URL + path, body, opts)).catch(function (err) {
      if (err && err.error === 'other_account') leavePage();
      throw err;
    });
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
    if (!state || !state.records || !phrase || (state.recent[0] && same(state.recent[0], phrase))) return;
    var mine = wipes;
    send('post', '/searches', { q: phrase }).then(function (data) {
      if (!data.recorded || mine !== wipes || !state) return;
      var rest = state.recent.filter(function (r) { return !same(r, phrase); });
      state = Object.assign({}, state, { recent: [phrase].concat(rest).slice(0, RECENT) });
    }).catch(function () { /* the search itself is not held up by it */ });
  }

  /* Forget them, with the phrases and the answers made from them. */
  function forget() {
    return send('delete', '/searches').then(function () {
      wipes++;
      state = Object.assign({}, state, { recent: [], phrases: [] });
      keep(null);
      dropAll();
    });
  }

  /* What this tab remembers of the person who is signed in. */
  function kept() {
    try {
      var held = JSON.parse(window.sessionStorage.getItem(STORE)) || {};
      return state && state.account && held.who === state.account ? held : {};
    } catch (e) { return {}; }
  }

  function keep(what) {
    if (what && !(state && state.account)) return;
    try {
      if (what) window.sessionStorage.setItem(STORE, JSON.stringify(Object.assign({}, kept(), what, { who: state.account })));
      else window.sessionStorage.removeItem(STORE);
    } catch (e) { /* storage blocked */ }
  }

  function isKept() {
    try { return window.sessionStorage.getItem(STORE) !== null; } catch (e) { return false; }
  }

  /* The title whose like are on a page of their own: { ref, name, poster, asked }. */
  function keptLike(ref) {
    return (kept().likes || []).filter(function (l) { return l.ref === ref; })[0] || null;
  }

  function keepLike(like) {
    var rest = (kept().likes || []).filter(function (l) { return l.ref !== like.ref; });
    keep({ likes: [like].concat(rest).slice(0, LIKES) });
  }

  /* Forget every question and what came of it. */
  function dropAll() {
    leave();
    items.clear();
    views = views.filter(function (v) { return v.host.isConnected; });
    views.forEach(function (v) { if (v.item) v.show(question(v.item.kind, v.item.value, v.item.name)); });
  }

  // A page that Back brings is as it was left: things may have been cleared since, or the person changed.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    if (items.size && !isKept()) dropAll();
    wipes++;
    loading = null;
    if (state) state = Object.assign({}, state, { recent: [], phrases: [] });
    load();
  });

  // Another tab may have signed someone else in.
  document.addEventListener('visibilitychange', function () { if (state && document.visibilityState === 'visible') load(); });
  window.addEventListener('focus', function () { if (state) load(); });

  /* ── Questions ── */

  var items = new Map(); // by key, the one asked last at the end
  var going = null; // the one being asked: { item, left, timer, posted, run }
  var leaving = Promise.resolve(); // the last one given up, till the server was told
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
      stage: '', // the step of its answer the server is at
      like: kind === 'ask' ? similarOf(value) : '', // what "similar to …" names
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

  /* Give up what is being asked. */
  function leave() {
    var g = going;
    if (!g) return;
    going = null;
    g.left = true;
    clearTimeout(g.timer);
    clearTimeout(g.slow);
    if (g.wake) g.wake();
    leaving = g.posted.then(function (run) { return run ? endRun(run) : null; });
    rest(g.item);
    changed(g.item);
  }

  function endRun(run) {
    return MM.api.delete(URL + '/runs/' + run).catch(function () { /* it has ended, or does by itself */ });
  }

  /* Once *told* settles, or takes too long. */
  function inTime(told) {
    return new Promise(function (resolve) {
      var timer = setTimeout(resolve, LEAVING_MS);
      function go() {
        clearTimeout(timer);
        resolve();
      }
      told.then(go, go);
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

  /* Ask it, or for it narrowed by *chip*, which is words of its own answer. *over* is the run
     of another tab whose place it takes. */
  function start(item, chip, over) {
    chip = chip || '';
    remember(item);
    leave();
    var g = going = { item: item, chip: chip, left: false, timer: null, slow: null, wake: null };
    g.posted = inTime(leaving).then(function () {
      return over && !g.left ? inTime(endRun(over)) : null;
    }).then(function () { return post(g, STOPPING_TRIES); });
    item.pressed = chip;
    item.note = '';
    item.refused = null;
    item.stage = '';
    if (chip) item.narrowing = true;
    else item.state = 'thinking';
    changed(item);
    g.slow = setTimeout(function () {
      item.slow = true;
      changed(item);
    }, SLOW_MS);
  }

  /* Returns the run to poll, or null: it was answered, refused or given up. */
  function post(g, tries) {
    if (g.left) return null;
    var item = g.item;
    var body = item.kind === 'like' ? { ref: item.value } : { q: item.value };
    if (g.chip) body.refine = g.chip;
    return send('post', '/' + item.kind, body).then(function (data) {
      if (data.status !== 'running') {
        if (!g.left) done(g, data);
        return null;
      }
      // Another page of this tab may find it going. Not over the newer one's.
      if (!going || going === g) keep({ run: data.run_id });
      if (!g.left) poll(g, data.run_id, POLL_TRIES);
      return data.run_id;
    }, function (err) {
      if (g.left) return null;
      var left = err && err.error === 'running' && err.data && err.data.run_id;
      if (left && left === kept().run) {
        keep({ run: 0 });
        return inTime(endRun(left)).then(function () { return post(g, tries); });
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
        else if (data.status === 'running') {
          if (data.stage && data.stage !== g.item.stage) {
            g.item.stage = data.stage;
            changed(g.item);
          }
          poll(g, run, POLL_TRIES);
        }
        else if (data.status === 'cancelled') {
          ended(g);
          changed(g.item);
        } else refuse(g, data.error === 'unavailable' ? { error: data.error, data: data } : null);
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

  /* ── Refusals, in words ── */

  function clock(at) { return at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); }

  /* "after midnight", or the person's own time for the server's. */
  function resets(iso) {
    var at = new Date(iso || NaN);
    if (isNaN(at)) return '';
    if (at.getHours() + at.getMinutes() === 0) return ' after midnight';
    return ' after ' + clock(at) + (at.toDateString() === new Date().toDateString() ? '' : ' tomorrow');
  }

  /* What the person is told: { title, text, again }, *again* the button that asks once more.
     *kind*: only under a question is there a search. */
  function reason(err, kind) {
    var search = kind === 'ask' ? 'Search below works as always.' : 'Search works as always.';
    var code = err && err.error;
    var data = (err && err.data) || {};
    var limit = typeof data.limit === 'number' ? data.limit : null;
    if (code === 'unavailable' || ((code === 'limit' || code === 'limit_everyone') && limit === 0)) {
      return { off: true, why: code === 'unavailable' ? data.why || (state && state.why) : 'off', title: 'AI isn’t available right now', text: search };
    }
    if (code === 'limit') {
      return {
        title: limit === null ? 'You’ve asked all your questions for today'
          : 'You’ve asked ' + limit + (limit === 1 ? ' question today' : ' questions today'),
        text: 'You can ask again' + (resets(data.resets_at) || ' tomorrow') + '. ' + search
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
    var why = reason(err, item.kind);
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

  function again(item) { start(item, '', item.refused && item.refused.run); }

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

  /* The poster of the title the others are like, in the mark's place. */
  function face(url) {
    var node = el('span', 'ask__mark ask__mark--art');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(MM.dom.icon('i-sparkles'));
    var src = safePath(url);
    if (src) {
      var img = el('img');
      img.src = src;
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
      node.appendChild(img);
    }
    return node;
  }

  function words(title, text) {
    var node = el('span', 'ask__words');
    node.appendChild(el('b', '', title));
    node.appendChild(el('small', '', text));
    return node;
  }

  /* What is asked, or what became of it, over the answer; *pic* stands in the mark's place. */
  function head(title, text, after, pic) {
    var node = el('div', 'ask__row');
    node.tabIndex = -1;
    node.appendChild(pic || mark());
    var said = words(title, text);
    said.classList.add('ask__swapin');
    node.appendChild(said);
    if (after) node.appendChild(after);
    return node;
  }

  /* ── The title a search names, and those like it ── */

  var SIMILAR_URL = '/api/watch/similar?q=';
  var SIMILAR_RE = /^(?:(?:something|anything|shows?|films?|movies?|series|titles?)\s+)?(?:(?:similar|close)\s+to|(?:more\s+)?like)\s+(.+)$/i;
  var LOOKS = 20; // titles named that the page remembers
  var looks = new Map(); // by what was named: { state: loading or ok, title, items }

  /* What "similar to …" or "like …" names, or ''. */
  function similarOf(text) {
    var found = SIMILAR_RE.exec(tidy(text));
    return found && wordCount(found[1]) ? found[1] : '';
  }

  function looked(item) { return item.like ? looks.get(item.like.toLowerCase()) || null : null; }

  /* The title *item* names, once TMDB has told it: { ref, library_ref, title, kind, year, poster_url }. */
  function named(item) {
    var found = looked(item);
    return found && found.state === 'ok' ? found.title : null;
  }

  /* Ask for what *item* names and the titles like it, once for each name. One that failed is asked again. */
  function lookSimilar(item) {
    var key = item.like && item.like.toLowerCase();
    if (!key || looks.has(key)) return;
    var found = { state: 'loading', title: null, items: [] };
    looks.set(key, found);
    if (looks.size > LOOKS) looks.delete(looks.keys().next().value);
    MM.api.get(SIMILAR_URL + encodeURIComponent(item.like)).then(function (data) {
      found.state = 'ok';
      found.title = data.title || null;
      found.items = data.items || [];
    }, function () {
      looks.delete(key);
    }).then(function () {
      views.forEach(function (v) { if (v.item && v.item.like && v.item.like.toLowerCase() === key) v.draw(); });
    });
  }

  /* ── The titles shown ── */

  var WAITING = 8; // poster outlines while the AI looks
  var WAITING_AFTER = 4; // of them after titles already shown
  var STAGGER_MS = 35;
  var STAGES = { taste: 'Reading what you watch…', choosing: 'Choosing titles…', checking: 'Checking them on TMDB…' };

  function titles(item) { return item.shown ? item.shown.owned.concat(item.shown.others) : []; }

  function refOf(t) { return (t.in_library && t.library_ref) || t.ref || ''; }

  /* What the grid holds: the titles like the one named, where they stand, then the AI's picks; one
     the AI picked of them too is its pick. *phase* is the question's. [{ t, ai }] */
  function picks(item, phase) {
    var like = named(item);
    var found = like && !item.chip ? looked(item).items : [];
    var ai = phase === 'answer' ? titles(item) : [];
    var chosen = new Map();
    ai.forEach(function (t) { chosen.set(t.ref, t); });
    var seen = new Set();
    var all = [];
    found.forEach(function (t) {
      if (seen.has(t.ref)) return;
      seen.add(t.ref);
      var pick = chosen.get(t.ref);
      all.push({ t: pick || t, ai: !!pick });
    });
    ai.forEach(function (t) {
      var itself = like && (t.ref === like.ref || (!!like.library_ref && t.library_ref === like.library_ref));
      if (seen.has(t.ref) || itself) return;
      seen.add(t.ref);
      all.push({ t: t, ai: true });
    });
    return all;
  }

  function count(item, phase) {
    var all = picks(item, phase);
    var held = all.filter(function (x) { return x.t.in_library; }).length;
    var n = all.length;
    return n + (n === 1 ? ' title' : ' titles') + (n ? ', ' + (held === n ? 'all' : held || 'none') + ' already here' : '');
  }

  /* A poster card as every row has, marked where the AI picked it, with why in its preview and its page. */
  function card(x) {
    var search = window.MMWatch.search;
    var t = x.t;
    var node = t.in_library
      ? search.heldCard(Object.assign({}, t, { ref: refOf(t) }))
      : search.tmdbCard(t);
    if (!x.ai) return node;
    node.classList.add('is-ai-pick');
    var link = node.querySelector('.watch-card__link');
    if (t.reason) link.setAttribute('data-ai-why', t.reason);
    var spark = el('span', 'watch-card__spark');
    spark.appendChild(MM.dom.icon('i-sparkles'));
    spark.appendChild(el('span', 'sr-only', 'AI pick'));
    node.querySelector('.watch-card__poster').appendChild(spark);
    return node;
  }

  function outlineCard() {
    var node = el('div', 'watch-card watch-card--skel ask__skel');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(el('span', 'skel skel--poster'));
    node.appendChild(el('span', 'skel skel--line'));
    return node;
  }

  function groupRow(key, title, note, n) {
    return {
      key: 'head-' + key,
      sig: note,
      make: function () {
        var node = el('div', 'results-group__head ask__group');
        node.appendChild(el('h2', 'results-group__title', title));
        node.appendChild(el('span', 'results-group__count'));
        if (note) node.appendChild(el('span', 'ask__group-note', note));
        return node;
      },
      update: function (node) { node.querySelector('.results-group__count').textContent = n + (n === 1 ? ' title' : ' titles'); }
    };
  }

  function cardRows(list) {
    return list.map(function (x) {
      return { key: refOf(x.t), sig: JSON.stringify([x.ai, x.t]), keepFocus: true, make: function () { return card(x); } };
    });
  }

  /* The grid's rows, grouped by whether the library has them; outlines follow while the AI looks.
     *can* is the page's can_download: those not in the library are offered for download only of a kind it allows. */
  function gridRows(item, phase, can) {
    var all = picks(item, phase);
    var held = all.filter(function (x) { return x.t.in_library; });
    var others = all.filter(function (x) { return !x.t.in_library; });
    var some = others.some(function (x) { return can[x.t.kind] !== false; });
    var rows = [];
    if (held.length) rows = rows.concat(groupRow('owned', 'In your library', '', held.length), cardRows(held));
    if (others.length) rows = rows.concat(groupRow('others', 'Not in your library', some ? 'Open one to download it' : '', others.length), cardRows(others));
    var found = looked(item);
    var waits = phase === 'thinking' || (!!found && found.state === 'loading' && phase !== 'refused');
    if (waits) {
      for (var i = 0; i < (all.length ? WAITING_AFTER : WAITING); i++) {
        rows.push({ key: 'skel-' + i, make: outlineCard });
      }
    }
    if (!rows.length && phase === 'answer' && item.chip) {
      rows = [{ key: 'none', sig: item.chip, make: function () { return el('p', 'ask__none', 'Nothing here fits “' + item.chip + '”'); } }];
    }
    return rows;
  }

  /* What the person is told where the AI is off: its *code* is the server's. An admin is told what to set. */
  function offWords(code, admin) {
    if (!admin) return { title: 'AI isn’t available right now', text: 'Search works as always.' };
    if (code === 'no_key') return { title: 'AI needs an OpenAI key', text: 'Add one in Settings, then ask again.', link: ['Open the OpenAI card', '/settings#intg-openai'] };
    if (code === 'refused') return { title: 'OpenAI refused the key', text: 'Check it in the OpenAI card.', link: ['Open the OpenAI card', '/settings#intg-openai'] };
    if (code === 'no_tmdb') return { title: 'AI needs TMDB', text: 'It checks every title it names against TMDB.', link: ['Set up TMDB', '/settings#setg-integrations'] };
    if (code === 'off') return { title: 'AI in search is switched off', text: 'A daily limit is set to 0 in the OpenAI card.', link: ['Open the OpenAI card', '/settings#intg-openai'] };
    return { title: 'AI isn’t available right now', text: 'Search works as always.' };
  }

  document.addEventListener('mm:title-changed', function (e) {
    function now(t) {
      if (t.ref !== e.detail.ref) return t;
      return Object.assign({}, t, { download_state: e.detail.download_state, download_pct: e.detail.download_pct });
    }
    items.forEach(function (item) {
      function set(s) {
        if (!s || !s.others.some(function (t) { return t.ref === e.detail.ref; })) return s;
        return { owned: s.owned, others: s.others.map(now) };
      }
      var all = item.shown === item.base;
      item.base = set(item.base);
      item.shown = all ? item.base : set(item.shown);
    });
    looks.forEach(function (found) { found.items = found.items.map(now); });
    views.forEach(function (v) { if (v.item) v.draw(); });
  });

  /* ── The place of a question ── */

  /* *host*, which stands on the page, shows a question. *opts*: say(text) reads it out, ask(text) asks another,
     plain(text) searches for it as it is, stopped(item), around(change) makes a change of height, can is the
     page's can_download and admin whether the person may change the settings. */
  function view(host, opts) {
    // Beside it: what is read out must not wait for what is busy.
    var status = el('p', 'sr-only');
    status.setAttribute('role', 'status');
    host.parentNode.insertBefore(status, host);
    var inside = host.appendChild(el('div', 'ask__in'));
    var v = { host: host, item: null, draw: draw, row: row, show: show, hide: hide, pick: pick, offered: offered };
    var drawn = null; // what *inside* holds: { item, phase, shape, grid, ... }
    views.push(v);

    function fill(item, phase, nodes) {
      var had = inside.contains(document.activeElement);
      inside.textContent = '';
      nodes = nodes.filter(Boolean);
      nodes.forEach(function (node) { inside.appendChild(node); });
      host.hidden = !nodes.length;
      drawn = nodes.length ? { item: item, phase: phase, shape: shapeOf(item) } : null;
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
    function offered() { return !!v.item && v.item.state === 'new' && (v.item.kind === 'like' || fits(v.item.value)); }

    /* The row is what Return does, until the arrow keys are on something else. */
    function pick(on) {
      var node = drawn && drawn.phase === 'new' && inside.firstChild && inside.firstChild.tagName === 'BUTTON' ? inside.firstChild : null;
      if (node) node.classList.toggle('is-picked', on);
    }

    /* What is drawn the same way: a change of it is drawn anew. */
    function shapeOf(item) {
      var like = named(item);
      return [item.state, like ? like.ref : '', offered()].join('|');
    }

    function draw() {
      var item = v.item;
      if (!item) return;
      var phase = item.state;
      var here = drawn && drawn.shape === shapeOf(item) ? drawn : null;
      if (here && phase === 'new') {
        inside.querySelector('b').textContent = rowTitle(item);
        drawn.item = item;
        drawGrid(item);
        return;
      }
      if (here && here.item === item && phase === 'answer') { update(item); return; }
      if (here && here.item === item && phase === 'thinking') { wait(item); return; }
      opts.around(function () {
        if (phase === 'new') offer(item);
        else if (phase === 'thinking') thinking(item);
        else if (phase === 'answer') answer(item);
        else if (phase === 'nothing') nothing(item);
        else refused(item);
      });
    }

    function heading(item) {
      var like = named(item);
      if (like) return 'Because you like ' + like.title;
      return item.kind === 'like' ? 'More like ' + item.name : '“' + item.value + '”';
    }

    function rowTitle(item) {
      if (named(item)) return heading(item);
      return item.kind === 'like' ? 'Find more like ' + item.name : 'Ask AI: “' + item.value + '”';
    }

    function pic(item) {
      var like = named(item);
      return like ? face(like.poster_url) : null;
    }

    /* The grid of titles, which every phase but a refusal of no title named has. */
    function body() {
      var node = el('div', 'ask__body');
      var grid = node.appendChild(el('div', 'watch-grid ask__grid'));
      return { node: node, grid: grid };
    }

    function drawGrid(item) {
      if (!drawn || !drawn.grid) return;
      window.MMWatch.search.glide(drawn.grid, gridRows(item, drawn.phase, opts.can || {}), null, STAGGER_MS);
    }

    /* Under titles like the one named, where the AI is not to be asked: why not, for an admin with the way to it. */
    function offNote() {
      if (available()) return null;
      var off = offWords(state && state.why, opts.admin);
      var note = el('p', 'ask__note ask__note--quiet');
      note.appendChild(document.createTextNode('AI picks: ' + off.title + '.' + (off.link ? ' ' + off.text + ' ' : '')));
      if (off.link) {
        var a = note.appendChild(el('a', 'ask__link', off.link[0]));
        a.href = off.link[1];
      }
      return note;
    }

    function offer(item) {
      var like = named(item);
      var b = null;
      if (offered()) {
        b = button('ask__row is-picked');
        b.appendChild(pic(item) || mark());
        b.appendChild(words(rowTitle(item), like ? 'Ask AI for picks that fit you too' : 'Real titles that fit, yours first'));
        var key = b.appendChild(el('kbd', 'ask__key', 'return'));
        key.setAttribute('aria-hidden', 'true');
        b.addEventListener('click', function () {
          if (v.item.kind === 'like') start(v.item);
          else opts.ask(v.item.value);
        });
      } else if (like) {
        b = head(heading(item), 'Titles TMDB finds like it', null, pic(item));
      }
      var part = like ? body() : null;
      fill(item, 'new', b ? [b, part && part.node, like && offNote()] : []);
      if (drawn && part) {
        drawn.grid = part.grid;
        drawGrid(item);
      }
      say('');
    }

    function waitText(item) {
      if (item.waiting) return 'Finishing your last question first…';
      var stage = item.stage || 'taste';
      var text = stage === 'taste' && !(state && state.records) ? 'Reading your question…' : STAGES[stage] || STAGES.taste;
      return item.slow ? text + ' This one is taking longer than usual.' : text;
    }

    /* The step it is at, in place of the one before. */
    function wait(item) {
      var text = waitText(item);
      var small = inside.querySelector('.ask__words small');
      if (small && small.textContent !== text) {
        var next = el('small', 'ask__step', text);
        small.replaceWith(next);
      }
      say(text);
      drawGrid(item);
    }

    function thinking(item) {
      var b = button('txt ask__stop', 'Stop');
      b.addEventListener('click', function () {
        stop(item);
        opts.stopped(item);
      });
      var part = body();
      if (!named(item)) part.node.setAttribute('aria-hidden', 'true');
      fill(item, 'thinking', [head(heading(item), waitText(item), b, pic(item)), part.node]);
      drawn.grid = part.grid;
      drawGrid(item);
      say(waitText(item));
    }

    function chip(text) { return button('chip', text); }

    function foot(item) {
      var node = el('p', 'ask__foot');
      node.appendChild(MM.dom.icon('i-sparkles'));
      node.appendChild(document.createTextNode(named(item) && !item.chip
        ? 'Marked titles are chosen by AI, each checked against TMDB. The rest are TMDB’s own.'
        : 'Chosen by AI, each checked against TMDB.'));
      return node;
    }

    function answer(item) {
      var part = body();
      var refine = el('div', 'refine');
      refine.setAttribute('role', 'group');
      refine.setAttribute('aria-label', 'Narrow it');
      var note = el('p', 'ask__note');
      note.hidden = true;
      fill(item, 'answer', [head(heading(item), '', null, pic(item)), part.node, note, refine, foot(item)]);
      drawn.grid = part.grid;
      drawn.refine = refine;
      drawn.note = note;
      drawChips(item);
      drawGrid(item);
      sayCount(item);
    }

    function sayCount(item) {
      var text = item.narrowing ? 'Narrowing' : count(item, 'answer');
      inside.querySelector('.ask__words small').textContent = text;
      drawn.note.hidden = !item.note;
      drawn.note.textContent = item.note;
      say(item.note || text);
    }

    function update(item) {
      host.classList.toggle('is-refining', item.narrowing);
      drawChips(item);
      opts.around(function () {
        drawGrid(item);
        sayCount(item);
      }, [drawn.refine, drawn.refine.nextElementSibling]);
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

    /* The ways on from a question that came to nothing: its own, a plain search for the title it named, and a setting to change. */
    function ways(item, own, link) {
      var row = el('div', 'refine is-wrap ask__ways');
      (own || []).forEach(function (b) { row.appendChild(b); });
      if (item.like && opts.plain) {
        var b = row.appendChild(chip('Search for “' + item.like + '”'));
        b.insertBefore(MM.dom.icon('i-magnifying-glass'), b.firstChild);
        b.addEventListener('click', function () { opts.plain(item.like); });
      }
      if (link) {
        var a = row.appendChild(el('a', 'chip', link[0]));
        a.href = link[1];
      }
      return row.children.length ? row : null;
    }

    /* What is like the title named stays under what came of the question. */
    function withLike(item, phase, nodes) {
      var part = named(item) ? body() : null;
      fill(item, phase, nodes.concat(part ? [part.node] : []));
      if (part && drawn) {
        drawn.grid = part.grid;
        drawGrid(item);
      }
    }

    function nothing(item) {
      var told = item.kind === 'like' ? 'Nothing real is like “' + item.name + '”.' : 'Nothing real matches “' + item.value + '”.';
      var less = item.instead.map(function (text) {
        var b = chip(text);
        b.addEventListener('click', function () { opts.ask(text); });
        return b;
      });
      withLike(item, 'nothing', [head('No title fits all of that', 'Nothing was made up to fill the gap', null, pic(item)),
        el('p', 'ask__note', told + (less.length ? ' Dropping one part may be enough:' : '')), ways(item, less)]);
      say('No title fits all of that');
    }

    function refused(item) {
      var why = item.refused;
      var off = why.off ? offWords(why.why, opts.admin) : why;
      var b = null;
      if (why.again) {
        b = chip(why.again);
        b.addEventListener('click', function () { again(item); });
      }
      withLike(item, 'refused', [head(off.title, off.text, b, pic(item)), ways(item, [], off.link)]);
      say(off.title + '. ' + off.text);
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
    kept: kept,
    keep: keep,
    keptLike: keptLike,
    keepLike: keepLike,
    question: question,
    start: start,
    view: view,
    dur: dur,
    similarOf: similarOf,
    lookSimilar: lookSimilar
  };
})();
