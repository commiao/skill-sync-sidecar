# Manifest v0

`manifest.json` is the canonical metadata file for Skill Sync packages. `SKILL.md` remains the agent-facing instruction entrypoint; `manifest.json` is the sidecar-facing sync, safety, and adapter contract.

## File Location

```text
<skill-dir>/
  SKILL.md
  manifest.json
  scripts/
  references/
```

## Minimal Example

```json
{
  "protocol_version": 0,
  "skill_id": "libtv-m-forward",
  "name": "libtv-m-forward",
  "description": "Maintain libtv/www upstream forwarding routes for the mobile BFF.",
  "scope": "project",
  "targets": ["codex", "cursor", "qoder"],
  "exclude": ["__pycache__", "*.pyc"]
}
```

## Fields

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `protocol_version` | yes | number | Must be `0` for this draft. |
| `skill_id` | yes | string | Stable lowercase id. Used for sync identity and conflict detection. |
| `name` | recommended | string | Human-facing name. Defaults to `SKILL.md` front matter or directory name. |
| `description` | recommended | string | Short description. Defaults to `SKILL.md` front matter. |
| `scope` | yes | string | `global` or `project`. |
| `targets` | recommended | string[] | Tool adapters where this skill may be installed or discovered. |
| `exclude` | optional | string[] | Per-skill exclude patterns, applied in addition to default generated-artifact excludes. |
| `project` | optional | object | Project association metadata for project-scoped skills. |
| `security` | optional | object | Encryption and review hints. |
| `external_references` | optional | string[] | Package-relative file paths allowed to be referenced from `SKILL.md` without being included in the synced package (for non-portable runtime helpers). |

## Scope Rules

`global` skills are portable across devices and can be installed into global tool roots such as `~/.cc-switch/skills`, `~/.skillshub`, or `~/.codex/skills`.

`project` skills depend on a repository context. Sidecar may sync and version them, but should not install them into global roots by default. A project skill should usually live under:

```text
<repo>/skills/<skill-id>/SKILL.md
```

During the transition period, sidecar recognizes this layout when the repo root has either `AGENTS.md` or `.git`.

## AGENTS.md Discovery Pointer

Before adapters are complete, repositories can expose project skills with a root `AGENTS.md` pointer:

```markdown
Project skills live under `skills/`.

- `skills/libtv-m-forward/SKILL.md`: mobile BFF forwarding workflow.
```

Sidecar scanning should treat `skills/<skill-id>/SKILL.md` under such a repo as `scope=project`.

## Exclude Rules

Sidecar always excludes generated and bulky defaults such as `__pycache__`, `node_modules`, `logs`, `tmp`, `.git`, `target`, `dist`, and `build`.

`manifest.json.exclude` adds package-specific patterns:

```json
{
  "exclude": ["generated", "*.tmp", "reports/*.html"]
}
```

`external_references` is used for skills whose `SKILL.md` documents runtime helpers outside the synced package, such as environment-specific wrappers or host tools. The paths are package-relative and are intentionally explicit so reviewers can approve portability trade-offs:

```json
{
  "external_references": ["scripts/local-runtime-helper.sh"]
}
```

Use this field only with `scope=global` peer-specific workflows (for example OpenClaw private runtime adaptations). For portable skills, prefer keeping referenced files inside the skill directory.

Patterns match either the relative path or the basename.

Sidecar also applies default safety excludes for common secret-like files such as `.encryption-key`, `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, SSH private-key names, `.npmrc`, and `.pypirc`. Do not rely on these defaults as the only policy for generated or application-specific state; declare those paths explicitly in `exclude`.

## Security and Encryption

Skill packages should not contain credentials. If a skill needs an external service, it should use the caller's configured auth, such as `lark-cli` OAuth.

Encrypted skill files are not implemented in v0. Current recommendation:

- Prefer private repo/WebDAV permissions for normal private skills.
- Do not sync secrets.
- If encryption is required, keep encrypted files opaque to sidecar and declare the convention in `security.encryption`.

Example:

```json
{
  "security": {
    "contains_secrets": false,
    "encryption": "none"
  }
}
```

## libtv-m-forward Acceptance Case

`libtv-m-forward` is the second P0/P1 black-box acceptance case after the 91-skill cc-switch snapshot:

- Project layout: `libtv-m/skills/libtv-m-forward/`
- Scope: `project`
- Files: `SKILL.md` plus Python scripts
- No embedded credentials
- External side effect: scripts may call `lark-cli` with caller OAuth
- Expected sidecar behavior: scan, validate, hash, package, and sync without global install by default
