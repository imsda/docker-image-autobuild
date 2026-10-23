"""Thin subprocess wrapper with logging and secret redaction."""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, output: str):
        self.cmd, self.returncode, self.output = cmd, returncode, output
        tail = "\n".join(output.strip().splitlines()[-20:])
        super().__init__(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({returncode}):\n{tail}")


def run(
    cmd: list[str],
    *,
    cwd=None,
    input: str | None = None,
    timeout: int | None = None,
    secrets: tuple[str, ...] = (),
    stream: bool = False,
) -> str:
    """Run a command, return combined stdout/stderr, raise CommandError on failure."""

    def redact(text: str) -> str:
        for s in secrets:
            if s:
                text = text.replace(s, "***")
        return text

    log.debug("$ %s", redact(" ".join(cmd)))
    if not stream:
        try:
            p = subprocess.run(
                cmd, cwd=cwd, input=input, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise CommandError(cmd, -1, f"timed out after {timeout}s") from None
        out = redact((p.stdout or "") + (p.stderr or ""))
        if p.returncode != 0:
            raise CommandError([redact(c) for c in cmd], p.returncode, out)
        return out

    # Streaming mode: forward output line by line to the log (for long builds).
    lines: list[str] = []
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.PIPE if input else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if input:
        proc.stdin.write(input)
        proc.stdin.close()
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, kill) if timeout else None
    if timer:
        timer.start()
    try:
        with proc.stdout:
            for line in proc.stdout:
                line = redact(line.rstrip())
                lines.append(line)
                log.info("  | %s", line)
        proc.wait()
    finally:
        if timer:
            timer.cancel()
    if timed_out.is_set():
        raise CommandError(cmd, -1, f"timed out after {timeout}s")
    out = "\n".join(lines)
    if proc.returncode != 0:
        raise CommandError([redact(c) for c in cmd], proc.returncode, out)
    return out
