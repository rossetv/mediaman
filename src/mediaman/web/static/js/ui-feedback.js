/* UI feedback helpers: styled replacements for alert() / confirm().
 *
 *   window.UIFeedback.confirm({
 *     title: 'Remove all keeps?',
 *     body:  'Seasons may be scheduled for deletion on the next scan.',
 *     confirmLabel: 'Remove all',        // default: 'Confirm'
 *     confirmVariant: 'danger',           // 'danger' | 'primary' (default)
 *     cancelLabel: 'Cancel',              // default: 'Cancel'
 *   }) → Promise<boolean>
 *
 *   window.UIFeedback.toast('Saved', { variant: 'success' })
 *                          ^ string text; variant: 'success' | 'error' | 'info' (default)
 *                          auto-dismisses after 4s.
 *
 *   window.UIFeedback.toast('Removed', {
 *     action: { label: 'Undo', onClick: function () { ... } }
 *   })
 *                          adds a button to the toast; clicking it runs onClick
 *                          and dismisses the toast. A toast with an action stays
 *                          8s unless `duration` says otherwise, and is not
 *                          dismissed while the button has keyboard focus.
 *
 *   window.UIFeedback.error(message)  — shorthand for toast(msg, { variant: 'error' })
 */
(function (global) {
  'use strict';

  function el(tag, props, children) {
    var n = document.createElement(tag);
    if (props) {
      for (var k in props) {
        if (k === 'class') n.className = props[k];
        else if (k === 'text') n.textContent = props[k];
        else n.setAttribute(k, props[k]);
      }
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  // Remove `node` once its exit transition ends; the timeout covers a
  // transition that never runs (hidden tab, no change in computed style).
  function removeAfterExit(node) {
    var done = false;
    function remove() {
      if (done) return;
      done = true;
      node.removeEventListener('transitionend', onEnd);
      if (node.parentNode) node.parentNode.removeChild(node);
    }
    function onEnd(e) { if (e.target === node && e.propertyName === 'opacity') remove(); }
    node.addEventListener('transitionend', onEnd);
    setTimeout(remove, exitMs(node) + 100);
  }

  // The longest transition (delay included) the node runs, in ms.
  function exitMs(node) {
    var cs = getComputedStyle(node);
    function list(v) {
      return v.split(',').map(function (t) {
        var n = parseFloat(t) || 0;
        return /ms\s*$/.test(t) ? n : n * 1000;
      });
    }
    var d = list(cs.transitionDuration), dl = list(cs.transitionDelay), max = 0;
    d.forEach(function (x, i) { max = Math.max(max, x + dl[i % dl.length]); });
    return max;
  }

  function dismissToast(node) {
    if (node.classList.contains('is-leaving')) return;
    node.classList.add('is-leaving');
    node.classList.remove('is-visible');
    removeAfterExit(node);
  }

  function ensureToastHost() {
    var host = document.getElementById('ui-toast-host');
    if (!host) {
      host = el('div', { id: 'ui-toast-host', class: 'ui-toast-host',
                         role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(host);
    }
    return host;
  }

  var undoListening = false;
  var undoHint = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '') ? 'Cmd+Z' : 'Ctrl+Z';

  // A confirm dialog or any open aria-modal dialog owns the keyboard.
  function modalOpen() {
    if (document.querySelector('.ui-confirm-overlay:not(.is-leaving)')) return true;
    return Array.prototype.some.call(document.querySelectorAll('[aria-modal="true"]'), function (m) {
      if (m.getAttribute('aria-hidden') === 'true') return false;
      var cs = getComputedStyle(m);
      return cs.display !== 'none' && cs.visibility !== 'hidden';
    });
  }
  // Ctrl/Cmd+Z presses the newest action toast's button, except in a text
  // field where it is the field's own undo.
  function listenForUndo() {
    if (undoListening) return;
    undoListening = true;
    document.addEventListener('keydown', function (e) {
      if (!e.key || e.key.toLowerCase() !== 'z' || !(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      if (modalOpen()) return;
      var t = e.target;
      if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
      var btns = document.querySelectorAll('.ui-toast--action:not(.is-leaving) .ui-toast__action');
      if (!btns.length) return;
      e.preventDefault();
      btns[btns.length - 1].click();
    });
  }

  function toast(message, opts) {
    opts = opts || {};
    var variant = opts.variant || 'info';
    var host = ensureToastHost();
    var node = el('div', {
      class: 'ui-toast ui-toast--' + variant,
      role: variant === 'error' ? 'alert' : 'status'
    });
    var action = opts.action;
    if (action && action.label && typeof action.onClick === 'function') {
      node.classList.add('ui-toast--action');
      var btn = el('button', { type: 'button', class: 'ui-toast__action', text: action.label,
                               'aria-keyshortcuts': 'Control+Z Meta+Z',
                               'aria-label': action.label + ' (' + undoHint + ')' });
      btn.addEventListener('click', function () {
        if (node.classList.contains('is-leaving')) return;
        dismissToast(node);
        action.onClick();
      });
      node.appendChild(el('span', { class: 'ui-toast__msg', text: message }));
      node.appendChild(btn);
    } else {
      action = null;
      node.textContent = message;
    }
    if (action) {
      // Only the newest action toast answers the shortcut.
      Array.prototype.forEach.call(host.querySelectorAll('.ui-toast__action'), function (b) {
        b.removeAttribute('aria-keyshortcuts');
        b.setAttribute('aria-label', b.textContent);
      });
      btn.setAttribute('aria-keyshortcuts', 'Control+Z Meta+Z');
      btn.setAttribute('aria-label', action.label + ' (' + undoHint + ')');
      listenForUndo();
    }
    // At most three at once, and never a stack taller than the host's
    // height budget. The new toast always stays; the oldest non-error goes
    // first, an error only (with its normal fade) when all are errors.
    var live = function () {
      return Array.prototype.filter.call(host.children, function (c) {
        return !c.classList.contains('is-leaving');
      });
    };
    host.appendChild(node);
    // Toasts already fading out leave the height measure: they are going.
    var overBudget = function () {
      var leaving = Array.prototype.filter.call(host.children, function (c) {
        return c.classList.contains('is-leaving');
      });
      leaving.forEach(function (c) { c.style.display = 'none'; });
      var over = host.scrollHeight > host.clientHeight;
      leaving.forEach(function (c) { c.style.display = ''; });
      return over;
    };
    var kids = live().filter(function (c) { return c !== node; });
    while (kids.length > 0 && (kids.length >= 3 || overBudget())) {
      // A toast with an action is never evicted while its time runs.
      var plain = kids.filter(function (c) {
        return !c.classList.contains('ui-toast--action');
      });
      var victim = plain.filter(function (c) {
        return !c.classList.contains('ui-toast--error');
      })[0] || plain[0];
      if (!victim) break;
      dismissToast(victim);
      kids = live().filter(function (c) { return c !== node; });
    }
    // Animate in on next frame
    requestAnimationFrame(function () { node.classList.add('is-visible'); });
    var duration = opts.duration || (action ? 8000 : 4000);
    function expire() {
      // Never take the button away from someone about to press it.
      if (action && (node.contains(document.activeElement) || node.matches(':hover'))) {
        setTimeout(expire, 2000);
        return;
      }
      dismissToast(node);
    }
    setTimeout(expire, duration);
    return node;
  }

  function confirmDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var titleId = 'ui-confirm-title-' + Date.now();
      var overlay = el('div', {
        class: 'ui-confirm-overlay',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': titleId
      });
      var card = el('div', { class: 'ui-confirm-card' });
      var h = el('h3', { id: titleId, class: 'ui-confirm-title', text: opts.title || 'Are you sure?' });
      var p = el('p', { class: 'ui-confirm-body' });
      if (opts.body) p.textContent = opts.body;
      var actions = el('div', { class: 'ui-confirm-actions' });
      var cancelBtn = el('button', { type: 'button', class: 'btn btn--ghost',
                                     text: opts.cancelLabel || 'Cancel' });
      var confirmClass = 'btn ' + (opts.confirmVariant === 'danger' ? 'btn--danger' : 'btn--accent-soft');
      var confirmBtn = el('button', { type: 'button', class: confirmClass,
                                      text: opts.confirmLabel || 'Confirm' });
      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      card.appendChild(h);
      if (opts.body) card.appendChild(p);
      card.appendChild(actions);
      overlay.appendChild(card);
      document.body.appendChild(overlay);

      var closed = false;
      function cleanup(result) {
        if (closed) return;
        closed = true;
        if (window.ModalA11y) window.ModalA11y.onClosed(titleId);
        // The caller gets its answer at once; the dialog fades out behind it.
        resolve(result);
        overlay.classList.add('is-leaving');
        overlay.classList.remove('is-visible');
        removeAfterExit(overlay);
      }
      overlay.id = titleId;
      cancelBtn.addEventListener('click', function () { cleanup(false); });
      confirmBtn.addEventListener('click', function () { cleanup(true); });
      overlay.addEventListener('click', function (e) {
        if (e.target === overlay) cleanup(false);
      });

      if (window.ModalA11y) {
        window.ModalA11y.register(titleId, function () { cleanup(false); });
        window.ModalA11y.onOpened(titleId);
      } else {
        // Fallback focus + Escape
        setTimeout(function () { cancelBtn.focus(); }, 0);
        overlay.addEventListener('keydown', function (e) {
          if (e.key === 'Escape') cleanup(false);
        });
      }
      // Animate in
      requestAnimationFrame(function () { overlay.classList.add('is-visible'); });
    });
  }

  global.UIFeedback = {
    confirm: confirmDialog,
    toast: toast,
    error: function (msg) { return toast(msg, { variant: 'error' }); },
    success: function (msg) { return toast(msg, { variant: 'success' }); }
  };
})(window);
