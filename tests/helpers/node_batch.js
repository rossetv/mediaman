/* Runs many one-shot node scripts (`node script.js args...`) in this one process.

   Each run gets a fresh vm context: its own globals, module cache, process.argv,
   stdin, stdout, stderr, exit code and 'exit' listeners, and process.env is put
   back as it was after each run, so nothing one run leaves behind reaches the
   next. A run ends as node itself would: at process.exit, at an uncaught error or
   unhandled rejection (code 1), or when no timer or pending I/O of its own is left;
   its code is process.exitCode as its 'exit' listeners leave it.

   Limits, which no harness relies on:
   - Async work node does not list as an active resource (crypto.subtle, async
     zlib) does not keep a run alive. If it fails after its run has ended, the
     runner stops with a message naming that run's file.
   - process.on works for 'exit' only; any other event throws.
   - Built-in modules (path, vm, crypto, v8...) and their flags are shared by every
     run, and objects they make come from this realm, so `instanceof` against the
     run's own Error or Uint8Array is false for them.

   Requests arrive one per line on stdin as JSON
     {"file": "/abs/script.js", "args": [...], "env": {...}, "input": "..." | null}
   and each answer is written as one JSON line {"code", "stdout", "stderr"} on the fd
   named by the first argument,
   so that nothing a script prints can be taken for an answer. */
'use strict';

const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { AsyncLocalStorage } = require('async_hooks');
const util = require('util');
const vm = require('vm');
const Module = require('module');

const SKIP = new Set(['global', 'globalThis', 'process', 'console', 'require', 'module', 'exports',
  'localStorage', 'sessionStorage', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'setImmediate', 'clearImmediate']);
// The run's timers are tracked one by one, and the runner's own waits are timers too.
const UNTRACKED = new Set(['Timeout', 'Immediate']);
const scripts = new Map();
const owner = new AsyncLocalStorage();
let current = null;

class Exit extends Error {
  constructor(run) { super('process.exit'); this.run = run; }
}

function pendingIo() {
  return process.getActiveResourcesInfo().filter((type) => !UNTRACKED.has(type)).length;
}

function compiled(file) {
  if (!scripts.has(file)) {
    scripts.set(file, new vm.Script(Module.wrap(fs.readFileSync(file, 'utf8')), { filename: file }));
  }
  return scripts.get(file);
}

function stream(run, key) {
  return {
    write(s, encoding, callback) {
      if (run.done) return true;
      run[key] += String(s);
      const then = typeof encoding === 'function' ? encoding : callback;
      if (typeof then === 'function') run.clock.setImmediate(then);
      return true;
    },
    isTTY: false,
  };
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

// As node does: process.exit(code) sets exitCode, the 'exit' listeners run and may
// change it, and an exit inside a listener stops the rest.
function end(run, code) {
  if (run.ending) return;
  run.ending = true;
  const proc = run.ctx.process;
  if (code !== undefined) proc.exitCode = code;
  for (const listener of [...run.exitListeners]) {
    try { listener.call(proc, Number(proc.exitCode || 0)); } catch (e) {
      if (!(e instanceof Exit)) { run.stderr += String((e && e.stack) || e) + '\n'; proc.exitCode = 1; }
      break;
    }
  }
  run.code = Number(proc.exitCode || 0);
  run.done = true;
  run.timers.forEach((t) => { clearTimeout(t); clearInterval(t); clearImmediate(t); });
  run.timers.clear();
  run.wake();
}

// Like node, a run stays for a timer only while it is ref'd.
function waiting(run) {
  for (const t of run.timers) if (!t.hasRef || t.hasRef()) return true;
  return false;
}

function timers(run) {
  const call = (fn, args) => { try { fn(...args); } catch (e) { crash(run, e); } };
  const forget = (t) => {
    // A Timeout may be cleared by its numeric id, as node allows.
    const timer = typeof t === 'number' ? [...run.timers].find((x) => +x === t) : t;
    run.timers.delete(timer);
    if (!waiting(run)) run.wake();
    return timer;
  };
  const track = (t) => {
    const unref = t.unref;
    t.unref = function () { unref.call(this); if (!waiting(run)) run.wake(); return this; };
    run.timers.add(t);
    return t;
  };
  return {
    setTimeout(fn, ms, ...args) {
      const t = setTimeout(() => { forget(t); call(fn, args); }, ms);
      return track(t);
    },
    setInterval(fn, ms, ...args) {
      return track(setInterval(() => call(fn, args), ms));
    },
    setImmediate(fn, ...args) {
      const t = setImmediate(() => { forget(t); call(fn, args); });
      return track(t);
    },
    clearTimeout(t) { clearTimeout(forget(t)); },
    clearInterval(t) { clearInterval(forget(t)); },
    clearImmediate(t) { clearImmediate(forget(t)); },
  };
}

function listeners(run) {
  const only = (event) => {
    if (event !== 'exit') throw new Error(`process.on('${String(event)}') is not supported in run_node`);
  };
  const add = (prepend) => function (event, fn) {
    only(event);
    if (prepend) run.exitListeners.unshift(fn); else run.exitListeners.push(fn);
    return this;
  };
  const remove = function (event, fn) {
    only(event);
    const i = run.exitListeners.lastIndexOf(fn);
    if (i >= 0) run.exitListeners.splice(i, 1);
    return this;
  };
  return {
    on: add(false), addListener: add(false), once: add(false),
    prependListener: add(true), prependOnceListener: add(true),
    off: remove, removeListener: remove,
    removeAllListeners(event) { if (event !== undefined) only(event); run.exitListeners.length = 0; return this; },
    listenerCount(event) { return event === 'exit' ? run.exitListeners.length : 0; },
    listeners(event) { return event === 'exit' ? [...run.exitListeners] : []; },
  };
}

function context(run) {
  const ctx = vm.createContext({});
  const own = new Set(Object.getOwnPropertyNames(vm.runInContext('globalThis', ctx)));
  for (const name of Object.getOwnPropertyNames(globalThis)) {
    if (own.has(name) || SKIP.has(name)) continue;
    try { ctx[name] = globalThis[name]; } catch (e) { /* a getter node refuses without a flag */ }
  }
  Object.assign(ctx, run.clock);
  ctx.global = vm.runInContext('globalThis', ctx);
  ctx.console = logger(run);
  const fields = {
    argv: [process.execPath, run.file, ...run.args],
    env: process.env,
    stdout: stream(run, 'stdout'),
    stderr: stream(run, 'stderr'),
    exitCode: undefined,
    exit(code) { end(run, code === undefined ? undefined : Number(code)); throw new Exit(run); },
    ...listeners(run),
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
    timers: new Set(), modules: new Map(), main: null, wake: () => {}, exitListeners: [],
    ending: false,
  };
  run.clock = timers(run);
  const input = req.input == null ? '' : req.input;
  run.fs = Object.assign(Object.create(fs), {
    readFileSync(p, ...rest) {
      if (p !== 0 && p !== '/dev/stdin') return fs.readFileSync(p, ...rest);
      const enc = typeof rest[0] === 'string' ? rest[0] : rest[0] && rest[0].encoding;
      return enc ? input : Buffer.from(input);
    },
  });
  const env = { ...process.env };
  Object.assign(process.env, req.env);
  const idle = pendingIo();
  current = run;
  try {
    run.ctx = context(run);
    owner.run(run, () => { try { load(run, run.file); } catch (e) { crash(run, e); } });
    // Like node: stay while a timer or I/O of this run is pending, then end once the queue is quiet.
    while (!run.done) {
      if (waiting(run)) await new Promise((resolve) => { run.wake = resolve; });
      else if (pendingIo() > idle) await new Promise((resolve) => setTimeout(resolve, 1));
      else {
        await new Promise((resolve) => setImmediate(resolve));
        if (!waiting(run)) await new Promise((resolve) => setImmediate(resolve));
        if (!waiting(run) && pendingIo() <= idle) { run.natural = true; end(run); }
      }
    }
  } finally {
    current = null;
    // Assigning TZ, or deleting it, is what makes node read the zone again.
    for (const k of Object.keys(process.env)) if (!(k in env)) delete process.env[k];
    for (const [k, v] of Object.entries(env)) if (process.env[k] !== v) process.env[k] = v;
  }
  return { code: run.code, stdout: run.stdout, stderr: run.stderr };
}

// An unhandled rejection reaches here too, as it would end a plain node run.
process.on('uncaughtException', (err) => {
  const run = (err instanceof Exit && err.run) || owner.getStore() || current;
  if (run && !run.done) crash(run, err);
  // Node would have stopped a run that exited before this work came back.
  else if (run && !run.natural) return;
  else {
    const where = run ? `async work left by a run of ${run.file} failed after it ended:` : 'outside any run:';
    process.stderr.write(util.format(where, err) + '\n');
    process.exit(70);
  }
});

// Written synchronously, so the runner itself never has I/O pending during a run.
const answers = Number(process.argv[2]);
function answer(out) {
  const bytes = Buffer.from(JSON.stringify(out) + '\n');
  for (let at = 0; at < bytes.length;) at += fs.writeSync(answers, bytes, at);
}
const queue = [];
let busy = false;
async function drain() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    const req = queue.shift();
    let out;
    try { out = await execute(req); } catch (e) { out = { code: 70, stdout: '', stderr: String(e && e.stack || e) }; }
    answer(out);
  }
  busy = false;
}
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  if (!line.trim()) return;
  queue.push(JSON.parse(line));
  drain();
});
