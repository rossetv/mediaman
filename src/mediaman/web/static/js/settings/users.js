/**
 * settings/users.js — Users settings module.
 *
 * Responsibilities:
 *   - User list rendering (GET /api/users), with adds, role changes and
 *     deletes applied to the rows in place
 *   - Add user form (#add-user-form, #btn-add-user) with its Viewer/Admin switch
 *   - Role change per user (PATCH /api/users/{id}/role + MM.reauth.run)
 *   - Email per user, in a form in their row (PATCH /api/users/{id}/email + MM.reauth.run)
 *   - Plex link state per user, with Unlink (DELETE /api/users/{id}/plex-link)
 *   - Delete user flow (UIFeedback.confirm + MM.reauth.run)
 *
 * Cross-module dependencies:
 *   MM.api     (core/api.js)
 *   MM.dom     (core/dom.js)
 *   MM.reauth  (core/reauth.js)
 *   MM.settings.rows (settings/rows.js)
 *
 * Exposes:
 *   MM.settings.users.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  /**
   * Say why something was refused in an element, with the per-rule issues of
   * the server's password policy, when there are some, as a list.
   */
  function showReason(target, text, issues) {
    target.textContent = '';
    if (issues && issues.length) {
      var p = document.createElement('div');
      p.textContent = text;
      target.appendChild(p);
      var ul = document.createElement('ul');
      ul.className = 'inline-form-issues';
      issues.forEach(function (item) {
        var li = document.createElement('li');
        li.textContent = String(item);
        ul.appendChild(li);
      });
      target.appendChild(ul);
    } else {
      target.textContent = text;
    }
  }

  // The dialog's fade out (--dur is 200 ms), with a little to spare.
  var REAUTH_FADE_MS = 220;
  // How long a changed role's button, which now undoes the change, ignores a press meant for the dialog.
  var ROLE_QUIET_MS = 1000;

  MM.settings.users = {

    init: function () {

      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger-text)';
        el.textContent = text;
        return el;
      }

      var rows = MM.settings.rows;
      var holdFocus = rows.holdFocus;
      var userList = document.getElementById('user-list');
      var currentUser = '';

      function loadUsers() {
        return MM.api.get('/api/users')
          .then(function (data) {
            currentUser = data.current;
            return data.users || [];
          });
      }

      function showUsers() {
        loadUsers()
          .then(function (users) {
            if (!userList) return;
            userList.replaceChildren();
            if (!users.length) { userList.appendChild(makeMsg('No users yet.')); return; }
            users.forEach(function (user) { userList.appendChild(buildRow(user)); });
          })
          .catch(function () {
            if (userList) { userList.replaceChildren(); userList.appendChild(makeMsg("Couldn't load users.", 'err')); }
          });
      }

      // A new user is added to the list as it stands, so focus and scroll stay put.
      function appendNewUsers(users) {
        if (!userList) return;
        // Each new row goes right after the row the server lists before it.
        var prev = null;
        users.forEach(function (user) {
          var existing = userList.querySelector('.usr-row[data-user-id="' + user.id + '"]');
          if (existing) { prev = existing; return; }
          Array.prototype.forEach.call(userList.querySelectorAll('.fld-sub'), function (m) { m.remove(); });
          var row = buildRow(user);
          userList.insertBefore(row, prev ? prev.nextSibling : userList.firstChild);
          prev = row;
          MM.settings.rows.enter(row);
        });
      }

      function avatar(user) {
        var el = document.createElement('span');
        el.className = 'avatar avatar--pic';
        el.setAttribute('aria-hidden', 'true');
        var img = document.createElement('img');
        img.alt = '';
        img.src = user.avatar_url;
        img.setAttribute('data-broken-on-error', '');
        el.appendChild(img);
        return el;
      }

      function buildRow(user) {
        var row = document.createElement('div');
        row.className = 'usr-row';
        row.dataset.userId = user.id;

        row.appendChild(avatar(user));

        var meta = document.createElement('div');
        meta.className = 'usr-meta';
        var name = document.createElement('div');
        name.className = 'usr-name';
        name.textContent = user.username;
        if (user.username === currentUser) {
          var you = document.createElement('span');
          you.className = 'you-pill';
          you.textContent = 'You';
          name.appendChild(document.createTextNode(' '));
          name.appendChild(you);
        }
        meta.appendChild(name);
        var sub = document.createElement('div');
        sub.className = 'usr-sub';
        paintSub(user, sub);
        meta.appendChild(sub);
        if (user.plex_linked_at) meta.appendChild(buildPlex(user, row));
        row.appendChild(meta);

        var roleLabel = document.createElement('span');
        row.appendChild(roleLabel);

        if (user.username !== currentUser) {
          var actions = document.createElement('div');
          actions.className = 'usr-actions';

          var emailBtn = document.createElement('button');
          emailBtn.type = 'button';
          emailBtn.className = 'btn btn--ghost btn--sm';
          emailBtn.dataset.userAction = 'email';
          emailBtn.textContent = 'Email';
          emailBtn.setAttribute('aria-label', 'Email: ' + user.username);
          actions.appendChild(emailBtn);

          var roleBtn = document.createElement('button');
          roleBtn.type = 'button';
          roleBtn.className = 'btn btn--ghost btn--sm';
          roleBtn.dataset.userAction = 'role';
          // A held Enter must not change the role back and forth.
          roleBtn.addEventListener('keydown', function (e) { if (e.repeat && e.key === 'Enter') e.preventDefault(); });
          roleBtn.addEventListener('click', function () {
            changeRoleFlow(user, isAdmin(user) ? 'viewer' : 'admin', roleBtn, function () {
              paintRole(user, roleLabel, roleBtn);
              roleLabel.classList.add('is-changed');
            });
          });
          actions.appendChild(roleBtn);

          var del = document.createElement('button');
          del.type = 'button';
          del.className = 'link-danger';
          del.dataset.userAction = 'delete';
          del.textContent = 'Delete';
          del.setAttribute('aria-label', 'Delete: ' + user.username);
          del.addEventListener('click', function () { deleteUserFlow(user, row, del); });
          actions.appendChild(del);

          row.appendChild(actions);
          row.appendChild(buildEmailForm(user, emailBtn, sub));
        }
        paintRole(user, roleLabel, roleBtn);
        roleLabel.addEventListener('animationend', function () { roleLabel.classList.remove('is-changed'); });
        return row;
      }

      function isAdmin(user) { return user.role !== 'viewer'; }

      function paintSub(user, sub) {
        var emailPart = user.email ? user.email : 'No email';
        var joined = new Date(user.created_at || NaN);
        sub.textContent = isNaN(joined) ? emailPart : emailPart + ' · joined ' + dayOf(joined);
      }

      function dayOf(date) {
        return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
      }

      // Your own account is unlinked on Profile, so it shows the state alone.
      function buildPlex(user, row) {
        var own = user.username === currentUser;
        var linked = new Date(user.plex_linked_at);
        var el = document.createElement('div');
        el.className = 'usr-plex';
        var state = document.createElement('span');
        state.appendChild(MM.dom.icon('i-plug'));
        state.appendChild(document.createTextNode(
          own || isNaN(linked) ? 'Plex linked' : 'Plex linked ' + dayOf(linked)));
        el.appendChild(state);
        if (own) return el;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'link-danger';
        btn.dataset.userAction = 'unlink';
        btn.textContent = 'Unlink';
        btn.setAttribute('aria-label', 'Unlink the Plex account of ' + user.username);
        btn.addEventListener('click', function () { unlinkFlow(user, row, el, btn); });
        el.appendChild(btn);
        return el;
      }

      function unlinkFlow(user, row, el, btn) {
        if (isBusy(btn)) return;
        holdFocus(btn);
        window.UIFeedback.confirm({
          title: 'Unlink ' + user.username + '’s Plex account?',
          body: 'The plays copied here and the recommendations made from them are deleted. They can link again.',
          confirmLabel: 'Unlink',
          confirmVariant: 'danger',
        }).then(function (ok) {
          if (!ok) return;
          setBusy(btn, true);
          function gone() {
            rows.leave(el, { fallback: row.querySelector('[data-user-action="email"]') });
            window.UIFeedback.success('Unlinked ' + user.username + '’s Plex account.');
          }
          MM.api.delete('/api/users/' + user.id + '/plex-link').then(gone, function (err) {
            // Already unlinked elsewhere.
            if (err && err.status === 404) { gone(); return; }
            var there = rows.stillAt(btn, btn);
            setBusy(btn, false);
            if (there) btn.focus({ preventScroll: true });
            window.UIFeedback.error(MM.api.messageOf(err, 'Could not unlink. Try again.'));
          });
        });
      }

      function buildEmailForm(user, opener, sub) {
        function make(tag, cls, text) {
          var el = document.createElement(tag);
          el.className = cls;
          if (text) el.textContent = text;
          return el;
        }
        function button(type, cls, text) {
          var el = make('button', 'btn btn--sm ' + cls, text);
          el.type = type;
          return el;
        }

        var form = make('form', 'inline-form setg-fold usr-email');
        form.id = 'user-email-' + user.id;
        form.noValidate = true;
        form.hidden = true;
        var inner = make('div', 'setg-fold-inner');
        var field = make('label', 'fld');
        field.appendChild(make('span', 'fld-lbl', 'Notification email'));
        var input = make('input', 'inp');
        input.type = 'email';
        input.autocomplete = 'off';
        input.setAttribute('inputmode', 'email');
        input.setAttribute('spellcheck', 'false');
        input.maxLength = 320;
        field.appendChild(input);
        field.appendChild(make('span', 'fld-sub', 'Download alerts for ' + user.username + ' go here. Leave empty to turn them off.'));
        inner.appendChild(field);
        var actions = make('div', 'inline-form-actions');
        var cancel = button('button', 'btn--ghost', 'Cancel');
        var save = button('submit', 'btn--primary', 'Save email');
        var msg = make('span', 'inline-form-msg');
        msg.setAttribute('role', 'status');
        actions.appendChild(cancel);
        actions.appendChild(save);
        actions.appendChild(msg);
        inner.appendChild(actions);
        form.appendChild(inner);

        function say(text, tone) {
          msg.className = 'inline-form-msg' + (tone ? ' ' + tone : '');
          msg.textContent = text;
        }
        function toggle(open) {
          opener.setAttribute('aria-expanded', open ? 'true' : 'false');
          if (open) {
            input.value = user.email || '';
            say('');
            rows.openInView(form);
            input.focus();
            return;
          }
          if (MM.dom.focusStillIn(form)) opener.focus();
          rows.close(form);
        }

        opener.setAttribute('aria-expanded', 'false');
        opener.setAttribute('aria-controls', form.id);
        opener.addEventListener('click', function () {
          if (isBusy(save)) return;
          holdFocus(opener);
          toggle(!rows.isOpen(form));
        });
        cancel.addEventListener('click', function () {
          if (isBusy(save)) return;
          holdFocus(cancel, form);
          toggle(false);
        });
        form.addEventListener('input', function () { if (!isBusy(save)) say(''); });
        form.addEventListener('submit', function (e) {
          e.preventDefault();
          if (isBusy(save)) return;
          var email = input.value.trim();
          holdFocus(save, form);
          setBusy(save, true);
          say('Saving…');
          rows.reveal(msg);
          MM.reauth.run(function () {
            return MM.api.patch('/api/users/' + user.id + '/email', { email: email });
          }).then(function (res) {
            setBusy(save, false);
            user.email = res.email;
            paintSub(user, sub);
            say('');
            toggle(false);
            window.UIFeedback.success(user.username + (user.email ? "'s email is now " + user.email : ' has no email now'));
          }).catch(function (err) {
            setBusy(save, false);
            if (err && err.error === 'reauth_cancelled') { say(''); return; }
            var there = rows.stillAt(msg, form);
            say(MM.api.messageOf(err, 'Could not save. Try again.'), 'err');
            if (there) MM.dom.refuse(input, msg);
          });
        });
        return form;
      }

      // A role change updates the row where it is, so focus stays on its button.
      function paintRole(user, pill, btn) {
        var admin = isAdmin(user);
        pill.className = 'pill ' + (admin ? 'pill--tv' : 'pill--neutral');
        pill.textContent = admin ? 'Admin' : 'Viewer';
        if (!btn) return;
        btn.textContent = admin ? 'Make viewer' : 'Make admin';
        btn.setAttribute('aria-label', btn.textContent + ': ' + user.username);
      }

      // Not `disabled`: that would drop the focus, which the password dialog returns to its button.
      function isBusy(btn) { return btn.getAttribute('aria-disabled') === 'true'; }
      function setBusy(btn, busy) {
        if (busy) btn.setAttribute('aria-disabled', 'true'); else btn.removeAttribute('aria-disabled');
      }

      function changeRoleFlow(user, role, btn, changed) {
        if (isBusy(btn)) return;
        var label = btn.textContent;
        holdFocus(btn);
        setBusy(btn, true);
        btn.textContent = 'Saving…';
        btn.setAttribute('aria-label', 'Saving…: ' + user.username);
        MM.reauth.run(function () {
          return MM.api.patch('/api/users/' + user.id + '/role', { role: role });
        }).then(function () {
          // The reauth dialog is still fading out; the new pill waits until it has gone.
          var behind = document.querySelector('.reauth-backdrop');
          function apply() {
            var there = rows.stillAt(btn, btn);
            user.role = role;
            changed();
            setTimeout(function () { setBusy(btn, false); }, ROLE_QUIET_MS);
            if (there) btn.focus({ preventScroll: true });
          }
          if (behind) setTimeout(apply, REAUTH_FADE_MS); else apply();
          window.UIFeedback.success(user.username + ' is now ' + (role === 'admin' ? 'an admin' : 'a viewer'));
        }).catch(function (err) {
          var there = rows.stillAt(btn, btn);
          setBusy(btn, false);
          btn.textContent = label;
          btn.setAttribute('aria-label', label + ': ' + user.username);
          if (there) btn.focus({ preventScroll: true });
          if (err && err.error === 'reauth_cancelled') return;
          window.UIFeedback.error(MM.api.messageOf(err, 'Could not change role'));
        });
      }

      // Delete user
      //
      // Two-step UX: a destructive-confirm dialog, then the centred
      // reauth modal opened transparently by MM.reauth.run when the
      // DELETE returns 403 reauth_required.
      function deleteUserFlow(user, row, btn) {
        if (isBusy(btn)) return;
        holdFocus(btn);
        window.UIFeedback.confirm({
          title: 'Delete ' + user.username + '?',
          body: 'Irreversible. All active sessions for this user will be terminated.',
          confirmLabel: 'Delete user',
          confirmVariant: 'danger',
        }).then(function (ok) {
          if (!ok) return;
          setBusy(btn, true);
          btn.textContent = 'Deleting…';
          btn.setAttribute('aria-label', 'Deleting…: ' + user.username);
          MM.reauth.run(function () {
            return MM.api.delete('/api/users/' + user.id);
          }, {
            title: 'Re-authenticate to delete ' + user.username,
            descText: 'mediaman asks for your own password before it deletes a user.',
            confirmLabel: 'Confirm & delete',
            confirmVariant: 'danger',
          }).then(function () {
            MM.settings.rows.leave(row, {
              focusSelector: '[data-user-action="delete"]',
              fallback: btnAddUser,
            });
            window.UIFeedback.success('Deleted ' + user.username);
          }).catch(function (err) {
            var there = rows.stillAt(btn, btn);
            setBusy(btn, false);
            btn.textContent = 'Delete';
            btn.setAttribute('aria-label', 'Delete: ' + user.username);
            if (there) btn.focus({ preventScroll: true });
            if (err && err.error === 'reauth_cancelled') return;
            window.UIFeedback.error(
              MM.api.messageOf(err, 'Could not delete user')
            );
          });
        });
      }

      function clearMsg(target) {
        if (!target) return;
        target.textContent = '';
        target.className = 'inline-form-msg';
      }

      function clearReasons(form) {
        form.querySelectorAll('.fld-why').forEach(function (why) { why.textContent = ''; });
      }

      // A message about what was typed goes once the person types again; a
      // form that is sending keeps its progress line.
      function clearOnInput(form, target, submitBtn) {
        if (!form) return;
        form.addEventListener('input', function () {
          if (submitBtn && isBusy(submitBtn)) return;
          if (target && target.textContent) clearMsg(target);
          clearReasons(form);
        });
      }

      var addUserForm      = document.getElementById('add-user-form');
      var btnAddUser       = document.getElementById('btn-add-user');
      var btnCancelAddUser = document.getElementById('btn-cancel-add-user');
      var btnSubmitAddUser = document.getElementById('btn-submit-add-user');
      var createResult     = document.getElementById('create-user-result');

      // On a narrow phone the form is taller than the screen, so the reason is said beside its field.
      function failOn(field, text, issues, onlySay) {
        clearMsg(createResult);
        clearReasons(addUserForm);
        var why = document.getElementById(field.id + '-why');
        if (!why) { why = createResult; why.className = 'inline-form-msg err'; }
        showReason(why, text, issues);
        if (!onlySay) MM.dom.refuse(field, why);
      }

      var roleSwitch = document.getElementById('new-user-role');
      function selectedRole() {
        var on = roleSwitch && roleSwitch.querySelector('.fpill.on');
        return on ? on.dataset.role : 'viewer';
      }
      function selectRole(role) {
        if (!roleSwitch) return;
        roleSwitch.querySelectorAll('.fpill').forEach(function (b) {
          var active = b.dataset.role === role;
          b.classList.toggle('on', active);
          b.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
      }
      if (roleSwitch) roleSwitch.addEventListener('click', function (e) {
        var b = e.target.closest('.fpill');
        if (b) selectRole(b.dataset.role);
      });

      function paintAddToggle(open) {
        if (btnAddUser) btnAddUser.setAttribute('aria-expanded', open ? 'true' : 'false');
      }
      if (btnAddUser && addUserForm) {
        btnAddUser.setAttribute('aria-controls', addUserForm.id);
        paintAddToggle(false);
      }

      // Closing folds the form away first and resets it once it is out of sight.
      function toggleAddUser(open, closed) {
        if (!addUserForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : !rows.isOpen(addUserForm);
        paintAddToggle(wantOpen);
        if (wantOpen) {
          rows.openInView(addUserForm);
          document.getElementById('new-username').focus();
          return;
        }
        rows.close(addUserForm, function () {
          addUserForm.reset();
          selectRole('viewer');
          clearMsg(createResult);
          clearReasons(addUserForm);
          if (closed) closed();
        });
      }
      // A form that is sending cannot be closed or reset from its own buttons.
      if (btnAddUser)       btnAddUser.addEventListener('click', function () {
        if (btnSubmitAddUser && isBusy(btnSubmitAddUser)) return;
        holdFocus(btnAddUser);
        toggleAddUser();
      });
      if (btnCancelAddUser) btnCancelAddUser.addEventListener('click', function () {
        if (btnSubmitAddUser && isBusy(btnSubmitAddUser)) return;
        toggleAddUser(false);
        if (btnAddUser) btnAddUser.focus();
      });

      clearOnInput(addUserForm, createResult, btnSubmitAddUser);
      if (addUserForm) addUserForm.addEventListener('submit', function (e) {
        e.preventDefault();
        if (isBusy(btnSubmitAddUser)) return;
        var nameInput = document.getElementById('new-username');
        var pwInput = document.getElementById('new-user-password');
        var username = nameInput.value.trim();
        var password = pwInput.value;
        if (username.length < 3) {
          failOn(nameInput, 'Username must be at least 3 characters.');
          return;
        }
        if (password.length < 12) {
          failOn(pwInput, 'Password must be at least 12 characters.');
          return;
        }
        clearReasons(addUserForm);
        holdFocus(btnSubmitAddUser, addUserForm);
        setBusy(btnSubmitAddUser, true);
        createResult.className = 'inline-form-msg';
        createResult.textContent = 'Creating…';
        rows.reveal(createResult);
        MM.reauth.run(function () {
          return MM.api.post('/api/users', { username: username, password: password, role: selectedRole() });
        }).then(function () {
          var there = rows.stillAt(createResult, addUserForm);
          createResult.className = 'inline-form-msg ok';
          createResult.textContent = 'User "' + username + '" created.';
          if (there) rows.reveal(createResult);
          var listed = loadUsers().catch(function () { return null; });
          // Submit stays busy until the form has closed, so a second Enter cannot create twice.
          // The new row comes in only then, so the list does not jump under it.
          var closed = new Promise(function (resolve) {
            setTimeout(function () {
              var hadFocus = MM.dom.focusStillIn(addUserForm);
              toggleAddUser(false, function () {
                // Once the fold has finished the button's place is settled: bring it into view.
                if (hadFocus && btnAddUser) {
                  var calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                  btnAddUser.scrollIntoView({ block: 'nearest', behavior: calm ? 'auto' : 'smooth' });
                }
                resolve();
              });
              if (hadFocus && btnAddUser) btnAddUser.focus({ preventScroll: true });
            }, 700);
          });
          Promise.all([listed, closed]).then(function (done) {
            setBusy(btnSubmitAddUser, false);
            if (done[0]) appendNewUsers(done[0]); else showUsers();
          });
        }).catch(function (err) {
          setBusy(btnSubmitAddUser, false);
          if (err && err.error === 'reauth_cancelled') {
            createResult.className = 'inline-form-msg';
            createResult.textContent = '';
            return;
          }
          var there = rows.stillAt(createResult, addUserForm);
          var text = MM.api.messageOf(err, 'Something went wrong. Try again.');
          if (err && (err.status === 0 || err.status === 429 || err.status >= 500)) {
            createResult.className = 'inline-form-msg err';
            createResult.textContent = text;
            if (there) rows.reveal(createResult);
            return;
          }
          var issues = err && err.issues;
          failOn(issues && issues.length ? pwInput : nameInput, text, issues, !there);
        });
      });

      showUsers();
    },
  };

})();
