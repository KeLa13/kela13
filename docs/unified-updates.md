# One ordinary Binder release

The reviewed 0.30.185 client migrates saved Beta preferences to the ordinary update channel. Its preview installer is private until the owner tests it.

Keep `unified: false` in `.release-request.json` during review. Beta stays pinned to the approved 0.30.182; no new release is exposed by this change.

After owner approval and publication of the ordinary migration release, set `unified: true`. The existing scheduled publisher first verifies Stable, then copies the same installer bytes to the legacy Beta tag/name required by old clients. The installed program switches to Stable without a channel chooser. There is no separate Beta build.

Both manifests and installer hashes are checked before changing pointers. Existing release assets cannot be overwritten. Owner-selected rollback propagates to legacy clients; the original verified Beta 182 installer remains the special historical baseline because its bytes differ from Stable 182.

Tests: `python -m unittest discover -s tests -p 'test_*.py'`.
