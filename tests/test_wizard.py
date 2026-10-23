import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autobuild.config import load_config
from autobuild.setup_wizard import (
    GIT_TOKEN_ENV, REGISTRY_PASSWORD_ENV, WEBHOOK_SECRET_ENV, Wizard, load_secrets,
    normalize_repo_url, repo_basename, secrets_path,
)


def scripted(answers, secrets=()):
    answers, secrets = list(answers), list(secrets)
    return (lambda _prompt: answers.pop(0)), (lambda _prompt: secrets.pop(0))


class HelperTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_repo_url("me/app"), "https://github.com/me/app.git")
        self.assertEqual(normalize_repo_url("https://github.com/me/app/"), "https://github.com/me/app.git")
        self.assertEqual(normalize_repo_url("git@github.com:me/app.git"), "git@github.com:me/app.git")

    def test_basename(self):
        self.assertEqual(repo_basename("https://github.com/Me/My_App.git"), "my_app")
        self.assertEqual(repo_basename("git@github.com:me/App.git"), "app")


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "autobuild.toml"
        self._env = {k: os.environ.pop(k, None)
                     for k in (REGISTRY_PASSWORD_ENV, GIT_TOKEN_ENV, WEBHOOK_SECRET_ENV)}
        self.out = []

    def tearDown(self):
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def wizard(self, answers, secrets=(), check_remote=False):
        inp, sec = scripted(answers, secrets)
        return Wizard(self.cfg, inp, sec, self.out.append, check_remote=check_remote)

    def test_full_setup_ghcr_private_repo_and_webhook(self):
        answers = [
            "",            # push to a registry? (default yes)
            "1",           # ghcr
            "Octo",        # username
            "",            # namespace default ghcr.io/octo
            "octo/Web-App",  # repo
            "y",           # private?
            "",            # reuse ghcr token
            "",            # branch -> main
            "",            # name -> web-app
            "",            # image default
            "docker/Dockerfile",
            "",            # add another? no
            "30",          # poll
            "y", "",       # webhook on, port 8080
            "",            # save
        ]
        self.assertTrue(self.wizard(answers, ["ghp_secret"]).run())
        c = load_config(self.cfg)
        r = c.repos[0]
        self.assertEqual((r.name, r.url, r.branch), ("web-app", "https://github.com/octo/Web-App.git", "main"))
        self.assertEqual((r.image, r.dockerfile, r.context), ("ghcr.io/octo/web-app", "docker/Dockerfile", "."))
        self.assertEqual(r.token_env, GIT_TOKEN_ENV)
        self.assertEqual((c.registries[0].server, c.registries[0].username), ("ghcr.io", "Octo"))
        self.assertEqual((c.settings.poll_interval, c.settings.webhook_port), (30, 8080))
        self.assertNotIn("ghp_secret", self.cfg.read_text())

        sp = secrets_path(self.cfg)
        self.assertEqual(stat.S_IMODE(sp.stat().st_mode), 0o600)
        text = sp.read_text()
        self.assertIn(f"{REGISTRY_PASSWORD_ENV}=ghp_secret", text)
        self.assertIn(f"{GIT_TOKEN_ENV}=ghp_secret", text)
        self.assertIn(f"{WEBHOOK_SECRET_ENV}=", text)

        for k in (REGISTRY_PASSWORD_ENV, GIT_TOKEN_ENV):
            os.environ.pop(k)
        load_secrets(sp)
        self.assertEqual(c.registries[0].password, "ghp_secret")
        self.assertEqual(r.token, "ghp_secret")

    def test_local_only_two_repos_and_add_repo(self):
        answers = ["n",                                 # no registry
                   "a/one", "n", "dev", "", "", "",     # public, branch dev
                   "y",
                   "a/one", "n", "", "", "", "",        # same basename -> one-2
                   "n", "", "n", ""]
        self.assertTrue(self.wizard(answers).run())
        c = load_config(self.cfg)
        self.assertEqual([r.name for r in c.repos], ["one", "one-2"])
        self.assertEqual(c.repos[0].tags, ["{branch}", "{sha}"])
        self.assertFalse(c.repos[0].push)
        self.assertEqual(c.registries, [])
        self.assertFalse(secrets_path(self.cfg).exists())

        w = self.wizard(["b/two", "n", "", "", "", "", ""])
        self.assertTrue(w.add_repo({"one", "one-2"}, push=False, namespace=None))
        self.assertEqual([r.name for r in load_config(self.cfg).repos], ["one", "one-2", "two"])

    def test_declining_save_writes_nothing(self):
        answers = ["n", "a/b", "n", "", "", "", "", "n", "", "n", "n"]
        self.assertFalse(self.wizard(answers).run())
        self.assertFalse(self.cfg.exists())

    def test_remote_check_detects_default_branch(self):
        origin = self.tmp / "origin"
        origin.mkdir()
        for cmd in (["init", "-q", "-b", "trunk"], ["-c", "user.email=a@b", "-c", "user.name=a",
                                                    "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(["git", *cmd], cwd=origin, check=True)
        answers = ["n", str(origin), "", "", "", "", "n", "", "n", ""]
        self.assertTrue(self.wizard(answers, check_remote=True).run())
        r = load_config(self.cfg).repos[0]
        self.assertEqual((r.branch, r.token_env), ("trunk", None))

    def test_remote_check_private_repo_asks_for_token(self):
        def fake(url, token=None):
            if token != "tok":
                raise RuntimeError("auth required")
            return "main"
        answers = ["n", "me/secret", "y", "", "", "", "", "n", "", "n", ""]
        with mock.patch("autobuild.git_ops.default_branch", side_effect=fake):
            self.assertTrue(self.wizard(answers, ["tok"], check_remote=True).run())
        self.assertEqual(load_config(self.cfg).repos[0].token_env, GIT_TOKEN_ENV)


if __name__ == "__main__":
    unittest.main()
