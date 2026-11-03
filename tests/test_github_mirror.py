import hashlib
import io
import base64
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.sync_github_updates import (
    SOURCE_ORIGIN, assert_assets_match, download_verified, public_manifest, validate_export, sync,
    sync_unified_beta,
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


class UnifiedMirrorTests(unittest.TestCase):
    """Exercise legacy delivery with a fake GitHub and real hash verification."""
    def setUp(self):
        self.releases = {}
        self.pointers = {}
        self.files = {}
        self.writes = []
        self.stable = self.add_release('stable', '0.30.185', b'MZ ordinary reviewed installer')
        self.pointers['stable'] = self.stable

    def add_release(self, channel, version, installer):
        asset = 'XyetaBinder-Beta-Setup.exe' if channel == 'beta' else 'XyetaBinder.exe'
        manifest = public_manifest(dict(version=version, channel=channel, asset=asset,
            size=len(installer), sha256=hashlib.sha256(installer).hexdigest(),
            package_type='installer', mandatory=False, components_asset='components.json'))
        tag = ('beta-v' if channel == 'beta' else 'v') + version
        content = {asset: installer, 'components.json': b'{"files":[]}',
                   'update.json': (json.dumps(manifest, ensure_ascii=False, indent=2)+'\n').encode()}
        release = dict(id=len(self.releases)+1, tag_name=tag, draft=False,
                       prerelease=channel == 'beta', assets=[])
        for name, data in content.items():
            self.files[tag, name] = data
            release['assets'].append(dict(name=name, size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest()))
        self.releases[tag] = release
        return manifest

    def request(self, method, endpoint, payload=None, *, upload=None, missing_ok=False):
        if method == 'GET' and endpoint.startswith('contents/'):
            value = self.pointers.get(endpoint.split('/')[1])
            text = json.dumps(value, ensure_ascii=False, indent=2)+'\n'
            return None if value is None else dict(sha='pointer', content=base64.b64encode(text.encode()).decode())
        if method == 'GET' and endpoint.startswith('releases/tags/'):
            return self.releases.get(endpoint.removeprefix('releases/tags/'))
        if method == 'GET' and endpoint.startswith('git/ref/tags/'):
            return None
        if method == 'GET' and endpoint.startswith('releases/'):
            return next(row for row in self.releases.values() if row['id'] == int(endpoint.split('/')[1]))
        self.writes.append((method, endpoint))
        if method == 'POST' and endpoint == 'releases':
            row = dict(payload, id=len(self.releases)+1, assets=[])
            self.releases[row['tag_name']] = row
            return row
        if upload:
            release_id, path = upload
            row = next(item for item in self.releases.values() if item['id'] == release_id)
            data = path.read_bytes()
            self.files[row['tag_name'], path.name] = data
            row['assets'].append(dict(name=path.name, size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest()))
            return {}
        if method == 'PATCH':
            row = self.request('GET', endpoint)
            row.update(payload)
            return row
        if method == 'PUT' and endpoint.startswith('contents/'):
            channel = endpoint.split('/')[1]
            self.pointers[channel] = json.loads(base64.b64decode(payload['content']))
            self.assertFalse(self.releases['beta-v'+self.pointers[channel]['version']]['draft'])
            return {}
        self.fail(f'Unexpected request {method} {endpoint}')

    def run_bridge(self):
        def opener(request, timeout):
            prefix = 'https://github.com/KeLa13/kela13/releases/download/'
            self.assertTrue(request.full_url.startswith(prefix))
            tag, name = request.full_url.removeprefix(prefix).split('/')
            response = io.BytesIO(self.files[tag, name]); response.status = 200
            return response
        with patch('scripts.sync_github_updates.download_verified',
                   lambda row, directory: download_verified(row, directory, opener)):
            sync_unified_beta(self)

    def test_legacy_alias_delivers_identical_ordinary_installer_and_is_idempotent(self):
        self.run_bridge()
        alias = self.pointers['beta']
        self.assertEqual(alias['version'], '0.30.185')
        self.assertEqual(alias['sha256'], self.stable['sha256'])
        self.assertEqual(alias['download_url'],
            'https://github.com/KeLa13/kela13/releases/download/beta-v0.30.185/XyetaBinder-Beta-Setup.exe')
        self.assertNotIn('delta_asset', alias)
        self.assertEqual(self.files['beta-v0.30.185', alias['asset']], self.files['v0.30.185', 'XyetaBinder.exe'])
        self.assertEqual(self.pointers['stable'], self.stable)
        self.assertEqual(self.releases['beta-v0.30.185']['make_latest'], 'false')
        before = list(self.writes)
        self.run_bridge()
        self.assertEqual(self.writes, before)

    def test_changed_source_pointer_or_installer_never_updates_legacy_pointer(self):
        self.pointers['stable'] = dict(self.stable, sha256='f'*64)
        with self.assertRaisesRegex(RuntimeError, 'pointer differs'):
            self.run_bridge()
        self.assertNotIn('beta', self.pointers)
        self.pointers['stable'] = self.stable
        self.files['v0.30.185', 'XyetaBinder.exe'] = b'MZ altered'
        with self.assertRaisesRegex(RuntimeError, 'size/SHA-256'):
            self.run_bridge()
        self.assertNotIn('beta', self.pointers)

    def test_owner_rollback_preserves_approved_legacy_182_and_then_can_promote(self):
        self.run_bridge()
        stable182 = self.add_release('stable', '0.30.182', b'MZ stable 182')
        beta182 = self.add_release('beta', '0.30.182', b'MZ original beta 182')
        old_manifest_bytes = self.files['beta-v0.30.182', 'update.json']
        self.pointers['stable'] = dict(stable182, rollback=True, mandatory=True)
        baseline = {key: beta182[key] for key in ('version', 'size', 'sha256')}
        with patch.dict('scripts.sync_github_updates.LEGACY_BASELINE', baseline):
            self.run_bridge()
        self.assertEqual(self.pointers['beta']['sha256'], beta182['sha256'])
        self.assertIs(self.pointers['beta']['rollback'], True)
        self.assertIs(self.pointers['beta']['mandatory'], True)
        self.assertEqual(self.files['beta-v0.30.182', 'update.json'], old_manifest_bytes)
        self.pointers['stable'] = self.stable
        self.run_bridge()
        self.assertEqual(self.pointers['beta']['version'], '0.30.185')
        self.assertNotIn('rollback', self.pointers['beta'])

    def test_activation_refuses_old_build_without_explicit_rollback(self):
        self.pointers['stable'] = self.add_release('stable', '0.30.182', b'MZ stable 182')
        with self.assertRaisesRegex(RuntimeError, 'reviewed migration build'):
            self.run_bridge()
        self.assertNotIn('beta', self.pointers)

    def test_preexisting_conflicting_alias_is_never_overwritten(self):
        self.add_release('beta', '0.30.185', b'MZ unrelated private beta')
        with self.assertRaisesRegex(RuntimeError, 'refusing to replace'):
            self.run_bridge()
        self.assertFalse(self.writes)
        self.assertNotIn('beta', self.pointers)
