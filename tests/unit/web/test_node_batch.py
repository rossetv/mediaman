"""The shared node runner the *_js.py tests use: many runs in one node process, each on its own."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.helpers.node_batch import run_node


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text("'use strict';\n" + source)
    return path


def test_a_run_answers_like_node_with_its_args_stdin_env_and_timers(tmp_path) -> None:
    script = _script(
        tmp_path,
        "echo.js",
        "const input = require('fs').readFileSync(0, 'utf8');\n"
        "setTimeout(() => console.log(JSON.stringify("
        "{ args: process.argv.slice(2), input, tz: process.env.SHOWN })), 20);\n",
    )
    done = run_node(script, "a", "b", input="given", env={"SHOWN": "yes"})
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == '{"args":["a","b"],"input":"given","tz":"yes"}\n'


def test_what_one_run_leaves_in_its_globals_or_modules_is_not_seen_by_the_next(tmp_path) -> None:
    _script(tmp_path, "shared.js", "module.exports = { count: 0 };\n")
    script = _script(
        tmp_path,
        "count.js",
        "const shared = require('./shared');\n"
        "shared.count += 1; globalThis.seen = (globalThis.seen || 0) + 1;\n"
        "process.stdout.write(JSON.stringify([shared.count, seen, process.argv[2]]));\n",
    )
    assert run_node(script, "first").stdout == '[1,1,"first"]'
    assert run_node(script, "second").stdout == '[1,1,"second"]'


@pytest.mark.parametrize(
    ("source", "code", "says"),
    [
        ("throw new Error('broke at the top');", 1, "broke at the top"),
        ("setTimeout(() => { throw new Error('broke in a timer'); }, 1);", 1, "broke in a timer"),
        ("Promise.reject(new Error('nobody caught this'));", 1, "nobody caught this"),
        (
            "process.stderr.write('own words'); process.exit(3); console.log('never');",
            3,
            "own words",
        ),
    ],
    ids=["thrown", "thrown_in_a_timer", "rejected", "exited"],
)
def test_a_run_that_fails_says_why_and_the_next_run_is_unharmed(tmp_path, source, code, says):
    bad = _script(tmp_path, "bad.js", source)
    good = _script(tmp_path, "good.js", "console.log('fine');")
    done = run_node(bad)
    assert done.returncode == code
    assert says in done.stderr
    assert done.stdout == ""
    assert run_node(good).stdout == "fine\n"


def test_a_node_that_dies_is_an_error_not_an_answer(tmp_path) -> None:
    script = _script(
        tmp_path, "die.js", "require('process').kill(require('process').pid, 'SIGKILL');"
    )
    with pytest.raises(RuntimeError, match="node stopped"):
        run_node(script)
    assert run_node(_script(tmp_path, "after.js", "console.log('again');")).stdout == "again\n"


def test_a_run_that_never_ends_times_out(tmp_path) -> None:
    script = _script(tmp_path, "forever.js", "setInterval(() => {}, 1000);")
    with pytest.raises(subprocess.TimeoutExpired):
        run_node(script, timeout=0.5)
    assert run_node(_script(tmp_path, "after.js", "console.log('again');")).stdout == "again\n"


def test_what_one_run_writes_to_the_environment_is_gone_for_the_next(tmp_path) -> None:
    probe = _script(
        tmp_path,
        "probe.js",
        "console.log(JSON.stringify([process.env.TZ, new Date(Date.UTC(2020, 0, 1)).getTimezoneOffset(),"
        " process.env.LEFT_BY_A_RUN]));",
    )
    before = run_node(probe, "before").stdout
    setter = _script(
        tmp_path,
        "setter.js",
        "process.env.TZ = 'Pacific/Kiritimati'; process.env.LEFT_BY_A_RUN = 'yes';\n"
        "console.log(new Date(Date.UTC(2020, 0, 1)).getTimezoneOffset());",
    )
    assert run_node(setter).stdout == "-840\n"
    assert run_node(probe, "after").stdout == before


@pytest.mark.parametrize(
    ("source", "code", "says"),
    [
        ("process.exitCode = 3; console.log('x');", 3, "x\n"),
        ("process.stdout.write('x', () => process.exit(4));", 4, "x"),
        (
            "process.on('exit', (code) => { console.log('exit ' + code); process.exitCode = 5; });",
            5,
            "exit 0\n",
        ),
        (
            "require('fs').readFile(__filename, () => { console.log('read'); process.exit(6); });",
            6,
            "read\n",
        ),
        (
            "require('fs').promises.readFile(__filename).then(() => { process.exitCode = 7; });",
            7,
            "",
        ),
    ],
    ids=["exit_code", "write_callback", "exit_listener", "fs_callback", "fs_promise"],
)
def test_a_run_ends_with_the_code_node_would_give(tmp_path, source, code, says) -> None:
    done = run_node(_script(tmp_path, "run.js", source))
    assert (done.returncode, done.stdout) == (code, says)


def test_an_exit_listener_belongs_to_its_run_alone(tmp_path) -> None:
    count = _script(tmp_path, "count.js", "console.log(process.listenerCount('exit'));")
    before = run_node(count, "before").stdout
    run_node(_script(tmp_path, "listens.js", "process.on('exit', () => console.log('bye'));"))
    assert run_node(count, "after").stdout == before


@pytest.mark.parametrize(
    "source",
    [
        "const t = setTimeout(() => {}, 60000); clearTimeout(+t);",
        "setInterval(() => {}, 1000).unref();",
        "const t = setInterval(() => {}, 1000); setImmediate(() => t.unref());",
    ],
    ids=["cleared_by_id", "unref", "unref_later"],
)
def test_a_run_does_not_wait_for_a_timer_node_would_not_wait_for(tmp_path, source) -> None:
    assert run_node(_script(tmp_path, "run.js", source), timeout=2).returncode == 0
