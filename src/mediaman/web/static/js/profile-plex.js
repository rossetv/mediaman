/** Plex account card of the Profile page. Requires: core/api.js (MM.api), core/dom.js (MM.dom),
 *  core/reauth.js (MM.reauth), ui-feedback.js (UIFeedback). */
(function () {
  'use strict';

  var POLL_MS = 2000;
  var SLOW_POLL_MS = 10000;
  var READING_MS = 3000;
  var APPROVAL_PAGE = 'https://app.plex.tv/auth#?';
  var RAN_OUT = 'The sign-in ran out. Start again.';
  var MOVES = 'This Plex account is linked to another person here; linking moves it to you.';
  var NOT_ANSWERING = 'Plex is slow to answer. Still waiting…';
  /* What focus moves to when a panel replaces the one that held it. */
  var MAIN = {
    unlinked: 'plex-start', waiting: 'plex-open', confirm: 'plex-confirm',
    failed: 'plex-retry', linked: 'plex-unlink',
  };
  var SAY = {
    waiting: 'Open Plex to sign in. Waiting for Plex.',
    confirm: 'Plex signed you in. Confirm it is you.',
    linked: 'Plex account linked.',
    unlinked: 'No Plex account linked.',
  };

  /* Set again after being empty, the same words are announced again. */
  function announce(el, text) {
    el.textContent = '';
    setTimeout(function () { el.textContent = text; }, 100);
  }

  /* A disabled button would lose the focus; one that only says so keeps it. */
  function busy(button, on) {
    if (on) button.setAttribute('aria-disabled', 'true');
    else button.removeAttribute('aria-disabled');
  }

  function isBusy(button) { return button.getAttribute('aria-disabled') === 'true'; }

  function byId(id) { return document.getElementById(id); }

  function when(iso) {
    return new Date(iso).toLocaleString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function initOwn() {
    var card = byId('plex-link');
    if (!card) return;
    var panels = MM.dom.qa('[data-plex-panel]', card);
    /* WebKit gives a clicked button's focus to the section around it, which can hold it. */
    var holder = byId('plex') || card;
    var errorLine = byId('plex-error');
    var current = 'loading';
    var attempt = null;
    /* Whether Try again loads the card again rather than starts a sign-in. */
    var reloads = false;
    var timer = null;
    /* Answers that arrive after the person has moved on are dropped. */
    var turn = 0;

    function stop() {
      turn += 1;
      clearTimeout(timer);
      timer = null;
    }

    function show(name, quiet) {
      var held = !quiet && MM.dom.focusStillIn(holder);
      var changed = name !== current;
      current = name;
      card.removeAttribute('aria-busy');
      errorLine.textContent = '';
      panels.forEach(function (p) { p.hidden = p.getAttribute('data-plex-panel') !== name; });
      if (!changed && name !== 'failed') return;
      if (!quiet) {
        announce(byId('plex-status'), name === 'failed' ? byId('plex-failed').textContent : SAY[name] || '');
      }
      if (held && MAIN[name]) byId(MAIN[name]).focus();
    }

    function fail(message, reload) {
      stop();
      reloads = !!reload;
      byId('plex-failed').textContent = message;
      show('failed');
    }

    function later(ms) {
      clearTimeout(timer);
      var mine = turn;
      timer = setTimeout(function () { if (mine === turn) check(); }, ms);
    }

    function check() {
      if (document.hidden) return;
      var mine = turn;
      MM.api.post('/api/account/plex/check').then(function (data) {
        if (mine !== turn) return;
        MM.dom.setText(byId('plex-wait'), 'Waiting for Plex…');
        render(data.attempt);
      }, function (err) {
        if (mine !== turn) return;
        if (err.status !== 429) MM.dom.setText(byId('plex-wait'), NOT_ANSWERING);
        later(SLOW_POLL_MS);
      });
    }

    /* Another tab may have linked, or started again, before this is called a failure. */
    function recover(message) {
      stop();
      var mine = turn;
      MM.api.get('/api/account/plex').then(function (data) {
        if (mine !== turn) return;
        showMoved(data.moved_at);
        if (data.link) showLink(data.link);
        else if (data.attempt) render(data.attempt);
        else fail(message);
      }, function () {
        if (mine === turn) fail(message, true);
      });
    }

    function render(next, quiet) {
      if (!next) { recover(RAN_OUT); return; }
      attempt = next;
      byId('plex-signout-note').hidden = attempt.signed_out;
      if (attempt.state === 'failed') {
        fail(attempt.message);
      } else if (attempt.state === 'confirm') {
        stop();
        byId('plex-found').textContent = attempt.name;
        byId('plex-moves').hidden = !attempt.moves;
        var held = !quiet && MM.dom.focusStillIn(holder);
        show('confirm', quiet);
        if (attempt.moves && !quiet) {
          announce(byId('plex-status'), SAY.confirm + ' ' + MOVES);
          /* Not on the button, where a key still held down would accept it unread. */
          if (held) byId('plex-moves').focus();
        }
      } else {
        if (attempt.url.indexOf(APPROVAL_PAGE) !== 0) {
          fail('Plex sent an unexpected address. Try again later.');
          return;
        }
        byId('plex-open').href = attempt.url;
        MM.dom.setText(byId('plex-app'), attempt.app);
        show('waiting', quiet);
        later(POLL_MS);
      }
    }

    /* Kept over every panel until the person dismisses it or links an account. */
    function showMoved(iso) {
      byId('plex-moved').hidden = !iso;
      if (iso) byId('plex-moved-when').textContent = when(iso);
    }

    function showLink(link, quiet) {
      byId('plex-name').textContent = link.name;
      byId('plex-since').textContent = when(link.linked_at);
      byId('plex-read').textContent = link.reading
        ? 'Reading your plays…'
        : link.last_read_at
          ? 'Plays last read ' + when(link.last_read_at) + '.'
          : 'Your plays will be read after the next scan.';
      show('linked', quiet);
      if (!link.reading) return;
      var mine = turn;
      timer = setTimeout(function () { if (mine === turn) load(true); }, READING_MS);
    }

    function load(quiet) {
      var mine = turn;
      return MM.api.get('/api/account/plex').then(function (data) {
        if (mine !== turn) return;
        showMoved(data.moved_at);
        if (data.link) showLink(data.link, quiet);
        else if (data.attempt) render(data.attempt, quiet);
        else show(data.configured ? 'unlinked' : 'unset', quiet);
      }, function (err) {
        if (mine !== turn) return;
        if (quiet && current === 'linked') {
          /* What is shown still holds; only the news of the read is late. */
          timer = setTimeout(function () { if (mine === turn) load(true); }, SLOW_POLL_MS);
          return;
        }
        if (quiet && current !== 'loading') {
          if (current === 'waiting') later(SLOW_POLL_MS);
          return;
        }
        fail(MM.api.messageOf(err, 'Could not load your Plex link.'), true);
      });
    }

    function begin(button) {
      stop();
      var mine = turn;
      errorLine.textContent = '';
      byId('plex-signout-note').hidden = true;
      busy(button, true);
      MM.reauth.run(function () { return MM.api.post('/api/account/plex'); }).then(function (data) {
        busy(button, false);
        if (mine === turn) render(data.attempt);
      }, function (err) {
        busy(button, false);
        if (mine !== turn || err.error === 'reauth_cancelled') return;
        if (err.error === 'already_linked') { recover(err.message); return; }
        if (err.error === 'plex_not_set_up') { show('unset'); return; }
        fail(MM.api.messageOf(err, 'Could not start. Try again.'));
      });
    }

    function cancel() {
      stop();
      show('unlinked');
      /* Named, so that arriving late it cannot end a sign-in started since. The sign-in ends by itself when this does not arrive. */
      MM.api.post('/api/account/plex/cancel', { attempt: attempt.id }).then(function (data) {
        if (!data.signed_out) byId('plex-signout-note').hidden = false;
      }, function () {});
    }

    /* A key held down presses whatever button the focus has moved to. */
    card.addEventListener('keydown', function (e) {
      if (e.repeat && (e.key === 'Enter' || e.key === ' ')) e.preventDefault();
    });

    byId('plex-moved-dismiss').addEventListener('click', function () {
      var button = this;
      if (isBusy(button)) return;
      errorLine.textContent = '';
      busy(button, true);
      MM.api.delete('/api/account/plex?notice=true').then(function () {
        busy(button, false);
        var held = MM.dom.focusStillIn(holder);
        showMoved(null);
        if (held && MAIN[current]) byId(MAIN[current]).focus();
      }, function (err) {
        busy(button, false);
        errorLine.textContent = MM.api.messageOf(err, 'Could not dismiss it. Try again.');
      });
    });

    byId('plex-start').addEventListener('click', function () {
      if (!isBusy(this)) begin(this);
    });
    byId('plex-retry').addEventListener('click', function () {
      if (isBusy(this)) return;
      if (reloads) load(); else begin(this);
    });
    byId('plex-cancel').addEventListener('click', cancel);
    byId('plex-decline').addEventListener('click', cancel);

    byId('plex-confirm').addEventListener('click', function () {
      var button = this;
      if (isBusy(button)) return;
      stop();
      var mine = turn;
      busy(button, true);
      MM.api.post('/api/account/plex', { attempt: attempt.id, move: !!attempt.moves }).then(function (data) {
        busy(button, false);
        if (mine !== turn) return;
        if (data.attempt) render(data.attempt); else load();
      }, function (err) {
        busy(button, false);
        if (mine !== turn) return;
        var message = MM.api.messageOf(err, 'Could not link it. Start again.');
        if (err.error === 'expired' || err.error === 'already_linked') recover(message);
        else fail(message);
      });
    });

    byId('plex-unlink').addEventListener('click', function () {
      var button = this;
      if (isBusy(button)) return;
      window.UIFeedback.confirm({
        title: 'Unlink your Plex account?',
        body: 'The plays copied here and the recommendations made from them are deleted. Nothing changes in Plex.',
        confirmLabel: 'Unlink',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (!ok) return;
        stop();
        var mine = turn;
        busy(button, true);
        MM.api.delete('/api/account/plex').then(function () {
          busy(button, false);
          if (mine === turn) load();
        }, function (err) {
          busy(button, false);
          errorLine.textContent = MM.api.messageOf(err, 'Could not unlink. Try again.');
        });
      });
    });

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && current === 'waiting') check();
    });
    /* Restored from the back/forward cache, the card may be long out of date. */
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) { stop(); load(true); }
    });

    load(true);
  }

  function initOthers() {
    var box = byId('plex-others');
    if (!box) return;

    function note(text) {
      box.textContent = '';
      var p = document.createElement('p');
      p.className = 'plex-link-fine';
      p.textContent = text;
      box.appendChild(p);
    }

    function row(link) {
      var el = document.createElement('div');
      el.className = 'usr-row';
      var meta = document.createElement('div');
      meta.className = 'usr-meta';
      var name = document.createElement('div');
      name.className = 'usr-name';
      name.textContent = link.username;
      var since = document.createElement('div');
      since.className = 'usr-sub';
      since.textContent = 'Linked ' + when(link.linked_at);
      meta.appendChild(name);
      meta.appendChild(since);
      var actions = document.createElement('div');
      actions.className = 'usr-actions';
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'link-danger';
      button.setAttribute('data-user-id', link.id);
      button.textContent = 'Unlink';
      button.setAttribute('aria-label', 'Unlink the Plex account of ' + link.username);
      button.addEventListener('click', function () { unlink(link, button); });
      actions.appendChild(button);
      el.appendChild(meta);
      el.appendChild(actions);
      return el;
    }

    function unlink(link, button) {
      if (isBusy(button)) return;
      window.UIFeedback.confirm({
        title: 'Unlink ' + link.username + '’s Plex account?',
        body: 'The plays copied here and the recommendations made from them are deleted. They can link again.',
        confirmLabel: 'Unlink',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (!ok) return;
        busy(button, true);
        var rows = MM.dom.qa('.link-danger', box);
        var next = rows[rows.indexOf(button) + 1];
        function gone() {
          var held = MM.dom.focusStillIn(box);
          return load().then(function () {
            announce(byId('plex-others-status'), 'Unlinked ' + link.username + '.');
            if (!held) return;
            var id = next && next.getAttribute('data-user-id');
            var to = MM.dom.qa('.link-danger', box).filter(function (b) {
              return b.getAttribute('data-user-id') === id;
            })[0];
            (to || box).focus();
          });
        }
        MM.api.delete('/api/users/' + link.id + '/plex-link').then(gone, function (err) {
          if (err.status === 404) return gone();
          busy(button, false);
          window.UIFeedback.error(MM.api.messageOf(err, 'Could not unlink. Try again.'));
        });
      });
    }

    function load() {
      return MM.api.get('/api/users/plex-links').then(function (data) {
        box.textContent = '';
        data.links.forEach(function (link) { box.appendChild(row(link)); });
        if (!data.links.length) note('Nobody else has linked a Plex account.');
      }, function () {
        note('Could not load the linked accounts. Reload the page to try again.');
      }).then(function () { box.removeAttribute('aria-busy'); });
    }

    load();
  }

  function init() {
    initOwn();
    initOthers();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
