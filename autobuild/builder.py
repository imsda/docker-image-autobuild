"""Core pipeline: check for changes -> pull -> build -> push."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import docker_ops, git_ops
from .config import Config, Repo
from .state import State

log = logging.getLogger(__name__)

MAX_BACKOFF = 3600


class Builder:
    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        self._seen: set[str] = set()  # repos checked since startup

    def check(self, repo: Repo, force: bool = False) -> bool:
        """Build `repo` if its branch moved since the last successful build.

        Returns True if a build was performed successfully.
        """
        st = self.state.get(repo.name)
        try:
            sha = git_ops.remote_head(repo.url, repo.branch, repo.token)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] could not query remote: %s", repo.name, e)
            self.state.update(repo.name, last_check=_now(), last_error=str(e))
            return False
        self.state.update(repo.name, last_check=_now(), remote_sha=sha)

        if not force:
            if sha == st.get("built_sha"):
                log.debug("[%s] up to date at %s", repo.name, sha[:7])
                return False
            # A restart retries immediately (the user may have fixed the cause).
            first_check = repo.name not in self._seen
            self._seen.add(repo.name)
            if (sha == st.get("failed_sha") and not first_check
                    and time.time() < st.get("retry_after", 0)):
                log.debug("[%s] %s failed previously; waiting for backoff", repo.name, sha[:7])
                return False

        if force:
            log.info("[%s] forced rebuild of %s at %s", repo.name, repo.branch, sha[:7])
        else:
            log.info("[%s] change detected on %s: %s -> %s", repo.name, repo.branch,
                     (st.get("built_sha") or "none")[:7], sha[:7])
        return self.build(repo, sha)

    def build(self, repo: Repo, sha: str) -> bool:
        s = self.config.settings
        dest = s.workdir / repo.name
        started = time.time()
        self.state.update(repo.name, status="building", building_sha=sha)
        try:
            sha = git_ops.sync(repo.url, repo.branch, dest, sha, repo.token)
            info = git_ops.commit_info(dest)
            log.info("[%s] pulled %s: %s (%s)", repo.name, sha[:7], info["subject"], info["author"])

            context = (dest / repo.context).resolve()
            dockerfile = (context / repo.dockerfile).resolve()
            if not dockerfile.is_relative_to(dest.resolve()) or not context.is_relative_to(dest.resolve()):
                raise RuntimeError("dockerfile/context must stay inside the repository")
            if not dockerfile.is_file():
                raise RuntimeError(f"Dockerfile not found: {repo.dockerfile} (context {repo.context})")

            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            tags = docker_ops.render_tags(repo.tags, sha=sha, branch=repo.branch, timestamp=ts)
            labels = {
                "org.opencontainers.image.source": _public_url(repo.url),
                "org.opencontainers.image.revision": sha,
                "org.opencontainers.image.created": datetime.now(timezone.utc).isoformat(),
                "org.opencontainers.image.ref.name": repo.branch,
            }
            log.info("[%s] building %s with tags %s", repo.name, repo.image, ", ".join(tags))
            refs = docker_ops.build_and_push(
                context=context, dockerfile=dockerfile, image=repo.image, tags=tags,
                build_args=repo.build_args, labels=labels, platforms=repo.platforms,
                push=repo.push, timeout=s.build_timeout,
            )
            if repo.push and s.prune_after_push and not repo.platforms:
                docker_ops.remove_images(refs)
        except Exception as e:  # noqa: BLE001
            failures = self.state.get(repo.name).get("failures", 0) + 1
            backoff = min(self._interval(repo) * 2 ** failures, MAX_BACKOFF)
            log.error("[%s] build of %s failed (attempt %d, retry in %ds): %s",
                      repo.name, sha[:7], failures, backoff, e)
            self.state.update(repo.name, status="failed", failed_sha=sha, failures=failures,
                              retry_after=time.time() + backoff, last_error=str(e)[-2000:],
                              building_sha=None)
            return False

        duration = round(time.time() - started, 1)
        log.info("[%s] %s %s in %ss", repo.name, "pushed" if repo.push else "built",
                 ", ".join(refs), duration)
        self.state.update(repo.name, status="ok", built_sha=sha, built_at=_now(), images=refs,
                          duration=duration, failures=0, failed_sha=None, last_error=None,
                          building_sha=None, commit=info)
        return True

    def _interval(self, repo: Repo) -> int:
        return repo.poll_interval or self.config.settings.poll_interval


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public_url(url: str) -> str:
    url = url.removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url
