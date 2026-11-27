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
