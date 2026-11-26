// The title overlay when the day's lookups are used up: it says when they come back, on the person's clock.
// It borrows the page builder of watch_detail_harness.js.
// Usage: TZ=UTC node watch_detail_day_limit_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, 'watch_detail_harness.js'), 'utf8')
  .replace(/scenarios\[process\.argv\[2\]\][\s\S]*$/, 'module.exports = { load, openCard, APIError };');
const mod = { exports: {} };
new Function('require', 'module', '__dirname', source)(require, mod, __dirname);
const { load, openCard, APIError } = mod.exports;

const REF = 'tmdb-movie-77';

async function refused(error, retryAfter) {
  const err = new APIError(429, error);
  if (retryAfter !== undefined) err.data = { ok: false, error, retry_after: retryAfter };
  // The page's clock stands at 12:00 UTC.
  const p = load({ clock: true, routes: { ['/api/watch/title/' + REF]: err } });
  p.run();
  await openCard(p, REF);
  return { line: p.text('.ttl__failed-line'), why: p.text('.ttl__failed-why'), retry: !!p.q('.ttl__failed-actions .watch-btn--grey'), heading: p.text('#ttl-title') };
}

const scenarios = {
  soon: () => refused('title_day_limit', 9000),
  tomorrow: () => refused('title_day_limit', 30 * 3600),
  noWait: () => refused('title_day_limit'),
  other: () => refused('too_many_requests'),
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
