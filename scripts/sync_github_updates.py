"""Run in the PUBLIC repository with its own Actions token; copy tested packages."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 600 * 1024 * 1024
MAX_JSON = 1024 * 1024
PUBLIC_REPOSITORY = "KeLa13/kela13"
SOURCE_ORIGIN = "https://xyeta-private-updater.20kella01.workers.dev"
VERSION_RE = re.compile(r"\d+(?:\.\d+){1,7}")
SHA_RE = re.compile(r"[0-9a-f]{64}")


def read_json(response):
    raw = response.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("JSON exceeds the manifest limit")
    return json.loads(raw.decode("utf-8-sig"))


def validate_export(payload, channel):
    if not isinstance(payload, dict) or not isinstance(payload.get("manifest"), dict):
        raise ValueError("Invalid export")
    manifest = dict(payload["manifest"])
    version = str(manifest.get("version", ""))
    if not VERSION_RE.fullmatch(version) or manifest.get("channel") != channel:
        raise ValueError("Wrong version/channel")
    installer = "XyetaBinder-Beta-Setup.exe" if channel == "beta" else "XyetaBinder.exe"
    if manifest.get("asset") != installer or manifest.get("package_type") != "installer":
        raise ValueError("Wrong installer")
    expected = {installer: (manifest.get("size"), manifest.get("sha256")), "components.json": None}
    if manifest.get("delta_asset"):
        previous = str(manifest.get("delta_from", ""))
        name = f"XyetaBinder-Beta-Delta-from-{previous}.zip"
        if channel != "beta" or not VERSION_RE.fullmatch(previous) or manifest["delta_asset"] != name:
            raise ValueError("Wrong delta package")
        expected[name] = (manifest.get("delta_size"), manifest.get("delta_sha256"))
    rows = payload.get("assets")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("Wrong exported assets")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("name") not in expected or row["name"] in seen:
            raise ValueError("Unexpected/duplicate exported asset")
        seen.add(row["name"])
        size, digest = row.get("size"), row.get("sha256")
        if type(size) is not int or not 0 < size <= MAX_BYTES or not SHA_RE.fullmatch(str(digest)):
            raise ValueError("Invalid asset size/digest")
        if expected[row["name"]] is not None and expected[row["name"]] != (size, digest):
            raise ValueError("Export does not match the release manifest")
        query = urllib.parse.urlencode({"asset": row["name"], "version": version})
        if row.get("url") != f"{SOURCE_ORIGIN}/github-updates/{channel}?{query}":
            raise ValueError("Unexpected export URL")
    return manifest, rows


def public_manifest(manifest):
    # An explicit allowlist prevents future backend credentials/internal URLs
    # from being carried into the public repository by a schema extension.
    allowed = {"version", "channel", "asset", "sha256", "size", "notes", "mandatory",
               "rollback", "package_type", "components_asset", "delta_from",
               "delta_asset", "delta_sha256", "delta_size"}
    result = {key: value for key, value in manifest.items() if key in allowed}
    tag = ("beta-v" if result["channel"] == "beta" else "v") + result["version"]
    prefix = f"https://github.com/{PUBLIC_REPOSITORY}/releases/download/{tag}/"
    result["download_url"] = prefix + urllib.parse.quote(result["asset"], safe="")
    if result.get("delta_asset"):
        result["delta_download_url"] = prefix + urllib.parse.quote(result["delta_asset"], safe="")
    return result


def download_verified(row, directory, opener=urllib.request.urlopen):
    path = Path(directory) / row["name"]
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(row["url"], headers={"User-Agent": "Xyeta-Mirror-Sync"})
    with opener(request, timeout=60) as response, path.open("wb") as handle:
        if response.status != 200:
            raise RuntimeError("Package download failed")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > row["size"]:
                raise RuntimeError("Package exceeds its declared size")
            digest.update(chunk)
            handle.write(chunk)
    if total != row["size"] or digest.hexdigest() != row["sha256"]:
        raise RuntimeError("Package size/SHA-256 mismatch")
    return path


class GitHubAPI:
    def __init__(self, token):
        if not token:
            raise ValueError("Actions publication token is missing")
        self.token = token

    def request(self, method, endpoint, payload=None, *, upload=None, missing_ok=False):
        url = f"https://api.github.com/repos/{PUBLIC_REPOSITORY}/{endpoint}"
        content_type = "application/json"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        if upload is not None:
            release_id, path = upload
            url = (f"https://uploads.github.com/repos/{PUBLIC_REPOSITORY}/releases/{release_id}/assets?"
                   + urllib.parse.urlencode({"name": path.name}))
            data = path.read_bytes()
            content_type = "application/octet-stream"
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": content_type, "User-Agent": "Xyeta-Mirror-Publisher",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(request, timeout=180 if upload else 30) as response:
                return read_json(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if missing_ok and code == 404:
                return None
            raise RuntimeError(f"GitHub publication HTTP {code}") from None


def assert_assets_match(release, rows):
    actual = {asset["name"]: asset for asset in release.get("assets", [])}
    for row in rows:
        asset = actual.get(row["name"])
        if (asset is None or asset.get("size") != row["size"]
                or asset.get("digest") != "sha256:" + row["sha256"]):
            raise RuntimeError("Existing release assets differ; refusing to replace them")


def sync(channel, api):
    source = f"{SOURCE_ORIGIN}/github-updates/{channel}"
    with urllib.request.urlopen(source, timeout=30) as response:
        manifest, rows = validate_export(read_json(response), channel)
    published = public_manifest(manifest)
    text = json.dumps(published, ensure_ascii=False, indent=2) + "\n"
    tag = ("beta-v" if channel == "beta" else "v") + manifest["version"]
    pointer_path = f"contents/{channel}/update.json"
    current = api.request("GET", pointer_path, missing_ok=True)
    if current:
        existing_text = base64.b64decode(current["content"]).decode("utf-8")
        existing_version = json.loads(existing_text).get("version", "")
        if (VERSION_RE.fullmatch(existing_version)
                and tuple(map(int, existing_version.split("."))) > tuple(map(int, manifest["version"].split(".")))
                and not manifest.get("rollback", False)):
            print(f"{channel}: keeping the newer published version")
            return
    manifest_row = {"name": "update.json", "size": len(text.encode("utf-8")),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    expected = rows + [manifest_row]
    release = api.request("GET", f"releases/tags/{tag}", missing_ok=True)
    if release is None:
        # A leftover tag must not silently point a new release at an old commit.
        if api.request("GET", f"git/ref/tags/{tag}", missing_ok=True) is not None:
            raise RuntimeError("Public tag already exists without a release")
        release = api.request("POST", "releases", {
            "tag_name": tag, "target_commitish": "master", "draft": True,
            "prerelease": channel == "beta", "name": f"Xyeta Binder {channel} {manifest['version']}",
            "body": str(manifest.get("notes", ""))[:12000],
        })
    if release.get("draft"):
        with tempfile.TemporaryDirectory(prefix="xyeta-mirror-") as directory:
            known = {asset["name"]: asset for asset in release.get("assets", [])}
            for row in expected:
                if row["name"] in known:
                    assert_assets_match({"assets": [known[row["name"]]]}, [row])
                    continue
                if row["name"] == "update.json":
                    path = Path(directory) / "update.json"
                    path.write_text(text, encoding="utf-8")
                else:
                    path = download_verified(row, directory)
                api.request("POST", "", upload=(release["id"], path))
        release = api.request("GET", f"releases/{release['id']}")
        assert_assets_match(release, expected)
        release = api.request("PATCH", f"releases/{release['id']}", {
            "draft": False, "prerelease": channel == "beta",
            "make_latest": "false" if channel == "beta" else "true",
        })
    else:
        if bool(release.get("prerelease")) != (channel == "beta"):
            raise RuntimeError("Existing release is in another channel")
        assert_assets_match(release, expected)
    # Publish the pointer only after every asset is verified and the draft is live.
    if not current or existing_text != text:
        payload = {"message": f"Publish {channel} update {manifest['version']}",
                   "content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "branch": "master"}
        if current:
            payload["sha"] = current["sha"]
        api.request("PUT", pointer_path, payload)
    print(f"{channel}: verified GitHub release {tag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=("stable", "beta"), required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != PUBLIC_REPOSITORY:
        raise SystemExit("This publisher must run inside the public distribution repository")
    sync(args.channel, GitHubAPI(os.environ.get("GH_TOKEN", "")))


if __name__ == "__main__":
    main()

