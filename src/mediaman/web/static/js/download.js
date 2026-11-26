/**
 * download.js — behaviour for the per-token download confirm page.
 *
 * Reads server-supplied values from the #page-bootstrap JSON island (token, poll_token,
 * service, tmdb_id, download_state, notify_by_email) and drives the download trigger and its
 * progress. What takes the button's place is cloned from the page's <template data-part>s. Lives
 * in its own file, not an inline <script>, so the CSP can forbid inline script.
 */
(function () {
  'use strict';

  var bootstrapEl = document.getElementById('page-bootstrap');
  if (!bootstrapEl) return;
  var raw = bootstrapEl.textContent;
  if (raw.toLowerCase().indexOf('</script') !== -1) {
    throw new Error('page-bootstrap: </script breakout detected — server-side escaping regression');
  }
  var boot = JSON.parse(raw);

  var _token = boot.token || '';
  var _pollToken = boot.poll_token || '';
  var _service = boot.service || '';
  var _tmdbId = boot.tmdb_id || 0;
  var _downloadState = boot.download_state || '';
  var _notifyByEmail = boot.notify_by_email === true;
  var _pollInterval = null;
  var _pollStopped = false;
  var _pollsSent = 0;
  var _pollDrawn = 0;

  /* How long the last step is seen before the way to watch takes the steps' place. */
  var DONE_AFTER_MS = 900;
  var STEP_OF = { searching: 1, queued: 1, downloading: 2, almost_ready: 2, needs_attention: 2, ready: 4 };

  var page = document.querySelector('.share-page');
  var hero = document.querySelector('.share-hero');
  var act = document.getElementById('download-action');
  var said = document.querySelector('[data-said]');

  function part(name) {
    return document.querySelector('template[data-part="' + name + '"]').content.firstElementChild.cloneNode(true);
  }

  /* What can be done and how it goes stand in one place, whose height eases. */
  function high(px) {
    return px ? { height: px + 'px' }
      : { height: 0, paddingTop: 0, paddingBottom: 0, marginTop: 0, marginBottom: 0, borderTopWidth: 0, borderBottomWidth: 0 };
  }

  function ease(el, from, to, then) {
    var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (still || from === to || !el.animate) { if (then) then(); return; }
    el.style.overflow = 'clip';
    el.style.overflowClipMargin = '4px';
    var run = el.animate([high(from), high(to)],
      { duration: 250, easing: 'cubic-bezier(.2,.8,.2,1)' });
    run.onfinish = run.oncancel = function () {
      el.style.overflow = el.style.overflowClipMargin = '';
      if (then) then();
    };
  }

  function show(node, unasked) {
    /* What comes of itself moves only the focus it takes away. */
    var keepsFocus = unasked ? act.contains(document.activeElement) : MM.dom.focusStillIn(act);
    var from = act.offsetHeight;
    node.classList.add('is-swapping-in');
    act.replaceChildren(node);
    var to = act.offsetHeight;
    ease(act, from, to);
    letGo();
    var first = node.querySelector('[tabindex="-1"]') || node.querySelector('.btn');
    if (keepsFocus && first) first.focus({ preventScroll: true });
  }

  function say(words) { MM.dom.setText(said, words); }

  function colour(share) {
    page.style.setProperty('--sat', share);
    page.style.setProperty('--lum', 0.78 + 0.22 * share);
  }

  function triggerDownload(btn) {
    /* Not the disabled attribute: that would drop the keyboard's focus. */
    if (btn.getAttribute('aria-disabled') === 'true') return;
    btn.style.minWidth = btn.offsetWidth + 'px';
    btn.setAttribute('aria-disabled', 'true');
    var spin = document.createElement('span');
    spin.className = 'btn-spinner';
    spin.setAttribute('aria-hidden', 'true');
    btn.replaceChildren(spin, document.createTextNode('Adding…'));
    /* Safari gives a clicked button no focus, and what comes next goes by where it is. */
    btn.focus();

    MM.api.post('/download/' + _token).then(function(data) {
      _service = data.service;
      _tmdbId = data.tmdb_id;
      showOnItsWay(data.poll_token, false, _notifyByEmail);
    }, function(err) {
      showRefusal(err);
    });
  }

  function showOnItsWay(pollToken, already, notify) {
    if (pollToken) {
      _pollToken = pollToken;
      /* A fresh token means polling can authenticate again. */
      _pollStopped = false;
    }
    var node = part('on-its-way');
    node.querySelector('[data-already]').hidden = !already;
    /* Not there on the page of a link with no email. */
    var mail = node.querySelector('[data-mail]');
    if (mail) mail.hidden = !notify;
    show(node);
    draw({ state: 'searching' });

    /* A 401 already stopped polling and told the visitor to reload. */
    if (!_pollStopped) {
      pollStatus();
      _startPolling();
    }
  }

  /* Polling uses setInterval internally; _startPolling / _stopPolling manage the
     timer and the visibilitychange pause so the tab doesn't hammer the
     server while hidden. */
  function _startPolling() {
    if (!_pollToken) return;
    if (_pollInterval !== null) { clearInterval(_pollInterval); }
    _pollInterval = setInterval(pollStatus, 4000);
  }

  function _stopPolling() {
    if (_pollInterval !== null) { clearInterval(_pollInterval); _pollInterval = null; }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      _stopPolling();
    } else if (_pollInterval === null && _downloadState !== 'ready' && !_pollStopped) {
      /* Resume: run one tick immediately then restart the interval. */
      pollStatus();
      _startPolling();
    }
  });

  function pollStatus() {
    /* Without a poll token the request can only be refused. */
    if (!_service || !_tmdbId || !_pollToken) return;
    /* Use the short-lived poll_token, not the long-lived download token. */
    var url = '/api/download/status?service=' + _service + '&tmdb_id=' + _tmdbId
      + '&poll_token=' + encodeURIComponent(_pollToken);
    var poll = ++_pollsSent;
    /* One that a newer answer has overtaken, or that comes once the title is ready. */
    function late() { return poll < _pollDrawn || _downloadState === 'ready'; }
    MM.api.get(url)
      .then(function(data) {
        if (late()) return;
        /* The server hands out a replacement before the current poll token expires. */
        if (data.poll_token) _pollToken = data.poll_token;
        /* The service did not answer: what is drawn is the last it said. */
        if (data.state === 'unknown') return;
        _pollDrawn = poll;
        draw(data);

        if (data.state === 'ready') {
          _downloadState = 'ready';
          _stopPolling();
          setTimeout(function() { show(part('ready'), true); }, DONE_AFTER_MS);
        }
      })
      .catch(function(err) {
        /* Other failures are transient; the next tick retries. */
        if (err instanceof MM.api.APIError && err.status === 401 && !late()) {
          _pollStopped = true;
          _stopPolling();
          showReloadNotice();
        }
      });
  }

  function draw(data) {
    var steps = act.querySelector('.share-steps');
    if (!steps) return;
    var at = STEP_OF[data.state] || 1;
    Array.prototype.forEach.call(steps.children, function(li, i) {
      li.classList.toggle('is-done', i < at);
      li.classList.toggle('is-now', i === at);
      if (i === at) li.setAttribute('aria-current', 'step');
      else li.removeAttribute('aria-current');
    });
    steps.classList.toggle('is-all', data.state === 'ready');
    MM.dom.setText(steps.children[1].querySelector('span'), data.state === 'queued' ? 'Queued' : 'Searching');
    var third = { almost_ready: 'Almost ready', needs_attention: 'Needs attention' }[data.state] || 'Downloading';
    MM.dom.setText(steps.children[2].querySelector('span'), third);

    var done = data.progress || 0;
    var running = data.state === 'downloading';
    act.querySelector('[data-fold]').classList.toggle('is-shut', !running);
    /* One that shuts keeps its last figures while it does. */
    if (running) {
      act.querySelector('progress').value = done;
      MM.dom.setText(act.querySelector('[data-done]'), done + '%');
      MM.dom.setText(act.querySelector('[data-eta]'), data.eta || '');
    }
    drawEpisodes(data);

    colour(running ? 0.25 + 0.0065 * done : data.state === 'ready' ? 1 : data.state === 'almost_ready' ? 0.92 : 0.25);
    say({ 1: data.state === 'queued' ? 'Queued' : 'Searching', 2: third, 4: 'Ready to watch' }[at]);
  }

  /* The words stand at the hero's foot: while the list is open they keep their place, and the
     hero grows under them. */
  function hold() {
    var from = hero.getBoundingClientRect().top + hero.clientTop;
    hero.style.paddingTop = act.closest('.watch-banner__body').getBoundingClientRect().top - from + 'px';
    hero.style.alignItems = 'flex-start';
  }

  function letGo() { hero.style.paddingTop = hero.style.alignItems = ''; }

  function toggleEpisodes(box) {
    var list = box.querySelector('[data-v="ep-list"]');
    if (box.hasAttribute('open')) {
      if (box.classList.contains('is-shutting')) return;
      box.classList.add('is-shutting');
      ease(list, list.offsetHeight, 0, function () {
        box.classList.remove('is-shutting');
        box.removeAttribute('open');
        letGo();
      });
      return;
    }
    hold();
    box.setAttribute('open', '');
    ease(list, 0, list.offsetHeight);
  }

  /* Open, the list stays as the person left it while episodes come and go. */
  function drawEpisodes(data) {
    var box = act.querySelector('.share-eps');
    var there = !!(data.episodes && data.episodes.length);
    if (there) MM.downloads.buildDom.syncEpisodes(box, data);
    else if (!box.hidden && box.contains(document.activeElement)) act.querySelector('.share-steps').focus({ preventScroll: true });
    box.hidden = !there;
    if (!there) letGo();
  }

  /* Polling can no longer authenticate: keep the last progress on screen and
     tell the visitor to reload, rather than leaving a bar that silently stops. */
  function showReloadNotice() {
    var notice = act.querySelector('[data-stopped]');
    if (!notice) return;
    notice.hidden = false;
    act.querySelector('[data-say]').hidden = true;
    say('Updates have stopped. Reload the page to see where it is now.');
  }

  function fail(name, what, why) {
    var node = part(name);
    if (what) node.querySelector('[data-what]').textContent = what;
    node.querySelector('[data-why]').textContent = why;
    show(node);
  }

  /* The request did not go through: say why where the button is. */
  function showRefusal(err) {
    var told = err.data || {};
    var status = err.status || 0;
    if (status === 409 && told.poll_token) {
      if (told.download_state === 'queued') showOnItsWay(told.poll_token, true, told.notify_by_email === true);
      else show(part(told.download_state === 'in_library' ? 'in-library' : 'tracked'));
      return;
    }
    if (status === 409) {
      fail('refused', 'This link has been used already.', 'Reload the page to see where the download is.');
      return;
    }
    /* The server's own words are an admin's: they name the services behind it. */
    if (status && status !== 429 && status < 500) {
      fail('refused', '', status === 410 ? 'This link does not work any more.' : 'Ask whoever sent you this link.');
      return;
    }
    fail('try-again', '', status ? 'Try again in a moment.' : MM.api.messageOf(err, 'Try again in a moment.'));
  }

  act.addEventListener('click', function(e) {
    var btn = e.target.closest('[data-dl]');
    if (btn) triggerDownload(btn);
    var summary = e.target.closest('.share-eps summary');
    if (summary) {
      e.preventDefault();
      toggleEpisodes(summary.parentNode);
    }
  });
  /* A phone's address bar comes and goes without a turn of the screen. */
  var width = window.innerWidth;
  window.addEventListener('resize', function() {
    if (window.innerWidth !== width) letGo();
    width = window.innerWidth;
  });

  if (_downloadState === 'queued' && _tmdbId) {
    pollStatus();
    _startPolling();
  }
}());
