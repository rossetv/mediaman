/* Watch: Ask AI. What the search panel knows of the AI (whether there is one, the person's
 * recent searches and phrases, how many answers they have left), what reads as a wish, their
 * questions, and a question's place on the page: the outlines that hold it, the wait, the
 * titles that answer it and the ways to narrow them.
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
  var LOW = 3; // answers left at which a wish waits for Return

  function tidy(text) { return String(text || '').replace(/\s+/g, ' ').trim().slice(0, LONGEST).trim(); }

  function wordCount(text) {
    return tidy(text).split(' ').filter(function (word) { return /[\p{L}\p{N}]/u.test(word); }).length;
  }

  /* ── What reads as a wish ── */

  // Always a wish, even over a title of the same words.
  var STRONG = /^(?:something|anything|shows?|films?|movies?|series|tv|documentar(?:y|ies)|anime)\s+(?:like|similar to)\b|\b(?:similar to|more like|close to|in the mood for|recommend|suggest|what (?:should|to|can)|to watch|i want|i'?m in the mood|for (?:kids|a date|date night|a rainy|tonight|a long|a short)|but (?:funnier|darker|shorter|lighter|scarier|sadder|newer|older|less|more|with|without|set))\b|\?\s*$/i;
  // A wish where no title has the words: a kind, a mood, a maker, a place or a time.
  var CUES = /\b(?:movies|films|shows|series|documentaries|docs|sitcoms|thrillers|comedies|dramas|westerns|musicals|originals|classics|miniseries|anime|horror|feel[- ]?good|funny|scary|cosy|cozy|dark|romantic|underrated|mind[- ]?bending|slow[- ]?burn|comfort|bingeable|short|quick|kids|family|netflix|hbo|apple tv|disney|pixar|a24|ghibli|marvel|bbc|nordic|korean|french|japanese|\d0s|(?:19|20)\d\d)\b/i;
  var JOINS = /\b(?:with|about|where|that|who|set in|without|not)\b/;
  // Ends in a word that waits for more: "shows like", "movies with".
  var TAIL = /\b(?:similar to|like|with|without|but|and|or|for|of|the|a|an|about|than|from|set in|in)\s*$/;

  function plainWords(text) { return tidy(text).toLowerCase().replace(/[^\p{L}\p{N}'\s]+/gu, ' ').replace(/\s+/g, ' ').trim(); }

  /* Whether *text* reads as a wish for titles, and whether it is whole enough to ask. *exact*: a
     title has all its words, so only a strong cue makes it a wish. */
  function intent(text, exact) {
    var n = plainWords(text);
    var words = n ? n.split(' ') : [];
    var wish = STRONG.test(tidy(text)) || (!exact && words.length >= 2 && CUES.test(n)) ||
      (!exact && words.length >= 6 && JOINS.test(n));
    wish = wish && words.length >= 2 && n.replace(/[\s']/g, '').length >= 6;
    return { wish: wish, ready: wish && !TAIL.test(n) };
  }

  /* ── What the panel knows ── */

  var state = null; // { account, available, tonight, records, recent, phrases, min_words }
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
        tonight: data.tonight === true,
        records: data.records === true,
        recent: data.recent || [],
        phrases: data.personal && data.phrases ? data.phrases : [],
        min_words: data.min_words || 2,
        // Answers left today: a number, or none known. Asking on its own is on unless switched off.
        left: typeof data.left === 'number' ? data.left : null,
        auto: data.auto !== false,
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

  /* Whether *text* reads as a wish the AI is there to answer. */
  function fits(text, exact) { return available() && intent(text, exact).wish; }

  /* Whether the AI may be asked *text* at all, wish or not, when the person asks for it. */
  function canAsk(text) { return available() && wordCount(text) >= state.min_words; }

  /* Whether a wish is asked once the person pauses: they have not switched it off, and it leaves
     them a few answers of the day to ask for themselves. */
  function autoOk() { return available() && state.auto && !(state.left !== null && state.left <= LOW); }

  /* Whether *text* would be a wish, were the AI on. */
  function shaped(text, exact) { return !!state && intent(text, exact).wish; }

  /* Whether an admin is told, under a wish, what the AI needs: never switched off, which is no news
     on every question. */
  function failing() { return !available() && !!state && /^(no_key|refused|no_tmdb)$/.test(state.why || ''); }

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
      heading: '', // the AI's few words for its answer
      halted: false, // stopped by the person: not asked again on its own
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

  /* The person stops it: it is not asked again until they ask. */
  function stop(item) {
    item.halted = true;
    if (going && going.item === item) leave();
  }

  /* Whether *item* is being asked. */
  function asking(item) { return !!going && going.item === item; }

  function pause(g, ms) {
    return new Promise(function (resolve) {
      g.wake = resolve;
      g.timer = setTimeout(resolve, ms);
    });
  }

  /* Ask it, or for it narrowed by *chip*, which is words of its own answer. *over* is the run
     of another tab whose place it takes; *auto*: the page asks it, as the person paused;
     *fresh*: other titles than its answer's, asked for by the person. A question that names a
     title is asked once that title is found, with it. */
  function start(item, chip, over, auto, fresh) {
    chip = chip || '';
    remember(item);
    leave();
    var g = going = { item: item, chip: chip, auto: !!auto && !chip && !fresh, fresh: !!fresh, left: false, timer: null, slow: null, wake: null };
    g.posted = inTime(leaving).then(function () {
      return over && !g.left ? inTime(endRun(over)) : null;
    }).then(function () { return item.like && !g.left ? lookSimilar(item) : null; })
      .then(function () { return post(g, STOPPING_TRIES); });
    item.halted = false;
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
    var like = likeOf(item);
    if (like) body.like = like;
    if (g.auto) body.auto = true;
    if (g.chip) body.refine = g.chip;
    if (g.fresh) body.fresh = true;
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
          var more = (data.owned || []).length + (data.others || []).length;
          // What is checked already shows, the rest outlined after it. A narrowing keeps the answer it narrows.
          if (!g.chip && more > titles(g.item).length) {
            g.item.shown = { owned: data.owned || [], others: data.others || [], lead: data.lead };
            g.item.stage = data.stage || g.item.stage;
            changed(g.item);
          } else if (data.stage && data.stage !== g.item.stage) {
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
    item.shown = { owned: data.owned || [], others: data.others || [], lead: data.lead };
    item.chips = data.chips || [];
    if (!g.chip) {
      item.base = item.shown;
      item.instead = data.instead || [];
      item.heading = tidy(data.heading);
    }
    item.state = g.chip || titles(item).length ? 'answer' : 'nothing';
    changed(item);
    // How many are left today, which an answer may have spent.
    load();
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
        title: limit === null ? 'That’s today’s AI answers' : 'That’s today’s ' + limit + (limit === 1 ? ' AI answer' : ' AI answers'),
        text: 'They come back' + (resets(data.resets_at) || ' tomorrow') + '. ' + search
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
    if (code === 'too_short') return { title: 'Ask with a few more words', text: 'Two or more say what you want.' };
    return { title: 'AI couldn’t answer just now', text: search, again: 'Try again' };
  }

  function refuse(g, err) {
    var item = g.item;
    var code = err && err.error;
    ended(g);
    // Switched off on another device since: it waits for Return, and says nothing of it.
    if (code === 'auto_off' && g.auto) {
      if (state) state = Object.assign({}, state, { auto: false });
      changed(item);
      return;
    }
    if (code === 'limit' && state) state = Object.assign({}, state, { left: 0 });
    var why = reason(err, item.kind);
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

  /* The sparkle with its stars as paths of its own: WebKit runs no animation inside what a <use>
     copies, and they twinkle in turn while the AI thinks. */
  function sparkle() {
    var icon = MM.dom.icon('i-sparkles');
    var symbol = document.getElementById('i-sparkles');
    var use = icon.querySelector && icon.querySelector('use');
    if (!symbol || !use) return icon;
    icon.setAttribute('viewBox', symbol.getAttribute('viewBox'));
    icon.setAttribute('overflow', 'visible');
    core.toArray(symbol.childNodes).forEach(function (n) { icon.insertBefore(n.cloneNode(true), use); });
    use.remove();
    return icon;
  }

  function orb(icon) {
    var node = el('span', 'ask__orb');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(icon ? MM.dom.icon(icon) : sparkle());
    return node;
  }

  /* "Because you like" the title a question names, with its poster. */
  function because(like) {
    var node = el('span', 'ask__because');
    var pic = node.appendChild(el('span', 'ask__because-pic'));
    pic.setAttribute('aria-hidden', 'true');
    var src = safePath(like.poster_url);
    if (src) {
      var img = pic.appendChild(el('img'));
      img.src = src;
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
    }
    node.appendChild(el('span', '', 'Because you like'));
    node.appendChild(el('b', '', like.title));
    return node;
  }

  /* ── The title a search names ── */

  var SIMILAR_URL = '/api/watch/similar?q=';
  // A bare "like …" is a title's own words as often as not ("Like Crazy"): it is searched for as it is.
  var SIMILAR_RE = /^(?:(?:(?:something|anything|shows?|films?|movies?|series|titles?)\s+)?(?:(?:similar|close)\s+to|more\s+like)|(?:something|anything|shows?|films?|movies?|series|titles?)\s+like)\s+(.+)$/i;
  var LOOKS = 20; // titles named that the page remembers
  var looks = new Map(); // by what was named: { state: loading or ok, title, ready }

  /* What "similar to …" or "more like …" names, or ''. */
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

  /* Find what *item* names, once for each name; one that failed is looked for again. Settles
     once it is known, found or not. */
  function lookSimilar(item) {
    var key = item.like && item.like.toLowerCase();
    if (!key) return Promise.resolve();
    if (looks.has(key)) return looks.get(key).ready;
    var found = { state: 'loading', title: null };
    looks.set(key, found);
    if (looks.size > LOOKS) looks.delete(looks.keys().next().value);
    found.ready = MM.api.get(SIMILAR_URL + encodeURIComponent(item.like)).then(function (data) {
      found.state = 'ok';
      found.title = data.title || null;
    }, function () {
      looks.delete(key);
    }).then(function () {
      views.forEach(function (v) { if (v.item && v.item.like && v.item.like.toLowerCase() === key) v.draw(); });
    });
    return found.ready;
  }

  /* The title named, as the server takes it: by its TMDB id. With none known, its question
     names it in words alone. */
  function likeOf(item) {
    var t = named(item);
    if (!t) return null;
    if (typeof t.tmdb_id === 'number' && t.tmdb_id > 0) return { tmdb_id: t.tmdb_id, kind: t.kind === 'movie' ? 'movie' : 'tv', title: t.title };
    var ref = /^tmdb-(movie|tv)-([0-9]{1,10})$/.exec(t.ref || '');
    return ref ? { tmdb_id: Number(ref[2]), kind: ref[1], title: t.title } : null;
  }

  /* ── The titles shown ── */

  var WAITING = 10; // poster outlines that hold the answer's place: one row of them shows
  var STAGGER_MS = 35;
  var LONG_Q = 48; // a question longer than this is said again under the AI's heading for it
  var STAGES = { taste: 'Reading what you watch…', choosing: 'Choosing titles…', checking: 'Checking each one is real…' };

  function titles(item) { return item.shown ? item.shown.owned.concat(item.shown.others) : []; }

  function refOf(t) { return (t.in_library && t.library_ref) || t.ref || ''; }

  // Of the library, or downloaded and on its way to it.
  function isHeld(t) { return window.MMWatch.search.isHeld(t); }

  /* The AI's picks, those ready to watch first, never the title the question names. */
  function picks(item) {
    var like = named(item);
    var all = titles(item).filter(function (t) {
      return !(like && (t.ref === like.ref || (!!like.library_ref && t.library_ref === like.library_ref)));
    });
    return all.filter(isHeld).concat(all.filter(function (t) { return !isHeld(t); }));
  }

  /* "8 titles · 4 ready to watch, 4 to download". *can* is the page's can_download. */
  function count(item, can) {
    var all = picks(item);
    var n = all.length;
    var ready = all.filter(isHeld).length;
    var get = all.filter(function (t) { return !isHeld(t) && can[t.kind] !== false; }).length;
    var parts = [];
    if (ready) parts.push(ready === n && n > 1 ? 'all ready to watch' : ready + ' ready to watch');
    if (get) parts.push(get + ' to download');
    return n + (n === 1 ? ' title' : ' titles') + (parts.length ? ' · ' + parts.join(', ') : '');
  }

  /* Marks *node* as the AI's pick, once, with why in its preview and its page. */
  function markPick(node, t) {
    if (node.classList.contains('is-ai-pick')) return;
    node.classList.add('is-ai-pick');
    var link = node.querySelector('.watch-card__link');
    if (t.reason) link.setAttribute('data-ai-why', t.reason);
    var title = node.querySelector('.watch-card__title');
    var spark = el('span', 'watch-card__spark');
    spark.setAttribute('aria-hidden', 'true');
    spark.appendChild(MM.dom.icon('i-sparkles'));
    title.insertBefore(spark, title.firstChild);
    title.after(el('span', 'sr-only', 'AI pick'));
  }

  /* A poster card as every row has, marked Ready to watch or Download. */
  function card(t, can) {
    var search = window.MMWatch.search;
    var node = isHeld(t) ? search.heldCard(Object.assign({}, t, { ref: refOf(t) }), true) : search.tmdbCard(t);
    if (t.in_library && t.kind === 'show' && t.season_count) {
      core.caption(node.querySelector('.watch-card__sub'), 'TV · ' + t.season_count + (t.season_count === 1 ? ' season' : ' seasons'));
    }
    var poster = node.querySelector('.watch-card__poster');
    if (!isHeld(t) && can[t.kind] !== false && !poster.querySelector('.watch-badge--state')) {
      var badge = el('span', 'watch-badge watch-badge--state watch-badge--get');
      badge.appendChild(MM.dom.icon('i-arrow-down'));
      badge.appendChild(document.createTextNode('Download'));
      poster.appendChild(badge);
    }
    markPick(node, t);
    addWhy(node, t);
    return node;
  }

  /* The why goes under a card once, so a card drawn while checking keeps its place when the answer says why. */
  function addWhy(node, t) {
    if (!t.reason || node.querySelector('.watch-card__why')) return;
    var sub = node.querySelector('.watch-card__sub');
    if (sub) sub.after(whyLine(t.reason, t.reason_lead));
    else node.appendChild(whyLine(t.reason, t.reason_lead));
  }

  /* Why the AI picked it, under the card. The server names the "Because you liked …" it starts with,
     whole, since a title may hold a colon of its own. */
  function whyLine(text, lead) {
    var p = el('p', 'watch-card__why');
    if (lead && text.startsWith(lead + ':')) {
      p.appendChild(el('b', '', lead));
      p.appendChild(document.createTextNode(' · ' + text.slice(lead.length + 1).trim()));
    } else p.textContent = text;
    return p;
  }

  function outlineCard() {
    var node = el('div', 'watch-card watch-card--skel ask__skel');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(el('span', 'skel skel--poster'));
    node.appendChild(el('span', 'skel skel--line'));
    node.appendChild(el('span', 'skel skel--line'));
    return node;
  }

  /* The one grid: the picks, and while the AI looks, outlines where the rest will land, so nothing
     on the page moves when they come. A card drawn already stays, and is only given its why. */
  function gridRows(item, phase, can) {
    var shown = phase === 'thinking' || phase === 'answer' ? picks(item) : [];
    var rows = shown.map(function (t) {
      return {
        key: refOf(t), sig: JSON.stringify(t), keepFocus: true,
        make: function () { return card(t, can); },
        update: function (node) { markPick(node, t); addWhy(node, t); }
      };
    });
    if (phase === 'thinking' || phase === 'new') {
      for (var i = rows.length; i < WAITING; i++) rows.push({ key: 'skel-' + i, make: outlineCard });
    }
    if (!rows.length && phase === 'answer' && item.chip) {
      rows = [{ key: 'none', sig: item.chip, make: function () { return el('p', 'ask__none', 'Nothing here fits “' + item.chip + '”'); } }];
    }
    return rows;
  }

  /* What the person is told where the AI is off: its *code* is the server's. An admin is told what to set. */
  function offWords(code, admin) {
    var card = ['Open the OpenAI card', '/settings#intg-openai'];
    if (!admin) return { title: 'AI isn’t available right now', text: 'Search works as always.' };
    if (code === 'no_key') return { title: 'AI could answer this', text: 'Add an OpenAI key in Settings and it will, right here.', link: card };
    if (code === 'refused') return { title: 'OpenAI refused the key', text: 'Check it in the OpenAI card.', link: card };
    // The search itself offers to set TMDB up, under its results.
    if (code === 'no_tmdb') return { title: 'AI needs TMDB', text: 'It checks every title it names against TMDB.' };
    if (code === 'off') return { title: 'AI in search is switched off', text: 'A daily limit is set to 0 in the OpenAI card.', link: card };
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
        return { owned: s.owned, others: s.others.map(now), lead: s.lead };
      }
      var all = item.shown === item.base;
      item.base = set(item.base);
      item.shown = all ? item.base : set(item.shown);
    });
    views.forEach(function (v) { if (v.item) v.draw(); });
  });

  /* ── The place of a question ── */

  /* *host*, which stands on the page, shows a question. *opts*: ask(text) asks another, plain(text)
     searches for it as it is, stopped(item), acted() when a chip is pressed, drawn(item) after each
     drawing, around(change) makes a change of height, can is the page's can_download and admin
     whether the person may change the settings. */
  function view(host, opts) {
    // Beside it: what is read out must not wait for what is busy.
    var status = el('p', 'sr-only');
    status.setAttribute('role', 'status');
    host.parentNode.insertBefore(status, host);
    var inside = host.appendChild(el('div', 'ask__in'));
    var v = { host: host, item: null, draw: draw, row: row, show: show, hide: hide, offered: offered };
    var drawn = null; // what *inside* holds: { item, phase, shape, part, grid, head, ... }
    var can = opts.can || {};
    views.push(v);

    /* *inside* holds *nodes*, with *part*, the titles, among them. What stays is not taken off the
       page: the cards in it are not drawn again. */
    function fill(item, phase, nodes, part, head) {
      var had = inside.contains(document.activeElement);
      nodes = nodes.filter(Boolean);
      core.toArray(inside.children).forEach(function (node) { if (nodes.indexOf(node) < 0) node.remove(); });
      nodes.forEach(function (node, i) {
        var at = inside.children[i] || null;
        if (at !== node) inside.insertBefore(node, at);
      });
      if (part) part.node.removeAttribute('aria-hidden');
      host.hidden = !nodes.length;
      drawn = nodes.length ? { item: item, phase: phase, shape: shapeOf(item), part: part || null, grid: part ? part.grid : null, head: head || null } : null;
      host.classList.toggle('is-thinking', phase === 'thinking');
      host.classList.toggle('is-waiting', phase === 'new' && !!part);
      host.classList.remove('is-refining');
      host.setAttribute('aria-busy', phase === 'thinking' ? 'true' : 'false');
      if (had && nodes.length && !inside.contains(document.activeElement)) nodes[0].focus({ preventScroll: true });
    }

    function say(text) { if (status.textContent !== text) status.textContent = text; }

    function hide() {
      v.item = null;
      say('');
      if (drawn) opts.around(function () { fill(null, '', []); });
      if (opts.drawn) opts.drawn(null);
    }

    function show(item) {
      if (views.indexOf(v) < 0) views.push(v);
      v.item = item;
      draw();
    }

    /* The question of *text*; *exact*: a title has all its words. */
    function row(text, exact) {
      var item = question('ask', text);
      item.exact = !!exact;
      show(item);
    }

    /* Whether Return would ask what is shown. */
    function offered() { return !!v.item && v.item.state === 'new' && (v.item.kind === 'like' || fits(v.item.value, v.item.exact)); }

    /* What a question not asked yet shows: the place its answer will take, a line where the AI
       cannot answer, or nothing. */
    function mode(item) {
      if (item.state !== 'new') return '';
      if (!available()) return item.kind === 'ask' && opts.admin && failing() && shaped(item.value, item.exact) ? 'off' : 'none';
      if (item.kind === 'ask' && !fits(item.value, item.exact)) return 'none';
      return state.left === 0 ? 'spent' : 'waiting';
    }

    /* What is drawn the same way: a change of it is drawn anew. */
    function shapeOf(item) {
      var like = named(item);
      return [item.state, like ? like.ref : '', mode(item)].join('|');
    }

    function draw() {
      var item = v.item;
      if (!item) return;
      var phase = item.state;
      var here = drawn && drawn.item === item && drawn.shape === shapeOf(item) ? drawn : null;
      if (here && phase === 'new') { drawHead(item); }
      else if (here && phase === 'answer') update(item);
      else if (here && phase === 'thinking') wait(item);
      else {
        opts.around(function () {
          if (phase === 'new') waiting(item);
          else if (phase === 'thinking') thinking(item);
          else if (phase === 'answer') answer(item);
          else if (phase === 'nothing') nothing(item);
          else refused(item);
        });
      }
      if (opts.drawn) opts.drawn(item);
    }

    /* ── Its heading, which stays from the wait to the answer ── */

    function headFor(item) {
      if (drawn && drawn.item === item && drawn.head) return drawn.head;
      var node = el('div', 'ask__row');
      node.tabIndex = -1;
      node.appendChild(orb());
      var words = node.appendChild(el('div', 'ask__words'));
      return {
        node: node, words: words, end: null,
        kicker: words.appendChild(el('span', 'ask__kicker')),
        title: words.appendChild(el('h2', 'ask__title')),
        asked: words.appendChild(el('p', 'ask__asked')),
        line: words.appendChild(el('p', 'ask__line'))
      };
    }

    function titleOf(item, phase) {
      if (item.kind === 'like') return 'More like ' + item.name;
      if (phase === 'answer' && item.heading) return item.heading;
      var like = named(item);
      if (like && phase !== 'new') return (like.kind === 'movie' ? 'Films like ' : 'Shows like ') + like.title.split(':')[0];
      return phase === 'new' ? 'Ask AI: “' + item.value + '”' : '“' + item.value + '”';
    }

    function waitingLine(item) {
      var touch = core.coarsePointer();
      if (item.kind === 'like') return 'Titles like it, chosen for you';
      if (autoOk() && !item.halted) return touch ? 'Answers when you stop typing, or tap Search' : 'Answers when you stop typing, or press ↵';
      return touch ? 'Tap Search to ask AI' : 'Press ↵ to ask AI';
    }

    function lineOf(item, phase) {
      if (phase === 'new') return waitingLine(item);
      if (phase === 'thinking') return waitText(item);
      if (item.narrowing) return 'Narrowing…';
      var left = !opts.admin && state && state.left !== null ? ' · ' + state.left + (state.left === 1 ? ' AI answer' : ' AI answers') + ' left today' : '';
      return count(item, can) + left;
    }

    var KICKERS = { new: 'AI', thinking: 'AI is looking', answer: 'AI picks' };

    /* The heading of *item* as its phase has it; *end*: what stands at its end. */
    function drawHead(item, end) {
      var phase = item.state;
      var h = headFor(item);
      h.kicker.textContent = KICKERS[phase] || 'AI';
      h.title.textContent = titleOf(item, phase);
      var asked = phase === 'answer' && item.kind === 'ask' && item.heading && item.value.length > LONG_Q ? '“' + item.value + '”' : '';
      h.asked.textContent = asked;
      h.asked.hidden = !asked;
      setLine(h, lineOf(item, phase));
      var like = named(item);
      if (like && !h.words.querySelector('.ask__because')) h.words.appendChild(because(like));
      // What stands at its end: Stop, or nothing.
      if (end !== undefined && end !== h.end) {
        if (h.end) h.end.remove();
        h.end = end;
        if (end) h.node.appendChild(end);
      }
      host.classList.toggle('is-refining', phase === 'answer' && item.narrowing);
      return h;
    }

    /* The line under the heading; a new step takes the place of the one before. */
    function setLine(h, text) {
      if (h.line.textContent === text) return;
      if (!h.line.textContent) { h.line.textContent = text; return; }
      var next = el('p', 'ask__line ask__step', text);
      h.line.replaceWith(next);
      h.line = next;
    }

    /* The grid of titles. Of the same question it is the one already drawn, cards and all. */
    function body(item) {
      if (drawn && drawn.item === item && drawn.part) return drawn.part;
      var node = el('div', 'ask__body');
      var grid = node.appendChild(el('div', 'watch-grid ask__grid'));
      return { node: node, grid: grid };
    }

    function drawGrid(item) {
      if (!drawn || !drawn.grid) return;
      window.MMWatch.search.glide(drawn.grid, gridRows(item, drawn.phase, can), null, STAGGER_MS);
    }

    /* One quiet line where the answer would be: why there is none, and the way on. */
    function quiet(item, phase, words, icon, after) {
      var node = el('div', 'ask__off');
      node.appendChild(orb(icon));
      var said = node.appendChild(el('div', 'ask__off-words'));
      said.appendChild(el('b', '', words.title));
      said.appendChild(el('span', '', words.text));
      if (words.link) {
        var a = node.appendChild(el('a', 'ask__link', words.link[0]));
        a.href = words.link[1];
      }
      (after || []).forEach(function (b) { if (b) node.appendChild(b); });
      fill(item, phase, [node]);
      say(words.title + '. ' + words.text);
    }

    function waiting(item) {
      var m = mode(item);
      if (m === 'none') { fill(item, 'new', []); say(''); return; }
      if (m === 'off') { quiet(item, 'new', offWords(state && state.why, opts.admin)); return; }
      if (m === 'spent') { quiet(item, 'new', reason({ error: 'limit', data: {} }, item.kind)); return; }
      var ask = null;
      if (item.kind === 'like') {
        ask = button('chip chip--again', 'Ask AI');
        ask.insertBefore(MM.dom.icon('i-sparkles'), ask.firstChild);
        ask.addEventListener('click', function () { start(v.item); });
      }
      var h = drawHead(item, ask);
      var part = body(item);
      fill(item, 'new', [h.node, part.node], part, h);
      part.node.setAttribute('aria-hidden', 'true');
      drawGrid(item);
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
      drawHead(item);
      say(waitText(item));
      if (drawn.part && titles(item).length) drawn.part.node.removeAttribute('aria-hidden');
      drawGrid(item);
    }

    function thinking(item) {
      var b = button('txt ask__stop', 'Stop');
      b.addEventListener('click', function () {
        stop(item);
        opts.stopped(item);
      });
      var h = drawHead(item, b);
      var part = body(item);
      fill(item, 'thinking', [h.node, part.node], part, h);
      if (!titles(item).length) part.node.setAttribute('aria-hidden', 'true');
      drawGrid(item);
      say(waitText(item));
    }

    function foot() {
      var node = el('p', 'ask__foot');
      node.appendChild(MM.dom.icon('i-sparkles'));
      node.appendChild(document.createTextNode('Chosen by AI from your question' + (state && state.records ? ' and what you watch' : '') + ', each checked to be real.'));
      return node;
    }

    function answer(item) {
      var h = drawHead(item, null);
      var part = body(item);
      var refine = el('div', 'refine');
      refine.setAttribute('role', 'group');
      refine.setAttribute('aria-label', 'Narrow it');
      var note = el('p', 'ask__note');
      note.hidden = true;
      fill(item, 'answer', [h.node, refine, note, part.node, foot()], part, h);
      drawn.refine = refine;
      drawn.note = note;
      drawChips(item);
      drawGrid(item);
      sayCount(item);
    }

    function sayCount(item) {
      drawn.note.hidden = !item.note;
      drawn.note.textContent = item.note;
      say(item.note || lineOf(item, 'answer'));
    }

    function update(item) {
      drawHead(item);
      drawChips(item);
      opts.around(function () {
        drawGrid(item);
        sayCount(item);
      }, [drawn.part.node.nextElementSibling]);
    }

    function chip(text) { return button('chip', text); }

    /* The ways to narrow it, and to ask for others, over the titles. */
    function drawChips(item) {
      var refine = drawn.refine;
      if (drawn.chips !== item.chips) {
        drawn.chips = item.chips;
        refine.textContent = '';
        refine.appendChild(el('small', '', 'Narrow it'));
        item.chips.forEach(function (text) {
          var b = chip(text);
          b.setAttribute('data-chip', text);
          b.addEventListener('click', function () {
            if (opts.acted) opts.acted(item);
            narrow(item, text);
          });
          refine.appendChild(b);
        });
        var other = refine.appendChild(chip('Different picks'));
        other.classList.add('chip--again');
        other.insertBefore(MM.dom.icon('i-rotate-right'), other.firstChild);
        other.addEventListener('click', function () {
          if (opts.acted) opts.acted(item);
          start(item, '', null, false, true);
        });
      }
      refine.firstChild.hidden = !item.chips.length;
      core.toArray(refine.querySelectorAll('.chip[data-chip]')).forEach(function (b) {
        var on = b.getAttribute('data-chip') === item.pressed;
        if (b.getAttribute('aria-pressed') === String(on)) return;
        b.setAttribute('aria-pressed', String(on));
        var tick = b.querySelector('.ico');
        if (tick) tick.remove();
        if (on) b.insertBefore(MM.dom.icon('i-check'), b.firstChild);
      });
    }

    /* The ways on from a question that came to nothing: its own, and a plain search for the title it named. */
    function ways(item, own) {
      var row = el('div', 'refine is-wrap ask__ways');
      (own || []).forEach(function (b) { row.appendChild(b); });
      if (item.like && opts.plain) {
        var b = row.appendChild(chip('Search for “' + item.like + '”'));
        b.insertBefore(MM.dom.icon('i-magnifying-glass'), b.firstChild);
        b.addEventListener('click', function () { opts.plain(item.like); });
      }
      return row.children.length ? row : null;
    }

    function nothing(item) {
      var told = item.kind === 'like' ? 'Nothing real is like “' + item.name + '”.' : 'Nothing real matches “' + item.value + '”.';
      var less = item.instead.map(function (text) {
        var b = chip(text);
        b.addEventListener('click', function () { opts.ask(text); });
        return b;
      });
      quiet(item, 'nothing', { title: 'No title fits all of that', text: told + (less.length ? ' Dropping one part may be enough:' : ' Nothing was made up to fill the gap.') });
      var more = ways(item, less);
      if (more) inside.appendChild(more);
    }

    function refused(item) {
      var why = item.refused;
      var off = why.off ? offWords(why.why, opts.admin) : why;
      var b = null;
      if (why.again) {
        b = chip(why.again);
        b.classList.add('chip--again');
        b.insertBefore(MM.dom.icon('i-rotate-right'), b.firstChild);
        b.addEventListener('click', function () { again(item); });
      }
      quiet(item, 'refused', off, why.again ? 'i-circle-exclamation' : null, [b]);
      var more = ways(item);
      if (more) inside.appendChild(more);
    }

    host.hidden = true;
    return v;
  }

  window.MMWatch.ask = {
    load: load,
    state: function () { return state; },
    available: available,
    intent: intent,
    fits: fits,
    canAsk: canAsk,
    autoOk: autoOk,
    shaped: shaped,
    failing: failing,
    record: record,
    forget: forget,
    kept: kept,
    keep: keep,
    keptLike: keptLike,
    keepLike: keepLike,
    question: question,
    start: start,
    stop: stop,
    asking: asking,
    view: view,
    sparkle: sparkle,
    dur: dur,
    similarOf: similarOf,
    lookSimilar: lookSimilar
  };
})();
