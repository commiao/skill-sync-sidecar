# Mac Cross-Tool Inventory 2026-06-25

## Scope

After OpenClaw sync reached green, this pass measured the current Mac tool roots against the latest WebDAV canonical snapshot and fixed one skillshub import diagnosis false positive.

Canonical snapshot:

```text
approved-push-20260624T143912.012979Z
```

Raw outputs:

```text
tmp/cross-tool-20260625/inventory.json
tmp/cross-tool-20260625/projection.json
tmp/cross-tool-20260625/projection-summary.json
tmp/cross-tool-20260625/hub-import-diagnosis-after-canonical.json
tmp/cross-tool-20260625/hub-import-preview-after-canonical/preview.json
tmp/cross-tool-20260625/hub-import-apply-after-canonical-dryrun.json
```

## Tool Projection

| Tool | Installed | Canonical targeted | Installed canonical | Missing | Drift | Unsupported scope | Not targeted | Extra local |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| cc-switch | 94 | 92 | 92 | 0 | 0 | 0 | 2 | 0 |
| skillshub | 94 | 92 | 60 | 28 | 4 | 0 | 2 | 29 |
| Codex | 28 | 94 | 0 | 92 | 0 | 2 | 0 | 28 |
| Cursor | 107 | 2 | 0 | 0 | 0 | 2 | 92 | 46 |
| Claude Code | 0 | 0 | 0 | 0 | 0 | 0 | 94 | 0 |

Interpretation:

- `cc-switch` is aligned with the canonical snapshot.
- `skillshub` has 28 missing global canonical skills and 4 drifted canonical skills; it also has 29 local extras, mostly Lark/Feishu skills.
- `Codex` has many missing canonical globals, but this should be applied in small allowlists, not bulk.
- `Cursor` and `Claude Code` are mostly not targeted by manifest metadata yet, so automatic global projection is intentionally blocked.
- `beijing-recruitment` and `tianjin-recruitment` are project-scoped and should not be installed into global Hub roots.

## Skillshub Diagnosis

Before the fix, `hub-import` read from local tool roots. Because `.cc-switch` extracted skill directories do not contain `manifest.json`, project skills such as `beijing-recruitment` and `tianjin-recruitment` were downgraded to scanner defaults:

```text
scope=global
targets=cc-switch,skillshub,codex,openclaw
```

That made skillshub preview report 30 importable skills.

The diagnosis now overlays canonical snapshot metadata by `skill_id` when available, so local tool roots that lack `manifest.json` still inherit canonical `scope` and `targets`.

Current skillshub diagnosis:

```json
{
  "already_in_hub": 125,
  "importable": 28,
  "not_compatible": 2,
  "update_available": 7
}
```

Current dry-run apply:

```json
{
  "total": 35,
  "allowed": 28,
  "blocked": 7,
  "by_action": {
    "preview_import": 28,
    "review_update": 7
  }
}
```

The two incompatible entries are:

```text
beijing-recruitment
tianjin-recruitment
```

## Why Skill Counts Differ

Different tools are not mirrors of one directory. Counts differ because:

- each tool has a different discovery root;
- each tool supports different scopes;
- manifest `targets` intentionally exclude some tools;
- local-only or marketplace skills exist outside the canonical WebDAV snapshot;
- some directories lose manifest metadata after being projected into a tool root.

The sidecar should remain the normalization and preflight layer instead of asking cc-switch or skillshub to change their internal behavior.

## Verification

```text
PYTHONPATH=src python3 -m unittest tests.test_projection
Ran 9 tests OK

PYTHONPATH=src python3 -m unittest discover -s tests
Ran 102 tests OK
```
