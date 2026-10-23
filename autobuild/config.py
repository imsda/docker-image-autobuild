"""Configuration loading (TOML, stdlib only)."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TAGS = ["latest", "{sha}"]
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ConfigError(ValueError):
    pass


@dataclass
class Registry:
    server: str
    username: str
    password_env: str

    @property
    def password(self) -> str | None:
        return os.environ.get(self.password_env)


@dataclass
class Repo:
    name: str
    url: str
    image: str
    branch: str = "main"
    dockerfile: str = "Dockerfile"
    context: str = "."
    tags: list[str] = field(default_factory=lambda: list(DEFAULT_TAGS))
    build_args: dict[str, str] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)
    push: bool = True
    poll_interval: int | None = None
    token_env: str | None = None  # env var holding a token for private repos
    webhook_secret_env: str | None = None

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) if self.token_env else None

    @property
    def webhook_secret(self) -> str | None:
        return os.environ.get(self.webhook_secret_env) if self.webhook_secret_env else None


@dataclass
class Settings:
    workdir: Path = Path("./data/repos")
    state_file: Path = Path("./data/state.json")
    poll_interval: int = 60
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 0  # 0 disables the HTTP server
    webhook_secret_env: str | None = None
    build_timeout: int = 3600
    prune_after_push: bool = False


@dataclass
class Config:
    settings: Settings
    repos: list[Repo]
    registries: list[Registry]

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)


def _expand(value):
    """Expand ${VAR} references in strings so secrets/hosts can come from env."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _build(cls, data: dict, where: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"{where}: {e}") from None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = _expand(tomllib.loads(path.read_text()))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse_config(raw)


def parse_config(raw: dict) -> Config:
    s = dict(raw.get("settings", {}))
    for key in ("workdir", "state_file"):
        if key in s:
            s[key] = Path(s[key])
    settings = _build(Settings, s, "[settings]")

    repos = []
    for i, r in enumerate(raw.get("repo", [])):
        where = f"[[repo]] #{i + 1}"
        repo = _build(Repo, dict(r), where)
        if not _NAME_RE.match(repo.name):
            raise ConfigError(f"{where}: invalid name {repo.name!r}")
        if not repo.tags:
            raise ConfigError(f"{where}: at least one tag is required")
        if any(x.name == repo.name for x in repos):
            raise ConfigError(f"{where}: duplicate repo name {repo.name!r}")
        repos.append(repo)
    if not repos:
        raise ConfigError("no [[repo]] entries configured")

    registries = [
        _build(Registry, dict(r), f"[[registry]] #{i + 1}")
        for i, r in enumerate(raw.get("registry", []))
    ]
    return Config(settings=settings, repos=repos, registries=registries)
