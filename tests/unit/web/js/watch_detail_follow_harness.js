// Scenarios for the title overlay that follow the page: a refused lookup, a window resized, an Arr slow to
// answer and an episode's facts. It borrows the page builder of watch_detail_harness.js.
// Usage: node watch_detail_follow_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, 'watch_detail_harness.js'), 'utf8')
  .replace(/scenarios\[process\.argv\[2\]\][\s\S]*$/, 'module.exports = { load, openCard, primary, MOVIE_DL, LIB_MOVIE, FACTS, APIError, settle };');
const mod = { exports: {} };
new Function('require', 'module', '__dirname', source)(require, mod, __dirname);
const { load, openCard, primary, MOVIE_DL, LIB_MOVIE, FACTS, APIError, settle } = mod.exports;

const REF = 'tmdb-movie-77';
const withDownload = (over) => Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, over) });

const scenarios = {
  // The lookups refused: the box says so in its own words, not that something broke.
  async tooMany() {
    const p = load({ routes: { ['/api/watch/title/' + REF]: new APIError(429, 'too_many_requests') } });
    p.run();
    await openCard(p, REF);
    return { why: p.text('.ttl__failed-why'), retry: !!p.q('.ttl__failed-actions .watch-btn--grey') };
  },

  // A window resized, or a phone turned, with a leaving title open: Keep follows the layout.
  async keepFollowsSize() {
    const opts = { admin: true, adminApi: { keep: () => {} }, narrow: false,
      routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS } };
    const p = load(opts);
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    const keepLink = () => !!p.q('.ttl__leaving .ttl__leaving-keep');
    const out = { wide: keepLink() };
    opts.narrow = true;
    p.fireWindow('resize');
    out.narrow = keepLink();
    out.words = p.text('.ttl__leaving');
    opts.narrow = false;
    p.fireWindow('resize');
    out.wideAgain = keepLink();
    out.wordsAgain = p.text('.ttl__leaving');
    return out;
  },

  // Radarr slow to answer: no Download until it has, however many asks that takes.
  async arrSlow() {
    let asks = 0;
    const answers = [{ state: null, answered: false }, { state: null, answered: false }, { state: 'queued', answered: true }];
    const p = load({ routes: { ['/api/watch/title/' + REF]: () => Promise.resolve(JSON.parse(JSON.stringify(withDownload(answers[Math.min(asks++, 2)])))) } });
    p.run();
    await openCard(p, REF);
    const waiting = () => p.text('.ttl__status');
    const out = { first: primary(p), firstSays: waiting(), waits: p.timers.filter((t) => t.fn).map((t) => t.ms) };
    const run = async () => { const due = p.timers.splice(0); due.forEach((t) => t.fn && t.fn()); await settle(); };
    await run();
    out.second = primary(p);
    out.secondSays = waiting();
    out.waitsThen = p.timers.filter((t) => t.fn).map((t) => t.ms);
    await run();
    out.third = primary(p);
    out.thirdSays = waiting();
    out.asks = asks;
    return out;
  },

  // Radarr never answers: the asks stop after a few.
  async arrSilent() {
    let asks = 0;
    const p = load({ routes: { ['/api/watch/title/' + REF]: () => { asks++; return Promise.resolve(JSON.parse(JSON.stringify(withDownload({ state: null, answered: false })))); } } });
    p.run();
    await openCard(p, REF);
    for (let i = 0; i < 8; i++) {
      const due = p.timers.splice(0);
      due.forEach((t) => t.fn && t.fn());
      await settle();
    }
    return { asks, primary: primary(p) };
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
