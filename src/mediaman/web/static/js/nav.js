/* Profile menu. On Watch the profile button opens the pane over it (watch/you.js) instead.
 * Elsewhere, a press on the profile button, or an arrow
 * key on it, opens the menu; arrows, Home and End move in it; Escape, Tab, a press
 * outside, focus moving away and the sheet's Close button close it. Below 700 px it
 * is a bottom sheet: the rest of the page is inert and does not scroll under it. The
 * fade is CSS; the open state changes at once, and a closed menu is inert while it fades.
 * The button also says how what is downloading stands, and its number how many titles are ready
 * that this person has not seen yet: in Downloading, or by opening the title. What was seen is
 * kept in this browser, by the download's key, while the title is still ready. */
(function () {
  'use strict';

  // iOS Safari only applies :active when a touchstart listener exists on the
  // document or body; this empty one switches the pressed states on.
  document.addEventListener('touchstart', function () {}, { passive: true });

  // Pressed again on the address it already led to, the skip link moves the focus
  // but leaves the page where it was: scroll it here. Any other press is the browser's own.
  document.addEventListener('click', function (e) {
    var link = e.target && e.target.closest ? e.target.closest('a.skip-link') : null;
    if (!link || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var href = link.getAttribute('href');
    var target = href && href.charAt(0) === '#' ? document.getElementById(href.slice(1)) : null;
    if (!target || window.location.hash !== href) return;
    e.preventDefault();
    target.focus({ preventScroll: true });
    target.scrollIntoView();
  });

  // The page says when the focus is in a bar that stays in sight, whose scroll padding would
  // scroll the page for it (_watch_search.css, _history.css).
  var BARS = '.nav-glass, .hx-tools, .watch-all__head, .dlg__head';
  function inBar(node) {
    document.documentElement.classList.toggle('is-bar-focus', !!(node && node.closest && node.closest(BARS)));
  }
  document.addEventListener('focusin', function (e) { inBar(e.target); });
  document.addEventListener('focusout', function (e) { inBar(e.relatedTarget); });

  // Log out. The POST goes at once, the request the form itself makes, and the page leaves around
  // it (is-leaving: _nav.css, _watch.css, _you.css, _ui_feedback.css), ending on the sign-in page's
  // own first frame; once the answer is in and the leaving played, the sign-in page opens.
  var LEAVE_MS = 980;
  var LEAVE_CALM_MS = 150;     // under reduced motion the page only fades
  var LEAVE_LATEST_MS = 2000;  // a page that never heard back leaves anyway, not sitting dark
  var SIGNED_OUT = '/login?out=1';

  // *panel* folds back into the avatar: the way there, for the fold's keyframes. False where the
  // avatar is out of sight, and the panel only fades.
  function foldInto(panel) {
    var ava = document.querySelector('.nav-glass .profile-ava');
    if (!panel || !ava) return false;
    var a = ava.getBoundingClientRect();
    var p = panel.getBoundingClientRect();
    var seen = a.width > 0 && a.bottom > 0 && a.right > 0 && a.top < window.innerHeight && a.left < window.innerWidth;
    if (!seen || !p.width || !p.height) return false;
    var s = Math.max(a.width / p.width, 0.06);
    panel.style.setProperty('--fs', String(s));
    panel.style.setProperty('--fx', (a.left + a.width / 2 - p.left - p.width * s / 2) + 'px');
    panel.style.setProperty('--fy', (a.top + a.height / 2 - p.top - p.height * s / 2) + 'px');
    return true;
  }

  function logOut(form, panel) {
    var html = document.documentElement;
    if (html.classList.contains('is-leaving')) return;
    // keepalive: the POST still lands if the page leaves before its answer.
    var answer = window.fetch(form.action, { method: 'POST', credentials: 'same-origin', redirect: 'manual', keepalive: true });
    var played = false;
    var out = false;
    var gone = false;
    function leave(how) {
      if (gone) return;
      gone = true;
      how();
    }
    function signIn() { window.location.replace(SIGNED_OUT); }
    function both() { if (played && out) leave(signIn); }
    function asToday() { form.submit(); }
    html.classList.toggle('is-leaving-flat', !foldInto(panel));
    html.classList.add('is-leaving');
    var calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.setTimeout(function () { played = true; both(); }, calm ? LEAVE_CALM_MS : LEAVE_MS);
    // Unanswered, the session may still stand: the home page shows which, sending a signed-out
    // person to sign in, where "Signed out" could be untrue.
    window.setTimeout(function () {
      leave(out ? signIn : function () { window.location.replace('/'); });
    }, LEAVE_LATEST_MS);
    answer.then(function (res) {
      // The redirect is the session ended; a 401, one that had already ended.
      if (res.ok || res.type === 'opaqueredirect' || res.status === 401) {
        out = true;
        both();
      } else {
        leave(asToday);
      }
    }, function () { leave(asToday); });
  }
  window.MM = window.MM || {};
  window.MM.logOut = logOut;

  // Back from the history after log-out: what it took away comes back. On every page, Watch too,
  // which has no menu.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) document.documentElement.classList.remove('is-leaving', 'is-leaving-flat');
  });

  var menu = document.getElementById('profile-menu');
  if (!menu) return;

  var phone = window.matchMedia('(max-width: 699.98px)');
  var pane = document.getElementById('you');
  var opener = null;
  var held = false;
  var openedAt = 0;
  var SETTLE_MS = 450; // a second tap of the one that opened the sheet comes within this

  var panel = menu.querySelector('.profile-panel');
  var handle = menu.querySelector('.profile-handle');
  var outForm = menu.querySelector('form[action="/api/auth/logout"]');
  if (outForm) {
    outForm.addEventListener('submit', function (e) {
      e.preventDefault();
      logOut(outForm, panel);
    });
  }
  var calm = window.matchMedia('(prefers-reduced-motion: reduce)');
  var SHUT_PX = 80;      // dragged this far down, the sheet shuts
  var FLICK_PX_MS = 0.5; // or flicked down this fast, from 20 px on, over the last bit of the drag
  var FLICK_WINDOW_MS = 80;

  function items() {
    return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]')).filter(function (n) { return !n.hasAttribute('hidden'); });
  }
  function buttonOf(target) {
    return target && target.closest ? target.closest('.profile-btn') : null;
  }
  function isOpen() { return menu.hasAttribute('open'); }

  function holdPage() {
    held = true;
    window.MM.inertPage.hold(menu);
    document.body.style.overflow = 'hidden';
  }

  function releasePage() {
    if (!held) return;
    held = false;
    window.MM.inertPage.release(menu);
    document.body.style.overflow = '';
  }

  function open(button, last) {
    opener = button;
    // A drag-close leaves the panel far below: put it back unseen, so every opening rises alike.
    panel.style.transition = 'none';
    panel.style.transform = '';
    void panel.offsetWidth;
    panel.style.transition = '';
    menu.inert = false;
    openedAt = Date.now();
    menu.setAttribute('open', '');
    button.setAttribute('aria-expanded', 'true');
    if (phone.matches) holdPage();
    var list = items();
    list[last ? list.length - 1 : 0].focus();
  }

  function close(returnFocus) {
    if (!isOpen()) return;
    if (drag) { drag = null; panel.style.transition = ''; panel.style.transform = ''; }
    menu.removeAttribute('open');
    releasePage();
    // CSS keeps the menu visible for the fade, so only this keeps it out of focus and the tree.
    menu.inert = true;
    opener.setAttribute('aria-expanded', 'false');
    if (returnFocus) opener.focus({ preventScroll: true });
  }

  document.addEventListener('click', function (e) {
    // The number leads where the ring does, not to the menu.
    if (e.target.closest && e.target.closest('.profile-count.is-on')) {
      e.preventDefault();
      ringLink.click();
      return;
    }
    var button = buttonOf(e.target);
    if (button && pane) {
      window.MM.you.go('profile');
      return;
    }
    if (button) {
      if (isOpen()) close(true); else open(button, false);
      return;
    }
    if (!isOpen()) return;
    // The sheet comes up under the finger, Log out near where the button was.
    if (phone.matches && e.detail && Date.now() - openedAt < SETTLE_MS && menu.contains(e.target)) {
      e.preventDefault();
      return;
    }
    // The sheet's backdrop is the menu's own box around the panel.
    if (e.target === menu || (e.target.closest && e.target.closest('.profile-close'))) close(true);
    else if (e.target.closest && e.target.closest('a[role="menuitem"]')) close(false);
  });

  // iOS sends a tap on the dimmed page to the document's click handler only if the menu has a handler of its own.
  menu.addEventListener('click', function () {});

  document.addEventListener('pointerdown', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  }, true);

  document.addEventListener('focusin', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  });

  // Capture, so the page's own shortcuts do not act on a key the menu took.
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    // An Escape a layer over the menu has already taken is that layer's alone.
    if (e.key === 'Escape' && e.defaultPrevented) return;
    if (!isOpen()) {
      var button = buttonOf(e.target);
      if (!button || pane || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
      e.preventDefault();
      e.stopPropagation();
      open(button, e.key === 'ArrowUp');
      return;
    }
    // Focus goes on from the button, as if the menu had not been there.
    if (e.key === 'Tab') { close(true); return; }
    var list = items();
    var at = list.indexOf(document.activeElement);
    var to = null;
    if (e.key === 'ArrowDown') to = at + 1;
    else if (e.key === 'ArrowUp') to = at < 0 ? list.length - 1 : at - 1;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = list.length - 1;
    if (to !== null) list[(to + list.length) % list.length].focus();
    else if (e.key === 'Escape') close(true);
    // A link answers Enter by itself, but not the space bar.
    else if (e.key === ' ' && at >= 0 && list[at].tagName === 'A') list[at].click();
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  // The handle drags the sheet down after the finger: shut past a distance or a flick, back otherwise.
  var drag = null;
  function dragTo(y) { panel.style.transform = 'translateY(' + Math.max(0, y - drag.y) + 'px)'; }
  if (handle) {
    handle.addEventListener('pointerdown', function (e) {
      if (!isOpen() || !phone.matches || e.button) return;
      drag = { id: e.pointerId, y: e.clientY, pts: [{ y: e.clientY, t: Date.now() }] };
      panel.style.transition = 'none';
      if (handle.setPointerCapture) handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      drag.pts.push({ y: e.clientY, t: Date.now() });
      dragTo(e.clientY);
    });
    var lift = function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var far = Math.max(0, e.clientY - drag.y);
      var now = Date.now();
      var pts = drag.pts;
      var ref = pts[pts.length - 1];
      for (var i = 0; i < pts.length; i++) {
        if (pts[i].t >= now - FLICK_WINDOW_MS) { ref = pts[i]; break; }
      }
      var speed = (e.clientY - ref.y) / Math.max(1, now - ref.t);
      var flick = far > 20 && speed > FLICK_PX_MS;
      var back = speed < -FLICK_PX_MS; // flicked back up: keep it open, however far down it was
      drag = null;
      panel.style.transition = '';
      if (e.type === 'pointerup' && !back && (far > SHUT_PX || flick)) {
        if (calm.matches) panel.style.transform = '';
        else panel.style.transform = 'translateY(100%)';
        close(true);
      } else {
        panel.style.transform = '';
      }
    };
    handle.addEventListener('pointerup', lift);
    handle.addEventListener('pointercancel', lift);
    handle.addEventListener('lostpointercapture', lift); // a release that never came
  }

  // A menu becomes a sheet, or a sheet a menu.
  phone.addEventListener('change', function () { close(true); });

  // Once the page has moved under the bar, the brand takes an island of its own.
  var bar = document.querySelector('.nav-glass');
  function scrolled() { bar.classList.toggle('is-scrolled', window.scrollY > 4); }
  if (bar) {
    window.addEventListener('scroll', scrolled, { passive: true });
    scrolled();
  }

  var button = document.querySelector('.profile-btn');
  var name = button.getAttribute('aria-label');
  var ring = button.querySelector('.profile-ring');
  var count = button.querySelector('.profile-count');
  // The ring is a link of its own beside the face: a link inside a button is not valid. It sits over
  // the ring, only while something downloads, and the face stays the profile button.
  var slot = document.createElement('span');
  slot.className = 'profile-slot';
  button.parentNode.insertBefore(slot, button);
  slot.appendChild(button);
  var ringLink = document.createElement('a');
  ringLink.className = 'profile-dl';
  ringLink.setAttribute('href', '/?dl=1');
  ringLink.setAttribute('data-dl-open', '');
  ringLink.setAttribute('tabindex', '0'); // Safari leaves out links that say nothing of it
  ringLink.setAttribute('hidden', '');
  slot.appendChild(ringLink);
  if (pane) {
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-controls', 'you');
  }

  function tone(node, state) {
    var sum = state.summary;
    node.classList.toggle('is-lost', !!state.lost);
    node.classList.toggle('is-ready', !state.lost && !sum.active && sum.ready > 0);
    node.classList.toggle('is-waiting', !state.lost && !sum.active && !sum.ready);
  }

  // Kept for each person, since people share a browser.
  var who = button.querySelector('.profile-btn-name');
  var SEEN_KEY = 'mm:ready-seen:' + (who ? who.textContent.trim() : '');
  // The menu's line of what is ready, there only while some is new: Downloading is not in the menu.
  var readyLine = document.createElement('a');
  readyLine.className = 'profile-item';
  readyLine.id = 'profile-ready';
  readyLine.setAttribute('role', 'menuitem');
  readyLine.setAttribute('tabindex', '-1');
  readyLine.setAttribute('href', '/?dl=1');
  readyLine.setAttribute('data-dl-open', '');
  readyLine.setAttribute('hidden', '');
  if (window.MM && window.MM.dom) readyLine.appendChild(window.MM.dom.icon('i-play'));
  var readyWords = readyLine.appendChild(document.createElement('span'));
  var menuItems = document.getElementById('profile-menu-items');
  menuItems.insertBefore(readyLine, menuItems.firstChild);
  var shown = null; // the last state drawn
  var seen = (function () {
    try {
      var kept = JSON.parse(window.localStorage.getItem(SEEN_KEY));
      return Array.isArray(kept) ? kept : [];
    } catch (e) { return []; }
  })();

  function keepSeen(keys) {
    seen = keys;
    try { window.localStorage.setItem(SEEN_KEY, JSON.stringify(keys)); } catch (e) { /* not kept */ }
  }

  function readyOf(state) {
    return (state.items || []).filter(function (item) { return item.state === 'ready' && item.playable !== false; });
  }

  // The title open now, by the address the Watch page gives it, or its own page's.
  function isOpenNow(item) {
    var at = /^#title=([^&]+)/.exec(window.location.hash || '');
    var ref = null;
    try { ref = at && decodeURIComponent(at[1]); } catch (e) { /* not a title */ }
    if (ref && (ref === item.ref || ref === item.tmdb_ref)) return true;
    return !!item.href && item.href.indexOf('#') < 0 && item.href === window.location.pathname;
  }

  /* How many of what is ready have not been seen. Only those the answer lists are counted, since
     Downloading shows no more than that. */
  function unseenOf(state) {
    var ready = readyOf(state);
    var keys = ready.map(function (item) { return item.key; });
    var now = seen.concat(ready.filter(isOpenNow).map(function (item) { return item.key; }));
    // An answer that lists nothing while some are there says nothing of what is no longer ready.
    var listed = !state.lost && Array.isArray(state.items) && (state.items.length > 0 || !state.summary.total);
    if (listed) now = now.filter(function (key) { return keys.indexOf(key) >= 0; });
    now = now.filter(function (key, i) { return now.indexOf(key) === i; });
    if (now.join('\n') !== seen.join('\n')) keepSeen(now);
    var fresh = keys.filter(function (key) { return seen.indexOf(key) < 0; }).length;
    return fresh;
  }

  function see(keys) {
    var more = (keys || []).filter(function (key) { return seen.indexOf(key) < 0; });
    if (!more.length) return;
    keepSeen(seen.concat(more));
    if (shown) drawDownloads(shown);
  }
  document.addEventListener('mm:ready-shown', function (e) { see(e.detail && e.detail.keys); });
  function looked() { if (shown) drawDownloads(shown); }
  window.addEventListener('hashchange', looked);
  window.addEventListener('popstate', looked);
  // A title opened on Watch changes the address with no event of its own.
  document.addEventListener('click', function () { window.setTimeout(looked, 0); });

  function drawDownloads(state) {
    shown = state;
    var sum = state.summary;
    var fresh = unseenOf(state);
    var going = sum.total - sum.ready;
    // Downloading lists no more than the answer holds, so a count past that is not shown there.
    var ready = Array.isArray(state.items) && state.items.length ? readyOf(state).length : sum.ready;
    var words = fresh ? fresh + ' ready to watch'
      : sum.active ? going + (sum.waiting ? ' in progress' : ' downloading')
      : sum.waiting ? sum.waiting + ' waiting to download'
      : ready + ' ready to watch';
    var also = !fresh && going && ready ? ', ' + ready + ' ready to watch' : '';
    var told = fresh > 0 && !state.lost;
    button.setAttribute('aria-label', name + (!sum.total ? '' : state.lost ? ', downloads not updating' : ', ' + words + also));

    ringLink.toggleAttribute('hidden', !sum.total);
    if (sum.total) {
      ringLink.setAttribute('aria-label', state.lost ? 'Downloads not updating'
        : sum.active && !told ? 'Downloading, ' + sum.percent + '%' : words);
    }
    if (told) {
      count.textContent = fresh;
      count.setAttribute('title', words);
      ringLink.setAttribute('title', words);
    } else ringLink.removeAttribute('title');
    readyLine.toggleAttribute('hidden', !told);
    if (told) readyWords.textContent = words;

    var was = ring.classList.contains('is-on');
    ring.classList.toggle('is-on', sum.total > 0);
    count.classList.toggle('is-on', told);
    if (!sum.total) return; // as it was, while it fades

    tone(ring, state);
    // A little at least, or the ring would be a plain circle.
    if (!was) ring.classList.add('is-set'); // not round from where it last stood
    ring.style.setProperty('--ring', Math.max(sum.percent, 6));
    if (!was) {
      void ring.offsetWidth;
      ring.classList.remove('is-set');
    }
  }
  if (window.MM && window.MM.downloading) window.MM.downloading.subscribe(drawDownloads);

  // A changed picture is loaded before it takes the place of the one on show; the last change wins.
  var newest = null;
  document.addEventListener('mm:avatar', function (e) {
    var picture = newest = new Image();
    picture.onload = function () {
      if (picture !== newest) return;
      document.querySelectorAll('.profile-btn .avatar, .profile-who .avatar').forEach(function (avatar) {
        var img = document.createElement('img');
        img.alt = '';
        img.src = picture.src;
        img.className = 'is-new';
        avatar.className = 'avatar avatar--pic';
        avatar.textContent = '';
        avatar.appendChild(img);
      });
    };
    picture.src = e.detail.url;
  });

  // Back brings the page as it was left, open menu and all.
  window.addEventListener('pageshow', function () { close(false); });
})();
