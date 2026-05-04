/**
 * core/api.js — centralised fetch wrapper for all mediaman API calls.
 *
 * Public surface (under the global MM.api namespace):
 *
 *   MM.api.get(endpoint, options?)         → Promise<data>
 *   MM.api.post(endpoint, body?, options?) → Promise<data>
 *   MM.api.put(endpoint, body?, options?)  → Promise<data>
 *   MM.api.patch(endpoint, body?, options?)→ Promise<data>
 *   MM.api.delete(endpoint, options?)      → Promise<data>
 *
 * Each method returns a Promise that resolves to the parsed JSON body.
 * If the response is not ok (HTTP >= 400), or the body contains
 * `{"ok": false}`, it rejects with an MM.api.APIError instance.
 *
 * MM.api.APIError extends Error with extra fields:
 *   .error   — machine-readable error code from the response body (string)
 *   .message — human-readable description
 *   .status  — HTTP status code (number)
 *
 * All requests include `credentials: 'same-origin'` for cookie auth.
 * Write methods (POST/PUT/PATCH/DELETE) default to
 * `Content-Type: application/json` and JSON-encode the body automatically.
 *
 * No external dependencies. Load this before any page-specific script
 * that calls MM.api.*.
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  /* ── APIError ──
   *
   * Three fields, three roles:
   *   .error    — machine-readable code (used by callers in switch statements)
   *   .message  — human-readable prose (used in toasts / inline error text)
   *   .issues   — optional structured per-rule failures (e.g. password-policy)
   *
   * Callers should prefer .message for display and .error for branching.
   */
  function APIError(error, message, status, issues) {
    this.name = 'APIError';
    this.error = error || 'unknown_error';
    this.message = message || error || 'API request failed';
    this.status = status || 0;
    this.issues = issues || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, APIError);
  }
  APIError.prototype = Object.create(Error.prototype);
  APIError.prototype.constructor = APIError;

  /* ── Core fetch helper ── */
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

    return fetch(endpoint, fetchOpts).then(function (resp) {
      var status = resp.status;
      return resp.json().catch(function () {
        /* Non-JSON body (e.g. a 204 No Content). */
        return {};
      }).then(function (data) {
        /* Prefer the server's human-readable .message over the machine code
         * when surfacing an error message to the user; fall back through
         * .error → resp.statusText → "HTTP <status>". The .issues array (if
         * present) is forwarded so the UI can render per-rule failures. */
        if (!resp.ok) {
          var errCode = (data && data.error) || ('http_' + status);
          var errMsg = (data && data.message) || (data && data.error) ||
                        resp.statusText || ('HTTP ' + status);
          throw new APIError(errCode, errMsg, status, data && data.issues);
        }
        /* Honour the `{"ok": false}` envelope this codebase uses. */
        if (data && data.ok === false) {
          var envCode = data.error || 'request_failed';
          var envMsg = data.message || data.error || 'Request failed';
          throw new APIError(envCode, envMsg, status, data.issues);
        }
        return data;
      });
    });
  }

  /* ── Public namespace ── */
  global.MM.api = {
    APIError: APIError,

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
  };

})(window);
