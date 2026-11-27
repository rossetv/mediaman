/* Runs many one-shot node scripts (`node script.js args...`) in this one process.

   Each run gets a fresh vm context: its own globals, module cache, process.argv,
   stdin, stdout, stderr and exit code, so nothing one run leaves behind reaches
   the next. A run ends as node itself would: at process.exit, at an uncaught
   error or unhandled rejection (code 1), or when no timer of its own is left.

   Requests arrive one per line on stdin as JSON
     {"file": "/abs/script.js", "args": [...], "env": {...}, "input": "..." | null}
   and each answer is written as one JSON line {"code", "stdout", "stderr"} on the fd
   named by the first argument,
   so that nothing a script prints can be taken for an answer. */
'use strict';

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const util = require('util');
const vm = require('vm');
const Module = require('module');

const SKIP = new Set(['global', 'globalThis', 'process', 'console', 'require', 'module', 'exports',
  'localStorage', 'sessionStorage', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'setImmediate', 'clearImmediate']);
const scripts = new Map();
let current = null;

class Exit extends Error {}

function compiled(file) {
  if (!scripts.has(file)) {
    scripts.set(file, new vm.Script(Module.wrap(fs.readFileSync(file, 'utf8')), { filename: file }));
  }
  return scripts.get(file);
}

function stream(run, key) {
  return { write(s) { if (!run.done) run[key] += String(s); return true; }, isTTY: false };
}

function logger(run) {
  const to = (key) => (...args) => { if (!run.done) run[key] += util.format(...args) + '\n'; };
  return { log: to('stdout'), info: to('stdout'), debug: to('stdout'), error: to('stderr'), warn: to('stderr') };
}

function crash(run, err) {
  if (run.done || err instanceof Exit) return;
  run.stderr += String((err && err.stack) || err) + '\n';
  end(run, 1);
}

function end(run, code) {
  if (run.done) return;
  run.code = code;
  run.done = true;
  run.timers.forEach((t) => { clearTimeout(t); clearInterval(t); clearImmediate(t); });
  run.timers.clear();
  run.wake();
}

function timers(run) {
  const call = (fn, args) => { try { fn(...args); } catch (e) { crash(run, e); } };
  const forget = (t) => { run.timers.delete(t); if (!run.timers.size) run.wake(); };
  return {
    setTimeout(fn, ms, ...args) {
      const t = setTimeout(() => { forget(t); call(fn, args); }, ms);
      run.timers.add(t);
      return t;
    },
    setInterval(fn, ms, ...args) {
      const t = setInterval(() => call(fn, args), ms);
      run.timers.add(t);
      return t;
    },
    setImmediate(fn, ...args) {
      const t = setImmediate(() => { forget(t); call(fn, args); });
      run.timers.add(t);
      return t;
    },
    clearTimeout(t) { clearTimeout(t); forget(t); },
    clearInterval(t) { clearInterval(t); forget(t); },
    clearImmediate(t) { clearImmediate(t); forget(t); },
  };
}

function context(run) {
  const ctx = vm.createContext({});
  const own = new Set(Object.getOwnPropertyNames(vm.runInContext('globalThis', ctx)));
  for (const name of Object.getOwnPropertyNames(globalThis)) {
    if (own.has(name) || SKIP.has(name)) continue;
    try { ctx[name] = globalThis[name]; } catch (e) { /* a getter node refuses without a flag */ }
  }
  Object.assign(ctx, timers(run));
  ctx.global = vm.runInContext('globalThis', ctx);
  ctx.console = logger(run);
  const fields = {
    argv: [process.execPath, run.file, ...run.args],
    env: process.env,
    stdout: stream(run, 'stdout'),
    stderr: stream(run, 'stderr'),
    exit(code) { end(run, code === undefined ? 0 : Number(code)); throw new Exit(); },
  };
  // process's own stdout and stderr are getters, so the run's are defined over them.
  ctx.process = Object.create(process, Object.fromEntries(
    Object.entries(fields).map(([k, value]) => [k, { value, writable: true, enumerable: true }])));
  return ctx;
}

function load(run, file) {
  if (run.modules.has(file)) return run.modules.get(file).exports;
  const module = { id: file, filename: file, exports: {}, loaded: false };
  run.modules.set(file, module);
  const own = (id) => {
    if (id === 'fs' || id === 'node:fs') return run.fs;
    if (!id.startsWith('.') && !path.isAbsolute(id)) return require(id);
    return load(run, own.resolve(id));
  };
  own.main = run.main || module;
  own.resolve = (id) => Module._resolveFilename(path.resolve(path.dirname(file), id), null);
  run.main = run.main || module;
  compiled(file).runInContext(run.ctx).call(module.exports, module.exports, own, module, file, path.dirname(file));
  module.loaded = true;
  return module.exports;
}

async function execute(req) {
  const run = {
    file: req.file, args: req.args || [], code: 0, stdout: '', stderr: '', done: false,
    timers: new Set(), modules: new Map(), main: null, wake: () => {},
  };
  const input = req.input == null ? '' : req.input;
  run.fs = Object.assign(Object.create(fs), {
    readFileSync(p, ...rest) {
      if (p !== 0 && p !== '/dev/stdin') return fs.readFileSync(p, ...rest);
      const enc = typeof rest[0] === 'string' ? rest[0] : rest[0] && rest[0].encoding;
      return enc ? input : Buffer.from(input);
    },
  });
  const saved = {};
  for (const [k, v] of Object.entries(req.env || {})) { saved[k] = process.env[k]; process.env[k] = v; }
  current = run;
  try {
    run.ctx = context(run);
    try { load(run, run.file); } catch (e) { crash(run, e); }
    // Like node: stay while a timer of this run is pending, then end once the queue is quiet.
    while (!run.done) {
      if (run.timers.size) await new Promise((resolve) => { run.wake = resolve; });
      else {
        await new Promise((resolve) => setImmediate(resolve));
        if (!run.timers.size) await new Promise((resolve) => setImmediate(resolve));
        if (!run.timers.size) end(run, 0);
      }
    }
  } finally {
    current = null;
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
  return { code: run.code, stdout: run.stdout, stderr: run.stderr };
}

// An unhandled rejection reaches here too, as it would end a plain node run.
process.on('uncaughtException', (err) => {
  if (current) crash(current, err);
  else { process.stderr.write(util.format('outside any run:', err) + '\n'); process.exit(70); }
});

const answers = fs.createWriteStream(null, { fd: Number(process.argv[2]) });
const queue = [];
let busy = false;
async function drain() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    const req = queue.shift();
    let out;
    try { out = await execute(req); } catch (e) { out = { code: 70, stdout: '', stderr: String(e && e.stack || e) }; }
    answers.write(JSON.stringify(out) + '\n');
  }
  busy = false;
}
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  if (!line.trim()) return;
  queue.push(JSON.parse(line));
  drain();
}).on('close', () => { const wait = setInterval(() => { if (!busy) { clearInterval(wait); answers.end(); } }, 5); });
