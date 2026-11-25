/* Profile menu. A press on the profile button, or an arrow
 * key on it, opens the menu; arrows, Home and End move in it; Escape, Tab, a press
 * outside, focus moving away and the sheet's Close button close it. Below 700 px it
 * is a bottom sheet: the rest of the page is inert and does not scroll under it. The
 * fade is CSS; the open state changes at once, and a closed menu is inert while it fades.
 * The button and the menu also say how what is downloading stands. */
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

  var menu = document.getElementById('profile-menu');
  if (!menu) return;

  var phone = window.matchMedia('(max-width: 699.98px)');
  var opener = null;
  var held = false;

  function items() {
    return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
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
    menu.inert = false;
    menu.setAttribute('open', '');
    button.setAttribute('aria-expanded', 'true');
    if (phone.matches) holdPage();
    var list = items();
    list[last ? list.length - 1 : 0].focus();
  }

  function close(returnFocus) {
    if (!isOpen()) return;
    menu.removeAttribute('open');
    releasePage();
    // CSS keeps the menu visible for the fade, so only this keeps it out of focus and the tree.
    menu.inert = true;
    opener.setAttribute('aria-expanded', 'false');
    if (returnFocus) opener.focus({ preventScroll: true });
  }

  document.addEventListener('click', function (e) {
    var button = buttonOf(e.target);
    if (button) {
      if (isOpen()) close(true); else open(button, false);
      return;
    }
    if (!isOpen()) return;
    // The sheet's backdrop is the menu's own box around the panel.
    if (e.target === menu || (e.target.closest && e.target.closest('.profile-close'))) close(true);
    else if (e.target.closest && e.target.closest('a[role="menuitem"]')) close(false);
  });

  document.addEventListener('pointerdown', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  }, true);

  document.addEventListener('focusin', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  });

  // Capture, so the page's own shortcuts do not act on a key the menu took.
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (!isOpen()) {
      var button = buttonOf(e.target);
      if (!button || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
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

  // A menu becomes a sheet, or a sheet a menu.
  phone.addEventListener('change', function () { close(true); });

  // A phone's keyboard shortens what is seen of the page, not the page. Zoomed in, the scale makes up for it.
  var seen = window.visualViewport;
  function keyboard() {
    var root = document.documentElement;
    root.classList.toggle('has-keyboard', root.clientHeight - seen.height * seen.scale > 150);
  }
  if (seen) seen.addEventListener('resize', keyboard);

  var button = document.querySelector('.profile-btn');
  var name = button.getAttribute('aria-label');
  var ring = button.querySelector('.profile-ring');
  var count = button.querySelector('.profile-count');
  var note = document.getElementById('profile-note');

  function tone(node, state) {
    var sum = state.summary;
    node.classList.toggle('is-lost', !!state.lost);
    node.classList.toggle('is-ready', !state.lost && !sum.active && sum.ready > 0);
    node.classList.toggle('is-waiting', !state.lost && !sum.active && !sum.ready);
  }

  function drawDownloads(state) {
    var sum = state.summary;
    var words = sum.ready ? sum.ready + ' ready to watch'
      : sum.active ? sum.total + ' downloading'
      : sum.waiting + ' waiting to download';
    button.setAttribute('aria-label', name + (!sum.total ? '' : state.lost ? ', downloads not updating' : ', ' + words));

    ring.classList.toggle('is-on', sum.total > 0);
    count.classList.toggle('is-on', sum.ready > 0 && !state.lost);
    note.hidden = !sum.total;
    if (sum.ready) count.textContent = sum.ready;
    if (!sum.total) return; // as it was, while it fades

    tone(ring, state);
    tone(note, state);
    // A little at least, or the ring would be a plain circle.
    ring.style.setProperty('--ring', Math.max(sum.percent, 6));
    note.style.setProperty('--ring', sum.percent);
    note.textContent = '';
    var pause = document.createElement('span');
    pause.className = 'sr-only';
    pause.textContent = ', ';
    note.appendChild(pause);
    if (sum.active && !state.lost) note.appendChild(document.createElement('i'));
    note.appendChild(document.createTextNode(
      state.lost ? 'Not updating' : sum.active ? sum.percent + '%' + (sum.total > 1 ? ' · ' + sum.total : '')
        : sum.ready ? sum.ready + ' ready' : sum.waiting + ' waiting'));
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
