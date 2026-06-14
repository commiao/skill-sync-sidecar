from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass(frozen=True)
class SnapshotDiff:
    added: List[str]
    removed: List[str]
    changed: List[str]
    unchanged: List[str]
    risk_changed: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "summary": {
                "added": len(self.added),
                "removed": len(self.removed),
                "changed": len(self.changed),
                "unchanged": len(self.unchanged),
                "risk_changed": len(self.risk_changed),
            },
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "risk_changed": self.risk_changed,
        }


def diff_snapshot_dirs(left_dir: Path, right_dir: Path) -> SnapshotDiff:
    return diff_snapshot_indexes(load_index(left_dir), load_index(right_dir))


def diff_snapshot_indexes(left: dict, right: dict) -> SnapshotDiff:
    left_entries = entries_by_key(left)
    right_entries = entries_by_key(right)
    left_keys = set(left_entries)
    right_keys = set(right_entries)

    added = sorted(right_keys - left_keys)
    removed = sorted(left_keys - right_keys)
    changed = []
    unchanged = []
    risk_changed = []

    for key in sorted(left_keys & right_keys):
        left_entry = left_entries[key]
        right_entry = right_entries[key]
        if left_entry.get("content_hash") == right_entry.get("content_hash"):
            unchanged.append(key)
        else:
            changed.append(key)
        if left_entry.get("risk_level") != right_entry.get("risk_level"):
            risk_changed.append(key)

    return SnapshotDiff(added, removed, changed, unchanged, risk_changed)


def load_index(snapshot_dir: Path) -> dict:
    return json.loads((snapshot_dir / "index.json").read_text(encoding="utf-8"))


def entries_by_key(index: dict) -> Dict[str, dict]:
    entries = {}
    for skill in index.get("skills", []):
        key = skill.get("key") or f"{skill.get('source')}/{skill.get('skill_id')}"
        entries[key] = skill
    return entries
