"""Pinned V1 primitive provenance for the V2 thin-adapter boundary."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any


VENDOR_SOURCE_RELEASE = "v0.1.0-b043748"
VENDOR_SOURCE_ARCHIVE_SHA256 = (
    "7708c17b0d02c9cfb874f775c177f9f09df1ab583e32fabe04429c6bf8c50e58"
)
VENDOR_TREE_SHA256 = "4b9fa52dd630983cb05df6c3d7cd36c210910ef4cad27d403d5c48a3f55644e4"
VENDOR_SNAPSHOT_KIND = "upstream_derived_v2_hardened"

PINNED_VENDOR_FILES = {
    "scripts/xhs/search.py": "969a84eecd9cba65ecd62fd18208e780009cc984221e5603d73cceee9ab36670",
    "scripts/xhs/bridge.py": "9eff623aacc0bf00e38ae78caca3059fbb0d35c700da23c1f91d6afc5026ca16",
    "scripts/xhs/navigation.py": "457b2f7c4ab20da7e0a8e8bdff4a8503a973865ac33aaea3763fb93fdcb868fc",
    "scripts/xhs/like_favorite.py": "9b881495897e55d045bb66a72efa7c2a306fb7056bbc62c0883f1ae1d251481c",
    "scripts/xhs/comment.py": "5f4340c716fc732a2dda16ed35b4557d9dff62de9083ff9ca4ec64a4d95b106c",
    "scripts/xhs/dm.py": "a860beb6142d49a6985cffea56ab9afa0403a8d3206c82ed4d0a11c80496a218",
    "scripts/cli.py": "da93827d20fe3d37b5ef07b0d4e6d6ad2e6a2606a773d8e4463d0e493e8914c8",
}

PRIMITIVE_MATRIX = {
    "search": {
        "operations": ("search_feeds_visible", "adopt_current_search_results"),
        "files": ("scripts/xhs/search.py", "scripts/xhs/bridge.py", "scripts/cli.py"),
        "evidence": ("docs/migration/21_ranfang_ordinary_search_surface_recovery_audit.md",),
    },
    "navigation": {
        "operations": ("open_search_result", "go_back_and_verify", "open_commenter_profile", "open_dm_conversation"),
        "files": ("scripts/xhs/bridge.py", "scripts/xhs/navigation.py", "scripts/cli.py"),
        "evidence": ("docs/migration/22_ranfang_complete_xhs_control_path_replication_audit.md",),
    },
    "note_like": {
        "operations": ("like_current_feed",),
        "files": ("scripts/xhs/like_favorite.py", "scripts/cli.py"),
        "evidence": ("docs/migration/10_ranfang_approved_execution_bridge_audit.md",),
    },
    "note_comment": {
        "operations": ("post_comment_current",),
        "files": ("scripts/xhs/comment.py", "scripts/cli.py"),
        "evidence": ("docs/migration/10_ranfang_approved_execution_bridge_audit.md",),
    },
    "comment_like": {
        "operations": ("like_current_comment",),
        "files": ("scripts/xhs/comment.py", "scripts/cli.py"),
        "evidence": ("docs/migration/22_ranfang_complete_xhs_control_path_replication_audit.md",),
    },
    "comment_reply": {
        "operations": ("reply_current_comment",),
        "files": ("scripts/xhs/comment.py", "scripts/cli.py"),
        "evidence": ("docs/migration/22_ranfang_complete_xhs_control_path_replication_audit.md",),
    },
    "single_dm": {
        "operations": ("send_current_dm_message",),
        "files": ("scripts/xhs/dm.py", "scripts/xhs/navigation.py", "scripts/cli.py"),
        "evidence": ("docs/migration/13_ranfang_dm_audit.md",),
    },
}


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def audit_v1_primitive_provenance(project_root: Path) -> dict[str, Any]:
    root = Path(project_root)
    vendor = root / "vendor" / "xiaohongshu-skills"
    file_rows = {}
    for relative, expected in PINNED_VENDOR_FILES.items():
        actual = _file_sha256(vendor / relative)
        file_rows[relative] = {
            "expected_sha256": expected,
            "actual_sha256": actual,
            "matches": actual == expected,
        }
    primitives = []
    for primitive, contract in PRIMITIVE_MATRIX.items():
        changed = [
            relative for relative in contract["files"]
            if not file_rows[relative]["matches"]
        ]
        primitives.append({
            "primitive": primitive,
            "operations": list(contract["operations"]),
            "provider": "XhsOperationGateway -> ranfang_run_agent -> XHS Bridge",
            "source_files": list(contract["files"]),
            "evidence": list(contract["evidence"]),
            "source_hashes_match": not changed,
            "changed_or_missing_files": changed,
            "requires_seam_revalidation": bool(changed),
            "reimplementation_required": False,
        })
    return {
        "schema_version": 1,
        "source_release": VENDOR_SOURCE_RELEASE,
        "source_archive_sha256": VENDOR_SOURCE_ARCHIVE_SHA256,
        "vendor_tree_sha256": VENDOR_TREE_SHA256,
        "vendor_snapshot_kind": VENDOR_SNAPSHOT_KIND,
        "file_hashes": file_rows,
        "primitives": primitives,
        "all_source_hashes_match": all(row["matches"] for row in file_rows.values()),
        "live_provider_count": 1,
        "live_provider": "ranfang_run_agent_xhs_bridge",
        "platform_actions_executed": 0,
    }
