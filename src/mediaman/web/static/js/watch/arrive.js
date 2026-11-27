/* Watch straight after signing in plays its half of the move from the sign-in page (_watch.css,
 * _nav.css). The sign-in page leaves { t, sky } in sessionStorage as it posts: when, and where its
 * two lights stood. Run in the head, before the first paint, so that paint is already the
 * hand-over frame: black and the lights where the sign-in page left them. A normal load has no
 * note, and plays nothing. */
(function () {
  'use strict';

  var KEY = 'mm.arrive';
  var FRESH_MS = 30000;
  var PLAYED_MS = 1200; // the arrival's longest part, with room to spare

  var note;
  try {
    note = window.sessionStorage.getItem(KEY);
    if (note === null) return;
    window.sessionStorage.removeItem(KEY);
    note = JSON.parse(note);
  } catch (e) {
    return;
  }
  if (!note || typeof note.t !== 'number') return;
  var age = Date.now() - note.t;
  if (!(age >= 0 && age < FRESH_MS)) return;

  var html = document.documentElement;
  html.classList.add('is-arriving');
  (Array.isArray(note.sky) ? note.sky : []).slice(0, 2).forEach(function (place, i) {
    if (typeof place === 'string') html.style.setProperty('--sky-' + i, place);
  });

  // Over, the class goes, so what the page draws later does not come in with it.
  document.addEventListener('DOMContentLoaded', function () {
    window.setTimeout(function () { html.classList.remove('is-arriving'); }, PLAYED_MS);
  });
})();
