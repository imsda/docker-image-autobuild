"""End-to-end pipeline test with a real local git repo and a fake `docker` binary."""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from autobuild.builder import Builder
from autobuild.config import parse_config
from autobuild.state import State

FAKE_DOCKER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    sys.exit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))
""")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git("init", "-q", "-b", "main", cwd=self.origin)
        git("config", "user.email", "t@example.com", cwd=self.origin)
        git("config", "user.name", "Test", cwd=self.origin)
        (self.origin / "Dockerfile").write_text("FROM scratch\n")
        git("add", ".", cwd=self.origin)
        git("commit", "-qm", "init", cwd=self.origin)

        bindir = self.tmp / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "docker.log"
        self.env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_DOCKER_LOG": str(self.log)}
        self._old_env = {k: os.environ.get(k) for k in [*self.env, "FAKE_DOCKER_EXIT"]}
        os.environ.update(self.env)

        self.config = parse_config({
            "settings": {"workdir": str(self.tmp / "work"), "state_file": str(self.tmp / "state.json")},
            "repo": [{"name": "app", "url": str(self.origin), "image": "registry.local/app",
                      "tags": ["latest", "{sha}"], "build_args": {"X": "1"}}],
        })
        self.state = State(self.config.settings.state_file)
        self.builder = Builder(self.config, self.state)
        self.repo = self.config.repos[0]

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.origin,
                              capture_output=True, text=True).stdout.strip()

    def test_build_push_then_skip_then_rebuild_on_change(self):
        sha1 = self.head()
        self.assertTrue(self.builder.check(self.repo))
        calls = self.calls()
        self.assertEqual(calls[0][0], "build")
        self.assertIn(f"registry.local/app:{sha1[:7]}", calls[0])
        self.assertIn("X=1", calls[0])
        self.assertIn(f"org.opencontainers.image.revision={sha1}", calls[0])
        self.assertEqual([c for c in calls if c[0] == "push"],
                         [["push", "registry.local/app:latest"], ["push", f"registry.local/app:{sha1[:7]}"]])
        self.assertEqual(self.state.get("app")["built_sha"], sha1)

        # No change -> no build.
        self.log.unlink()
        self.assertFalse(self.builder.check(self.repo))
        self.assertEqual(self.calls(), [])

        # New commit -> pulled and rebuilt.
        (self.origin / "file.txt").write_text("hi")
        git("add", ".", cwd=self.origin)
        git("commit", "-qm", "change", cwd=self.origin)
        sha2 = self.head()
        self.assertTrue(self.builder.check(self.repo))
        self.assertIn(f"registry.local/app:{sha2[:7]}", self.calls()[0])
        self.assertTrue((self.config.settings.workdir / "app" / "file.txt").exists())
        self.assertEqual(self.state.get("app")["built_sha"], sha2)

    def test_failure_backs_off_then_force_retries(self):
        os.environ["FAKE_DOCKER_EXIT"] = "1"
        self.assertFalse(self.builder.check(self.repo))
        st = self.state.get("app")
        self.assertEqual((st["status"], st["failures"]), ("failed", 1))
        self.log.unlink()
        self.assertFalse(self.builder.check(self.repo))  # within backoff window
        self.assertEqual(self.calls(), [])
        # A fresh process (restart) retries right away despite the backoff.
        os.environ["FAKE_DOCKER_EXIT"] = "0"
        self.assertTrue(Builder(self.config, self.state).check(self.repo))
        self.state.update("app", built_sha=None)
        self.assertTrue(self.builder.check(self.repo, force=True))
        self.assertEqual(self.state.get("app")["failures"], 0)

    def test_missing_dockerfile_fails_cleanly(self):
        self.repo.dockerfile = "nope/Dockerfile"
        self.assertFalse(self.builder.check(self.repo))
        self.assertIn("Dockerfile not found", self.state.get("app")["last_error"])

    def test_context_escape_rejected(self):
        self.repo.context = "../.."
        self.assertFalse(self.builder.check(self.repo))
        self.assertIn("inside the repository", self.state.get("app")["last_error"])


if __name__ == "__main__":
    unittest.main()
