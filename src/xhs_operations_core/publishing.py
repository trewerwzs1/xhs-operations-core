"""Immutable, single-action image/video publishing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .action_preflight import (
    ActionPreflightError,
    RuntimeMode,
    UnifiedActionPreflightStore,
    UnifiedActionRequest,
    UnifiedPreflightState,
)
from .platform.xhs import (
    BOUNDED_WRITE_UAT_CONFIRMATION,
    RunAgentClient,
    RunAgentError,
)
from .storage import read_json, write_json_atomic


PUBLISH_APPROVAL_CONFIRMATION = "I_APPROVE_SINGLE_XHS_PUBLISH"
MEDIA_TYPES = {"image", "video"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


class PublishContractError(ValueError):
    pass


def _safe_id(name: str, value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text) is None:
        raise PublishContractError(f"{name} must be a safe id")
    return text


def _timestamp(name: str, value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublishContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublishContractError(f"{name} must include timezone")
    return text


def _title_units(value: str) -> int:
    weighted = 0
    encoded = value.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[index : index + 2], "little")
        weighted += 2 if unit > 127 else 1
    return (weighted + 1) // 2


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_media_path(project_root: Path, value: object) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise PublishContractError("media path must be text")
    root = Path(project_root).resolve()
    candidate = Path(value)
    absolute = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise PublishContractError("publish media must be inside the project directory") from exc
    if not absolute.is_file():
        raise PublishContractError(f"publish media is missing: {relative.as_posix()}")
    return relative.as_posix(), absolute


@dataclass(frozen=True)
class PublishPlan:
    plan_id: str
    schema_version: int
    account_id: str
    media_type: str
    title: str
    content: str
    tags: tuple[str, ...]
    media_paths: tuple[str, ...]
    media_hashes: tuple[str, ...]
    visibility: str
    created_at: str
    content_hash: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, project_root: Path) -> "PublishPlan":
        input_fields = {
            "schema_version", "account_id", "media_type", "title", "content",
            "tags", "media_paths", "visibility", "created_at",
        }
        persisted_fields = input_fields | {"plan_id", "media_hashes", "content_hash"}
        keys = set(value)
        if keys != input_fields and keys != persisted_fields:
            raise PublishContractError("publish plan fields are incomplete or unknown")
        if value["schema_version"] != 1:
            raise PublishContractError("publish plan schema_version must be 1")
        media_type = str(value["media_type"] or "").strip()
        if media_type not in MEDIA_TYPES:
            raise PublishContractError("publish media_type must be image or video")
        title = " ".join(str(value["title"] or "").split()).strip()
        if not title or _title_units(title) > 20 or "\ufffd" in title or set(title) == {"?"}:
            raise PublishContractError("publish title failed length or Unicode validation")
        content = str(value["content"] or "").strip()
        if not content or len(content) > 1000 or "\ufffd" in content or set(content) == {"?"}:
            raise PublishContractError("publish content failed length or Unicode validation")
        raw_tags = value["tags"]
        if not isinstance(raw_tags, list) or len(raw_tags) > 10:
            raise PublishContractError("publish tags must be a list with at most 10 items")
        tags = tuple(str(item or "").strip().lstrip("#") for item in raw_tags)
        if any(not item or len(item) > 30 or re.search(r"\s|#", item) for item in tags):
            raise PublishContractError("publish tag is empty, too long or contains whitespace")
        if len(tags) != len(set(tags)):
            raise PublishContractError("publish tags cannot contain duplicates")
        raw_paths = value["media_paths"]
        if not isinstance(raw_paths, list):
            raise PublishContractError("publish media_paths must be a list")
        if media_type == "image" and not 1 <= len(raw_paths) <= 9:
            raise PublishContractError("image publish requires 1-9 images")
        if media_type == "video" and len(raw_paths) != 1:
            raise PublishContractError("video publish requires exactly one video")
        relative_paths: list[str] = []
        hashes: list[str] = []
        for raw_path in raw_paths:
            relative, absolute = _project_media_path(project_root, raw_path)
            suffix = absolute.suffix.lower()
            allowed = IMAGE_EXTENSIONS if media_type == "image" else VIDEO_EXTENSIONS
            if suffix not in allowed:
                raise PublishContractError(f"unsupported {media_type} media extension: {suffix}")
            maximum = 20 * 1024 * 1024 if media_type == "image" else 2 * 1024 * 1024 * 1024
            if absolute.stat().st_size <= 0 or absolute.stat().st_size > maximum:
                raise PublishContractError(f"{media_type} media size is outside the product limit")
            relative_paths.append(relative)
            hashes.append(_file_sha256(absolute))
        if len(relative_paths) != len(set(relative_paths)):
            raise PublishContractError("publish media paths cannot contain duplicates")
        if value["visibility"] != "public":
            raise PublishContractError("V2 initial publish supports public visibility only")
        created_at = _timestamp("created_at", value["created_at"])
        payload = {
            "schema_version": 1,
            "account_id": _safe_id("account_id", value["account_id"]),
            "media_type": media_type,
            "title": title,
            "content": content,
            "tags": list(tags),
            "media_paths": relative_paths,
            "media_hashes": hashes,
            "visibility": "public",
            "created_at": created_at,
        }
        digest = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        plan_id = "publish_" + digest[:20]
        if "media_hashes" in value and value["media_hashes"] != hashes:
            raise PublishContractError("publish media hashes changed")
        if "plan_id" in value and value["plan_id"] != plan_id:
            raise PublishContractError("publish plan_id mismatch")
        if "content_hash" in value and value["content_hash"] != digest:
            raise PublishContractError("publish content hash mismatch")
        return cls(
            plan_id=plan_id,
            schema_version=1,
            account_id=payload["account_id"],
            media_type=media_type,
            title=title,
            content=content,
            tags=tags,
            media_paths=tuple(relative_paths),
            media_hashes=tuple(hashes),
            visibility="public",
            created_at=created_at,
            content_hash=digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "media_type": self.media_type,
            "title": self.title,
            "content": self.content,
            "tags": list(self.tags),
            "media_paths": list(self.media_paths),
            "media_hashes": list(self.media_hashes),
            "visibility": self.visibility,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class PublishApproval:
    approval_id: str
    schema_version: int
    plan_id: str
    plan_hash: str
    account_id: str
    approved_at: str

    @classmethod
    def create(
        cls,
        plan: PublishPlan,
        *,
        approved_at: str,
        confirmation: str,
    ) -> "PublishApproval":
        if confirmation != PUBLISH_APPROVAL_CONFIRMATION:
            raise PublishContractError("exact single-publish approval is required")
        moment = _timestamp("approved_at", approved_at)
        digest = sha256(
            f"{plan.account_id}|{plan.plan_id}|{plan.content_hash}|{moment}".encode("utf-8")
        ).hexdigest()
        return cls(
            approval_id="publish_approval_" + digest[:20],
            schema_version=1,
            plan_id=plan.plan_id,
            plan_hash=plan.content_hash,
            account_id=plan.account_id,
            approved_at=moment,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PublishApproval":
        if set(value) != set(cls.__dataclass_fields__):
            raise PublishContractError("publish approval fields are incomplete or unknown")
        approval = cls(**value)
        _safe_id("approval_id", approval.approval_id)
        _safe_id("plan_id", approval.plan_id)
        _safe_id("account_id", approval.account_id)
        _timestamp("approved_at", approval.approved_at)
        if approval.schema_version != 1 or re.fullmatch(r"[0-9a-f]{64}", approval.plan_hash) is None:
            raise PublishContractError("publish approval version or hash is invalid")
        return approval

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


class PublishRuntimeStore:
    def __init__(self, runtime_dir: Path) -> None:
        self.root = Path(runtime_dir) / "publish"

    def approval_path(self, plan_id: str) -> Path:
        return self.root / "approvals" / f"{_safe_id('plan_id', plan_id)}.json"

    def receipt_path(self, plan_id: str) -> Path:
        return self.root / "receipts" / f"{_safe_id('plan_id', plan_id)}.json"

    def save_approval(self, approval: PublishApproval) -> Path:
        path = self.approval_path(approval.plan_id)
        existing = read_json(path, default=None)
        if existing is not None and PublishApproval.from_dict(existing) != approval:
            raise PublishContractError("a different approval already exists for this publish plan")
        write_json_atomic(path, approval.to_dict())
        return path

    def load_approval(self, plan: PublishPlan) -> PublishApproval:
        value = read_json(self.approval_path(plan.plan_id), default=None)
        if not isinstance(value, dict):
            raise PublishContractError("publish approval is missing")
        approval = PublishApproval.from_dict(value)
        if (
            approval.plan_id != plan.plan_id
            or approval.plan_hash != plan.content_hash
            or approval.account_id != plan.account_id
        ):
            raise PublishContractError("publish approval does not match the exact plan")
        return approval

    def save_receipt(
        self,
        plan: PublishPlan,
        result: Mapping[str, Any],
        *,
        operation_receipt: Mapping[str, Any],
    ) -> Path:
        path = self.receipt_path(plan.plan_id)
        if path.exists():
            raise PublishContractError("publish plan already has a terminal receipt")
        payload = {
            "schema_version": 2,
            "plan_id": plan.plan_id,
            "plan_hash": plan.content_hash,
            "account_id": plan.account_id,
            "media_type": plan.media_type,
            "result": dict(result),
            "operation_receipt": dict(operation_receipt),
        }
        write_json_atomic(path, payload)
        return path


def _publish_request(
    plan: PublishPlan,
    approval: PublishApproval,
    *,
    checked_at: str,
    daily_limit: int,
    minimum_interval_seconds: int,
    budget_timezone: str,
) -> UnifiedActionRequest:
    approval_hash = sha256(
        json.dumps(approval.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return UnifiedActionRequest(
        schema_version=1,
        action_id=plan.plan_id,
        action_kind=f"publish_{plan.media_type}",
        account_id=plan.account_id,
        target_ref_hash=sha256(plan.plan_id.encode("utf-8")).hexdigest(),
        dedupe_key_hash=plan.content_hash,
        plan_hash=plan.content_hash,
        approval_ref=approval.approval_id,
        approval_hash=approval_hash,
        checked_at=checked_at,
        budget_timezone=budget_timezone,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        verification_method="visible_publish_terminal",
    )


def approve_publish_plan(
    *,
    project_root: Path,
    runtime_dir: Path,
    plan: PublishPlan,
    approved_at: str,
    confirmation: str,
    state: UnifiedPreflightState,
    client: RunAgentClient | None = None,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[PublishApproval, dict[str, Any]]:
    approval = PublishApproval.create(
        plan,
        approved_at=approved_at,
        confirmation=confirmation,
    )
    run_agent = client or RunAgentClient(project_root)
    identity = run_agent.assert_current_account_identity()
    operation = f"publish_{plan.media_type}_current"
    capability_audit = run_agent.capability_audit()
    allowed = {
        str(item.get("operation", ""))
        for item in capability_audit.get("allowed", [])
        if isinstance(item, dict)
    }
    if operation not in allowed:
        raise PublishContractError(
            "publish live capability is frozen until its exact single-action UAT passes"
        )
    request = _publish_request(
        plan,
        approval,
        checked_at=approved_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    evaluated_state = UnifiedPreflightState(
        platform_access_allowed=state.platform_access_allowed,
        login_ready=state.login_ready,
        account_identity_ready=(
            state.account_identity_ready and identity.get("verified") is True
        ),
        target_ready=state.target_ready,
        approval_ready=state.approval_ready,
        capability_ready=state.capability_ready,
        additional_blockers=state.additional_blockers,
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    try:
        decision = UnifiedActionPreflightStore(runtime_dir).evaluate(
            request,
            phase="authorize",
            state=evaluated_state,
        )
    except ActionPreflightError as exc:
        raise PublishContractError(str(exc)) from exc
    if not decision.allowed:
        raise PublishContractError(
            "publish unified preflight blocked: " + "; ".join(decision.blockers)
        )
    branch = f"publish_{plan.media_type}"
    lease = run_agent.authorize_bounded_write_uat(
        confirmation=BOUNDED_WRITE_UAT_CONFIRMATION,
        account_id=plan.account_id,
        session_id=plan.plan_id,
        note_id=plan.plan_id,
        plan_hash=plan.content_hash,
        branch=branch,
        max_actions=1,
    )
    try:
        PublishRuntimeStore(runtime_dir).save_approval(approval)
    except BaseException:
        run_agent.revoke_bounded_write_uat()
        raise
    return approval, lease


def execute_publish_plan(
    *,
    project_root: Path,
    runtime_dir: Path,
    plan: PublishPlan,
    executed_at: str,
    state: UnifiedPreflightState,
    client: RunAgentClient | None = None,
    daily_limit: int = 10,
    minimum_interval_seconds: int = 600,
    budget_timezone: str = "UTC",
) -> tuple[dict[str, Any], Path]:
    store = PublishRuntimeStore(runtime_dir)
    approval = store.load_approval(plan)
    if store.receipt_path(plan.plan_id).exists():
        raise PublishContractError("publish plan was already executed")
    run_agent = client or RunAgentClient(project_root)
    identity = run_agent.assert_current_account_identity()
    branch = f"publish_{plan.media_type}"
    run_agent.require_bounded_write_uat(
        session_id=plan.plan_id,
        note_id=plan.plan_id,
        plan_hash=plan.content_hash,
        branch=branch,
    )
    request = _publish_request(
        plan,
        approval,
        checked_at=executed_at,
        daily_limit=daily_limit,
        minimum_interval_seconds=minimum_interval_seconds,
        budget_timezone=budget_timezone,
    )
    evaluated_state = UnifiedPreflightState(
        platform_access_allowed=state.platform_access_allowed,
        login_ready=state.login_ready,
        account_identity_ready=(
            state.account_identity_ready and identity.get("verified") is True
        ),
        target_ready=state.target_ready,
        approval_ready=state.approval_ready,
        capability_ready=state.capability_ready,
        exact_lease_ready=True,
        additional_blockers=state.additional_blockers,
        runtime_mode=RuntimeMode.SCOPED_UAT,
        scoped_uat_authorized=True,
        scoped_uat_actions_remaining=1,
    )
    preflight = UnifiedActionPreflightStore(runtime_dir)
    try:
        decision = preflight.evaluate(request, phase="execute", state=evaluated_state)
    except ActionPreflightError as exc:
        raise PublishContractError(str(exc)) from exc
    if not decision.allowed:
        raise PublishContractError(
            "publish execution preflight blocked: " + "; ".join(decision.blockers)
        )
    absolute_paths = [str((Path(project_root) / item).resolve()) for item in plan.media_paths]
    try:
        if plan.media_type == "image":
            result = run_agent.publish_image_current(
                plan_hash=plan.content_hash,
                title=plan.title,
                content=plan.content,
                tags=list(plan.tags),
                image_paths=absolute_paths,
                media_hashes=list(plan.media_hashes),
            )
        else:
            result = run_agent.publish_video_current(
                plan_hash=plan.content_hash,
                title=plan.title,
                content=plan.content,
                tags=list(plan.tags),
                video_path=absolute_paths[0],
                media_hash=plan.media_hashes[0],
            )
        if result.get("actionDispatched") is False:
            preflight.record_result(
                request,
                status="not_dispatched",
                recorded_at=executed_at,
                reason_code=str(result.get("failureCode") or "publish_not_dispatched"),
            )
            raise PublishContractError("publish write was not dispatched")
        if result.get("verified") is not True or result.get("platform_actions_executed") != 1:
            preflight.record_result(
                request,
                status="unknown",
                recorded_at=executed_at,
                reason_code="publish_visible_verification_missing",
            )
            raise RunAgentError("publish result lacked exact visible verification")
        if result.get("planHash") != plan.content_hash:
            preflight.record_result(
                request,
                status="unknown",
                recorded_at=executed_at,
                reason_code="publish_plan_hash_mismatch",
            )
            raise RunAgentError("publish result plan hash mismatch")
    except BaseException:
        stop = read_json(Path(runtime_dir) / "comment_flow" / "STOP.json", default=None)
        if isinstance(stop, Mapping) and stop.get("requires_manual_reconciliation") is True:
            preflight.record_result(
                request,
                status="unknown",
                recorded_at=executed_at,
                reason_code="platform_write_unknown",
            )
        raise
    finally:
        run_agent.revoke_bounded_write_uat()
    evidence_hash = sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    operation_receipt = preflight.record_result(
        request,
        status="verified",
        recorded_at=executed_at,
        evidence_hash=evidence_hash,
    )
    path = store.save_receipt(
        plan,
        result,
        operation_receipt=operation_receipt,
    )
    return result, path
