"""Docker operations: login, build, push."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Registry
from .runner import run

log = logging.getLogger(__name__)

_TAG_BAD = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_tag(tag: str) -> str:
    """Docker tags: [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}."""
    tag = _TAG_BAD.sub("-", tag).lstrip(".-")
    return tag[:128] or "latest"


def render_tags(templates: list[str], *, sha: str, branch: str, timestamp: str) -> list[str]:
    values = {"sha": sha[:7], "full_sha": sha, "branch": branch, "timestamp": timestamp}
    tags = []
    for t in templates:
        tag = sanitize_tag(t.format(**values))
        if tag not in tags:
            tags.append(tag)
    return tags


def login(registry: Registry) -> None:
    password = registry.password
    if not password:
        log.warning("registry %s: env var %s is not set; skipping login",
                    registry.server, registry.password_env)
        return
    run(["docker", "login", registry.server, "--username", registry.username,
         "--password-stdin"], input=password, timeout=120, secrets=(password,))
    log.info("logged in to %s as %s", registry.server, registry.username)


def build_and_push(
    *,
    context: Path,
    dockerfile: Path,
    image: str,
    tags: list[str],
    build_args: dict[str, str],
    labels: dict[str, str],
    platforms: list[str],
    push: bool,
    timeout: int,
) -> list[str]:
    """Build the image and (optionally) push all tags. Returns full image refs."""
    refs = [f"{image}:{t}" for t in tags]
    args: list[str] = []
    for k, v in build_args.items():
        args += ["--build-arg", f"{k}={v}"]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    for ref in refs:
        args += ["--tag", ref]
    args += ["--file", str(dockerfile)]

    if platforms:
        # Multi-arch builds need buildx; it pushes directly (no local image store).
        cmd = ["docker", "buildx", "build", "--platform", ",".join(platforms), *args]
        cmd.append("--push" if push else "--load")
        run([*cmd, str(context)], timeout=timeout, stream=True)
        return refs

    run(["docker", "build", *args, str(context)], timeout=timeout, stream=True)
    if push:
        for ref in refs:
            log.info("pushing %s", ref)
            run(["docker", "push", ref], timeout=timeout, stream=True)
    return refs


def remove_images(refs: list[str]) -> None:
    try:
        run(["docker", "image", "rm", *refs])
    except Exception as e:  # noqa: BLE001 - best effort cleanup
        log.warning("image cleanup failed: %s", e)
