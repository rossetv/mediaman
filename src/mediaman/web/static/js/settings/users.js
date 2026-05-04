/**
 * settings/users.js — Users settings module.
 *
 * Responsibilities:
 *   - User list rendering (GET /api/users)
 *   - Add user form (#add-user-form, #btn-add-user)
 *   - Delete user flow (inline password drawer)
 *   - Self password-change form (#self-password-form)
 *   - Revoke other sessions (#btn-revoke-others)
 *   - Password strength meters (data-strength attribute)
 *   - Password-match indicator (#self-match-label)
 *   - Shared inline password-prompt drawer (openPasswordPrompt)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.users.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  MM.settings.users = {

    init: function () {

      // ----------------------------------------------------------------
      // Shared helpers
      // ----------------------------------------------------------------
      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger)';
        el.textContent = text;
        return el;
      }

      // ----------------------------------------------------------------
      // User list
      // ----------------------------------------------------------------
      function loadUsers() {
        MM.api.get('/api/users')
          .then(renderUsers)
          .catch(function () {
            var list = document.getElementById('user-list');
            if (list) { list.replaceChildren(); list.appendChild(makeMsg("Couldn’t load users.", 'err')); }
          });
      }

      function renderUsers(data) {
        var list = document.getElementById('user-list');
        if (!list) return;
        list.replaceChildren();
        if (!data.users || !data.users.length) { list.appendChild(makeMsg('No users yet.')); return; }
        data.users.forEach(function (user) {
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
          if (user.username === data.current) {
            var you = document.createElement('span');
            you.className = 'you-pill';
            you.textContent = 'You';
            name.appendChild(document.createTextNode(' '));
            name.appendChild(you);
          }
          meta.appendChild(name);
          var sub = document.createElement('div');
          sub.className = 'usr-sub';
          sub.textContent = user.created_at ? ('Joined ' + String(user.created_at).slice(0, 10)) : '';
          meta.appendChild(sub);
          row.appendChild(meta);

          if (user.username !== data.current) {
            var del = document.createElement('button');
            del.type = 'button';
            del.className = 'link-danger';
            del.textContent = 'Delete';
            del.addEventListener('click', function () { openDeleteDrawer(user, row); });
            row.appendChild(del);
          }
          list.appendChild(row);
        });
      }

      // ----------------------------------------------------------------
      // Inline password-prompt drawer — shared by delete-user and any
      // future flow that needs an inline password confirmation.
      //
      // opts:
      //   anchor        — element to insert next to
      //   where         — 'before' | 'after' (default: 'after')
      //   title         — string or DocumentFragment
      //   descText      — optional subtitle string
      //   confirmLabel  — button label (default: 'Confirm')
      //   dangerConfirm — use btn--danger instead of btn--primary
      //   onSubmit(pw)  — returns Promise<{ok, error?}>
      //   onCancel()    — called when the user dismisses
      // ----------------------------------------------------------------
      function openPasswordPrompt(opts) {
        document.querySelectorAll('.setg-pg .pw-prompt').forEach(function (n) { n.remove(); });

        var drawer = document.createElement('div');
        drawer.className = 'inline-form pw-prompt';

        var title = document.createElement('div');
        title.className = 'fld-lbl';
        title.style.textTransform = 'none';
        title.style.letterSpacing = '-.005em';
        title.style.fontSize = '14px';
        if (typeof opts.title === 'string') title.textContent = opts.title;
        else if (opts.title) title.appendChild(opts.title);
        drawer.appendChild(title);

        if (opts.descText) {
          var desc = document.createElement('div');
          desc.className = 'fld-sub';
          desc.style.marginTop = '6px';
          desc.textContent = opts.descText;
          drawer.appendChild(desc);
        }

        var lbl = document.createElement('label');
        lbl.className = 'fld';
        lbl.style.marginTop = '14px';
        var lblText = document.createElement('span');
        lblText.className = 'fld-lbl';
        lblText.textContent = 'Your password (re-authenticate)';
        lbl.appendChild(lblText);
        var input = document.createElement('input');
        input.className = 'inp';
        input.type = 'password';
        input.autocomplete = 'current-password';
        lbl.appendChild(input);
        drawer.appendChild(lbl);

        var actions = document.createElement('div');
        actions.className = 'inline-form-actions';
        var cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn btn--ghost btn--sm';
        cancelBtn.textContent = 'Cancel';
        actions.appendChild(cancelBtn);
        var confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = opts.dangerConfirm ? 'btn btn--danger btn--sm' : 'btn btn--primary btn--sm';
        confirmBtn.textContent = opts.confirmLabel || 'Confirm';
        actions.appendChild(confirmBtn);
        var msg = document.createElement('span');
        msg.className = 'inline-form-msg';
        msg.style.margin = '0';
        actions.appendChild(msg);
        drawer.appendChild(actions);

        if (opts.where === 'before') opts.anchor.before(drawer);
        else opts.anchor.after(drawer);
        input.focus();

        function close(viaCancel) {
          drawer.remove();
          if (viaCancel && typeof opts.onCancel === 'function') opts.onCancel();
        }
        cancelBtn.addEventListener('click', function () { close(true); });

        function submit() {
          var pw = input.value;
          if (!pw) {
            msg.className = 'inline-form-msg err';
            msg.textContent = 'Password required.';
            return;
          }
          confirmBtn.disabled = true;
          msg.className = 'inline-form-msg';
          msg.textContent = '';
          Promise.resolve(opts.onSubmit(pw)).then(function (res) {
            confirmBtn.disabled = false;
            if (res && res.ok) {
              drawer.remove();
            } else {
              msg.className = 'inline-form-msg err';
              msg.textContent = (res && res.error) || 'Failed';
              input.focus();
              input.select();
            }
          }).catch(function () {
            confirmBtn.disabled = false;
            msg.className = 'inline-form-msg err';
            msg.textContent = 'Network error. Try again.';
          });
        }
        confirmBtn.addEventListener('click', submit);
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') submit();
          else if (e.key === 'Escape') close(true);
        });
      }

      // ----------------------------------------------------------------
      // Delete user
      // ----------------------------------------------------------------
      function openDeleteDrawer(user, row) {
        var titleFrag = document.createDocumentFragment();
        titleFrag.appendChild(document.createTextNode('Delete '));
        var strong = document.createElement('strong');
        strong.textContent = user.username;
        titleFrag.appendChild(strong);
        titleFrag.appendChild(document.createTextNode('?'));

        openPasswordPrompt({
          anchor: row,
          title: titleFrag,
          descText: 'Irreversible. All active sessions for this user will be terminated.',
          confirmLabel: 'Delete user',
          dangerConfirm: true,
          onSubmit: function (pw) {
            return fetch('/api/users/' + user.id, {
              method: 'DELETE',
              credentials: 'same-origin',
              headers: { 'X-Confirm-Password': pw },
            }).then(function (r) {
              return r.json().catch(function () { return {}; }).then(function (data) {
                if (data && data.ok) { loadUsers(); return { ok: true }; }
                return { ok: false, error: (data && data.error) || 'Delete failed' };
              });
            });
          },
        });
      }

      // ----------------------------------------------------------------
      // Add user form
      // ----------------------------------------------------------------
      var addUserForm      = document.getElementById('add-user-form');
      var btnAddUser       = document.getElementById('btn-add-user');
      var btnCancelAddUser = document.getElementById('btn-cancel-add-user');
      var btnSubmitAddUser = document.getElementById('btn-submit-add-user');
      var createResult     = document.getElementById('create-user-result');

      function toggleAddUser(open) {
        if (!addUserForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : addUserForm.hidden;
        addUserForm.hidden = !wantOpen;
        if (wantOpen) { document.getElementById('new-username').focus(); }
        else {
          ['new-username', 'new-user-password'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
          });
          if (createResult) { createResult.textContent = ''; createResult.className = 'inline-form-msg'; }
        }
      }
      if (btnAddUser)       btnAddUser.addEventListener('click', function () { toggleAddUser(); });
      if (btnCancelAddUser) btnCancelAddUser.addEventListener('click', function () { toggleAddUser(false); });

      if (btnSubmitAddUser) btnSubmitAddUser.addEventListener('click', function () {
        var username = document.getElementById('new-username').value.trim();
        var password = document.getElementById('new-user-password').value;
        if (username.length < 3) {
          createResult.className = 'inline-form-msg err';
          createResult.textContent = 'Username must be at least 3 characters.';
          return;
        }
        if (password.length < 12) {
          createResult.className = 'inline-form-msg err';
          createResult.textContent = 'Password must be at least 12 characters.';
          return;
        }
        btnSubmitAddUser.disabled = true;
        MM.api.post('/api/users', { username: username, password: password })
          .then(function (data) {
            btnSubmitAddUser.disabled = false;
            createResult.className = 'inline-form-msg ok';
            createResult.textContent = 'User "' + username + '" created.';
            setTimeout(function () { toggleAddUser(false); }, 700);
            loadUsers();
          })
          .catch(function (err) {
            btnSubmitAddUser.disabled = false;
            createResult.className = 'inline-form-msg err';
            // MM.api rejects with an APIError whose .message is the server's error string.
            createResult.textContent = (err && err.message) || 'Failed';
            // TODO: surface data.issues list if the backend sends one — MM.api
            // currently discards it (APIError carries only .error/.message).
          });
      });

      // ----------------------------------------------------------------
      // Self password-change form
      // ----------------------------------------------------------------
      var pwForm       = document.getElementById('self-password-form');
      var btnChangePw  = document.getElementById('btn-change-password');
      var btnCancelPw  = document.getElementById('btn-cancel-password');
      var btnSubmitPw  = document.getElementById('btn-submit-password');
      var pwResult     = document.getElementById('password-result');

      function togglePwForm(open) {
        if (!pwForm) return;
        var wantOpen = (typeof open === 'boolean') ? open : pwForm.hidden;
        pwForm.hidden = !wantOpen;
        if (wantOpen) { document.getElementById('self-old-password').focus(); }
        else {
          ['self-old-password', 'self-new-password', 'self-confirm-password'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
          });
          if (pwResult) { pwResult.textContent = ''; pwResult.className = 'inline-form-msg'; }
        }
      }
      if (btnChangePw) btnChangePw.addEventListener('click', function () { togglePwForm(); });
      if (btnCancelPw) btnCancelPw.addEventListener('click', function () { togglePwForm(false); });

      if (btnSubmitPw) btnSubmitPw.addEventListener('click', function () {
        var oldPw = document.getElementById('self-old-password').value;
        var newPw = document.getElementById('self-new-password').value;
        var conf  = document.getElementById('self-confirm-password').value;
        if (!oldPw) {
          pwResult.className = 'inline-form-msg err';
          pwResult.textContent = 'Enter your current password.';
          return;
        }
        if (newPw.length < 12) {
          pwResult.className = 'inline-form-msg err';
          pwResult.textContent = 'New password must be at least 12 characters.';
          return;
        }
        if (newPw !== conf) {
          pwResult.className = 'inline-form-msg err';
          pwResult.textContent = "Passwords don’t match.";
          return;
        }
        btnSubmitPw.disabled = true;
        MM.api.post('/api/users/change-password', { old_password: oldPw, new_password: newPw })
          .then(function () {
            btnSubmitPw.disabled = false;
            pwResult.className = 'inline-form-msg ok';
            pwResult.textContent = 'Password updated.';
            setTimeout(function () { togglePwForm(false); }, 900);
          })
          .catch(function (err) {
            btnSubmitPw.disabled = false;
            pwResult.className = 'inline-form-msg err';
            pwResult.textContent = (err && err.message) || 'Failed';
          });
      });

      // ----------------------------------------------------------------
      // Revoke other sessions
      // ----------------------------------------------------------------
      var btnRevokeOthers = document.getElementById('btn-revoke-others');
      if (btnRevokeOthers) btnRevokeOthers.addEventListener('click', function () {
        var orig = btnRevokeOthers.textContent;
        btnRevokeOthers.disabled = true;
        btnRevokeOthers.textContent = 'Signing out…';
        MM.api.post('/api/users/sessions/revoke-others')
          .then(function (data) {
            btnRevokeOthers.disabled = false;
            btnRevokeOthers.textContent = data && data.ok
              ? 'Signed out ' + (data.revoked || 0)
              : orig;
            setTimeout(function () { btnRevokeOthers.textContent = orig; }, 2400);
          })
          .catch(function () {
            btnRevokeOthers.disabled = false;
            btnRevokeOthers.textContent = orig;
          });
      });

      // ----------------------------------------------------------------
      // Password strength meters
      // ----------------------------------------------------------------
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
        var tone = 'weak', label = 'Too weak';
        if (s >= 5)      { tone = 'strong'; label = 'Strong'; }
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
            label.textContent = ' ';
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
          if (!cp.value || !pw.value) { lab.textContent = ' '; lab.className = 'fpc-caption'; return; }
          if (cp.value === pw.value) {
            lab.textContent = 'Passwords match';
            lab.className = 'fpc-caption fpc-caption-strong';
          } else {
            lab.textContent = "Passwords don’t match yet";
            lab.className = 'fpc-caption fpc-caption-weak';
          }
        }
        cp.addEventListener('input', render);
        pw.addEventListener('input', render);
      })();

      // ----------------------------------------------------------------
      // Boot.
      // ----------------------------------------------------------------
      loadUsers();
    },
  };

})();
