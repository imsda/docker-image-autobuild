"""Optional HTTP server: GitHub push webhooks, manual triggers, status, health."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config, Repo
from .service import Service
from .state import State

log = logging.getLogger(__name__)
MAX_BODY = 25 * 1024 * 1024


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _norm(url: str) -> str:
    url = url.strip().lower().removesuffix("/").removesuffix(".git")
    if url.startswith("git@github.com:"):
        url = "github.com/" + url.split(":", 1)[1]
    return url.split("://", 1)[-1].split("@", 1)[-1]


def match_repos(config: Config, payload: dict) -> list[Repo]:
    """Repos whose URL and branch match a GitHub push payload."""
    r = payload.get("repository") or {}
    urls = {_norm(u) for u in (r.get("clone_url"), r.get("html_url"), r.get("ssh_url"),
                               r.get("git_url")) if u}
    ref = payload.get("ref", "")
    return [repo for repo in config.repos
            if _norm(repo.url) in urls and ref == f"refs/heads/{repo.branch}"]


def make_handler(config: Config, service: Service, state: State):
    global_secret_env = config.settings.webhook_secret_env

    def secret_for(repo: Repo | None) -> str | None:
        import os
        if repo and repo.webhook_secret:
            return repo.webhook_secret
        return os.environ.get(global_secret_env) if global_secret_env else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "autobuild"

        def log_message(self, fmt, *args):
            log.debug("http %s - " + fmt, self.address_string(), *args)

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"error": "payload too large"})
                return None
            return self.rfile.read(length)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if self.path == "/status":
                return self._send(200, state.all())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            if body is None:
                return
            if self.path == "/webhook":
                return self._webhook(body)
            if self.path.startswith("/trigger/"):
                return self._trigger(self.path.removeprefix("/trigger/"))
            self._send(404, {"error": "not found"})

        def _webhook(self, body: bytes):
            event = self.headers.get("X-GitHub-Event", "")
            if event == "ping":
                return self._send(200, {"ok": True, "pong": True})
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            repos = match_repos(config, payload)
            sig = self.headers.get("X-Hub-Signature-256")
            accepted = []
            for repo in repos:
                secret = secret_for(repo)
                if secret and not verify_signature(secret, body, sig):
                    log.warning("[%s] webhook signature mismatch", repo.name)
                    continue
                service.enqueue(repo.name)
                accepted.append(repo.name)
            if repos and not accepted:
                return self._send(401, {"error": "invalid signature"})
            if accepted:
                log.info("webhook %s: queued %s", event or "?", ", ".join(accepted))
            self._send(202 if accepted else 200, {"queued": accepted})

        def _trigger(self, name: str):
            repo = config.repo(name)
            if repo is None:
                return self._send(404, {"error": f"unknown repo {name!r}"})
            secret = secret_for(repo)
            auth = self.headers.get("Authorization", "")
            if not secret:
                return self._send(403, {"error": "manual triggers need a webhook secret configured"})
            if not hmac.compare_digest(auth, f"Bearer {secret}"):
                return self._send(401, {"error": "unauthorized"})
            service.enqueue(name, force=True)
            log.info("[%s] manual rebuild triggered", name)
            self._send(202, {"queued": [name], "force": True})

    return Handler


def start_server(config: Config, service: Service, state: State) -> ThreadingHTTPServer | None:
    s = config.settings
    if not s.webhook_port:
        return None
    httpd = ThreadingHTTPServer((s.webhook_host, s.webhook_port), make_handler(config, service, state))
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("http server listening on %s:%d (/webhook, /trigger/<repo>, /status, /healthz)",
             s.webhook_host, s.webhook_port)
    if not s.webhook_secret_env and not any(r.webhook_secret_env for r in config.repos):
        log.warning("no webhook secret configured: webhooks are accepted unsigned "
                    "(they only trigger a change check) and manual triggers are disabled")
    return httpd
