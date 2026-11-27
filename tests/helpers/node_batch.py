"""Run the node test harnesses many times in one node process instead of one process each.

``run_node(script, *args)`` answers like ``subprocess.run(["node", script, *args])``:
a ``CompletedProcess`` with the script's exit code, stdout and stderr. Each run gets a
fresh vm context in ``node_batch.js``, so runs share nothing but the process. The same
run asked for twice is answered once, from memory, since the harnesses are deterministic.

A node process that dies, or stops answering, raises here: a test never reads an answer
that was not given.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import select
import subprocess
import tempfile
import time
from pathlib import Path

_RUNNER = Path(__file__).with_name("node_batch.js")


class _Node:
    """One node process running ``node_batch.js``, asked one run at a time."""

    def __init__(self, node_args: tuple[str, ...]) -> None:
        self._answers, write = os.pipe()
        self._stderr = tempfile.TemporaryFile()  # noqa: SIM115 - lives as long as the process
        self._proc = subprocess.Popen(
            ["node", *node_args, str(_RUNNER), str(write)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            pass_fds=(write,),
        )
        os.close(write)
        self._buffer = b""

    def ask(self, request: dict, timeout: float) -> dict:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(request).encode() + b"\n")
            self._proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError(f"node stopped before the run: {self._died()}") from None
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self._answers], [], [], left)[0]:
                raise subprocess.TimeoutExpired(request["file"], timeout)
            chunk = os.read(self._answers, 1 << 16)
            if not chunk:
                raise RuntimeError(f"node stopped without answering: {self._died()}")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def _died(self) -> str:
        code = self._proc.wait(timeout=5)
        self._stderr.seek(0)
        return f"exit code {code}\n{self._stderr.read().decode(errors='replace')}"

    def close(self) -> None:
        assert self._proc.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            self._proc.stdin.close()
        if self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()
        os.close(self._answers)
        self._stderr.close()


_nodes: dict[tuple[str, ...], _Node] = {}
_answered: dict[tuple, subprocess.CompletedProcess[str]] = {}


def run_node(
    script: Path,
    *args: str,
    input: str | None = None,
    env: dict[str, str] | None = None,
    node_args: tuple[str, ...] = (),
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``node [node_args] script args`` with ``input`` on stdin and ``env`` added to its environment."""
    request = {
        "file": str(Path(script).resolve()),
        "args": list(args),
        "env": env or {},
        "input": input,
    }
    key = (node_args, json.dumps(request, sort_keys=True))
    if key in _answered:
        return _answered[key]
    node = _nodes.get(node_args) or _nodes.setdefault(node_args, _Node(node_args))
    try:
        answer = node.ask(request, timeout)
    except BaseException:
        # Whatever state it is in, the next run gets a new process.
        _nodes.pop(node_args).close()
        raise
    done = subprocess.CompletedProcess(
        ["node", *node_args, str(script), *args], answer["code"], answer["stdout"], answer["stderr"]
    )
    _answered[key] = done
    return done


@atexit.register
def _close_all() -> None:
    for node in _nodes.values():
        node.close()
    _nodes.clear()
