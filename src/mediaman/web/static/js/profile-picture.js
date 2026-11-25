/**
 * The picture dialog of the Profile page: pick a drawn avatar, or upload a photo and place it in its circle.
 *
 * A change is told to the document as `mm:avatar`, with the new address in `detail.url`.
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom), core/modal.js (MM.modal), modal-a11y.js,
 * ui-feedback.js (UIFeedback).
 */
(function () {
  'use strict';

  var MAX_BYTES = 10 * 1024 * 1024;
  var MIN_SIDE = 128;
  /* Safari draws no canvas larger than this each way. */
  var WORK_SIDE = 4096;
  var OUT_MIN = 256;
  var OUT_MAX = 512;
  var MAX_ZOOM = 4;
  var VIEW = 640;
  var PAD = 32;
  var ACROSS = VIEW - 2 * PAD;
  var STEP = 16;
  var WORDS = {
    type: 'That file is not a photo Mediaman can use. Choose a JPEG, PNG or WebP.',
    heic: 'This browser cannot open HEIC photos. Choose a JPEG, PNG or WebP.',
    place: 'This browser could not prepare that photo. Try another.',
    save: 'Could not save your picture. Try again.',
    big: 'That photo is too big. Choose a smaller one.',
  };

  function byId(id) { return document.getElementById(id); }
  function isBusy(button) { return button.getAttribute('aria-disabled') === 'true'; }

  /* Up to a tenth, so that just over the limit is not said to be the limit. */
  function megabytes(bytes) { return Math.ceil(bytes / 104857.6) / 10; }

  function isHeic(file) { return /^image\/hei[cf]/.test(file.type) || /\.hei[cf]$/i.test(file.name || ''); }

  /* Not by MM.api, which sends its bodies as JSON. */
  function sendPhoto(blob) {
    return fetch('/api/account/avatar/photo', {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': blob.type }, body: blob,
    }).catch(function () {
      throw new MM.api.APIError('network_error', 'Could not reach mediaman. Check your connection and try again.', 0);
    }).then(function (resp) {
      /* Signed out: asked by MM.api, this leaves for the sign-in page. */
      if (resp.status === 401) return MM.api.get('/api/account/avatar');
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (resp.ok) return data;
        /* A proxy in front may refuse it in words of its own. */
        if (resp.status === 413 && !data.message) data.message = WORDS.big;
        throw new MM.api.APIError(data.error, data.message, resp.status, null, data);
      });
    });
  }

  function start() {
    var dialog = byId('pic-dialog');
    var opener = byId('pic-open');
    if (!dialog || !opener) return;
    var now = byId('pic-now');
    var errorBox = byId('pic-error');
    var fileInput = byId('pic-file');
    var save = byId('pic-save');
    var removeBtn = byId('pic-remove');
    var radios = MM.dom.qa('input[type="radio"]', dialog);
    var drawn = radios.map(function (r) { return r.nextElementSibling.getAttribute('src'); });
    var own = Number(dialog.getAttribute('data-default'));
    /* What is kept on the server; `url` of a photo, `choice` of a drawing, neither of the default. */
    var kept = { choice: Number(dialog.getAttribute('data-choice')) || null, url: dialog.getAttribute('data-photo') };
    /* What is chosen here; `blob` of a photo not sent yet. */
    var draft = null;
    var letGo = null;
    var made = [];
    /* A photo read or drawn after the person has moved on is dropped. */
    var turn = 0;
    var place = null;

    function addressOf(pick) { return pick.url || drawn[(pick.choice || own) - 1]; }

    function showError(text) {
      errorBox.textContent = '';
      if (!text) return;
      var p = document.createElement('p');
      p.className = 'pic-err';
      p.appendChild(MM.dom.icon('i-circle-exclamation'));
      var span = document.createElement('span');
      span.textContent = text;
      p.appendChild(span);
      errorBox.appendChild(p);
      p.scrollIntoView({ block: 'nearest' });
    }

    function swapIn(img, url) {
      img.onload = function () {
        img.classList.remove('is-new');
        void img.offsetWidth;
        img.classList.add('is-new');
      };
      img.src = url;
    }

    function paint(still) {
      var url = addressOf(draft);
      if (still) now.src = url;
      else swapIn(now, url);
      byId('pic-kind').textContent = draft.url ? 'Your photo' : draft.choice ? 'Your pick' : 'Your default picture';
      removeBtn.hidden = !(draft.url || draft.choice);
      removeBtn.textContent = draft.url ? 'Remove photo' : 'Remove';
      radios.forEach(function (r) { r.checked = !draft.url && Number(r.value) === (draft.choice || own); });
      save.disabled = draft.url === kept.url && draft.choice === kept.choice;
    }

    function choose(pick) {
      draft = pick;
      showError('');
      paint();
    }

    function pane(placing) {
      byId('pic-pick').hidden = placing;
      byId('pic-place').hidden = !placing;
      byId('pic-title').textContent = placing ? 'Place your photo' : 'Your picture';
    }

    function setSaving(on) {
      save.textContent = '';
      if (on) {
        save.setAttribute('aria-disabled', 'true');
        var spinner = document.createElement('span');
        spinner.className = 'btn-spinner';
        spinner.setAttribute('aria-hidden', 'true');
        save.appendChild(spinner);
      } else {
        save.removeAttribute('aria-disabled');
      }
      save.appendChild(document.createTextNode(on ? 'Saving…' : 'Save'));
    }

    /* A drag of the photo or the slider may end on the backdrop, which is no click on it. */
    var pressedOn = null;
    dialog.addEventListener('pointerdown', function (e) { pressedOn = e.target; });
    dialog.addEventListener('click', function (e) {
      if (e.target === dialog && pressedOn !== dialog) e.stopImmediatePropagation();
    });

    var modal = MM.modal.setupDetail(dialog, {
      manageBodyOverflow: false, /* _ui_feedback.css holds the page */
      onClose: function () {
        if (letGo) letGo();
        letGo = null;
        turn += 1;
        place = null;
        fileInput.value = '';
        made.forEach(function (url) { URL.revokeObjectURL(url); });
        made = [];
      },
    });

    opener.addEventListener('click', function () {
      draft = { choice: kept.choice, url: kept.url };
      showError('');
      pane(false);
      paint(true);
      letGo = MM.dom.holdPage();
      /* Safari focuses no button that is clicked, and the dialog gives the focus back to what had it. */
      opener.focus({ preventScroll: true });
      modal.open();
    });
    byId('pic-close').addEventListener('click', modal.close);
    byId('pic-cancel').addEventListener('click', modal.close);

    radios.forEach(function (r) {
      r.addEventListener('change', function () { choose({ choice: Number(r.value), url: null }); });
    });
    removeBtn.addEventListener('click', function () {
      choose({ choice: null, url: null });
      byId('pic-upload').focus();
    });
    byId('pic-upload').addEventListener('click', function () { fileInput.click(); });
    fileInput.addEventListener('change', function () {
      var file = fileInput.files[0];
      fileInput.value = '';
      if (file) take(file);
    });

    function take(file) {
      var mine = turn += 1;
      showError('');
      if (!/^image\/(jpeg|png|webp)$/.test(file.type) && !isHeic(file)) { showError(WORDS.type); return; }
      if (file.size > MAX_BYTES) {
        showError('That photo is ' + megabytes(file.size) + ' MB. The most is 10 MB.');
        return;
      }
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        if (mine !== turn) return;
        var w = img.naturalWidth, h = img.naturalHeight;
        if (Math.min(w, h) < MIN_SIDE) {
          showError('That photo is ' + w + ' × ' + h + ' pixels. It needs at least ' + MIN_SIDE + ' each way.');
          return;
        }
        startPlacing(img, file.type === 'image/jpeg' || isHeic(file) ? 'image/jpeg' : 'image/png');
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        if (mine === turn) showError(isHeic(file) ? WORDS.heic : WORDS.type);
      };
      img.src = url;
    }

    /* A large photo drawn smaller once moves smoothly. */
    function workCopy(img) {
      var k = Math.min(1, WORK_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      var copy = document.createElement('canvas');
      copy.width = Math.max(1, Math.round(img.naturalWidth * k));
      copy.height = Math.max(1, Math.round(img.naturalHeight * k));
      copy.getContext('2d').drawImage(img, 0, 0, copy.width, copy.height);
      return copy;
    }

    var crop = byId('pic-crop');
    var canvas = crop.querySelector('canvas');
    var zoomInput = byId('pic-zoom');

    function startPlacing(img, type) {
      var copy = workCopy(img);
      place = { copy: copy, type: type, fit: ACROSS / Math.min(copy.width, copy.height), zoom: 1, x: 0, y: 0 };
      pane(true);
      zoomTo(1);
      crop.focus({ preventScroll: true });
    }

    function draw() {
      var w = place.copy.width * place.fit * place.zoom, h = place.copy.height * place.fit * place.zoom;
      var roomX = Math.max(0, (w - ACROSS) / 2), roomY = Math.max(0, (h - ACROSS) / 2);
      place.x = Math.max(-roomX, Math.min(roomX, place.x));
      place.y = Math.max(-roomY, Math.min(roomY, place.y));
      var g = canvas.getContext('2d');
      g.imageSmoothingQuality = 'high';
      g.clearRect(0, 0, VIEW, VIEW);
      g.drawImage(place.copy, (VIEW - w) / 2 + place.x, (VIEW - h) / 2 + place.y, w, h);
    }

    function move(dx, dy) {
      place.x += dx;
      place.y += dy;
      draw();
    }

    /* Zooms about the middle of the circle, or about the point (cx, cy) from it. */
    function zoomTo(zoom, cx, cy) {
      zoom = Math.max(1, Math.min(MAX_ZOOM, zoom));
      var k = zoom / place.zoom;
      place.x = (place.x - (cx || 0)) * k + (cx || 0);
      place.y = (place.y - (cy || 0)) * k + (cy || 0);
      place.zoom = zoom;
      zoomInput.value = Math.round(zoom * 100);
      draw();
    }

    var fingers = {};
    function scale() { return VIEW / (crop.clientWidth || VIEW); }
    function pinch() {
      var ids = Object.keys(fingers);
      if (ids.length < 2) return null;
      var a = fingers[ids[0]], b = fingers[ids[1]];
      var box = crop.getBoundingClientRect();
      return {
        gap: Math.hypot(a.x - b.x, a.y - b.y),
        x: ((a.x + b.x) / 2 - box.left) * scale() - VIEW / 2,
        y: ((a.y + b.y) / 2 - box.top) * scale() - VIEW / 2,
      };
    }
    crop.addEventListener('pointerdown', function (e) {
      if (e.button) return;
      crop.setPointerCapture(e.pointerId);
      fingers[e.pointerId] = { x: e.clientX, y: e.clientY };
    });
    crop.addEventListener('pointermove', function (e) {
      var from = fingers[e.pointerId];
      if (!from || !place) return;
      var before = pinch();
      fingers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var after = pinch();
      if (!before) {
        move((e.clientX - from.x) * scale(), (e.clientY - from.y) * scale());
      } else {
        place.x += after.x - before.x;
        place.y += after.y - before.y;
        zoomTo(before.gap ? place.zoom * after.gap / before.gap : place.zoom, after.x, after.y);
      }
    });
    function lift(e) { delete fingers[e.pointerId]; }
    crop.addEventListener('pointerup', lift);
    crop.addEventListener('pointercancel', lift);
    crop.addEventListener('lostpointercapture', lift);
    crop.addEventListener('wheel', function (e) {
      e.preventDefault();
      if (place) zoomTo(place.zoom * Math.exp(-e.deltaY / 300));
    }, { passive: false });
    crop.addEventListener('keydown', function (e) {
      if (!place || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'ArrowLeft') move(STEP, 0);
      else if (e.key === 'ArrowRight') move(-STEP, 0);
      else if (e.key === 'ArrowUp') move(0, STEP);
      else if (e.key === 'ArrowDown') move(0, -STEP);
      else if (e.key === '+' || e.key === '=') zoomTo(place.zoom * 1.1);
      else if (e.key === '-') zoomTo(place.zoom / 1.1);
      else return;
      e.preventDefault();
    });
    zoomInput.addEventListener('input', function () { if (place) zoomTo(zoomInput.value / 100); });

    byId('pic-back').addEventListener('click', function () {
      turn += 1;
      place = null;
      pane(false);
      byId('pic-upload').focus({ preventScroll: true });
    });

    byId('pic-use').addEventListener('click', function () {
      if (!place) return;
      var mine = turn += 1;
      /* The square of the photo that stands around the circle, in its own pixels. */
      var unit = place.fit * place.zoom;
      var side = ACROSS / unit;
      var size = Math.max(OUT_MIN, Math.min(OUT_MAX, Math.round(side)));
      var out = document.createElement('canvas');
      out.width = out.height = size;
      var g = out.getContext('2d');
      g.imageSmoothingQuality = 'high';
      g.drawImage(place.copy,
        (place.copy.width - side) / 2 - place.x / unit, (place.copy.height - side) / 2 - place.y / unit, side, side,
        0, 0, size, size);
      out.toBlob(function (blob) {
        if (mine !== turn) return;
        place = null;
        pane(false);
        if (!blob) {
          showError(WORDS.place);
          byId('pic-upload').focus({ preventScroll: true });
          return;
        }
        var url = URL.createObjectURL(blob);
        made.push(url);
        choose({ choice: null, url: url, blob: blob });
        save.focus({ preventScroll: true });
      }, place.type, 0.9);
    });

    save.addEventListener('click', function () {
      if (isBusy(save)) return;
      var sent = draft;
      showError('');
      setSaving(true);
      var request = sent.blob ? sendPhoto(sent.blob)
        : sent.choice ? MM.api.put('/api/account/avatar', { choice: sent.choice })
          : MM.api.delete('/api/account/avatar');
      request.then(function (data) {
        setSaving(false);
        kept = { choice: data.choice, url: data.kind === 'photo' ? data.url : null };
        MM.dom.qa('img', opener).forEach(function (img) { swapIn(img, data.url); });
        document.dispatchEvent(new CustomEvent('mm:avatar', { detail: { url: data.url } }));
        UIFeedback.toast(data.kind === 'default' ? 'Picture removed.' : 'Picture saved.', { variant: 'success' });
        if (draft === sent) modal.close();
        else paint(true);
      }, function (err) {
        setSaving(false);
        var words = MM.api.messageOf(err, WORDS.save);
        if (dialog.getAttribute('aria-hidden') === 'true') UIFeedback.error(words);
        else showError(words);
      });
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
