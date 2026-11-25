/** Plex account card of the Profile page. Requires: core/api.js (MM.api), core/dom.js (MM.dom),
 *  core/reauth.js (MM.reauth), ui-feedback.js (UIFeedback). */
(function () {
  'use strict';

  var POLL_MS = 2000;
  var SLOW_POLL_MS = 10000;
  var READING_MS = 3000;
  var APPROVAL_PAGE = 'https://app.plex.tv/auth#?';
  var MOVES = 'This Plex account is linked to another person here; linking moves it to you.';
  var APPROVE = 'Approve mediaman in the Plex tab. This page updates by itself.';
  var BLOCKED = 'Your browser did not open the Plex tab. Open Plex here and approve mediaman. This page updates by itself.';
  var CLOSED = 'The tab was closed before Plex opened. Open Plex here and approve mediaman. This page updates by itself.';
  var WAIT = 'Waiting for Plex…';
  var STILL_WAIT = 'Not approved yet. If you closed the Plex tab, open Plex again.';
  var CANCELLED = 'Cancelled, not linked.';
  var NOT_ANSWERING = 'Plex is slow to answer. Still waiting…';
  var ASK = {
    title: 'Confirm it’s you',
    descText: 'Enter your Mediaman password to link a Plex account.',
    confirmLabel: 'Continue',
  };
  /* What focus moves to when a panel replaces the one that held it. */
  var MAIN = {
    unlinked: 'plex-start', waiting: 'plex-open', confirm: 'plex-confirm',
    failed: 'plex-retry', linked: 'plex-unlink',
  };
  var SAY = {
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

  /* Opened by the press itself, before the server has answered, or a pop-up blocker stops it. */
  function openTab() {
    var tab = window.open('', '_blank');
    if (!tab) return null;
    tab.opener = null;
    var doc = tab.document;
    doc.title = 'Opening Plex…';
    doc.documentElement.style.colorScheme = 'dark';
    doc.body.style.font = '16px system-ui, sans-serif';
    doc.body.style.margin = '24px';
    doc.body.textContent = 'Opening Plex…';
    return tab;
  }

  function sendTab(tab, url) {
    if (tab.closed) return false;
    try {
      /* A link, as setting its location would tell Plex the address of this page. */
      var link = tab.document.createElement('a');
      link.rel = 'noreferrer';
      link.href = url;
      tab.document.body.appendChild(link);
      link.click();
      return true;
    } catch (e) {
      /* The person took the tab somewhere else. */
      return false;
    }
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
    var movedAt = null;
    /* Whether Try again loads the card again rather than starts a sign-in. */
    var reloads = false;
    var timer = null;
    /* Open and not yet sent to Plex. */
    var tab = null;
    /* The one sent there; where the browser keeps the two apart it can no longer be closed from here. */
    var sentTab = null;
    /* The start that has not been answered. */
    var underway = null;
    var checking = null;
    var waitWords = WAIT;
    var justLinked = false;
    /* Answers that arrive after the person has moved on are dropped. */
    var turn = 0;

    function stop() {
      turn += 1;
      clearTimeout(timer);
      timer = null;
      checking = null;
      closeTab();
    }

    function closeTab() {
      if (tab) tab.close();
      tab = null;
    }

    function opening(button, on) {
      var label = button.textContent;
      busy(button, on);
      button.textContent = '';
      if (on) {
        underway = { button: button, label: label, cancelled: false };
        var spinner = document.createElement('span');
        spinner.className = 'btn-spinner';
        spinner.setAttribute('aria-hidden', 'true');
        button.appendChild(spinner);
        button.appendChild(document.createTextNode('Opening Plex…'));
      } else {
        button.textContent = underway.label;
        underway = null;
      }
      byId(button.id + '-stop').hidden = !on;
    }

    function sayCancelled() {
      show('unlinked');
      byId('plex-note').textContent = CANCELLED;
      announce(byId('plex-status'), CANCELLED);
    }

    function offer(words, again) {
      var open = byId('plex-open');
      byId('plex-lead').textContent = words;
      open.textContent = again ? 'Open Plex again' : 'Open Plex';
      open.classList.toggle('btn--primary', !again);
      open.classList.toggle('btn--secondary', again);
    }

    function show(name, quiet) {
      var held = !quiet && MM.dom.focusStillIn(holder);
      var changed = name !== current;
      current = name;
      card.removeAttribute('aria-busy');
      errorLine.textContent = '';
      byId('plex-note').textContent = '';
      panels.forEach(function (p) { p.hidden = p.getAttribute('data-plex-panel') !== name; });
      if (!changed && name !== 'failed') return;
      if (!quiet) {
        announce(byId('plex-status'),
          name === 'failed' ? byId('plex-failed').textContent
            : name === 'waiting' ? byId('plex-lead').textContent
              : name === 'linked' && justLinked ? 'Your Plex account ' + byId('plex-done-name').textContent + ' is linked.'
                : SAY[name] || '');
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
      clearTimeout(timer);
      /* The last one asked is the one heard. */
      var mine = checking = {};
      MM.api.post('/api/account/plex/check').then(function (data) {
        if (mine !== checking) return;
        checking = null;
        MM.dom.setText(byId('plex-wait'), waitWords);
        if (data.attempt) render(data.attempt); else recover(data.message);
      }, function (err) {
        if (mine !== checking) return;
        checking = null;
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
        if (current !== 'waiting') {
          waitWords = WAIT;
          byId('plex-wait').textContent = WAIT;
          offer(APPROVE, true);
          if (underway) {
            sentTab = tab;
            tab = null;
            if (!sentTab) offer(BLOCKED, false);
            else if (!sendTab(sentTab, attempt.url)) offer(CLOSED, false);
          }
        }
        MM.dom.setText(byId('plex-app'), attempt.app);
        show('waiting', quiet);
        later(POLL_MS);
      }
    }

    /* Kept over every panel until the person dismisses it or links an account. */
    function showMoved(iso) {
      movedAt = iso;
      byId('plex-moved').hidden = !iso;
      if (iso) byId('plex-moved-when').textContent = when(iso);
    }

    function showLink(link, quiet) {
      byId('plex-name').textContent = link.name;
      byId('plex-done-name').textContent = link.name;
      byId('plex-done').hidden = !justLinked;
      byId('plex-linked').hidden = justLinked;
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

    /* The press that gives the password may open the tab; the answer to it may not. */
    function withPassword() {
      return new Promise(function (resolve, reject) {
        var cancelled = false;
        MM.reauth.openModal({
          title: ASK.title,
          descText: ASK.descText,
          confirmLabel: ASK.confirmLabel,
          onSubmit: function (pw) {
            closeTab();
            tab = openTab();
            return MM.api.post('/api/auth/reauth', { password: pw }).then(function () {
              if (!cancelled) resolve(MM.api.post('/api/account/plex'));
              return { ok: true };
            }, function (err) {
              if (!cancelled) closeTab();
              return { ok: false, error: MM.api.messageOf(err, 'Could not check the password. Try again.') };
            });
          },
          onCancel: function () {
            cancelled = true;
            reject({ error: 'reauth_cancelled' });
          },
        });
      });
    }

    function begin(button) {
      if (underway) return;
      stop();
      var mine = turn;
      errorLine.textContent = '';
      byId('plex-note').textContent = '';
      byId('plex-signout-note').hidden = true;
      justLinked = false;
      tab = openTab();
      opening(button, true);
      var start = underway;
      function settle() { if (underway === start) opening(button, false); }
      MM.api.post('/api/account/plex').then(null, function (err) {
        if (mine !== turn || !(err.status === 403 && err.data && err.data.reauth_required)) throw err;
        closeTab();
        return withPassword();
      }).then(function (data) {
        if (mine === turn) render(data.attempt);
        else if (start.cancelled) giveUp(data.attempt.id);
        settle();
      }, function (err) {
        settle();
        if (mine !== turn) return;
        stop();
        if (err.error === 'reauth_cancelled') { sayCancelled(); return; }
        if (err.error === 'already_linked') { recover(err.message); return; }
        if (err.error === 'plex_not_set_up') { show('unset'); return; }
        fail(MM.api.messageOf(err, 'Could not start. Try again.'));
      });
    }

    /* Named, so that arriving late it cannot end a sign-in started since. The sign-in ends by itself when this does not arrive. */
    function giveUp(id) {
      MM.api.post('/api/account/plex/cancel', { attempt: id }).then(function (data) {
        if (!data.signed_out) byId('plex-signout-note').hidden = false;
      }, function () {});
    }

    function cancel() {
      stop();
      if (sentTab) sentTab.close();
      sentTab = null;
      sayCancelled();
      giveUp(attempt.id);
    }

    function cancelOpening() {
      if (!underway) return;
      var button = underway.button;
      underway.cancelled = true;
      stop();
      var held = MM.dom.focusStillIn(holder);
      opening(button, false);
      sayCancelled();
      if (held) byId('plex-start').focus();
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
      MM.api.delete('/api/account/plex/notice?moved_at=' + encodeURIComponent(movedAt)).then(function (data) {
        busy(button, false);
        var held = MM.dom.focusStillIn(holder);
        showMoved(data.moved_at);
        if (held && !data.moved_at) (byId(MAIN[current]) || holder).focus();
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
    byId('plex-start-stop').addEventListener('click', cancelOpening);
    byId('plex-retry-stop').addEventListener('click', cancelOpening);
    byId('plex-open').addEventListener('click', function () { offer(APPROVE, true); });
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
        if (data.attempt) { render(data.attempt); return; }
        justLinked = true;
        load();
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
      if (document.hidden || current !== 'waiting') return;
      waitWords = STILL_WAIT;
      check();
    });
    /* A Plex window beside this one hides nothing. */
    window.addEventListener('focus', function () {
      if (current === 'waiting' && !checking) check();
    });
    /* Restored from the back/forward cache, the card may be long out of date. */
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) { stop(); justLinked = false; load(true); }
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
