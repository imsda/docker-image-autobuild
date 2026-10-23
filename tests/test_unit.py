import hashlib
import hmac
import os
import unittest

from autobuild.config import ConfigError, parse_config
from autobuild.docker_ops import render_tags, sanitize_tag
from autobuild.webhook import match_repos, verify_signature

BASE = {"repo": [{"name": "app", "url": "https://github.com/o/app.git", "image": "ghcr.io/o/app"}]}


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        c = parse_config(BASE)
        r = c.repos[0]
        self.assertEqual((r.branch, r.dockerfile, r.context, r.push), ("main", "Dockerfile", ".", True))
        self.assertEqual(r.tags, ["latest", "{sha}"])
        self.assertEqual(c.settings.poll_interval, 60)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config({"repo": [{**BASE["repo"][0], "imagee": "x"}]})

    def test_missing_required(self):
        with self.assertRaises(ConfigError):
            parse_config({"repo": [{"name": "a", "url": "u"}]})

    def test_duplicate_names(self):
        with self.assertRaises(ConfigError):
            parse_config({"repo": BASE["repo"] * 2})

    def test_no_repos(self):
        with self.assertRaises(ConfigError):
            parse_config({})


class TagTests(unittest.TestCase):
    def test_render(self):
        tags = render_tags(["latest", "{sha}", "{branch}", "{branch}-{timestamp}", "latest"],
                           sha="abcdef1234567890", branch="feature/x", timestamp="20260101000000")
        self.assertEqual(tags, ["latest", "abcdef1", "feature-x", "feature-x-20260101000000"])

    def test_sanitize(self):
        self.assertEqual(sanitize_tag(".-bad tag!"), "bad-tag-")
        self.assertEqual(len(sanitize_tag("a" * 300)), 128)
        self.assertEqual(sanitize_tag("..."), "latest")


class WebhookTests(unittest.TestCase):
    def test_signature(self):
        body = b'{"a":1}'
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("s3cret", body, sig))
        self.assertFalse(verify_signature("wrong", body, sig))
        self.assertFalse(verify_signature("s3cret", body, None))

    def test_match(self):
        c = parse_config(BASE)
        payload = {"ref": "refs/heads/main", "repository": {
            "clone_url": "https://github.com/O/app.git", "html_url": "https://github.com/O/app"}}
        self.assertEqual([r.name for r in match_repos(c, payload)], ["app"])
        self.assertEqual(match_repos(c, {**payload, "ref": "refs/heads/dev"}), [])
        ssh = {"ref": "refs/heads/main", "repository": {"ssh_url": "git@github.com:o/app.git"}}
        self.assertEqual(len(match_repos(c, ssh)), 1)


if __name__ == "__main__":
    unittest.main()
