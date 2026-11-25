/* The keep page: its days as they fall where the reader is, as Watch says them. */
(function () {
  'use strict';

  document.querySelectorAll('[data-keep-day]').forEach(function (el) {
    var at = new Date(el.getAttribute('datetime'));
    if (isNaN(at)) return;
    el.textContent = at.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
    // The count beside it goes by the same days.
    var left = el.parentNode.querySelector('[data-keep-in]');
    if (!left) return;
    var now = new Date();
    var days = Math.round((new Date(at.getFullYear(), at.getMonth(), at.getDate()) -
      new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 864e5);
    left.textContent = days < 1 ? 'today' : days === 1 ? 'tomorrow' : 'in ' + days + ' days';
  });
})();
