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

  function ensureToastHost() {
    var host = document.getElementById('ui-toast-host');
    if (!host) {
      host = el('div', { id: 'ui-toast-host', class: 'ui-toast-host',
                         role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(host);
    }
    return host;
  }

  function toast(message, opts) {
    opts = opts || {};
    var variant = opts.variant || 'info';
    var host = ensureToastHost();
    var node = el('div', {
      class: 'ui-toast ui-toast--' + variant,
      role: variant === 'error' ? 'alert' : 'status'
    });
    node.textContent = message;
    host.appendChild(node);
    // Animate in on next frame
    requestAnimationFrame(function () { node.classList.add('is-visible'); });
    var duration = opts.duration || 4000;
    setTimeout(function () {
      node.classList.remove('is-visible');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 250);
    }, duration);
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
      var cancelBtn = el('button', { type: 'button', class: 'btn btn-ghost',
                                     text: opts.cancelLabel || 'Cancel' });
      var confirmClass = 'btn ' + (opts.confirmVariant === 'danger' ? 'btn-danger' : 'btn-keep');
      var confirmBtn = el('button', { type: 'button', class: confirmClass,
                                      text: opts.confirmLabel || 'Confirm' });
      actions.appendChild(cancelBtn);
      actions.appendChild(confirmBtn);
      card.appendChild(h);
      if (opts.body) card.appendChild(p);
      card.appendChild(actions);
      overlay.appendChild(card);
      document.body.appendChild(overlay);

      function cleanup(result) {
        if (window.ModalA11y) window.ModalA11y.onClosed(titleId);
        overlay.parentNode.removeChild(overlay);
        resolve(result);
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
