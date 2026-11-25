/**
 * core/api.js — centralised fetch wrapper for all mediaman API calls.
 *
 * Public surface (under the global MM.api namespace):
 *
 *   MM.api.get(endpoint, options?)             → Promise<data>
 *   MM.api.post(endpoint, body?, options?)     → Promise<data>
 *   MM.api.put(endpoint, body?, options?)      → Promise<data>
 *   MM.api.patch(endpoint, body?, options?)    → Promise<data>
 *   MM.api.delete(endpoint, options?)          → Promise<data>
 *   MM.api.postForm(endpoint, params, options?) → Promise<data>
 *
 * Each method returns a Promise that resolves to the parsed JSON body.
 * If the response is not ok (HTTP >= 400), or the body contains
 * `{"ok": false}`, it rejects with an MM.api.APIError instance.
 *
 * MM.api.APIError extends Error with extra fields:
 *   .error   — machine-readable error code from the response body (string)
 *   .message — human-readable description
 *   .status  — HTTP status code (number)
 *   .issues  — optional structured per-rule failures (e.g. password-policy)
 *   .data    — full parsed JSON body so callers can read non-standard
 *              fields the server returned alongside an error
 *              (e.g. cooldown's ``next_available_at``)
 *
 * All requests include `credentials: 'same-origin'` for cookie auth.
 * Write methods (POST/PUT/PATCH/DELETE) default to
 * `Content-Type: application/json` and JSON-encode the body automatically.
 *
 * ``postForm`` builds an ``application/x-www-form-urlencoded`` body from
 * a plain object or URLSearchParams and is the right call when the
 * server endpoint reads ``Form()`` parameters rather than JSON.
 *
 * Options may include ``signal`` to wire up an AbortController for
 * cancellable requests; aborted fetches reject with the native
 * ``AbortError`` (``err.name === 'AbortError'``) so callers can detect
 * cancellation without inspecting status.
 *
 * A network failure (the request never reached the server) rejects with an
 * APIError whose ``.error`` is ``'network_error'``, ``.status`` is 0 and
 * ``.message`` is one plain sentence every page can show as is. Aborts are
 * not mapped.
 *
 * A 401 from an admin page sends the browser to /login (and back to the page
 * after signing in) and never settles,
 * so callers do not flash an error while the page unloads. The public keep,
 * download and login pages, reached by emailed token or without a session,
 * keep their own 401 handling.
 *
 * A page holding edits that a redirect would throw away sets
 * ``MM.api.hasUnsavedChanges = () => boolean``. While it returns true a 401
 * shows an error and rejects as a normal APIError instead of redirecting, so
 * the caller's error path runs and the form keeps its values.
 *
 * No external dependencies. Load this before any page-specific script
 * that calls MM.api.*.
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  /*
   * Fields, by role:
   *   .error    — machine-readable code (used by callers in switch statements)
   *   .message  — human-readable prose (used in toasts / inline error text)
   *   .status   — HTTP status code (helpful for 401/403/429 branching)
   *   .issues   — optional structured per-rule failures (e.g. password-policy)
   *   .data     — the full parsed response body so callers can read
   *               non-standard fields a specific endpoint returns alongside
   *               an error envelope (e.g. ``next_available_at`` on 429
   *               cooldowns). Always present on APIError; may be {} when
   *               the response had no JSON body.
   *
   * Callers should prefer .message for display and .error for branching.
   */
  function APIError(error, message, status, issues, data) {
    this.name = 'APIError';
    this.error = error || 'unknown_error';
    this.message = message || error || 'API request failed';
    this.status = status || 0;
    this.issues = issues || null;
    this.data = data || {};
    if (Error.captureStackTrace) Error.captureStackTrace(this, APIError);
  }
  APIError.prototype = Object.create(Error.prototype);
  APIError.prototype.constructor = APIError;

  /* Pages reached by emailed token (or without a session) handle a 401 themselves. */
  var PUBLIC_PATH = /^\/(keep|download|login)(\/|$)/;

  /* Sign-in, and back to this page after it; the browser keeps the hash over the redirects. */
  function loginUrl() {
    var here = window.location.pathname + (window.location.search || '');
    return '/login' + (here === '/watch' ? '' : '?next=' + encodeURIComponent(here)) +
      (window.location.hash || '');
  }

  var NETWORK_MESSAGE = 'Could not reach mediaman. Check your connection and try again.';

  function request(method, endpoint, body, options) {
    options = options || {};
    var isWrite = method !== 'GET' && method !== 'HEAD';
    var headers = {};

    /* Default Content-Type for write methods when a body is provided. */
    if (isWrite && body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
    }
    /* Caller-supplied headers override our defaults. */
    if (options.headers) {
      Object.keys(options.headers).forEach(function (k) {
        headers[k] = options.headers[k];
      });
    }

    var fetchOpts = {
      method: method,
      credentials: 'same-origin',
      headers: headers,
    };
    if (body !== undefined && body !== null && isWrite) {
      fetchOpts.body = typeof body === 'string' ? body : JSON.stringify(body);
    }
    /* Allow callers to pass through any remaining fetch options (e.g. signal). */
    if (options.signal) fetchOpts.signal = options.signal;

    return fetch(endpoint, fetchOpts).catch(function (err) {
      /* fetch rejects with a TypeError only when the request never reached
       * the server; an abort keeps its native AbortError. */
      if (err && err.name === 'TypeError') {
        throw new APIError('network_error', NETWORK_MESSAGE, 0);
      }
      throw err;
    }).then(function (resp) {
      var status = resp.status;
      return resp.json().catch(function () {
        /* Non-JSON body (e.g. a 204 No Content). */
        return {};
      }).then(function (data) {
        /* Prefer the server's human-readable .message over the machine code
         * when surfacing an error message to the user; fall back through
         * .error → resp.statusText → "HTTP <status>". The .issues array (if
         * present) is forwarded so the UI can render per-rule failures. */
        if (status === 401 && !PUBLIC_PATH.test(window.location.pathname)) {
          if (!global.MM.api.hasUnsavedChanges()) {
            window.location.href = loginUrl();
            return new Promise(function () {});
          }
          if (window.UIFeedback) {
            window.UIFeedback.error(
              'Your session has expired. Log in again in a new tab, then save.');
          }
        }
        if (!resp.ok) {
          var errCode = (data && data.error) || ('http_' + status);
          var errMsg = (data && data.message) || (data && data.error) ||
                        resp.statusText || ('HTTP ' + status);
          throw new APIError(errCode, errMsg, status, data && data.issues, data);
        }
        /* Honour the `{"ok": false}` envelope this codebase uses. */
        if (data && data.ok === false) {
          var envCode = data.error || 'request_failed';
          var envMsg = data.message || data.error || 'Request failed';
          throw new APIError(envCode, envMsg, status, data.issues, data);
        }
        return data;
      });
    });
  }

  global.MM.api = {
    APIError: APIError,

    /* Replaced by a page whose unsaved edits a login redirect would lose. */
    hasUnsavedChanges: function () { return false; },

    /* The app's own message for a failure, or ``fallback``. Only a JSON body
     * that set ``message`` counts: otherwise the error holds a status line
     * or a bare code, which vary by proxy and mean nothing to a person. A
     * lost connection keeps its own sentence. */
    messageOf: function (err, fallback) {
      if (!(err instanceof APIError)) return fallback;
      if (err.error === 'network_error') return err.message;
      var msg = err.data && err.data.message;
      return (typeof msg === 'string' && msg) ? msg : fallback;
    },

    get: function (endpoint, options) {
      return request('GET', endpoint, undefined, options);
    },
    post: function (endpoint, body, options) {
      return request('POST', endpoint, body, options);
    },
    put: function (endpoint, body, options) {
      return request('PUT', endpoint, body, options);
    },
    patch: function (endpoint, body, options) {
      return request('PATCH', endpoint, body, options);
    },
    delete: function (endpoint, options) {
      return request('DELETE', endpoint, undefined, options);
    },

    /**
     * POST an ``application/x-www-form-urlencoded`` body. ``params`` may
     * be a plain object (each value coerced to a string) or an existing
     * URLSearchParams instance. Server-side, this matches FastAPI's
     * ``Form(...)`` parameters — used by the legacy ``/api/media/.../keep``
     * endpoint that predates the JSON-everywhere convention.
     */
    postForm: function (endpoint, params, options) {
      options = options || {};
      var search = (params instanceof URLSearchParams)
        ? params
        : new URLSearchParams(params || {});
      var headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
      if (options.headers) {
        Object.keys(options.headers).forEach(function (k) {
          headers[k] = options.headers[k];
        });
      }
      return request('POST', endpoint, search.toString(), {
        headers: headers,
        signal: options.signal,
      });
    },
  };

})(window);
