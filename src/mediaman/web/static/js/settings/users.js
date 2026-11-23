/**
 * settings/users.js — Users settings module.
 *
 * Responsibilities:
 *   - User list rendering (GET /api/users), with adds, role changes and
 *     deletes applied to the rows in place
 *   - Add user form (#add-user-form, #btn-add-user) with its Viewer/Admin switch
 *   - Role change per user (PATCH /api/users/{id}/role + MM.reauth.run)
 *   - Delete user flow (UIFeedback.confirm + MM.reauth.run)
 *   - Self password-change form (#self-password-form)
 *   - Revoke other sessions (#btn-revoke-others)
 *   - Password strength meters (data-strength attribute)
 *   - Password-match indicator (#self-match-label)
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

  MM.settings.users = {

    init: function () {

      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger)';
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
      function appendNewUsers() {
        return loadUsers().then(function (users) {
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

        var isAdmin = user.role !== 'viewer';
        var roleLabel = document.createElement('span');
        roleLabel.className = 'pill ' + (isAdmin ? 'pill--tv' : 'pill--neutral');
        roleLabel.textContent = isAdmin ? 'Admin' : 'Viewer';
        row.appendChild(roleLabel);

        if (user.username !== currentUser) {
          var actions = document.createElement('div');
          actions.className = 'usr-actions';

          var roleBtn = document.createElement('button');
          roleBtn.type = 'button';
          roleBtn.className = 'btn btn--ghost btn--sm';
          roleBtn.dataset.userAction = 'role';
          roleBtn.textContent = isAdmin ? 'Make viewer' : 'Make admin';
          roleBtn.setAttribute('aria-label', roleBtn.textContent + ': ' + user.username);
          roleBtn.addEventListener('click', function () {
            changeRoleFlow(user, isAdmin ? 'viewer' : 'admin', row, roleBtn);
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
        return row;
      }

      function changeRoleFlow(user, role, row, btn) {
        var label = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Saving…';
        btn.setAttribute('aria-label', 'Saving…: ' + user.username);
        MM.reauth.run(function () {
          return MM.api.patch('/api/users/' + user.id + '/role', { role: role });
        }).then(function () {
          var refreshed = buildRow(Object.assign({}, user, { role: role }));
          row.replaceWith(refreshed);
          var again = refreshed.querySelector('[data-user-action="role"]');
          if (again && MM.dom.focusStillIn(refreshed)) again.focus();
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

      // Only one of the account and team forms is open at a time. A form whose
      // request is in flight stays open, so its result lands where it can be
      // seen; it closes itself when it finishes.
      var closers = [];
      function closeOthers(keep) {
        closers.forEach(function (close) { if (close !== keep) close(); });
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

      function toggleAddUser(open) {
        if (!addUserForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : addUserForm.hidden;
        if (wantOpen) closeOthers(closeAddUser);
        addUserForm.hidden = !wantOpen;
        if (wantOpen) { document.getElementById('new-username').focus(); }
        else {
          addUserForm.reset();
          ['new-username', 'new-user-password'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.dispatchEvent(new Event('input'));
          });
          selectRole('viewer');
          if (createResult) { createResult.textContent = ''; createResult.className = 'inline-form-msg'; }
        }
      }
      function closeAddUser() { if (addUserForm && !addUserForm.hidden) toggleAddUser(false); }
      closers.push(function () { if (!(btnSubmitAddUser && btnSubmitAddUser.disabled)) closeAddUser(); });
      if (btnAddUser)       btnAddUser.addEventListener('click', function () { toggleAddUser(); });
      if (btnCancelAddUser) btnCancelAddUser.addEventListener('click', function () {
        toggleAddUser(false);
        if (btnAddUser) btnAddUser.focus();
      });

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
          // Submit stays disabled until the form closes, so a second Enter cannot create twice.
          setTimeout(function () {
            var hadFocus = MM.dom.focusStillIn(addUserForm);
            btnSubmitAddUser.disabled = false;
            toggleAddUser(false);
            if (hadFocus && btnAddUser) btnAddUser.focus();
          }, 700);
          appendNewUsers().catch(showUsers);
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

      var pwForm       = document.getElementById('self-password-form');
      var btnChangePw  = document.getElementById('btn-change-password');
      var btnCancelPw  = document.getElementById('btn-cancel-password');
      var btnSubmitPw  = document.getElementById('btn-submit-password');
      var pwResult     = document.getElementById('password-result');

      function togglePwForm(open) {
        if (!pwForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : pwForm.hidden;
        if (wantOpen) closeOthers(closePwForm);
        pwForm.hidden = !wantOpen;
        if (wantOpen) { document.getElementById('self-old-password').focus(); }
        else {
          pwForm.reset();
          ['self-old-password', 'self-new-password', 'self-confirm-password'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.dispatchEvent(new Event('input'));
          });
          if (pwResult) { pwResult.textContent = ''; pwResult.className = 'inline-form-msg'; }
        }
      }
      function closePwForm() { if (pwForm && !pwForm.hidden) togglePwForm(false); }
      closers.push(function () { if (!(btnSubmitPw && btnSubmitPw.disabled)) closePwForm(); });
      if (btnChangePw) btnChangePw.addEventListener('click', function () { togglePwForm(); });
      if (btnCancelPw) btnCancelPw.addEventListener('click', function () {
        togglePwForm(false);
        if (btnChangePw) btnChangePw.focus();
      });

      if (pwForm) pwForm.addEventListener('submit', function (e) {
        e.preventDefault();
        if (btnSubmitPw.disabled) return;
        var oldInput = document.getElementById('self-old-password');
        var newInput = document.getElementById('self-new-password');
        var confInput = document.getElementById('self-confirm-password');
        var oldPw = oldInput.value;
        var newPw = newInput.value;
        var conf  = confInput.value;
        if (!oldPw) { failOn(pwResult, 'Enter your current password.', oldInput); return; }
        if (newPw.length < 12) { failOn(pwResult, 'New password must be at least 12 characters.', newInput); return; }
        if (newPw !== conf) { failOn(pwResult, "Passwords don't match.", confInput); return; }
        btnSubmitPw.disabled = true;
        pwResult.className = 'inline-form-msg';
        pwResult.textContent = 'Updating…';
        MM.reauth.run(function () {
          return MM.api.post('/api/users/change-password', {
            old_password: oldPw,
            new_password: newPw,
          });
        }).then(function () {
          pwResult.className = 'inline-form-msg ok';
          pwResult.textContent = 'Password updated.';
          setTimeout(function () {
            var hadFocus = MM.dom.focusStillIn(pwForm);
            btnSubmitPw.disabled = false;
            togglePwForm(false);
            if (hadFocus && btnChangePw) btnChangePw.focus();
          }, 900);
        }).catch(function (err) {
          btnSubmitPw.disabled = false;
          if (err && err.error === 'reauth_cancelled') {
            pwResult.className = 'inline-form-msg';
            pwResult.textContent = '';
            return;
          }
          pwResult.className = 'inline-form-msg err';
          renderApiError(pwResult, err);
          (err && err.error === 'wrong_password' ? oldInput : newInput).focus();
        });
      });

      var btnRevokeOthers = document.getElementById('btn-revoke-others');
      if (btnRevokeOthers) btnRevokeOthers.addEventListener('click', function () {
        var orig = btnRevokeOthers.textContent;
        btnRevokeOthers.disabled = true;
        btnRevokeOthers.textContent = 'Signing out…';
        MM.reauth.run(function () {
          return MM.api.post('/api/users/sessions/revoke-others');
        }).then(function (data) {
          btnRevokeOthers.disabled = false;
          btnRevokeOthers.textContent = data && data.ok
            ? 'Signed out ' + (data.revoked || 0)
            : orig;
          setTimeout(function () { btnRevokeOthers.textContent = orig; }, 2400);
        }).catch(function (err) {
          btnRevokeOthers.disabled = false;
          btnRevokeOthers.textContent = orig;
          if (err && err.error === 'reauth_cancelled') return;
          // Existing UX: silent failure here; no toast wired up.
        });
      });

      function uniqueCount(s) { return new Set(s).size; }
      function classCount(s) {
        var n = 0;
        if (/[a-z]/.test(s)) n++;
        if (/[A-Z]/.test(s)) n++;
        if (/\d/.test(s)) n++;
        if (/[^A-Za-z0-9\s]/.test(s)) n++;
        return n;
      }
      function score(pw) {
        if (!pw) return { pct: 0, tone: '', label: '' };
        var len = pw.length, uniq = uniqueCount(pw), cls = classCount(pw);
        var passphrase = len >= 20 && uniq >= 12;
        var s = 0;
        if (len >= 12) s++;
        if (passphrase || cls >= 3) s++;
        if (uniq >= 6) s++;
        if (len >= 16) s++;
        if (len >= 20 || cls >= 4) s++;
        // The server also refuses common passwords, so the best this can say is "so far".
        var tone = 'weak', label = 'Too simple so far';
        if (s >= 5)      { tone = 'strong'; label = 'Looks good so far'; }
        else if (s >= 3) { tone = 'ok';     label = 'Getting there'; }
        return { pct: Math.min(100, (s / 5) * 100), tone: tone, label: label };
      }
      function wireStrength(inputId) {
        var input = document.getElementById(inputId);
        if (!input) return;
        var key = input.getAttribute('data-strength');
        if (!key) return;
        var meter = document.getElementById(key + '-meter');
        var label = document.getElementById(key + '-label');
        function render() {
          var r = score(input.value);
          if (!input.value) {
            meter.style.width = '0%';
            meter.className = 'fpc-meter-fill';
            label.textContent = ' ';
            label.className = 'fpc-caption';
            return;
          }
          meter.style.width = r.pct + '%';
          meter.className = 'fpc-meter-fill ' + r.tone;
          label.textContent = r.label;
          label.className = 'fpc-caption fpc-caption-' + r.tone;
        }
        input.addEventListener('input', render);
      }
      wireStrength('self-new-password');
      wireStrength('new-user-password');

      // Password-match indicator
      (function () {
        var pw  = document.getElementById('self-new-password');
        var cp  = document.getElementById('self-confirm-password');
        var lab = document.getElementById('self-match-label');
        if (!pw || !cp || !lab) return;
        function render() {
          if (!cp.value || !pw.value) { lab.textContent = ' '; lab.className = 'fpc-caption'; return; }
          if (cp.value === pw.value) {
            lab.textContent = 'Passwords match';
            lab.className = 'fpc-caption fpc-caption-strong';
          } else {
            lab.textContent = "Passwords don't match yet";
            lab.className = 'fpc-caption fpc-caption-weak';
          }
        }
        cp.addEventListener('input', render);
        pw.addEventListener('input', render);
      })();

      // Edit-email drawer (self card).
      // The reauth prompt is provided by MM.reauth.run — no inline
      // password field on this form.
      function initEmailEditor() {
        var btn = document.getElementById('btn-edit-email');
        var form = document.getElementById('self-email-form');
        var cancel = document.getElementById('btn-cancel-email');
        var submit = document.getElementById('btn-submit-email');
        var emailInp = document.getElementById('self-email-input');
        var msg = document.getElementById('email-result');
        var display = document.getElementById('self-email-display');
        if (!btn || !form) return;

        function show() {
          closeOthers(hide);
          form.hidden = false;
          msg.textContent = '';
          msg.className = 'inline-form-msg';
          emailInp.focus();
        }
        function hide() {
          if (form.hidden) return;
          form.hidden = true;
          form.reset();
          msg.textContent = '';
          msg.className = 'inline-form-msg';
        }
        closers.push(function () { if (!submit.disabled) hide(); });

        btn.addEventListener('click', function () {
          if (form.hidden) show(); else hide();
        });
        cancel.addEventListener('click', function () { hide(); btn.focus(); });

        form.addEventListener('submit', function (e) {
          e.preventDefault();
          if (submit.disabled) return;
          submit.disabled = true;
          msg.textContent = 'Saving…';
          msg.className = 'inline-form-msg';
          MM.reauth.run(function () {
            return MM.api.patch('/api/users/me/email', { email: emailInp.value });
          }).then(function () {
            // Keep submit disabled through the 800 ms close delay so a
            // fast second click during the "Saved" confirmation does
            // not re-issue the PATCH.
            msg.textContent = 'Saved';
            msg.className = 'inline-form-msg ok';
            var newVal = emailInp.value.trim();
            // Closing resets the form, so what was saved becomes its starting value.
            emailInp.defaultValue = newVal;
            if (display) {
              display.textContent = newVal || 'Not set · download alerts disabled';
            }
            btn.textContent = newVal ? 'Edit email' : 'Add email';
            setTimeout(function () {
              var hadFocus = MM.dom.focusStillIn(form);
              hide();
              submit.disabled = false;
              if (hadFocus) btn.focus();
            }, 800);
          }).catch(function (err) {
            submit.disabled = false;
            if (err && err.error === 'reauth_cancelled') {
              msg.textContent = '';
              msg.className = 'inline-form-msg';
              return;
            }
            msg.textContent = MM.api.messageOf(err, 'Could not save');
            msg.className = 'inline-form-msg err';
            emailInp.focus();
          });
        });
      }

      showUsers();
      initEmailEditor();
    },
  };

})();
