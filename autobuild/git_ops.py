"""Git operations: detect remote changes, clone/fetch, checkout."""

from __future__ import annotations

import base64
from pathlib import Path

from .runner import run


def _auth_args(token: str | None) -> list[str]:
    """Pass a token as an HTTP header so it never lands in .git/config or URLs."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def _secrets(token: str | None) -> tuple[str, ...]:
    if not token:
        return ()
    return (token, base64.b64encode(f"x-access-token:{token}".encode()).decode())


def remote_head(url: str, branch: str, token: str | None = None) -> str:
    """Return the commit SHA the remote branch points at, without cloning."""
    out = run(
        ["git", *_auth_args(token), "ls-remote", "--exit-code", url, f"refs/heads/{branch}"],
        timeout=120, secrets=_secrets(token),
    )
    line = out.strip().splitlines()[0] if out.strip() else ""
    sha = line.split()[0] if line else ""
    if len(sha) < 40:
        raise RuntimeError(f"could not resolve {branch} on {url}: {out!r}")
    return sha


def default_branch(url: str, token: str | None = None) -> str:
    """Return the remote's default branch name (what HEAD points at)."""
    out = run(["git", *_auth_args(token), "ls-remote", "--symref", url, "HEAD"],
              timeout=60, secrets=_secrets(token))
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("HEAD"):
            return line.split()[1].removeprefix("refs/heads/")
    raise RuntimeError(f"could not determine default branch of {url}")


def sync(url: str, branch: str, dest: Path, sha: str, token: str | None = None) -> str:
    """Clone or update `dest` to exactly `sha` (on `branch`). Returns the checked-out SHA."""
    auth, secrets = _auth_args(token), _secrets(token)
    dest = Path(dest)
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["git", *auth, "clone", "--branch", branch, "--single-branch", url, str(dest)],
            timeout=1800, secrets=secrets)
    else:
        run(["git", "remote", "set-url", "origin", url], cwd=dest)
        run(["git", *auth, "fetch", "--prune", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=dest, timeout=1800, secrets=secrets)
    run(["git", "checkout", "--force", "--detach", sha], cwd=dest)
    run(["git", "clean", "-ffdx"], cwd=dest)
    run(["git", *auth, "submodule", "update", "--init", "--recursive", "--force"],
        cwd=dest, timeout=1800, secrets=secrets)
    return run(["git", "rev-parse", "HEAD"], cwd=dest).strip()


def commit_info(dest: Path) -> dict[str, str]:
    out = run(["git", "log", "-1", "--format=%H%n%an%n%cI%n%s"], cwd=dest)
    sha, author, date, subject = (out.strip().split("\n", 3) + ["", "", "", ""])[:4]
    return {"sha": sha, "author": author, "date": date, "subject": subject}
