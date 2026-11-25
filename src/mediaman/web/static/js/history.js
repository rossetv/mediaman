/**
 * History page — everyone's events and plays for an admin, else a person's own.
 *
 *  - The first page comes with the document (#history-data) and the rest by cursor as the reader
 *    nears the end, or by a button for the keyboard. An event is listed once, whatever arrives.
 *  - Search and filters are kept in the address: a new one is a step Back undoes, typing on is not.
 *    `date` is a name (today, yesterday, week, month) or two days, `2026-11-01..2026-11-25`, of the
 *    reader's own zone; the server is asked for the moments they begin and end at,
 *    and for `since` and `until` as an address names them.
 *  - Only the last question's answer is shown: a slower one before it is dropped.
 *  - Each row is a <details>. Opening one keeps `?event=<id>` in the address (replaceState, so
 *    Back does not step through every row). What lies below a row slides along with it, and the
 *    edge of its card moves with them; with reduced motion it opens and folds at once.
 *  - Times are shown in the reader's zone; the details copied hold the UTC time too.
 */
(function () {
  'use strict';

  var CLOSE_FALLBACK_MS = 220;
  var TYPING_MS = 180;
  var SLOW_MS = 600;
  var DAY_MS = 864e5;
  var MAX = { title: 1000, user: 300 };
  var KEYS = ['q', 'user', 'action', 'title', 'date', 'since', 'until'];
  var MOMENT = /^[12]\d{3}-\d\d-\d\d(T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d(\.\d+)?)?(Z|[+-]\d\d:\d\d)?)?$/;
  var DATES = { today: 'Today', yesterday: 'Yesterday', week: 'Last 7 days', month: 'Last 30 days' };
  var NAMES = { user: 'User', action: 'Action', title: 'Title', date: 'Date' };

  /* What an action is said as, its kind and its icon. */
  var ACTIONS = {
    scanned: ['scanned', 'scan', 'i-rotate-right'],
    scheduled_deletion: ['scheduled the deletion of', 'del', 'i-clock'],
    snoozed: ['kept', 'keep', 'i-lock'],
    kept: ['kept', 'keep', 'i-lock'],
    'protected': ['kept', 'keep', 'i-lock'],
    protected_forever: ['kept forever', 'keep', 'i-lock'],
    kept_show: ['kept the show', 'keep', 'i-lock'],
    unkept: ['stopped keeping', 'unkeep', 'i-xmark'],
    unprotected: ['stopped keeping', 'unkeep', 'i-xmark'],
    removed_show_keep: ['stopped keeping the show', 'unkeep', 'i-xmark'],
    deleted: ['deleted', 'del', 'i-trash-can'],
    downloaded: ['downloaded', 'download', 'i-download'],
    re_downloaded: ['downloaded again', 'download', 'i-download'],
    delete_failed: ['could not delete', 'bad', 'i-triangle-exclamation'],
    delete_failed_permanent: ['could not delete', 'bad', 'i-triangle-exclamation'],
    delete_refused: ['refused to delete', 'bad', 'i-triangle-exclamation'],
    delete_incomplete: ['did not wholly delete', 'bad', 'i-triangle-exclamation'],
    delete_folders_failed: ['could not clean up the folder of', 'bad', 'i-triangle-exclamation'],
    delete_root_unavailable: ['could not reach the', 'bad', 'i-triangle-exclamation'],
    delete_root_available: ['reached again the', 'scan', 'i-check'],
    delete_awaiting_scan: ['waits to delete', 'scan', 'i-clock'],
    dry_run_skip: ['in a dry run, skipped', 'scan', 'i-rotate-right'],
    media_missing: ['could not find', 'scan', 'i-magnifying-glass'],
    unmonitor_skipped: ['did not stop monitoring', 'scan', 'i-rotate-right'],
    library_left_out: ['did not scan', 'bad', 'i-triangle-exclamation']
  };
  /* A security event: what a person did, as a verb. What is not here reads by its label. */
  var DEEDS = {
    'login.success': 'signed in', logout: 'signed out', 'user.created': 'added the user',
    'user.deleted': 'deleted the user', 'user.role_changed': 'changed a role:',
    'user.email_updated': 'changed an email address', 'user.unlocked': 'unlocked a user',
    'password.changed': 'changed a password', 'password.force_changed': 'changed a password',
    'reauth.granted': 'confirmed a password', 'reauth.failed': 'failed to confirm a password',
    'session.revoke_one': 'signed out a device', 'session.revoke_all': 'ended all sessions',
    'settings.write': 'changed settings', 'scan.triggered': 'started a scan',
    'scan.cleared': 'cleared a scan', 'library.sync': 'synced the library',
    'newsletter.sent': 'sent the newsletter', 'subscriber.added': 'added a subscriber',
    'subscriber.removed': 'removed a subscriber', 'plex.linked': 'linked Plex',
    'plex.unlinked': 'unlinked Plex', 'plex.link_started': 'began to link Plex',
    'plex.link_failed': 'could not link Plex', 'session.destroy': 'ended a session'
  };
  var NAMED = ['user.created', 'user.deleted', 'user.role_changed', 'user.email_updated'];
  /* Filters, by the server's names for them. */
  var FILTER_ICONS = {
    plays: 'i-play', downloads: 'i-download', keeps: 'i-lock', deletions: 'i-trash-can', sign_ins: 'i-user',
    people: 'i-users', settings: 'i-gear', scans: 'i-rotate-right', newsletter: 'i-envelope'
  };

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function el(tag, className, kids) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    [].concat(kids == null ? [] : kids).forEach(function (k) {
      n.appendChild(typeof k === 'string' ? document.createTextNode(k) : k);
    });
    return n;
  }

  function button(className, kids, attrs) {
    var b = el('button', className, kids);
    b.setAttribute('type', 'button');
    Object.keys(attrs || {}).forEach(function (k) { b.setAttribute(k, attrs[k]); });
    return b;
  }

  /* The clipboard API needs a secure page; a Mediaman reached over plain http on a
     home network is not one, so fall back to a selected hidden textarea. */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.className = 'sr-only';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  function dayStart(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function shift(d, days) { return new Date(d.getFullYear(), d.getMonth(), d.getDate() + days); }
  function localDay(text) {
    var m = /^(\d{4})-(\d\d)-(\d\d)$/.exec(text);
    var d = m && new Date(+m[1], m[2] - 1, +m[3]);
    return d && d.getMonth() === m[2] - 1 ? d : null;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dayText(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }

  /* The moments a date filter begins and ends at: {since, until}, or null for none that reads. */
  function moments(date, now) {
    var today = dayStart(now);
    var first = null;
    var last = null;
    if (date === 'today') first = today;
    else if (date === 'yesterday') { first = shift(today, -1); last = first; }
    else if (date === 'week') first = shift(today, -6);
    else if (date === 'month') first = shift(today, -29);
    else {
      var ends = String(date).split('..');
      if (ends.length !== 2) return null;
      first = localDay(ends[0]);
      last = localDay(ends[1]);
      if ((ends[0] && !first) || (ends[1] && !last) || !(first || last)) return null;
    }
    var out = {};
    if (first) out.since = first.toISOString();
    if (last) out.until = shift(last, 1).toISOString();
    return out;
  }

  function dateLabel(date) {
    if (DATES[date]) return DATES[date];
    var ends = date.split('..').map(function (t) {
      var d = localDay(t);
      var words = { day: 'numeric', month: 'short' };
      if (d && d.getFullYear() !== new Date().getFullYear()) words.year = 'numeric';
      return d ? d.toLocaleDateString('en-GB', words) : '';
    });
    if (ends[0] && ends[1]) return ends[0] === ends[1] ? ends[0] : ends[0] + ' – ' + ends[1];
    return ends[0] ? 'From ' + ends[0] : 'Until ' + ends[1];
  }

  /* The day a moment of the address falls on: the one written, or the reader's own. */
  function momentDay(text, isEnd) {
    var at = localDay(text) || new Date(/T.*(Z|[+-]\d\d:\d\d)$/.test(text) ? text : text + 'Z');
    return dayText(isEnd && text.length > 10 ? new Date(at - 1) : at);
  }

  function momentsLabel(since, until) {
    return dateLabel((since && momentDay(since)) + '..' + (until && momentDay(until, true)));
  }

  function start(root, data) {
    var MM = window.MM;
    var icon = MM.dom.icon;
    var q = MM.dom.q;
    var tools = q('.hx-tools', root);
    var search = q('.hx-search', root);
    var input = q('.hx-search input', root);
    var chipsBox = q('.hx-chips', root);
    var sum = q('.hx-sum', root);
    var count = q('.hx-sum span', root);
    var clearAllBtn = q('[data-hist-clear]', root);
    var list = q('.hx-list', root);
    var more = q('.hx-more', root);
    var said = q('[data-hist-said]', root);
    var menu = q('.hx-menu');
    var veil = q('.hx-veil');

    var people = Object.create(null); // by what stands for them in the address
    data.people.forEach(function (p) { people[p.value] = p; });

    var F = {};            // what is searched and filtered by
    var cursor = null;     // where the next page begins
    var seen = {};         // the ids listed
    var lastRow = null;    // the last event listed
    var opened = null;     // the id of the event the address opens
    var everything = null; // how many events there are unfiltered
    var total = 0;
    var asked = 0;         // numbers each question, so a late answer to an older one is dropped
    var asking = null;     // the question under way
    var loadingMore = false;
    var typed = null;      // the timer of the search being typed
    var typing = false;    // the address's step is already this search's
    var menuOf = null;     // the filter whose menu is open
    var titlesOf = null;   // the last titles to choose from, and the question they answer
    var menuAsked = 0;
    var at = '';           // the address listed, without its hash

    function here() {
      var url = new URL(window.location.href);
      return url.pathname + url.search;
    }

    function go(method) {
      window.history[method](null, '', address());
      at = here();
    }

    function filtered() { return KEYS.some(function (k) { return !!F[k]; }); }

    /* --- the address --- */

    /* Whether the address's moments include one this page does not read, as the server may have. */
    function readAddress() {
      var unread = false;
      var p = new URL(window.location.href).searchParams;
      KEYS.forEach(function (k) { F[k] = (p.get(k) || '').trim().slice(0, MAX[k] || 200); });
      if (!data.admin) F.user = '';
      if (F.date && !moments(F.date, new Date())) F.date = '';
      /* A date named in the address stands for both ends. */
      ['since', 'until'].forEach(function (k) {
        var text = p.get(k) || '';
        F[k] = !F.date && MOMENT.test(text) && localDay(text.slice(0, 10)) && !isNaN(new Date(text)) ? text : '';
        if (text && !F[k]) unread = true;
      });
      opened = p.get('event');
      return unread;
    }

    function address(withoutEvent) {
      var p = new URLSearchParams();
      KEYS.forEach(function (k) { if (F[k]) p.set(k, F[k]); });
      if (opened && !withoutEvent) p.set('event', opened);
      var text = p.toString();
      return '/history' + (text ? '?' + text : '');
    }

    function query(extra, path) {
      var p = new URLSearchParams();
      ['q', 'user', 'action', 'title', 'since', 'until'].forEach(function (k) { if (F[k]) p.set(k, F[k]); });
      var when = F.date ? moments(F.date, new Date()) : null;
      Object.keys(when || {}).forEach(function (k) { p.set(k, when[k]); });
      Object.keys(extra || {}).forEach(function (k) { p.set(k, extra[k]); });
      var text = p.toString();
      return '/api/history' + (path || '') + (text ? '?' + text : '');
    }

    /* --- a row --- */

    /* Text with what is searched for marked in it. */
    function marked(tag, text) {
      var out = document.createElement(tag);
      var find = F.q.toLowerCase();
      var low = text.toLowerCase();
      var at = 0;
      /* A letter whose small form is longer would put every mark after it out of place. */
      if (find && low.length === text.length) {
        for (var i = low.indexOf(find); i >= 0; i = low.indexOf(find, at)) {
          if (i > at) out.appendChild(document.createTextNode(text.slice(at, i)));
          out.appendChild(el('mark', '', text.slice(i, i + find.length)));
          at = i + find.length;
        }
      }
      if (at < text.length || !text) out.appendChild(document.createTextNode(text.slice(at)));
      return out;
    }

    function face(name, url, unsigned) {
      var span = el('span', 'avatar');
      span.setAttribute('aria-hidden', 'true');
      if (url) {
        span.className = 'avatar avatar--pic';
        var img = document.createElement('img');
        img.setAttribute('src', url);
        img.setAttribute('alt', '');
        img.setAttribute('data-broken-on-error', '');
        span.appendChild(img);
      } else if (!name || unsigned) {
        span.className = 'avatar is-mm';
        span.appendChild(icon(unsigned ? 'i-user' : 'i-gear'));
      } else {
        span.className = 'avatar is-gone';
        span.textContent = name.charAt(0).toUpperCase();
      }
      return span;
    }

    /* Who a line names. A name typed or an address is never on it. */
    function whoOf(item) {
      var who = item.who;
      if (who.kind === 'account') return { name: who.name };
      if (who.kind === 'former') return { name: who.name.indexOf('@') >= 0 ? '' : who.name, gone: true };
      if (item.action === 'sec:login.failed') return { name: '', unsigned: true };
      if (item.action === 'sec:subscriber.opted_out') return { name: 'a subscriber', unsigned: true };
      if (item.action === 'sec:user.unlocked' && item.actor) return { name: 'the command line', unsigned: true };
      return { name: 'mediaman' };
    }

    /* Whom a value of the address stands for, also one no longer among the people. */
    function person(value) {
      return people[value] || { value: value, username: value.replace(/^~former:/, ''), kind: 'former', avatar: null };
    }

    function nameOf(p) { return p.username + (p.kind === 'former' ? ' (former)' : ''); }

    function faceOf(p) { return face(p.kind === 'system' ? '' : p.username, p.avatar); }

    function deedOf(item) {
      var out = { verb: item.action_label, object: item.line_title, sub: item.summary, kind: 'scan', icon: 'i-gear' };
      var act = ACTIONS[item.action];
      if (item.action === 'play') {
        var eps = item.episodes || [];
        var several = eps.length > 1;
        out.verb = item.finished || several ? 'finished' : 'started';
        if (several) out.verb = (item.finished ? 'watched ' : 'started ') + eps.length + ' episodes of';
        var facts = item.media_type === 'movie' ? ['Movie', item.year] : [];
        if (item.summary) facts.push(item.summary);
        if (item.finished && !several) facts.push(item.runtime_text);
        out.sub = facts.filter(Boolean).join(' · ');
        out.kind = 'play';
        out.icon = item.finished ? 'i-check' : 'i-play';
      } else if (item.is_security) {
        var event = item.action.slice(4);
        var deed = DEEDS[event];
        out.kind = /^(login|logout|session|password|reauth)/.test(event) ? 'sign' : 'people';
        out.icon = out.kind === 'sign' ? 'i-user' : /^(user|subscriber)/.test(event) ? 'i-users'
          : /^newsletter/.test(event) ? 'i-envelope' : /^(scan|library)/.test(event) ? 'i-rotate-right'
            : /^plex/.test(event) ? 'i-plug' : 'i-gear';
        if (/failed|throttled|rate_limited/.test(event)) { out.kind = 'bad'; out.icon = 'i-triangle-exclamation'; }
        out.verb = deed || '';
        out.object = '';
        if (event === 'newsletter.sent') {
          var sent = /^(\d+) sent$/.exec(out.sub);
          if (sent) out.sub = 'To ' + sent[1] + (sent[1] === '1' ? ' person' : ' people');
        }
        if (!deed) out.label = item.line_title;
        else if (NAMED.indexOf(event) >= 0 && item.summary) { out.object = item.summary; out.sub = ''; }
      } else if (act) {
        out.verb = act[0];
        out.kind = act[1];
        if (item.space_impact && out.sub === item.space_impact) out.sub += ' freed';
        out.icon = act[2];
        if (item.action.indexOf('delete_root') === 0) out.object = 'delete folder';
      }
      return out;
    }

    function cap(text) { return text.charAt(0).toUpperCase() + text.slice(1); }

    function lineOf(item, what) {
      var line = el('span', 'hx-l1');
      var who = whoOf(item);
      function put(node) {
        if (line.childNodes.length) line.appendChild(document.createTextNode(' '));
        line.appendChild(node);
      }
      if (item.action === 'sec:login.failed') {
        put(marked('span', 'Sign-in failed for'));
        put(people[item.actor] && people[item.actor].kind === 'account' ? marked('b', item.actor) : marked('span', 'an unknown name'));
        return line;
      }
      if (data.admin) {
        put(marked('b', who.name || 'A former user'));
        if (who.gone && who.name) put(el('span', '', '(former user)'));
      }
      if (what.label) put(marked(data.admin ? 'span' : 'b', data.admin ? '· ' + what.label : what.label));
      else if (what.verb) put(marked(data.admin || what.object ? 'span' : 'b', data.admin ? what.verb : cap(what.verb)));
      if (what.object) put(marked('b', what.object));
      return line;
    }

    function timeField(item) {
      for (var i = 0; i < item.fields.length; i++) if (item.fields[i].at) return item.fields[i];
      return null;
    }

    function momentOf(item) {
      var field = timeField(item);
      var at = new Date(field ? field.at : NaN);
      return isNaN(at) ? null : at;
    }

    function clock(at) { return at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); }

    function fullTime(at) {
      return at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) + ', ' +
        at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    function hasTitle(item) {
      return !item.is_security && item.line_title && item.line_title !== 'Unknown' &&
        item.action !== 'library_left_out' && item.action.indexOf('delete_root') !== 0;
    }

    function rowOf(item) {
      var what = deedOf(item);
      var who = whoOf(item);
      var at = momentOf(item);
      var row = el('details', 'hist');
      row.setAttribute('data-event', item.id);

      var act = el('span', 'hx-act hx-act--' + what.kind, icon(what.icon));
      var msg = el('span', 'hist-msg', lineOf(item, what));
      if (what.sub) {
        var sub = marked('span', what.sub);
        sub.className = 'hx-l2' + (what.kind === 'bad' ? ' is-bad' : '');
        msg.appendChild(sub);
      }
      var time = el(at ? 'time' : 'span', 'hist-date', at ? clock(at) : String(item.created_at).slice(0, 16));
      if (at) {
        time.setAttribute('datetime', at.toISOString());
        time.setAttribute('title', fullTime(at) + ' (' + timeField(item).value + ')');
      }
      row.appendChild(el('summary', 'hist-sum', [
        el('span', 'hx-who', [face(item.who.kind === 'system' ? '' : who.name || '?', item.avatar, who.unsigned), act]),
        msg, time, el('span', 'hist-chev', icon('i-chevron-down'))
      ]));

      var fields = el('dl', 'hist-fields');
      item.fields.forEach(function (f) {
        if (f.empty) return;
        var moment = f.at ? new Date(f.at) : NaN;
        var dd = el('dd', '', isNaN(moment) ? f.value : fullTime(moment));
        if (!isNaN(moment)) dd.setAttribute('title', f.value);
        if (item.progress && f.key === 'progress') {
          var done = el('i');
          done.style.width = Math.round(item.progress.fraction * 100) + '%';
          dd.appendChild(el('span', 'hx-bar', done));
        }
        fields.appendChild(el('div', 'hist-field', [el('dt', '', f.label), dd]));
      });
      var acts = el('div', 'hx-acts', button('btn btn--ghost btn--sm', 'Copy details', { 'data-hist-copy': '' }));
      if (hasTitle(item)) acts.appendChild(button('btn btn--ghost btn--sm', 'Only this title', { 'data-title': item.line_title }));
      if (data.admin && item.who.kind !== 'system' && who.name) {
        acts.appendChild(button('btn btn--ghost btn--sm', 'Only ' + (who.name.length > 22 ? 'this user' : who.name), { 'data-user': item.who.value }));
      }
      row.appendChild(el('div', 'hist-body', [fields, acts]));
      return row;
    }

    /* --- the list --- */

    function dayName(at) {
      if (!at) return ['Day not known', ''];
      var now = new Date();
      var gap = Math.round((dayStart(now) - dayStart(at)) / DAY_MS);
      var words = { weekday: 'long', day: 'numeric', month: 'long' };
      if (at.getFullYear() !== now.getFullYear()) words.year = 'numeric';
      var text = at.toLocaleDateString('en-GB', words).replace(',', '');
      return gap === 0 ? ['Today', text] : gap === 1 ? ['Yesterday', text] : [text, ''];
    }

    /* Lists those of the items that are not listed yet, and answers their rows. */
    function put(items) {
      var rows = [];
      var before = list.lastElementChild;
      items.forEach(function (item) {
        var id = String(item.id);
        if (Object.prototype.hasOwnProperty.call(seen, id)) return;
        seen[id] = true;
        lastRow = item;
        var name = dayName(momentOf(item));
        var day = list.lastElementChild;
        if (!day || day.getAttribute('data-day') !== name.join('|')) {
          var h = el('h2', 'hx-day__h', name[0]);
          if (name[1]) h.appendChild(el('small', '', name[1]));
          day = el('section', 'hx-day hx-in', [h, el('div', 'card card--bordered card--hist hx-card')]);
          day.setAttribute('data-day', name.join('|'));
          list.appendChild(day);
        }
        var row = rowOf(item);
        if (day === before) row.classList.add('hx-in');
        day.lastElementChild.appendChild(row);
        rows.push(row);
      });
      return rows;
    }

    function skelRows(n) {
      var card = el('div', 'card card--bordered hx-card');
      card.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < n; i++) {
        var lines = el('div', '', [el('i', 'skel'), el('i', 'skel')]);
        lines.children[0].style.width = '46%';
        lines.children[1].style.width = '28%';
        card.appendChild(el('div', 'hx-skel', [el('span', 'skel'), lines, el('span', 'skel')]));
      }
      return card;
    }

    function empty(node) { node.textContent = ''; }

    function foot() {
      empty(more);
      if (cursor) {
        more.appendChild(skelRows(3));
        more.appendChild(button('btn btn--secondary btn--sm hx-more__btn', 'Show earlier events', { 'data-hist-more': '' }));
        return;
      }
      if (!lastRow) return;
      var at = momentOf(lastRow);
      more.appendChild(el('p', 'hx-end', filtered() || !at ? 'That is every match.' : 'That is everything, back to ' +
        at.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' }) + '.'));
    }

    function countText() {
      if (!total) return '';
      var n = total.toLocaleString('en-GB');
      if (filtered() && everything !== null && everything >= total) n += ' of ' + everything.toLocaleString('en-GB');
      return n + ' event' + (total === 1 ? '' : 's');
    }

    function box(iconEl, title, text, label, attr) {
      var kids = [el('div', 'empty-ico', iconEl), el('h3', '', title), el('p', '', text)];
      if (label) {
        var attrs = {};
        attrs[attr] = '';
        kids.push(button('btn btn--secondary', label, attrs));
      }
      return el('div', 'empty hx-in', kids);
    }

    function words() {
      var w = [];
      if (F.q) w.push('“' + F.q + '”');
      if (F.user) w.push(nameOf(person(F.user)));
      if (F.action) w.push(actionLabel(F.action).toLowerCase());
      if (F.title) w.push('“' + F.title + '”');
      if (F.since || F.until) w.push(momentsLabel(F.since, F.until));
      if (F.date) w.push(DATES[F.date] ? DATES[F.date].toLowerCase() : dateLabel(F.date));
      return w.join(', ');
    }

    /* The search, the filters and the count go when there is nothing to search. */
    function showTools(on) {
      tools.hidden = !on;
      sum.hidden = !on;
    }

    function emptyList() {
      empty(list);
      seen = {};
      lastRow = null;
    }

    /* The way to the newest events, when the list begins at a later page than the first. */
    function linkNewest(page) {
      var link = q('.hx-newest', root);
      if (link) link.remove();
      if (!(page > 1)) return;
      link = el('a', 'hx-newest', 'Show the newest');
      link.setAttribute('href', address(true));
      sum.appendChild(link);
    }

    function show(page) {
      emptyList();
      cursor = page.cursor;
      total = page.total || 0;
      if (!filtered()) everything = total;
      list.classList.remove('is-old');
      list.removeAttribute('aria-busy');
      sum.classList.remove('is-waiting');
      var rows = put(page.items);
      var none = !rows.length;
      showTools(filtered() || !none);
      clearAllBtn.hidden = none || !filtered();
      MM.dom.setText(count, countText());
      foot();
      if (none && filtered()) {
        list.appendChild(box(icon('i-magnifying-glass'), 'Nothing matches', 'No event fits ' + words() + '.', 'Clear search and filters', 'data-hist-clear'));
        said.textContent = 'Nothing matches';
      } else if (none) {
        list.appendChild(box(icon('i-clock-rotate-left'), 'No history yet', data.admin
          ? 'What people play, download and keep, and what Mediaman does by itself, will show here.'
          : 'What you play, download and keep will show here.'));
      } else {
        said.textContent = '';
      }
      var row = opened && MM.dom.findByAttr(list, 'data-event', opened);
      if (row) {
        row.setAttribute('open', '');
        if (row.scrollIntoView) row.scrollIntoView({ block: 'center', behavior: 'auto' });
      } else if (opened) {
        opened = null;
        go('replaceState');
      }
      linkNewest(page.page);
      near();
    }

    function skeleton() {
      emptyList();
      empty(more);
      list.classList.remove('is-old');
      list.setAttribute('aria-busy', 'true');
      sum.classList.add('is-waiting');
      var bar = el('i', 'skel');
      list.appendChild(el('section', 'hx-day', [el('div', 'hx-day__h', bar), skelRows(8)]));
    }

    function failed() {
      emptyList();
      empty(more);
      cursor = null;
      list.classList.remove('is-old');
      list.removeAttribute('aria-busy');
      sum.classList.add('is-waiting');
      var node = box(icon('i-triangle-exclamation'), 'History did not load', 'Mediaman could not be reached. Nothing is lost.', 'Try again', 'data-hist-retry');
      node.setAttribute('role', 'alert');
      list.appendChild(node);
    }

    function ask() {
      asked += 1;
      loadingMore = false;
      if (asking) asking.abort();
      asking = new window.AbortController();
      return { n: asked, signal: asking.signal };
    }

    function load(now) {
      var me = ask();
      var slow = null;
      cursor = null;
      linkNewest(1);
      if (now) skeleton();
      else {
        list.classList.add('is-old');
        slow = window.setTimeout(function () { if (me.n === asked) skeleton(); }, SLOW_MS);
      }
      var extra = opened ? { event: opened } : {};
      return MM.api.get(query(extra), { signal: me.signal }).then(function (res) {
        window.clearTimeout(slow);
        if (me.n !== asked) return;
        show({ items: res.items, cursor: res.next_cursor, total: res.total, page: res.page });
        tellEverything();
      }, function () {
        window.clearTimeout(slow);
        if (me.n === asked) failed();
      });
    }

    /* The button goes, so its focus waits on the list and then takes what came. */
    function retry() {
      list.focus({ preventScroll: true });
      load(true).then(function () {
        var next = q('.hist-sum', list) || q('button', list);
        if (document.activeElement === list && next) next.focus({ preventScroll: true });
      });
    }

    /* How many there are in all, for "12 of 340": asked once, and never waited for. */
    function tellEverything() {
      if (everything !== null || !filtered() || !total) return;
      MM.api.get('/api/history?per_page=1').then(function (res) {
        if (everything === null) everything = res.total;
        MM.dom.setText(count, countText());
      }, function () {});
    }

    function loadMore(byHand) {
      if (loadingMore || !cursor || q('.hx-fail', more)) return;
      var me = ask();
      loadingMore = true;
      MM.api.get(query({ cursor: cursor }), { signal: me.signal }).then(function (res) {
        if (me.n !== asked) return;
        loadingMore = false;
        /* A cursor that does not move on would be asked for without end. */
        cursor = res.next_cursor === cursor ? null : res.next_cursor;
        var rows = put(res.items);
        var held = more.contains(document.activeElement);
        foot();
        if (rows.length) said.textContent = rows.length + ' more event' + (rows.length === 1 ? '' : 's') + (cursor ? '' : ', the last');
        var sums = MM.dom.qa('.hist-sum', list);
        var next = rows.length ? rows[0].querySelector('.hist-sum') : sums[sums.length - 1];
        if ((byHand || held) && next) next.focus({ preventScroll: !byHand });
        near();
      }, function () {
        if (me.n !== asked) return;
        loadingMore = false;
        var held = more.contains(document.activeElement);
        empty(more);
        var fail = el('div', 'hx-fail', [el('span', '', 'The earlier events did not load.'),
          button('btn btn--secondary btn--sm', 'Try again', { 'data-hist-more': '' })]);
        fail.setAttribute('role', 'alert');
        more.appendChild(fail);
        if (held) q('button', fail).focus({ preventScroll: true });
      });
    }

    function near() {
      if (cursor && more.getBoundingClientRect().top < window.innerHeight + 300) loadMore(false);
    }

    /* --- search and filters --- */

    function actionLabel(value) {
      for (var i = 0; i < data.actions.length; i++) if (data.actions[i][0] === value) return data.actions[i][1];
      if (value === data.filters.action) return cap(data.filterLabel);
      return cap(value.replace(/_/g, ' '));
    }

    function chipLabel(k) {
      if (k === 'date' && (F.since || F.until)) return momentsLabel(F.since, F.until);
      if (!F[k]) return '';
      if (k === 'user') return nameOf(person(F.user));
      return k === 'action' ? actionLabel(F[k]) : k === 'date' ? dateLabel(F[k]) : F[k];
    }

    function chips() {
      var had = document.activeElement && document.activeElement.getAttribute ? document.activeElement.getAttribute('data-menu') : null;
      empty(chipsBox);
      ['user', 'action', 'title', 'date'].forEach(function (k) {
        if (k === 'user' && !data.admin) return;
        var label = chipLabel(k);
        var open = button('hx-chip__open', [], {
          'data-menu': k, 'aria-haspopup': 'dialog', 'aria-expanded': 'false', 'aria-label': NAMES[k] + (label ? ': ' + label : '')
        });
        if (k === 'user' && F.user) open.appendChild(faceOf(person(F.user)));
        open.appendChild(el('span', '', label || NAMES[k]));
        open.appendChild(icon('i-chevron-down'));
        var chip = el('span', 'hx-chip' + (label ? ' is-set' : ''), open);
        if (label) chip.appendChild(button('hx-chip__clear', icon('i-xmark'), { 'data-clear': k, 'aria-label': 'Clear ' + NAMES[k].toLowerCase() }));
        chipsBox.appendChild(chip);
      });
      if (had) focusChip(had);
      input.setAttribute('placeholder', window.innerWidth < 400 ? 'Search history' : data.admin ? 'Search people, titles and events' : 'Search your history');
      search.classList.toggle('has-q', !!input.value);
    }

    function focusChip(k) {
      var b = MM.dom.findByAttr(chipsBox, 'data-menu', k);
      if (b) b.focus({ preventScroll: true });
    }

    function barTop() { return parseFloat(window.getComputedStyle(tools).top) || 0; }

    /* The list's top, under the tools, when the reader is further down. */
    function toTop() {
      var away = sum.getBoundingClientRect().top - tools.getBoundingClientRect().bottom;
      if (away < 0) window.scrollTo({ top: window.scrollY + away });
    }

    /* A change of what is asked for: the address's next step, unless it goes on from the one there is. */
    function change(sameStep) {
      window.clearTimeout(typed);
      typed = null;
      opened = null;
      go(sameStep ? 'replaceState' : 'pushState');
      chips();
      toTop();
      load(false);
    }

    function choose(k, value) {
      typing = false;
      F[k] = value;
      if (k === 'date') { F.since = ''; F.until = ''; }
      closeMenu(true);
      change(false);
      focusChip(k);
    }

    function clearAll() {
      typing = false;
      KEYS.forEach(function (k) { F[k] = ''; });
      input.value = '';
      change(false);
      input.focus({ preventScroll: true });
    }

    function searchNow() {
      window.clearTimeout(typed);
      typed = null;
      var text = input.value.trim().slice(0, 200);
      if (text === F.q) return;
      F.q = text;
      change(typing);
      typing = true;
    }

    /* --- a filter's menu --- */

    /* The filter's menu is a sheet, as the styles have it. */
    function phone() { return window.innerWidth < 700 || window.innerHeight < 520; }

    function option(k, value, kids) {
      var o = button('hx-opt', kids, { role: 'menuitemradio', 'aria-checked': String(F[k] === value), 'data-k': k, 'data-v': value });
      o.appendChild(icon('i-check', 'tick'));
      return o;
    }

    function countOf(n) {
      var small = el('small');
      if (n != null) small.textContent = n.toLocaleString('en-GB');
      return small;
    }

    function userOptions() {
      return data.people.map(function (p) {
        var you = p.kind === 'account' && p.username === data.me ? ' (you)' : '';
        return option('user', p.value, [faceOf(p), el('span', 'n', nameOf(p) + you), countOf()]);
      });
    }

    function actionOptions() {
      return data.actions.map(function (a) {
        return option('action', a[0], [el('span', 'k', icon(FILTER_ICONS[a[0]] || 'i-gear')), el('span', 'n', a[1]), countOf()]);
      });
    }

    /* A menu's choices with their counts, unless another menu was opened since. */
    function askMenu(k, extra, then, fail) {
      menuAsked += 1;
      var me = menuAsked;
      var ifStill = function (fn) {
        return function (res) { if (me === menuAsked && menuOf === k) fn(res); };
      };
      MM.api.get(query(Object.assign({ menu: k }, extra), '/menu')).then(ifStill(then), ifStill(fail || function () {}));
    }

    /* The choices stand already: only their counts are new. */
    function fillCounts(k, scroll) {
      askMenu(k, {}, function (res) {
        res.options.forEach(function (o) {
          var row = MM.dom.findByAttr(scroll, 'data-v', o.value);
          if (!row) return;
          var small = row.querySelector('small');
          small.textContent = o.count.toLocaleString('en-GB');
          small.classList.add('hx-in');
        });
      });
    }

    /* The titles with the most events, of those the text typed is in: at once of those this menu
       has had, then of all. */
    function titleMenu(scroll, field) {
      var none = el('p', 'hx-none');
      var known = titlesOf && titlesOf.question === query() ? titlesOf.options : [];
      var wait = null;
      none.setAttribute('role', 'status');

      function rows() { return MM.dom.qa('.hx-opt', scroll); }

      function show(options) {
        var active = document.activeElement;
        var at = active && scroll.contains(active) ? active.getAttribute('data-v') : null;
        var top = scroll.scrollTop;
        empty(scroll);
        options.forEach(function (o) {
          scroll.appendChild(option('title', o.value, [el('span', 'n', o.label), countOf(o.count)]));
        });
        scroll.appendChild(none);
        none.hidden = true;
        scroll.scrollTop = top;
        var same = at && MM.dom.findByAttr(scroll, 'data-v', at);
        if (same) same.focus({ preventScroll: true });
        else if (at) field.focus({ preventScroll: true });
      }

      function say(text) {
        none.textContent = text;
        none.hidden = !text;
      }

      function keep(options) {
        options.forEach(function (o) {
          if (!known.some(function (k) { return k.value === o.value; })) known = known.concat(o);
        });
      }

      function failed(byHand) {
        MM.dom.qa('.hx-opt-skel', scroll).forEach(function (s) { s.remove(); });
        say('The titles did not load.');
        var again = button('btn btn--secondary btn--sm', 'Try again', { 'data-titles-retry': '' });
        none.appendChild(again);
        if (byHand) again.focus({ preventScroll: true });
      }

      function look(byHand) {
        var find = field.value.trim().slice(0, 200);
        var low = find.toLowerCase();
        var ask = function () {
          if (!scroll.isConnected) return;
          askMenu('title', find ? { find: find } : {}, function (res) {
            var held = byHand && none.contains(document.activeElement);
            if (!find) {
              titlesOf = { question: query(), options: res.options };
              known = res.options;
            }
            keep(res.options);
            show(res.options);
            say(res.options.length ? (res.more ? 'These have the most events. Type to find others.' : '') : find ? 'No title by that name.' : 'No titles yet.');
            if (held) (q('.hx-opt', scroll) || menu).focus({ preventScroll: true });
          }, function () { failed(byHand && none.contains(document.activeElement)); });
        };
        window.clearTimeout(wait);
        menuAsked += 1;
        if (byHand) { ask(); return; }
        if (!find && titlesOf && titlesOf.question === query()) show(titlesOf.options);
        else if (known.length) show(known.filter(function (o) { return o.label.toLowerCase().indexOf(low) >= 0; }));
        else {
          show([]);
          for (var i = 0; i < 8; i++) scroll.appendChild(el('div', 'hx-opt-skel', el('i', 'skel')));
        }
        if (find) wait = window.setTimeout(ask, TYPING_MS);
        else ask();
      }

      var clear = q('.hx-x', menu);
      clear.addEventListener('click', function () {
        field.value = '';
        field.focus();
        typedIn();
      });
      none.addEventListener('click', function (e) {
        var again = e.target.closest('[data-titles-retry]');
        if (!again) return;
        /* Safari focuses no button that is clicked. */
        again.focus({ preventScroll: true });
        look(true);
      });
      function typedIn() {
        field.parentNode.classList.toggle('has-q', !!field.value);
        look();
      }
      field.addEventListener('input', typedIn);
      look();
    }

    function dateForm(scroll) {
      var ends = F.date && !DATES[F.date] ? F.date.split('..') : ['', ''];
      var form = el('form', 'hx-dates');
      var fields = ['From', 'To'].map(function (label, i) {
        var f = document.createElement('input');
        f.setAttribute('type', 'date');
        f.setAttribute('max', dayText(new Date()));
        f.value = ends[i] || '';
        form.appendChild(el('label', '', [label, f]));
        return f;
      });
      var wrong = el('span', 'hx-dates__e', 'The first day is after the last.');
      wrong.setAttribute('role', 'alert');
      wrong.hidden = true;
      form.appendChild(wrong);
      form.appendChild(el('button', 'btn btn--secondary btn--sm', 'Show these days'));
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var a = fields[0].value;
        var b = fields[1].value;
        if (!a && !b) return;
        wrong.hidden = !(a && b && a > b);
        if (!wrong.hidden) { fields[0].focus(); return; }
        choose('date', a + '..' + b);
      });
      menu.appendChild(form);
      Object.keys(DATES).forEach(function (d) { scroll.appendChild(option('date', d, el('span', 'n', DATES[d]))); });
    }

    function openMenu(k, btn) {
      if (menuOf === k) { closeMenu(true); return; }
      closeMenu(false);
      menuOf = k;
      menuAsked += 1;
      empty(menu);
      menu.setAttribute('aria-label', NAMES[k]);
      menu.classList.toggle('hx-menu--titles', k === 'title');
      menu.appendChild(el('div', 'hx-menu__h', [el('span', '', NAMES[k]), button('', icon('i-xmark'), { 'aria-label': 'Close', 'data-menu-close': '' })]));
      var find = null;
      if (k === 'title') {
        find = document.createElement('input');
        find.setAttribute('type', 'search');
        find.setAttribute('placeholder', 'Find a title');
        find.setAttribute('aria-label', 'Find a title');
        find.setAttribute('maxlength', '200');
        menu.appendChild(el('div', 'hx-menu__f', [find, button('hx-x', icon('i-xmark'), { 'aria-label': 'Clear' })]));
      }
      var scroll = el('div', 'hx-menu__scroll');
      scroll.setAttribute('role', 'menu');
      scroll.setAttribute('aria-label', NAMES[k]);
      menu.appendChild(scroll);
      if (k === 'user') userOptions().forEach(function (o) { scroll.appendChild(o); });
      if (k === 'action') actionOptions().forEach(function (o) { scroll.appendChild(o); });
      if (k === 'date') dateForm(scroll);
      if (k === 'user' || k === 'action') fillCounts(k, scroll);
      if (find) {
        titleMenu(scroll, find);
      }
      var r = btn.getBoundingClientRect();
      var left = Math.max(12, Math.min(r.left, document.documentElement.clientWidth - 312));
      menu.style.left = left + 'px';
      menu.style.top = r.bottom + 6 + 'px';
      menu.style.maxHeight = phone() ? '' : Math.min(440, window.innerHeight - r.bottom - 20) + 'px';
      if (menu.style.setProperty) menu.style.setProperty('--ox', r.left - left + 30 + 'px');
      btn.setAttribute('aria-expanded', 'true');
      menu.classList.add('on');
      veil.classList.add('on');
      var checked = MM.dom.findByAttr(menu, 'aria-checked', 'true');
      /* On a phone a field would bring up the keyboard over the sheet. */
      var first = phone() ? menu : find || checked || q('.hx-opt', menu);
      (first || menu).focus({ preventScroll: true });
    }

    function closeMenu(back) {
      if (!menuOf) return;
      var k = menuOf;
      menuOf = null;
      menu.classList.remove('on');
      veil.classList.remove('on');
      var b = MM.dom.findByAttr(chipsBox, 'data-menu', k);
      if (b) b.setAttribute('aria-expanded', 'false');
      if (back) focusChip(k);
    }

    function menuKeys(e) {
      if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return; }
      if (e.key === 'Tab') {
        var stops = MM.dom.qa('button, input', menu).filter(function (n) { return n.getClientRects().length; });
        var last = e.shiftKey ? stops[0] : stops[stops.length - 1];
        if (document.activeElement === last || document.activeElement === menu) {
          e.preventDefault();
          (e.shiftKey ? stops[stops.length - 1] : stops[0]).focus();
        }
        return;
      }
      var edge = e.key === 'Home' || e.key === 'End';
      var inField = e.target.tagName === 'INPUT';
      if (!edge && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      if (inField && (edge || e.target.getAttribute('type') === 'date')) return;
      var opts = MM.dom.qa('.hx-opt', menu).filter(function (o) { return !o.hidden; });
      if (!opts.length) return;
      e.preventDefault();
      if (edge) { opts[e.key === 'Home' ? 0 : opts.length - 1].focus(); return; }
      var down = e.key === 'ArrowDown';
      var i = opts.indexOf(document.activeElement);
      opts[i < 0 ? (down ? 0 : opts.length - 1) : (i + (down ? 1 : -1) + opts.length) % opts.length].focus();
    }

    /* --- opening and folding a row --- */

    function syncEvent(row) {
      var id = row.getAttribute('data-event');
      /* Only what is recorded has a number to link to; a sitting has not. */
      if (!/^\d+$/.test(id)) return;
      if (row.open) opened = id;
      else if (opened === id) opened = null;
      else return;
      go('replaceState');
    }

    /* Slide what follows the row, up to the page's end, along with its details. */
    function slideBelow(row, body, closing) {
      if (!body.animate || reduceMotion()) return [];
      var style = window.getComputedStyle(body);
      var timing = { duration: parseFloat(style.animationDuration) * 1000, easing: style.animationTimingFunction };
      var height = body.offsetHeight;
      var away = 'translateY(' + -height + 'px)';
      var below = [];
      for (var from = row; from && from !== root; from = from.parentNode) {
        for (var n = from.nextElementSibling; n; n = n.nextElementSibling) below.push(n);
      }
      var moves = below.map(function (node) {
        return node.animate({ transform: closing ? ['none', away] : [away, 'none'] },
          Object.assign({ fill: closing ? 'forwards' : 'none', composite: 'add' }, timing));
      });
      var card = row.closest('.hx-card');
      if (card && card.animate) {
        var whole = 'inset(0 0 0 0 round 12px)';
        var cut = 'inset(0 0 ' + height + 'px 0 round 12px)';
        moves.push(card.animate({ clipPath: closing ? [whole, cut] : [cut, whole] },
          Object.assign({ fill: closing ? 'forwards' : 'none' }, timing)));
      }
      return moves;
    }

    function fold(row) {
      var body = row.querySelector('.hist-body');
      var done = false;
      var slides = [];
      function finish() {
        if (done) return;
        done = true;
        row.classList.remove('is-closing');
        row.removeAttribute('open');
        slides.forEach(function (a) { a.cancel(); });
        syncEvent(row);
        near();
      }
      if (reduceMotion()) return finish();
      row.classList.add('is-closing');
      slides = slideBelow(row, body, true);
      body.addEventListener('animationend', finish, { once: true });
      window.setTimeout(finish, CLOSE_FALLBACK_MS);
    }

    function onCopy(btn) {
      var text = Array.prototype.map.call(btn.closest('.hist').querySelectorAll('.hist-field'), function (f) {
        var dd = f.querySelector('dd');
        var utc = dd.getAttribute('title');
        return f.querySelector('dt').textContent + ': ' + dd.textContent + (utc ? ' (' + utc + ')' : '');
      }).join('\n');
      copyText(text).then(function () {
        btn.focus();
        if (window.UIFeedback) window.UIFeedback.toast('Details copied', { variant: 'success' });
      }, function () {
        btn.focus();
        if (window.UIFeedback) window.UIFeedback.error('Could not copy. Select the text and copy it by hand.');
      });
    }

    function onSummary(e, row) {
      if (row.open && !row.classList.contains('is-closing')) {
        e.preventDefault();
        fold(row);
      } else if (row.classList.contains('is-closing')) {
        e.preventDefault();
      } else {
        /* Open by the next frame, and not yet painted. */
        window.requestAnimationFrame(function () {
          if (row.open) slideBelow(row, row.querySelector('.hist-body'), false);
        });
      }
    }

    /* --- wiring --- */

    root.addEventListener('click', function (e) {
      var b;
      if ((b = e.target.closest('.hist-sum'))) onSummary(e, b.parentNode);
      else if ((b = e.target.closest('[data-hist-copy]'))) onCopy(b);
      else if ((b = e.target.closest('[data-menu]'))) openMenu(b.getAttribute('data-menu'), b);
      else if ((b = e.target.closest('[data-clear]'))) {
        var k = b.getAttribute('data-clear');
        choose(k, '');
      } else if ((b = e.target.closest('[data-title]'))) choose('title', b.getAttribute('data-title'));
      else if ((b = e.target.closest('[data-user]'))) choose('user', b.getAttribute('data-user'));
      else if (e.target.closest('[data-hist-clear]')) clearAll();
      else if (e.target.closest('[data-hist-retry]')) retry();
      else if (e.target.closest('[data-hist-more]')) {
        var failing = q('.hx-fail', more);
        if (failing) {
          foot();
          q('[data-hist-more]', more).focus({ preventScroll: true });
        }
        loadMore(true);
      } else if (e.target.closest('.hx-x')) {
        input.value = '';
        searchNow();
        search.classList.remove('has-q');
        input.focus();
      }
    });

    menu.addEventListener('click', function (e) {
      var b = e.target.closest('.hx-opt');
      if (b) {
        var k = b.getAttribute('data-k');
        var v = b.getAttribute('data-v');
        choose(k, F[k] === v ? '' : v);
      } else if (e.target.closest('[data-menu-close]')) closeMenu(true);
    });
    menu.addEventListener('keydown', menuKeys);
    veil.addEventListener('click', function () { closeMenu(true); });

    list.addEventListener('toggle', function (e) {
      if (e.target.open) syncEvent(e.target);
    }, true);

    q('form.hx-line', root).addEventListener('submit', function (e) {
      e.preventDefault();
      searchNow();
    });
    input.addEventListener('input', function () {
      search.classList.toggle('has-q', !!input.value);
      window.clearTimeout(typed);
      typed = window.setTimeout(searchNow, TYPING_MS);
    });
    input.addEventListener('blur', function () { typing = false; });
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || !input.value) return;
      e.preventDefault();
      input.value = '';
      search.classList.remove('has-q');
      searchNow();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || menuOf || tools.hidden) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || document.querySelector('.profile-menu[open]')) return;
      e.preventDefault();
      input.focus();
    });

    window.addEventListener('scroll', function () {
      tools.classList.toggle('is-stuck', tools.getBoundingClientRect().top <= barTop());
      near();
    }, { passive: true });
    window.addEventListener('resize', function () {
      if (phone()) menu.style.maxHeight = '';
      else closeMenu(false);
      near();
    });
    window.addEventListener('popstate', function () {
      if (here() === at) return;
      at = here();
      window.clearTimeout(typed);
      typing = false;
      closeMenu(false);
      readAddress();
      input.value = F.q;
      chips();
      load(false);
    });

    at = here();
    var unread = readAddress();
    /* The server's words for what it filtered by, where the address has them. */
    ['q', 'user', 'action', 'title'].forEach(function (k) { F[k] = data.filters[k] || ''; });
    input.value = F.q;
    chips();
    if (unread) go('replaceState');
    if (F.date || unread) load(true);
    else {
      show({ items: data.items, cursor: data.cursor, total: data.total, page: data.page });
      tellEverything();
    }
  }

  function init() {
    var root = document.querySelector('[data-hist]');
    var island = document.getElementById('history-data');
    if (root && island) start(root, JSON.parse(island.textContent));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
