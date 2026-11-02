import hashlib
import io
import base64
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.sync_github_updates import (
    SOURCE_ORIGIN, assert_assets_match, download_verified, public_manifest, validate_export, sync,
)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"version": "0.30.138", "channel": "stable", "asset": "XyetaBinder.exe",
                         "size": 2, "sha256": "a" * 64, "package_type": "installer"}
        self.rows = [{"name": "XyetaBinder.exe", "size": 2, "sha256": "a" * 64,
                      "url": SOURCE_ORIGIN + "/github-updates/stable?asset=XyetaBinder.exe&version=0.30.138"},
                     {"name": "components.json", "size": 2, "sha256": "b" * 64,
                      "url": SOURCE_ORIGIN + "/github-updates/stable?asset=components.json&version=0.30.138"}]

    def test_export_matches_manifest_and_allowed_assets(self):
        manifest, rows = validate_export({"manifest": self.manifest, "assets": self.rows}, "stable")
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(rows, self.rows)
        self.rows[1]["name"] = "private-source.zip"
        with self.assertRaises(ValueError):
            validate_export({"manifest": self.manifest, "assets": self.rows}, "stable")

    def test_export_rejects_foreign_urls(self):
        self.rows[0]["url"] = "https://untrusted.example/file"
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_export({"manifest": self.manifest, "assets": self.rows}, "stable")

    def test_public_manifest_excludes_backend_credentials_and_urls(self):
        self.manifest.update(access_key="secret", source_url="https://internal.example")
        public = public_manifest(self.manifest)
        self.assertNotIn("access_key", public)
        self.assertNotIn("source_url", public)
        self.assertEqual(public["download_url"],
                         "https://github.com/KeLa13/kela13/releases/download/v0.30.138/XyetaBinder.exe")

    def test_existing_release_is_never_silently_overwritten(self):
        release = {"assets": [{"name": row["name"], "size": row["size"], "digest": "sha256:" + row["sha256"]}
                              for row in self.rows]}
        assert_assets_match(release, self.rows)
        release["assets"][0]["digest"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(RuntimeError, "refusing"):
            assert_assets_match(release, self.rows)

    def test_owner_rollback_changes_only_the_pointer_and_preserves_release_assets(self):
        release_text = json.dumps(public_manifest(self.manifest), ensure_ascii=False, indent=2) + "\n"
        release_rows = self.rows + [{"name": "update.json", "size": len(release_text.encode()),
                                    "sha256": hashlib.sha256(release_text.encode()).hexdigest()}]
        release = {"draft": False, "prerelease": False, "assets": [
            {"name": row["name"], "size": row["size"], "digest": "sha256:" + row["sha256"]}
            for row in release_rows]}
        current = {"version": "0.30.185", "channel": "stable"}
        writes = []
        owner_rollback = True

        class API:
            def request(inner, method, endpoint, payload=None, **kwargs):
                nonlocal current
                if method == "GET" and endpoint == "contents/stable/update.json":
                    return {"sha": "pointer-sha", "content": base64.b64encode(json.dumps(current).encode()).decode()}
                if method == "GET" and endpoint == "releases/tags/v0.30.138":
                    return release
                if method == "PUT" and endpoint == "contents/stable/update.json":
                    self.assertEqual(payload["sha"], "pointer-sha")
                    current = json.loads(base64.b64decode(payload["content"]))
                    writes.append(endpoint)
                    return {}
                self.fail(f"Unexpected release mutation or asset download: {method} {endpoint}")

        def opener(request, timeout):
            self.assertEqual(request.full_url, SOURCE_ORIGIN + "/github-updates/stable")
            return io.BytesIO(json.dumps({"manifest": self.manifest, "assets": self.rows,
                                         "policy": {"rollback": owner_rollback}}).encode())

        with patch("scripts.sync_github_updates.urllib.request.urlopen", opener):
            sync("stable", API())
            self.assertEqual(current["version"], "0.30.138")
            self.assertIs(current["rollback"], True)
            self.assertIs(current["mandatory"], True)
            owner_rollback = False
            sync("stable", API())
            self.assertNotIn("rollback", current)
            owner_rollback = True
            # A damaged installer still blocks the pointer even during rollback.
            release["assets"][0]["digest"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(RuntimeError, "refusing"):
                sync("stable", API())
            self.assertNotIn("rollback", current)
        self.assertEqual(writes, ["contents/stable/update.json"] * 2)

    def test_download_is_anonymous_and_hash_verified(self):
        data = b"MZverified"
        row = {"name": "XyetaBinder.exe", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
               "url": self.rows[0]["url"]}

        def opener(request, timeout):
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("X-update-key"))
            response = io.BytesIO(data)
            response.status = 200
            return response

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(download_verified(row, directory, opener).read_bytes(), data)
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                download_verified(row | {"sha256": "f" * 64}, directory, opener)
