/* The keep page: its days as they fall where the reader is, as Watch says them. */
(function () {
  'use strict';

  var FORMS = {
    short: { day: 'numeric', month: 'short' },
    month: { month: 'short' },
    number: { day: 'numeric' }
  };
  var now = new Date();

  document.querySelectorAll('[data-keep-day]').forEach(function (el) {
    var at = new Date(el.getAttribute('datetime'));
    if (isNaN(at)) return;
    var form = FORMS[el.getAttribute('data-keep-day')] || { day: 'numeric', month: 'long' };
    if (form.month === 'long' && at.getFullYear() !== now.getFullYear()) form.year = 'numeric';
    el.textContent = at.toLocaleDateString('en-GB', form);
  });

  // The count beside them goes by the same days.
  document.querySelectorAll('[data-keep-in]').forEach(function (el) {
    var at = new Date(el.getAttribute('data-keep-in'));
    if (isNaN(at)) return;
    var leaves = new Date(at.getFullYear(), at.getMonth(), at.getDate());
    var days = Math.round((leaves - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 864e5);
    el.textContent = days < 1 ? 'today' : days === 1 ? 'tomorrow' : 'in ' + days + ' days';

    // So are the lengths: one that ends the day the title leaves keeps nothing.
    var lengths = Array.from(document.querySelectorAll('.share-keep button'));
    lengths.forEach(function (button) {
      var until = new Date(button.querySelector('time').getAttribute('datetime'));
      button.hidden = new Date(until.getFullYear(), until.getMonth(), until.getDate()) <= leaves;
    });
    if (lengths.length && lengths.every(function (button) { return button.hidden; })) {
      document.querySelector('.share-keep').hidden = true;
      document.querySelector('[data-keep-none]').hidden = false;
    }
  });
})();
