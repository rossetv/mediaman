/**
 * settings/users.js — Users settings module.
 *
 * Responsibilities:
 *   - User list rendering (GET /api/users), with adds, role changes and
 *     deletes applied to the rows in place
 *   - Add user form (#add-user-form, #btn-add-user) with its Viewer/Admin switch
 *   - Role change per user (PATCH /api/users/{id}/role + MM.reauth.run)
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
   * Render an APIError into an inline-form-msg element.
   * Surfaces the human-readable .message and, when present, the per-rule
   * .issues array as a bulleted list (used by the password-policy
   * validator on the server).
   */
  function renderApiError(target, err) {
    if (!target) return;
    var msg = MM.api.messageOf(err, 'Something went wrong. Try again.');
    var issues = err && err.issues;
    if (issues && issues.length) {
      target.textContent = '';
      var p = document.createElement('div');
      p.textContent = msg;
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
      target.textContent = msg;
    }
  }

  // The dialog's fade out (--dur is 200 ms), with a little to spare.
  var REAUTH_FADE_MS = 220;

  MM.settings.users = {

    init: function () {

      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger-text)';
        el.textContent = text;
        return el;
      }

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

      function buildRow(user) {
        var row = document.createElement('div');
        row.className = 'usr-row';
        row.dataset.userId = user.id;

        var av = document.createElement('div');
        av.className = 'av';
        av.textContent = (user.username || '?').charAt(0).toUpperCase();
        row.appendChild(av);

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
        var emailPart = user.email ? user.email : 'No email';
        var joinedPart = user.created_at
          ? 'joined ' + String(user.created_at).slice(0, 10)
          : '';
        sub.textContent = joinedPart
          ? emailPart + ' · ' + joinedPart
          : emailPart;
        meta.appendChild(sub);
        row.appendChild(meta);

        var roleLabel = document.createElement('span');
        row.appendChild(roleLabel);

        if (user.username !== currentUser) {
          var actions = document.createElement('div');
          actions.className = 'usr-actions';

          var roleBtn = document.createElement('button');
          roleBtn.type = 'button';
          roleBtn.className = 'btn btn--ghost btn--sm';
          roleBtn.dataset.userAction = 'role';
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
        }
        paintRole(user, roleLabel, roleBtn);
        roleLabel.addEventListener('animationend', function () { roleLabel.classList.remove('is-changed'); });
        return row;
      }

      function isAdmin(user) { return user.role !== 'viewer'; }

      // A role change updates the row where it is, so focus stays on its button.
      function paintRole(user, pill, btn) {
        var admin = isAdmin(user);
        pill.className = 'pill ' + (admin ? 'pill--tv' : 'pill--neutral');
        pill.textContent = admin ? 'Admin' : 'Viewer';
        if (!btn) return;
        btn.textContent = admin ? 'Make viewer' : 'Make admin';
        btn.setAttribute('aria-label', btn.textContent + ': ' + user.username);
      }

      function changeRoleFlow(user, role, btn, changed) {
        var label = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Saving…';
        btn.setAttribute('aria-label', 'Saving…: ' + user.username);
        MM.reauth.run(function () {
          return MM.api.patch('/api/users/' + user.id + '/role', { role: role });
        }).then(function () {
          // The reauth dialog is still fading out; the new pill waits until it has gone.
          var behind = document.querySelector('.reauth-backdrop');
          function apply() {
            var keepFocus = MM.dom.focusStillIn(btn);
            user.role = role;
            btn.disabled = false;
            changed();
            if (keepFocus) btn.focus();
          }
          if (behind) setTimeout(apply, REAUTH_FADE_MS); else apply();
          window.UIFeedback.success(user.username + ' is now ' + (role === 'admin' ? 'an admin' : 'a viewer'));
        }).catch(function (err) {
          var keepFocus = MM.dom.focusStillIn(btn);
          btn.disabled = false;
          btn.textContent = label;
          btn.setAttribute('aria-label', label + ': ' + user.username);
          if (keepFocus) btn.focus();
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
        window.UIFeedback.confirm({
          title: 'Delete ' + user.username + '?',
          body: 'Irreversible. All active sessions for this user will be terminated.',
          confirmLabel: 'Delete user',
          confirmVariant: 'danger',
        }).then(function (ok) {
          if (!ok) return;
          btn.disabled = true;
          btn.textContent = 'Deleting…';
          btn.setAttribute('aria-label', 'Deleting…: ' + user.username);
          MM.reauth.run(function () {
            return MM.api.delete('/api/users/' + user.id);
          }).then(function () {
            MM.settings.rows.leave(row, {
              focusSelector: '[data-user-action="delete"]',
              fallback: btnAddUser,
            });
            window.UIFeedback.success('Deleted ' + user.username);
          }).catch(function (err) {
            var keepFocus = MM.dom.focusStillIn(btn);
            btn.disabled = false;
            btn.textContent = 'Delete';
            btn.setAttribute('aria-label', 'Delete: ' + user.username);
            if (keepFocus) btn.focus();
            if (err && err.error === 'reauth_cancelled') return;
            window.UIFeedback.error(
              MM.api.messageOf(err, 'Could not delete user')
            );
          });
        });
      }

      var rows = MM.settings.rows;

      function clearMsg(target) {
        if (!target) return;
        target.textContent = '';
        target.className = 'inline-form-msg';
      }

      // A message about what was typed goes once the person types again; a
      // form that is sending keeps its progress line.
      function clearOnInput(form, target, submitBtn) {
        if (!form) return;
        form.addEventListener('input', function () {
          if (submitBtn && submitBtn.disabled) return;
          if (target && target.textContent) clearMsg(target);
        });
      }

      // A check that fails before sending puts the cursor in the field to fix.
      function failOn(target, text, field) {
        target.className = 'inline-form-msg err';
        target.textContent = text;
        if (field) field.focus();
      }

      var addUserForm      = document.getElementById('add-user-form');
      var btnAddUser       = document.getElementById('btn-add-user');
      var btnCancelAddUser = document.getElementById('btn-cancel-add-user');
      var btnSubmitAddUser = document.getElementById('btn-submit-add-user');
      var createResult     = document.getElementById('create-user-result');

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

      // Closing folds the form away first and resets it once it is out of sight.
      function toggleAddUser(open, closed) {
        if (!addUserForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : !rows.isOpen(addUserForm);
        if (wantOpen) {
          rows.open(addUserForm);
          document.getElementById('new-username').focus();
          return;
        }
        rows.close(addUserForm, function () {
          addUserForm.reset();
          selectRole('viewer');
          clearMsg(createResult);
          if (closed) closed();
        });
      }
      // A form that is sending cannot be closed or reset from its own buttons.
      if (btnAddUser)       btnAddUser.addEventListener('click', function () {
        if (btnSubmitAddUser && btnSubmitAddUser.disabled) return;
        toggleAddUser();
      });
      if (btnCancelAddUser) btnCancelAddUser.addEventListener('click', function () {
        if (btnSubmitAddUser && btnSubmitAddUser.disabled) return;
        toggleAddUser(false);
        if (btnAddUser) btnAddUser.focus();
      });

      clearOnInput(addUserForm, createResult, btnSubmitAddUser);
      if (addUserForm) addUserForm.addEventListener('submit', function (e) {
        e.preventDefault();
        if (btnSubmitAddUser.disabled) return;
        var nameInput = document.getElementById('new-username');
        var pwInput = document.getElementById('new-user-password');
        var username = nameInput.value.trim();
        var password = pwInput.value;
        if (username.length < 3) {
          failOn(createResult, 'Username must be at least 3 characters.', nameInput);
          return;
        }
        if (password.length < 12) {
          failOn(createResult, 'Password must be at least 12 characters.', pwInput);
          return;
        }
        btnSubmitAddUser.disabled = true;
        createResult.className = 'inline-form-msg';
        createResult.textContent = 'Creating…';
        MM.reauth.run(function () {
          return MM.api.post('/api/users', { username: username, password: password, role: selectedRole() });
        }).then(function () {
          createResult.className = 'inline-form-msg ok';
          createResult.textContent = 'User "' + username + '" created.';
          var listed = loadUsers().catch(function () { return null; });
          // Submit stays disabled until the form has closed, so a second Enter cannot create twice.
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
            btnSubmitAddUser.disabled = false;
            if (done[0]) appendNewUsers(done[0]); else showUsers();
          });
        }).catch(function (err) {
          btnSubmitAddUser.disabled = false;
          if (err && err.error === 'reauth_cancelled') {
            createResult.className = 'inline-form-msg';
            createResult.textContent = '';
            return;
          }
          createResult.className = 'inline-form-msg err';
          renderApiError(createResult, err);
          var field = err && err.issues && err.issues.length ? pwInput : nameInput;
          field.focus();
        });
      });

      showUsers();
    },
  };

})();
