from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Sequence


class UtilityError(RuntimeError):
    """A bounded local document utility did not complete successfully."""


def _append_bounded(target: bytearray, chunk: bytes, limit: int) -> None:
    room = limit - len(target)
    if room > 0:
        target.extend(chunk[:room])


def _terminate_group(process: subprocess.Popen[bytes], grace: float = 1.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_utility(
    command: Sequence[str],
    *,
    timeout: float = 120.0,
    diagnostic_limit: int = 64 * 1024,
    cwd: Path | None = None,
    check: bool = True,
) -> dict[str, object]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        raise UtilityError(f"could not start {command[0]}: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    deadline = started + timeout
    timed_out = False

    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0 and process.poll() is None:
            timed_out = True
            _terminate_group(process)
            remaining = 0
        events = selector.select(timeout=min(max(remaining, 0), 0.25))
        if not events and process.poll() is not None:
            events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
        for key, _ in events:
            stream = key.fileobj
            chunk = os.read(stream.fileno(), 64 * 1024)
            if not chunk:
                selector.unregister(stream)
                stream.close()
                continue
            name = key.data
            totals[name] += len(chunk)
            _append_bounded(captured[name], chunk, diagnostic_limit)

    if process.poll() is None:
        process.wait()
    returncode = int(process.returncode or 0)
    result: dict[str, object] = {
        "command": Path(command[0]).name,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": bytes(captured["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(captured["stderr"]).decode("utf-8", errors="replace"),
        "stdout_bytes": totals["stdout"],
        "stderr_bytes": totals["stderr"],
        "stdout_truncated": totals["stdout"] > len(captured["stdout"]),
        "stderr_truncated": totals["stderr"] > len(captured["stderr"]),
    }
    if check and (timed_out or returncode != 0):
        reason = "timed out" if timed_out else f"exited {returncode}"
        detail = str(result["stderr"]).strip() or str(result["stdout"]).strip()
        if detail:
            reason = f"{reason}: {detail}"
        raise UtilityError(f"{Path(command[0]).name} {reason}")
    return result
