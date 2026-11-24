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
 *   window.UIFeedback.dismissConfirm()   any that is open answers false
 *
 *   window.UIFeedback.toast('Saved', { variant: 'success' })
 *                          ^ string text; variant: 'success' | 'error' | 'info' (default)
 *                          auto-dismisses after 4s; an error stays while the
 *                          pointer is on it.
 *
 *   window.UIFeedback.toast('Removed', {
 *     action: { label: 'Undo', onClick: function () { ... } }
 *   })
 *                          adds a button to the toast; clicking it runs onClick
 *                          and dismisses the toast. A toast with an action stays
 *                          8s unless `duration` says otherwise, and is not
 *                          dismissed while the button has keyboard focus. With
 *                          `dialog`, the aria-modal it came from, Ctrl+Z presses
 *                          the button while that one is open too.
 *
 *   window.UIFeedback.error(message)  — shorthand for toast(msg, { variant: 'error' })
 *
 * Requires: core/dom.js (MM.dom.holdPage), for confirm().
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

  // What is full screen hides all that is outside it. A video shows no child.
  function toastHome() {
    var full = document.fullscreenElement || document.webkitFullscreenElement;
    return full && full.tagName !== 'VIDEO' ? full : document.body;
  }

  function ensureToastHost() {
    var host = document.getElementById('ui-toast-host');
    if (!host) {
      host = el('div', { id: 'ui-toast-host', class: 'ui-toast-host',
                         role: 'status', 'aria-live': 'polite' });
      toastHome().appendChild(host);
      ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (name) {
        document.addEventListener(name, rehome);
      });
    }
    return host;
  }

  function rehome() {
    var host = document.getElementById('ui-toast-host');
    var active = document.activeElement;
    if (host.parentNode === toastHome()) return;
    toastHome().appendChild(host);
    if (active && host.contains(active)) active.focus();
  }

  var raisedFrom = new WeakMap(); // the dialog an action toast came from, by its button
  var undoListening = false;
  var undoHint = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || '') ? 'Cmd+Z' : 'Ctrl+Z';

  // A confirm dialog or any open aria-modal dialog owns the keyboard.
  // Not *own*, the dialog a toast came from.
  function modalOpen(own) {
    if (document.querySelector('.ui-confirm-overlay:not(.is-leaving)')) return true;
    return Array.prototype.some.call(document.querySelectorAll('[aria-modal="true"]'), function (m) {
      if (m === own || m.getAttribute('aria-hidden') === 'true') return false;
      // No box: it, or something it stands in, is not shown.
      return m.getClientRects().length > 0 && getComputedStyle(m).visibility !== 'hidden';
    });
  }
  // Ctrl/Cmd+Z presses the newest action toast's button, except in a text
  // field where it is the field's own undo.
  function listenForUndo() {
    if (undoListening) return;
    undoListening = true;
    document.addEventListener('keydown', function (e) {
      if (!e.key || e.key.toLowerCase() !== 'z' || !(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
      var t = e.target;
      if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
      var btns = document.querySelectorAll('.ui-toast--action:not(.is-leaving) .ui-toast__action');
      var last = btns[btns.length - 1];
      if (!last || modalOpen(raisedFrom.get(last))) return;
      e.preventDefault();
      last.click();
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
      if (opts.dialog) raisedFrom.set(btn, opts.dialog);
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
    // At most three at once, two on a phone, and never a stack taller than the
    // host's height budget. The new toast always stays; the oldest non-error goes
    // first, an error only (with its normal fade) when all are errors.
    var phone = window.matchMedia('(max-width: 600px), (max-height: 420px)').matches;
    var most = phone ? 2 : 3;
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
    while (kids.length > 0 && (kids.length >= most || overBudget())) {
      // A toast with an action is never evicted while its time runs, but on a
      // phone two of them leave no room: the newer takes the older one's place.
      var plain = kids.filter(function (c) {
        return !c.classList.contains('ui-toast--action');
      });
      var victim = plain.filter(function (c) {
        return !c.classList.contains('ui-toast--error');
      })[0] || plain[0];
      if (!victim && phone && kids.length >= most) {
        victim = kids.filter(function (c) { return !c.contains(document.activeElement); })[0];
      }
      if (!victim) break;
      dismissToast(victim);
      kids = live().filter(function (c) { return c !== node; });
    }
    // Animate in on next frame
    requestAnimationFrame(function () { node.classList.add('is-visible'); });
    var duration = opts.duration || (action ? 8000 : 4000);
    function expire() {
      // Never take the button away from someone about to press it, nor an
      // error from under the pointer of someone reading it. A touch screen's
      // sticky :hover after a tap does not count as a pointer.
      var pointerHover = window.matchMedia('(hover: hover)').matches && node.matches(':hover');
      if ((action && node.contains(document.activeElement)) || ((action || variant === 'error') && pointerHover)) {
        setTimeout(expire, 2000);
        return;
      }
      dismissToast(node);
    }
    setTimeout(expire, duration);
    return node;
  }

  var asked = 0;
  var open = []; // what answers each open confirm with no, the newest last

  function confirmDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var id = 'ui-confirm-' + (++asked);
      var titleId = id + '-title';
      var bodyId = id + '-body';
      var overlay = el('div', {
        class: 'ui-confirm-overlay',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': titleId
      });
      var card = el('div', { class: 'ui-confirm-card' });
      var h = el('h3', { id: titleId, class: 'ui-confirm-title', text: opts.title || 'Are you sure?' });
      var p = el('p', { id: bodyId, class: 'ui-confirm-body' });
      if (opts.body) {
        p.textContent = opts.body;
        overlay.setAttribute('aria-describedby', bodyId);
      }
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
      var letGo = window.MM.dom.holdPage();
      document.body.appendChild(overlay);

      var closed = false;
      function cleanup(result) {
        if (closed) return;
        closed = true;
        open.splice(open.indexOf(cancel), 1);
        overlay.classList.add('is-leaving');
        if (window.ModalA11y) window.ModalA11y.onClosed(id);
        // The caller gets its answer at once; the dialog fades out behind it.
        resolve(result);
        overlay.classList.remove('is-visible');
        letGo();
        removeAfterExit(overlay);
      }
      function cancel() { cleanup(false); }
      overlay.id = id;
      open.push(cancel);
      // The rest of a double click on what asked lands on the dialog, and a slow
      // one comes before it could be read: neither answers yes, nor no by the backdrop.
      var openedAt = performance.now();
      // A click that begins a run of its own here makes the run the dialog's.
      var begun = false;
      function stray(e) {
        if (!(e.detail > 1)) begun = true;
        return !begun;
      }
      function early() { return performance.now() - openedAt < 300; }
      cancelBtn.addEventListener('click', function (e) { if (!stray(e)) cancel(); });
      confirmBtn.addEventListener('click', function (e) { if (!stray(e) && !early()) cleanup(true); });
      overlay.addEventListener('click', function (e) {
        if (!stray(e) && e.target === overlay && !early()) cancel();
      });

      if (window.ModalA11y) {
        window.ModalA11y.register(id, cancel);
        window.ModalA11y.onOpened(id);
      } else {
        // Fallback focus + Escape
        setTimeout(function () { cancelBtn.focus(); }, 0);
        overlay.addEventListener('keydown', function (e) {
          if (e.key === 'Escape') cancel();
        });
      }
      // Animate in
      requestAnimationFrame(function () { overlay.classList.add('is-visible'); });
    });
  }

  global.UIFeedback = {
    confirm: confirmDialog,
    dismissConfirm: function () { while (open.length) open[open.length - 1](); },
    toast: toast,
    error: function (msg) { return toast(msg, { variant: 'error' }); },
    success: function (msg) { return toast(msg, { variant: 'success' }); }
  };
})(window);
