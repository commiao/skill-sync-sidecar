#!/usr/bin/env python3
"""Create a filtered local snapshot from an existing sidecar snapshot.

The filtered snapshot reuses content-addressed archives from the source
snapshot. It is useful for allowlist-only staging tests without publishing a new
remote index.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Source snapshot directory with index.json.")
    parser.add_argument("--out", required=True, help="Filtered snapshot output directory.")
    parser.add_argument("--skill-id", action="append", required=True, help="Skill id to include. May be repeated.")
    parser.add_argument("--label", required=True, help="Snapshot id for the filtered snapshot.")
    args = parser.parse_args()

    source = Path(args.source)
    out = Path(args.out)
    wanted = set(args.skill_id)
    index_path = source / "index.json"
    if not index_path.is_file():
        raise SystemExit(f"source index not found: {index_path}")

    index = json.loads(index_path.read_text(encoding="utf-8"))
    selected = [item for item in index.get("skills", []) if item.get("skill_id") in wanted]
    found = {item.get("skill_id") for item in selected}
    missing = sorted(wanted - found)
    if missing:
        raise SystemExit(f"skill ids not found in source snapshot: {', '.join(missing)}")

    out.mkdir(parents=True, exist_ok=True)
    for item in selected:
        archive = item.get("archive")
        if not archive:
            raise SystemExit(f"skill has no archive path: {item.get('skill_id')}")
        src_archive = source / archive
        dst_archive = out / archive
        if not src_archive.is_file():
            raise SystemExit(f"source archive not found: {src_archive}")
        dst_archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_archive, dst_archive)

    filtered = {
        "protocol_version": index.get("protocol_version", 0),
        "snapshot_id": args.label,
        "created_at": index.get("created_at"),
        "total": len(selected),
        "skills": selected,
        "filtered_from": {
            "snapshot_id": index.get("snapshot_id"),
            "total": index.get("total"),
            "source": str(source),
        },
    }
    (out / "index.json").write_text(json.dumps(filtered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"snapshot_id={args.label}")
    print(f"total={len(selected)}")
    print(f"out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
