#!/usr/bin/env python3
"""Build an OpenClaw admission report for remote_new skills.

This is intentionally conservative: it does not apply anything. It reads a
reconcile report plus the remote snapshot index and groups OpenClaw pull
candidates by review priority.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


SIDE_EFFECT_KEYWORDS = {
    "deploy",
    "ship",
    "upgrade",
    "setup",
    "browser",
    "browse",
    "qa",
    "canary",
    "benchmark",
    "agent",
    "cookies",
    "gbrain",
    "health",
}

OPENCLAW_NATIVE = {
    "gstack-openclaw-ceo-review",
    "gstack-openclaw-investigate",
    "gstack-openclaw-office-hours",
    "gstack-openclaw-retro",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconcile-report", required=True)
    parser.add_argument("--remote-index", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args()

    reconcile = _load_json(Path(args.reconcile_report))
    index = _load_json(Path(args.remote_index))
    by_skill = {item["skill_id"]: item for item in index.get("skills", [])}
    remote_new = [item["skill_id"] for item in reconcile.get("items", []) if item.get("status") == "remote_new"]

    rows = []
    for skill_id in sorted(remote_new):
        remote = by_skill[skill_id]
        rows.append(classify(remote))

    summary = Counter(row["admission"] for row in rows)
    report = {
        "report_type": "openclaw-admission-report",
        "reconcile_report": str(Path(args.reconcile_report)),
        "remote_index": str(Path(args.remote_index)),
        "remote_snapshot_id": index.get("snapshot_id"),
        "remote_total": index.get("total"),
        "remote_new_total": len(rows),
        "summary": dict(sorted(summary.items())),
        "rows": rows,
    }

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"remote_new={len(rows)}")
    print(f"summary={dict(sorted(summary.items()))}")
    print(f"markdown={out_md}")
    print(f"json={out_json}")
    return 0


def classify(remote: Dict[str, Any]) -> Dict[str, Any]:
    skill_id = remote["skill_id"]
    description = remote.get("description") or ""
    risk = remote.get("risk_level") or "unknown"
    size = int(remote.get("size_bytes") or 0)
    file_count = int(remote.get("file_count") or 0)
    text = f"{skill_id} {description}".lower()

    reasons: List[str] = []
    admission = "p1_review"

    if skill_id in OPENCLAW_NATIVE:
        admission = "p0_candidate"
        reasons.append("OpenClaw-native skill")
    elif risk != "ok":
        admission = "p2_defer"
        reasons.append(f"risk={risk}")
    elif size > 1_000_000 or file_count > 50:
        admission = "p2_defer"
        reasons.append("large package or many files")
    elif any(keyword in text for keyword in SIDE_EFFECT_KEYWORDS):
        admission = "p2_defer"
        reasons.append("tooling, browser, deploy, setup, or automation side effects")
    elif file_count <= 3 and size <= 25_000:
        admission = "p0_candidate"
        reasons.append("small low-risk package")
    else:
        reasons.append("needs human review before OpenClaw install")

    if _has_issue(remote, "large_package"):
        admission = "p2_defer"
        reasons.append("scanner issue: large_package")

    return {
        "skill_id": skill_id,
        "admission": admission,
        "risk_level": risk,
        "file_count": file_count,
        "size_bytes": size,
        "description": description,
        "archive": remote.get("archive"),
        "content_hash": remote.get("content_hash"),
        "reasons": reasons,
    }


def render_markdown(report: Dict[str, Any]) -> str:
    rows = report["rows"]
    lines = [
        "# OpenClaw Admission Report - 2026-06-15",
        "",
        "Purpose: classify the 60 `pull_new` skills from the Mac/WebDAV canonical snapshot before any OpenClaw live apply.",
        "",
        "This report is dry-run only. It does not install skills.",
        "",
        "## Snapshot",
        "",
        "```text",
        f"remote_snapshot_id={report.get('remote_snapshot_id')}",
        f"remote_total={report.get('remote_total')}",
        f"remote_new_total={report.get('remote_new_total')}",
        f"summary={report.get('summary')}",
        "```",
        "",
        "## Admission Policy",
        "",
        "- `p0_candidate`: small, low-risk, or OpenClaw-native; eligible for a tiny supervised batch.",
        "- `p1_review`: potentially useful but needs human review before install.",
        "- `p2_defer`: large, warning-risk, browser/deploy/setup/automation-heavy, or otherwise unsuitable for first live batch.",
        "",
        "## P0 Candidates",
        "",
        "*_Still dry-run only. These are candidates for the first supervised OpenClaw live batch, not approved installs._",
        "",
    ]
    lines.extend(render_table(row for row in rows if row["admission"] == "p0_candidate"))
    lines.extend(["", "## P1 Review", ""])
    lines.extend(render_table(row for row in rows if row["admission"] == "p1_review"))
    lines.extend(["", "## P2 Defer", ""])
    lines.extend(render_table(row for row in rows if row["admission"] == "p2_defer"))
    lines.extend(
        [
            "",
            "## Next Gate",
            "",
            "1. Review the P0 list and select a tiny first batch.",
            "2. Run an isolated target-root apply test for that batch, not `/home/admin/clawd/skills`.",
            "3. Run OpenClaw live apply only after an explicit allowlist exists.",
            "4. Keep `openclaw-skill-sync-sidecar-dryrun.service` in `--dry-run` mode until full admission is complete.",
            "",
        ]
    )
    return "\n".join(lines)


def render_table(rows: Iterable[Dict[str, Any]]) -> List[str]:
    materialized = list(rows)
    if not materialized:
        return ["_None._"]
    lines = [
        "| Skill | Risk | Files | Size | Reason |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in materialized:
        lines.append(
            "| {skill_id} | {risk_level} | {file_count} | {size_bytes} | {reasons} |".format(
                skill_id=row["skill_id"],
                risk_level=row["risk_level"],
                file_count=row["file_count"],
                size_bytes=row["size_bytes"],
                reasons="<br>".join(row["reasons"]),
            )
        )
    return lines


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _has_issue(remote: Dict[str, Any], code: str) -> bool:
    return any(issue.get("code") == code for issue in remote.get("issues") or [])


if __name__ == "__main__":
    raise SystemExit(main())
