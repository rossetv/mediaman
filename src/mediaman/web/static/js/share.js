/* The pages a shared link opens: the trailer, which asks YouTube nothing until it is pressed,
   and the minute after too many requests. */
(function () {
  'use strict';

  var ORIGIN = 'https://www.youtube-nocookie.com';
  /* The rest of a double press does nothing. */
  var TWICE_MS = 400;
  var KEY_RE = /^[A-Za-z0-9_-]{11}$/;
  var hero = document.querySelector('.share-hero');
  var body = hero && hero.querySelector('.watch-banner__body');
  var bar = document.querySelector('.share-bar');
  var player = null;
  var pressedAt = 0;
  var ours = false; // the entry on top of the history is the trailer's

  function still() { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; }
  function on() { return hero.classList.contains('is-trailer'); }
  function frame() { return player.querySelector('iframe'); }

  function buildPlayer() {
    player = document.createElement('div');
    player.className = 'share-player';
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'ttl__close';
    close.setAttribute('aria-label', 'Close trailer');
    close.appendChild(MM.dom.icon('i-xmark'));
    close.addEventListener('click', stop);
    var box = document.createElement('div');
    box.className = 'share-player__frame';
    player.append(close, box);
    player.addEventListener('transitionend', function (e) {
      if (e.target === player && !on()) empty();
    });
    hero.appendChild(player);
  }

  function empty() { player.querySelector('.share-player__frame').replaceChildren(); }

  function tell(video, message) {
    if (video.contentWindow) video.contentWindow.postMessage(JSON.stringify(message), ORIGIN);
  }

  function play(key) {
    if (!player) buildPlayer();
    var video = document.createElement('iframe');
    video.title = 'Trailer';
    video.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox');
    video.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
    video.setAttribute('referrerpolicy', 'strict-origin');
    video.setAttribute('allowfullscreen', '');
    video.src = ORIGIN + '/embed/' + key + '?rel=0&autoplay=1&playsinline=1&enablejsapi=1&origin=' + encodeURIComponent(window.location.origin);
    // YouTube's player reports its state only to a page that has asked.
    video.addEventListener('load', function () { tell(video, { event: 'listening' }); });
    player.querySelector('.share-player__frame').replaceChildren(video);
    void player.offsetWidth; // so that a player just made fades in too
    hero.classList.add('is-trailer');
    cover();
    player.querySelector('.ttl__close').focus({ preventScroll: true });
    hero.scrollIntoView({ block: 'start', behavior: still() ? 'auto' : 'smooth' });
  }

  /* Back closes the trailer, and leaves the page where it is. */
  function open(key) {
    window.history.scrollRestoration = 'manual';
    var here = window.history.state;
    if (!(here && here.trailer)) window.history.pushState({ trailer: key }, '');
    ours = true;
    play(key);
  }

  /* What the player lies over goes with it; on a phone held upright the buttons can be clear of
     it, and then they stay of use. */
  function cover() {
    var act = hero.querySelector('.share-act');
    player.style.setProperty('--clear', act.getBoundingClientRect().top - hero.getBoundingClientRect().top - 16 + 'px');
    var over = player.getBoundingClientRect().bottom > act.getBoundingClientRect().top;
    var loses = over && body.contains(document.activeElement);
    body.inert = over;
    if (loses) player.querySelector('.ttl__close').focus({ preventScroll: true });
  }

  function stop() {
    if (!on()) return;
    var back = ours;
    shut();
    if (back) window.history.back();
  }

  function shut() {
    var active = document.activeElement;
    /* Safari gives a clicked button no focus: it is on the page around it. */
    var lost = !active || active.contains(player) || player.contains(active);
    ours = false;
    pressedAt = Date.now();
    hero.classList.remove('is-trailer');
    bar.style.pointerEvents = 'none'; // its button comes back under the pointer
    body.inert = false;
    tell(frame(), { event: 'command', func: 'pauseVideo', args: [] });
    if (still()) empty();
    settle();
    /* The button may have made way for the download's progress. */
    if (lost) (hero.querySelector('[data-trailer]') || hero.querySelector('.watch-banner__title')).focus({ preventScroll: true });
  }

  function settle() {
    setTimeout(function () {
      bar.style.pointerEvents = '';
      if (on()) return;
      if (player) empty();
      window.history.scrollRestoration = 'auto';
    }, TWICE_MS);
  }

  if (hero) {
    /* Left from a reload: that trailer is not on, and its entry would take a Back. Not before the
       page has loaded: going back ends that in Safari. */
    if (window.history.state && window.history.state.trailer) {
      window.addEventListener('load', function () {
        if (!on() && window.history.state && window.history.state.trailer) window.history.back();
      });
    }

    hero.addEventListener('click', function (e) {
      var link = e.target.closest('[data-trailer]');
      if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var key = link.getAttribute('data-trailer');
      if (!KEY_RE.test(key)) return;
      e.preventDefault();
      if (Date.now() - pressedAt < TWICE_MS) return;
      pressedAt = Date.now();
      if (on()) stop();
      else open(key);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') stop();
      /* Held on Close, Enter goes on to the Trailer button that takes the focus. */
      else if (e.repeat && e.key === 'Enter' && e.target.closest('[data-trailer], .ttl__close')) e.preventDefault();
    });
    window.addEventListener('popstate', function (e) {
      var key = e.state && e.state.trailer;
      ours = !!key;
      if (key && !on()) play(key);
      else if (!key && on()) shut();
      else if (!key) settle();
    });
    window.addEventListener('message', function (e) {
      var video = player && frame();
      if (!video || e.source !== video.contentWindow || e.origin !== ORIGIN) return;
      var told;
      try { told = JSON.parse(e.data); } catch (err) { return; }
      var state = told && (told.event === 'onStateChange' ? told.info
        : told.event === 'infoDelivery' && told.info ? told.info.playerState : null);
      if (state === 0) stop();
    });
    window.addEventListener('resize', function () {
      if (on()) cover();
    });
  }

  var again = document.querySelector('[data-count]');
  if (again) {
    var words = again.textContent;
    var until = Date.now() + Number(again.getAttribute('data-count')) * 1000;
    var tick = function () {
      var left = Math.ceil((until - Date.now()) / 1000);
      again.textContent = left > 0 ? words + ' in ' + left + ' s' : words;
      if (left > 0) again.setAttribute('aria-disabled', 'true');
      else {
        again.removeAttribute('aria-disabled');
        clearInterval(timer);
      }
    };
    var timer = setInterval(tick, 1000);
    tick();
    again.addEventListener('click', function (e) {
      if (again.getAttribute('aria-disabled')) e.preventDefault();
    });
  }
})();
