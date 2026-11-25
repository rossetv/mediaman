/* The pages a shared link opens: the trailer, which asks YouTube nothing until it is pressed,
   and the minute after too many requests. */
(function () {
  'use strict';

  var ORIGIN = 'https://www.youtube-nocookie.com';
  var KEY_RE = /^[A-Za-z0-9_-]{11}$/;
  var hero = document.querySelector('.share-hero');
  var body = hero && hero.querySelector('.watch-banner__body');
  var player = null;

  function buildPlayer() {
    player = document.createElement('div');
    player.className = 'share-player';
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'ttl__close';
    close.setAttribute('aria-label', 'Close trailer');
    close.appendChild(MM.dom.icon('i-xmark'));
    close.addEventListener('click', stop);
    var frame = document.createElement('div');
    frame.className = 'share-player__frame';
    player.append(close, frame);
    hero.appendChild(player);
  }

  function play(key) {
    if (!player) buildPlayer();
    var video = document.createElement('iframe');
    video.title = 'Trailer';
    video.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox');
    video.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
    video.setAttribute('referrerpolicy', 'strict-origin');
    video.setAttribute('allowfullscreen', '');
    video.src = ORIGIN + '/embed/' + key + '?rel=0&autoplay=1';
    player.querySelector('.share-player__frame').replaceChildren(video);
    void player.offsetWidth; // so that a player just made fades in too
    hero.classList.add('is-trailer');
    cover();
    player.querySelector('.ttl__close').focus({ preventScroll: true });
  }

  /* What the player lies over goes with it; on a phone held upright the buttons can be clear of
     it, and then they stay of use. */
  function cover() {
    var act = hero.querySelector('.share-act');
    var over = player.getBoundingClientRect().bottom > act.getBoundingClientRect().top;
    var loses = over && body.contains(document.activeElement);
    body.inert = over;
    if (loses) player.querySelector('.ttl__close').focus({ preventScroll: true });
  }

  function stop() {
    if (!hero.classList.contains('is-trailer')) return;
    hero.classList.remove('is-trailer');
    body.inert = false;
    player.querySelector('.share-player__frame').replaceChildren();
    /* The button may have made way for the download's progress. */
    (hero.querySelector('[data-trailer]') || hero.querySelector('.watch-banner__title')).focus({ preventScroll: true });
  }

  if (hero) {
    hero.addEventListener('click', function (e) {
      var link = e.target.closest('[data-trailer]');
      if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var key = link.getAttribute('data-trailer');
      if (!KEY_RE.test(key)) return;
      e.preventDefault();
      play(key);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') stop();
    });
    window.addEventListener('resize', function () {
      if (hero.classList.contains('is-trailer')) cover();
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
