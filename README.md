# docker-image-autobuild

A small, self-hosted service that watches GitHub repositories and, whenever a
branch gets a new commit, **pulls it, builds a Docker image, and pushes the
image** to your registry (GHCR, Docker Hub, or any other registry).

- **Change detection** by polling (`git ls-remote`, cheap, no clone needed) and/or
  **GitHub webhooks** for instant builds
- **Pull** with clone-once / fetch-after, pinned to the exact commit, submodules included
- **Build** with `docker build`, or `docker buildx` for multi-arch (`platforms = [...]`)
- **Push** every configured tag: `latest`, short SHA, branch, timestamp
- OCI labels (`source`, `revision`, `created`) on every image
- Persistent state, so restarts don't trigger rebuilds; failed builds retry with
  exponential backoff (max 1h)
- Private repos via token (sent as an HTTP header, never written to disk)
- Only the Python standard library, nothing to `pip install`

## Quick start

On first run, autobuild asks where to pull from and where to push to, so you don't
need to edit any files:

```text
$ python -m autobuild
Welcome to docker-image-autobuild. Let's set things up.
Push images to a registry? (choose 'n' to only build locally) (Y/n):
Registry:
  1) GitHub Container Registry
  2) Docker Hub
  3) Other / self-hosted registry
Choose [1]:
Username for ghcr.io: octocat
Password/token for ghcr.io (leave blank to set $REGISTRY_PASSWORD yourself):
Image namespace (images are pushed as <namespace>/<repo>) [ghcr.io/octocat]:

== Which repositories should be watched? ==
Repository to watch (owner/repo or git URL): octocat/my-app
  Checking https://github.com/octocat/my-app.git ...
  OK, default branch is 'main'.
...
```

The wizard checks that each repo is reachable, finds its default branch, asks for a
token if the repo is private, and then writes `autobuild.toml`. Tokens are stored
separately in `autobuild.secrets.env` with `0600` permissions, never in the config.
After that it starts watching.

- `python -m autobuild init` runs the setup again
- `python -m autobuild add-repo` watches another repo

### With Docker Compose

```bash
docker compose run --rm autobuild init   # one-time interactive setup, saved to ./config
docker compose up -d --build
docker compose logs -f
```

The container uses the host's Docker daemon through `/var/run/docker.sock`.

### Without Docker

You need Python 3.11+, `git`, and the `docker` CLI.

```bash
python -m autobuild                # first run: setup, then watch forever
python -m autobuild run            # watch forever
python -m autobuild once           # check every repo once, build what changed, exit
python -m autobuild once --force my-app   # rebuild one repo now
python -m autobuild status         # last build per repo
python -m autobuild check-config
```

`once` works well from cron or a systemd timer if you'd rather not run a daemon.
Only one instance can use a given state file at a time.

## Configuration

The wizard covers the common cases. For everything else (build args, multi-arch
`platforms`, custom tags, per-repo poll intervals), edit `autobuild.toml` directly.
[`autobuild.example.toml`](autobuild.example.toml) documents every option. A minimal setup:

```toml
[[registry]]
server = "ghcr.io"
username = "octocat"
password_env = "REGISTRY_PASSWORD"

[[repo]]
name = "my-app"
url = "https://github.com/octocat/my-app.git"
branch = "main"
image = "ghcr.io/octocat/my-app"
tags = ["latest", "{sha}"]
```

Tag placeholders: `{sha}` (7 chars), `{full_sha}`, `{branch}`, `{timestamp}`.
Characters that aren't valid in tags are replaced, so `feature/x` becomes `feature-x`.

### Tokens

Secrets are read from environment variables. Any that aren't set are loaded from
`autobuild.secrets.env` next to the config, which is the file the wizard writes.

| Variable | Purpose |
|---|---|
| `REGISTRY_PASSWORD` | Push to the registry. GHCR: token with `write:packages`. Docker Hub: access token with Read & Write |
| `GIT_TOKEN` | Pull private repos. GitHub: `repo` scope, or fine-grained with Contents: read |
| `WEBHOOK_SECRET` | Verifies GitHub webhooks and protects `/trigger` |

## Webhooks (optional, for instant builds)

Say yes to webhooks during setup (or set `webhook_port` and `webhook_secret_env`).
The wizard generates a secret and prints these settings. Add a webhook in GitHub under
**Settings → Webhooks**:

- Payload URL: `http://your-host:8080/webhook`
- Content type: `application/json`
- Secret: the same value as `$WEBHOOK_SECRET`
- Events: *Just the push event*

A webhook only triggers a check. The service still asks the remote for the branch
head, so a forged payload can't make it build an arbitrary commit. Keep polling
turned on as a fallback; `poll_interval = 300` is plenty when webhooks are set up.

### HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook` | GitHub push events (HMAC-SHA256 verified when a secret is set) |
| `POST` | `/trigger/<repo>` | Forced rebuild; needs `Authorization: Bearer $WEBHOOK_SECRET` |
| `GET` | `/status` | JSON state for every repo |
| `GET` | `/healthz` | Liveness |

```bash
curl -X POST -H "Authorization: Bearer $WEBHOOK_SECRET" http://localhost:8080/trigger/my-app
```

## How it works

```
 scheduler (every poll_interval) ─┐
 webhook / manual trigger ────────┤──► queue (de-duplicated) ──► build worker (one build at a time)
                                  │
 build worker:  git ls-remote ─► changed? ─► clone/fetch + checkout SHA ─► docker build ─► docker push ─► save state
```

Builds run one at a time so several repos changing at once don't overload the host.

## Security notes

- Mounting `/var/run/docker.sock` gives the container root-equivalent access to the
  host. Only watch repositories you trust, because their Dockerfiles run on your daemon.
- If you expose the webhook port to the internet, set a webhook secret and put the
  port behind HTTPS (a reverse proxy, or a tunnel such as Cloudflare Tunnel).
- Secrets are redacted from logs and error messages.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The pipeline tests use a real local git repository and a fake `docker` binary, so
they don't need a Docker daemon.
