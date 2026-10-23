"""Interactive first-run setup: asks where to pull from and push to, writes the config."""

from __future__ import annotations

import getpass
import os
import re
import secrets as secrets_mod
from pathlib import Path
from typing import Callable

from . import git_ops

REGISTRIES = {
    "1": ("GitHub Container Registry", "ghcr.io"),
    "2": ("Docker Hub", "docker.io"),
    "3": ("Other / self-hosted registry", None),
}
REGISTRY_PASSWORD_ENV = "REGISTRY_PASSWORD"
GIT_TOKEN_ENV = "GIT_TOKEN"
WEBHOOK_SECRET_ENV = "WEBHOOK_SECRET"

_GITHUB_SHORT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SetupAborted(Exception):
    pass


def secrets_path(config_path: Path) -> Path:
    """Secrets live next to the config: autobuild.toml -> autobuild.secrets.env."""
    return Path(config_path).with_suffix(".secrets.env")


def load_secrets(path: Path) -> None:
    """Load KEY=value lines into the environment without overriding real env vars."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def normalize_repo_url(text: str) -> str:
    """Accept owner/repo, a GitHub URL, an SSH URL, or any git URL/path."""
    text = text.strip()
    if _GITHUB_SHORT.match(text) and not os.path.exists(text):
        return f"https://github.com/{text.removesuffix('.git')}.git"
    if text.startswith("https://github.com/") and not text.endswith(".git"):
        return text.rstrip("/") + ".git"
    return text


def repo_basename(url: str) -> str:
    name = url.rstrip("/").removesuffix(".git").replace(":", "/").rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9_.-]", "-", name.lower()).strip(".-") or "app"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot serialize {type(v).__name__}")


def _toml_table(header: str, data: dict) -> str:
    lines = [header] + [f"{k} = {_toml_value(v)}" for k, v in data.items() if v is not None]
    return "\n".join(lines) + "\n"


class Wizard:
    def __init__(
        self,
        config_path: Path,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        print_fn: Callable[[str], None] = print,
        check_remote: bool = True,
    ):
        self.config_path = Path(config_path)
        self.input, self.secret, self.print = input_fn, secret_fn, print_fn
        self.check_remote = check_remote
        self.new_secrets: dict[str, str] = {}

    # ---- prompt helpers -------------------------------------------------

    def ask(self, question: str, default: str | None = None, required: bool = True) -> str:
        suffix = f" [{default}]" if default else ""
        while True:
            try:
                answer = self.input(f"{question}{suffix}: ").strip()
            except EOFError:
                raise SetupAborted() from None
            if answer:
                return answer
            if default is not None:
                return default
            if not required:
                return ""
            self.print("  This is required.")

    def yes(self, question: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.ask(f"{question} ({hint})", required=False).lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            self.print("  Please answer y or n.")

    def choose(self, question: str, options: dict[str, str], default: str) -> str:
        self.print(question)
        for key, label in options.items():
            self.print(f"  {key}) {label}")
        while True:
            answer = self.ask("Choose", default)
            if answer in options:
                return answer
            self.print(f"  Please enter one of: {', '.join(options)}")

    def ask_secret(self, question: str) -> str:
        try:
            return self.secret(f"{question}: ").strip()
        except EOFError:
            raise SetupAborted() from None

    # ---- steps -------------------------------------------------------------

    def ask_registry(self) -> dict:
        self.print("\n== Where should built images be pushed? ==")
        choice = self.choose("Registry:", {k: v[0] for k, v in REGISTRIES.items()}, "1")
        server = REGISTRIES[choice][1] or self.ask("Registry host (e.g. registry.example.com:5000)")
        username = self.ask(f"Username for {server}")
        if server == "ghcr.io":
            self.print("  Use a GitHub token with the 'write:packages' scope.")
        elif server == "docker.io":
            self.print("  Use a Docker Hub access token with Read & Write permission.")
        token = self.ask_secret(f"Password/token for {server} (leave blank to set "
                                f"${REGISTRY_PASSWORD_ENV} yourself)")
        if token:
            self.new_secrets[REGISTRY_PASSWORD_ENV] = token
        default_ns = f"{server}/{username.lower()}" if server != "docker.io" else username.lower()
        namespace = self.ask("Image namespace (images are pushed as <namespace>/<repo>)",
                             default_ns).rstrip("/")
        return {"server": server, "username": username, "namespace": namespace}

    def ask_repo(self, registry: dict | None, taken: set[str]) -> dict:
        self.print("")
        while True:
            url = normalize_repo_url(self.ask("Repository to watch (owner/repo or git URL)"))
            branch, needs_token = self._probe(url, registry)
            if branch or not self.check_remote or self.yes("  Use it anyway?", False):
                break

        branch = self.ask("Branch to build", branch or "main")
        base = repo_basename(url)
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        name = self.ask("Short name for this repo", name)
        while name in taken or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", name):
            name = self.ask("  Name must be unique and contain only letters, digits, _ . -")
        default_image = f"{registry['namespace']}/{base}" if registry else base
        image = self.ask("Image to push", default_image).lower()
        dockerfile = self.ask("Dockerfile path (relative to the repo root)", "Dockerfile")
        tags = ["latest", "{sha}"] if branch in ("main", "master") else ["{branch}", "{sha}"]

        repo = {"name": name, "url": url, "branch": branch, "image": image,
                "dockerfile": dockerfile, "tags": tags}
        if needs_token:
            repo["token_env"] = GIT_TOKEN_ENV
        return repo

    def _probe(self, url: str, registry: dict | None) -> tuple[str | None, bool]:
        """Find the default branch and whether the repo needs a token to read."""
        if not self.check_remote:
            private = self.yes("  Is this repository private?", False)
            if private and not self._token():
                self._ask_git_token(registry)
            return None, private
        self.print(f"  Checking {url} ...")
        attempts = [None]
        if self._token():
            attempts.append(self._token())
        for token in attempts:
            try:
                branch = git_ops.default_branch(url, token)
                self.print(f"  OK, default branch is '{branch}'.")
                return branch, token is not None
            except Exception:  # noqa: BLE001
                pass
        self.print("  Could not reach that repository.")
        if self._token() or not self.yes("  Is it private (needs a token)?", True):
            return None, False
        self._ask_git_token(registry)
        try:
            branch = git_ops.default_branch(url, self._token())
            self.print(f"  OK, default branch is '{branch}'.")
            return branch, True
        except Exception as e:  # noqa: BLE001
            self.print(f"  Still failing: {str(e).strip().splitlines()[-1]}")
            return None, True

    def _token(self) -> str | None:
        return self.new_secrets.get(GIT_TOKEN_ENV) or os.environ.get(GIT_TOKEN_ENV)

    def _ask_git_token(self, registry: dict | None) -> None:
        if registry and registry["server"] == "ghcr.io" and REGISTRY_PASSWORD_ENV in self.new_secrets:
            if self.yes("  Reuse your ghcr.io token for pulling (needs the 'repo' scope)?", True):
                self.new_secrets[GIT_TOKEN_ENV] = self.new_secrets[REGISTRY_PASSWORD_ENV]
                return
        token = self.ask_secret("  Git token with read access (GitHub: 'repo' scope or "
                                "fine-grained 'Contents: read')")
        if token:
            self.new_secrets[GIT_TOKEN_ENV] = token

    def ask_settings(self) -> dict:
        self.print("\n== Change detection ==")
        while True:
            interval = self.ask("Check for new commits every N seconds", "60")
            if interval.isdigit() and int(interval) >= 10:
                break
            self.print("  Enter a whole number of at least 10.")
        settings = {"workdir": "./data/repos", "state_file": "./data/state.json",
                    "poll_interval": int(interval), "webhook_port": 0}
        if self.yes("Enable the webhook server for instant builds on push?", False):
            port = self.ask("Port", "8080")
            settings["webhook_port"] = int(port) if port.isdigit() else 8080
            settings["webhook_secret_env"] = WEBHOOK_SECRET_ENV
            if not os.environ.get(WEBHOOK_SECRET_ENV):
                self.new_secrets[WEBHOOK_SECRET_ENV] = secrets_mod.token_hex(20)
        return settings

    # ---- entry points -----------------------------------------------------

    def run(self) -> bool:
        """Full setup. Returns True if a config was written."""
        self.print("Welcome to docker-image-autobuild. Let's set things up.\n"
                   f"(Config will be written to {self.config_path}; press Ctrl+C to cancel.)")
        if self.config_path.exists() and not self.yes(
                f"{self.config_path} already exists. Overwrite it?", False):
            return False

        registry = self.ask_registry() if self.yes(
            "Push images to a registry? (choose 'n' to only build locally)", True) else None
        self.print("\n== Which repositories should be watched? ==")
        repos: list[dict] = []
        while True:
            repos.append(self.ask_repo(registry, {r["name"] for r in repos}))
            if not self.yes("Add another repository?", False):
                break
        settings = self.ask_settings()
        for r in repos:
            r["push"] = registry is not None

        self.print("\n== Summary ==")
        if registry:
            self.print(f"Push to:  {registry['server']} as {registry['username']}")
        for r in repos:
            arrow = "->" if r["push"] else "-> (local only)"
            self.print(f"Watch:    {r['url']} @ {r['branch']} {arrow} {r['image']}")
        self.print(f"Polling:  every {settings['poll_interval']}s"
                   + (f", webhooks on port {settings['webhook_port']}" if settings["webhook_port"] else ""))
        if not self.yes("Save this configuration?", True):
            return False

        parts = ["# Generated by `autobuild init`. See autobuild.example.toml for all options.\n",
                 _toml_table("[settings]", settings)]
        if registry:
            parts.append(_toml_table("[[registry]]", {
                "server": registry["server"], "username": registry["username"],
                "password_env": REGISTRY_PASSWORD_ENV}))
        parts += [_toml_table("[[repo]]", r) for r in repos]
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("\n".join(parts))
        self._write_secrets()
        self._finish(settings, registry)
        return True

    def add_repo(self, existing_names: set[str], push: bool, namespace: str | None) -> bool:
        registry = {"server": "", "namespace": namespace} if namespace else None
        repo = self.ask_repo(registry, existing_names)
        repo["push"] = push
        if not self.yes(f"Add {repo['url']} -> {repo['image']}?", True):
            return False
        with self.config_path.open("a") as f:
            f.write("\n" + _toml_table("[[repo]]", repo))
        self._write_secrets()
        self.print(f"Added '{repo['name']}'. Restart the service to pick it up.")
        return True

    def _write_secrets(self) -> None:
        if not self.new_secrets:
            return
        path = secrets_path(self.config_path)
        existing = {}
        if path.is_file():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        existing.update(self.new_secrets)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("# Secrets for docker-image-autobuild. Keep this file private.\n")
            f.writelines(f"{k}={v}\n" for k, v in existing.items())
        os.chmod(path, 0o600)
        os.environ.update(self.new_secrets)
        self.print(f"Secrets saved to {path} (readable only by you).")

    def _finish(self, settings: dict, registry: dict | None) -> None:
        self.print(f"\nSaved {self.config_path}.")
        if registry and REGISTRY_PASSWORD_ENV not in self.new_secrets:
            self.print(f"Remember to set ${REGISTRY_PASSWORD_ENV} before starting.")
        if settings["webhook_port"]:
            secret = self.new_secrets.get(WEBHOOK_SECRET_ENV, f"${WEBHOOK_SECRET_ENV}")
            self.print("\nTo build instantly on push, add a webhook in each GitHub repo "
                       "(Settings -> Webhooks):\n"
                       f"  Payload URL:  http://<this-host>:{settings['webhook_port']}/webhook\n"
                       "  Content type: application/json\n"
                       f"  Secret:       {secret}\n"
                       "  Events:       Just the push event")
        self.print("\nRun `autobuild init` again to start over, or `autobuild add-repo` to watch more repos.")
