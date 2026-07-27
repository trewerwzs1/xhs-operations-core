"""Campaign, authorized-fact, and lifecycle contracts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from xhs_operations_core.contracts import ActionType


class CampaignContractError(ValueError):
    """Raised when a Campaign or AuthorizedFact violates its contract."""


class ActivityType(str):
    """Validated open activity slug.

    Campaign used to expose a closed industry enum here.  The core only needs
    a stable, serializable classification label, so arbitrary domain slugs are
    accepted as long as they follow the public identifier contract.
    """

    _PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+){0,7}")

    def __new__(cls, value: object) -> "ActivityType":
        if isinstance(value, ActivityType):
            return value
        if not isinstance(value, str):
            raise ValueError("activity_type must be text")
        normalized = value.strip().lower()
        if len(normalized) > 64 or cls._PATTERN.fullmatch(normalized) is None:
            raise ValueError(
                "activity_type must be a lowercase slug of at most 64 characters "
                "with letters, numbers, and underscores"
            )
        return str.__new__(cls, normalized)

    @property
    def value(self) -> str:
        return str(self)


# Neutral sentinels are the only built-in classifications. All real domain
# labels arrive as StrategyPack data rather than Python constants.
ActivityType.OTHER_ACTIVITY = ActivityType("other_activity")  # type: ignore[attr-defined]
ActivityType.NOT_AN_ACTIVITY = ActivityType("not_an_activity")  # type: ignore[attr-defined]


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ANALYZED = "analyzed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    EXPIRED = "expired"
    BLOCKED = "blocked"


class FactKind(str, Enum):
    ACTIVITY_TITLE = "activity_title"
    CITY = "city"
    VENUE = "venue"
    START_AT = "start_at"
    END_AT = "end_at"
    REGISTRATION_DEADLINE = "registration_deadline"
    PRICE = "price"
    CAPACITY = "capacity"
    REMAINING_CAPACITY = "remaining_capacity"
    ORGANIZER = "organizer"
    REGISTRATION_METHOD = "registration_method"
    ITINERARY = "itinerary"
    CORE_EXPERIENCE = "core_experience"
    CONVERSION_ACTION = "conversion_action"
    CUSTOM = "custom"


class FactSourceType(str, Enum):
    NOTE_SNAPSHOT = "note_snapshot"
    USER_PROVIDED = "user_provided"
    APPROVED_DOCUMENT = "approved_document"
    BUSINESS_SYSTEM = "business_system"


class StatusActor(str, Enum):
    USER = "user"
    SYSTEM = "system"


DYNAMIC_FACT_KINDS = {
    FactKind.VENUE,
    FactKind.START_AT,
    FactKind.END_AT,
    FactKind.REGISTRATION_DEADLINE,
    FactKind.PRICE,
    FactKind.CAPACITY,
    FactKind.REMAINING_CAPACITY,
    FactKind.REGISTRATION_METHOD,
    FactKind.ITINERARY,
}

TIMESTAMP_FACT_KINDS = {
    FactKind.START_AT,
    FactKind.END_AT,
    FactKind.REGISTRATION_DEADLINE,
}

TEXT_FACT_KINDS = {
    FactKind.ACTIVITY_TITLE,
    FactKind.CITY,
    FactKind.VENUE,
    FactKind.ORGANIZER,
    FactKind.REGISTRATION_METHOD,
    FactKind.CORE_EXPERIENCE,
    FactKind.CONVERSION_ACTION,
}


ALLOWED_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset({CampaignStatus.ANALYZED, CampaignStatus.BLOCKED}),
    CampaignStatus.ANALYZED: frozenset(
        {CampaignStatus.AWAITING_CONFIRMATION, CampaignStatus.BLOCKED}
    ),
    CampaignStatus.AWAITING_CONFIRMATION: frozenset(
        {CampaignStatus.READY, CampaignStatus.BLOCKED}
    ),
    CampaignStatus.READY: frozenset(
        {CampaignStatus.ACTIVE, CampaignStatus.PAUSED, CampaignStatus.BLOCKED}
    ),
    CampaignStatus.ACTIVE: frozenset(
        {
            CampaignStatus.PAUSED,
            CampaignStatus.COMPLETED,
            CampaignStatus.EXPIRED,
            CampaignStatus.BLOCKED,
        }
    ),
    CampaignStatus.PAUSED: frozenset(
        {
            CampaignStatus.ACTIVE,
            CampaignStatus.COMPLETED,
            CampaignStatus.EXPIRED,
            CampaignStatus.BLOCKED,
        }
    ),
    CampaignStatus.BLOCKED: frozenset({CampaignStatus.ANALYZED}),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.EXPIRED: frozenset(),
}


def _enum(enum_type: type[Enum], value: Any, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise CampaignContractError(f"{name} must be one of: {allowed}") from exc


def _non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CampaignContractError(f"{name} must be a non-empty string")


def _safe_id(name: str, value: str) -> None:
    _non_empty(name, value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None:
        raise CampaignContractError(
            f"{name} must use 1-128 letters, numbers, underscores, or hyphens"
        )


def _timestamp(name: str, value: str) -> datetime:
    _non_empty(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(name: str, value: str | None) -> datetime | None:
    return None if value is None else _timestamp(name, value)


@dataclass(frozen=True)
class AuthorizedFact:
    fact_id: str
    kind: FactKind
    value: Any
    source_type: FactSourceType
    source_ref: str
    verified_at: str
    approved_for_public: bool
    dynamic: bool
    valid_until: str | None = None
    custom_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(FactKind, self.kind, "fact.kind"))
        object.__setattr__(
            self, "source_type", _enum(FactSourceType, self.source_type, "fact.source_type")
        )
        _safe_id("fact_id", self.fact_id)
        _non_empty("fact.source_ref", self.source_ref)
        if type(self.approved_for_public) is not bool:
            raise CampaignContractError("fact.approved_for_public must be a boolean")
        if type(self.dynamic) is not bool:
            raise CampaignContractError("fact.dynamic must be a boolean")
        verified = _timestamp("fact.verified_at", self.verified_at)
        valid_until = _optional_timestamp("fact.valid_until", self.valid_until)
        if valid_until is not None and valid_until <= verified:
            raise CampaignContractError("fact.valid_until must be later than verified_at")
        if self.kind is FactKind.CUSTOM:
            _non_empty("fact.custom_key", self.custom_key or "")
        elif self.custom_key is not None:
            raise CampaignContractError("custom_key is only valid for custom facts")
        if self.kind in DYNAMIC_FACT_KINDS and not self.dynamic:
            raise CampaignContractError(f"{self.kind.value} must be marked dynamic")
        if self.kind not in DYNAMIC_FACT_KINDS and self.dynamic:
            raise CampaignContractError(f"{self.kind.value} cannot be marked dynamic")
        if self.value is None or self.value == "" or self.value == [] or self.value == {}:
            raise CampaignContractError("fact.value cannot be empty")
        if self.kind in TIMESTAMP_FACT_KINDS:
            if not isinstance(self.value, str):
                raise CampaignContractError(f"{self.kind.value} value must be an ISO timestamp")
            _timestamp(f"fact.value:{self.kind.value}", self.value)
        if self.kind in TEXT_FACT_KINDS:
            if not isinstance(self.value, str) or not self.value.strip():
                raise CampaignContractError(f"{self.kind.value} value must be non-empty text")
        if self.kind in {FactKind.CAPACITY, FactKind.REMAINING_CAPACITY}:
            if type(self.value) is not int or self.value < 0:
                raise CampaignContractError(
                    f"{self.kind.value} value must be a non-negative integer"
                )
        if self.kind is FactKind.PRICE:
            valid_price = (
                isinstance(self.value, str) and bool(self.value.strip())
            ) or (
                type(self.value) in {int, float} and self.value >= 0
            )
            if not valid_price:
                raise CampaignContractError(
                    "price value must be non-empty text or a non-negative number"
                )
        try:
            json.dumps(self.value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CampaignContractError("fact.value must be JSON serializable") from exc

    def is_valid_at(self, at: str) -> bool:
        moment = _timestamp("at", at)
        valid_until = _optional_timestamp("fact.valid_until", self.valid_until)
        return valid_until is None or moment <= valid_until

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["source_type"] = self.source_type.value
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AuthorizedFact":
        allowed = {
            "fact_id",
            "kind",
            "value",
            "source_type",
            "source_ref",
            "verified_at",
            "approved_for_public",
            "dynamic",
            "valid_until",
            "custom_key",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise CampaignContractError(
                f"unknown AuthorizedFact fields: {', '.join(sorted(unknown))}"
            )
        try:
            return cls(**dict(payload))
        except TypeError as exc:
            raise CampaignContractError(f"invalid AuthorizedFact: {exc}") from exc


@dataclass(frozen=True)
class StatusTransition:
    from_status: CampaignStatus
    to_status: CampaignStatus
    changed_at: str
    actor: StatusActor
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "from_status", _enum(CampaignStatus, self.from_status, "from_status")
        )
        object.__setattr__(self, "to_status", _enum(CampaignStatus, self.to_status, "to_status"))
        object.__setattr__(self, "actor", _enum(StatusActor, self.actor, "actor"))
        _timestamp("changed_at", self.changed_at)
        _non_empty("transition.reason", self.reason)
        if self.to_status not in ALLOWED_TRANSITIONS[self.from_status]:
            raise CampaignContractError(
                f"illegal Campaign transition: {self.from_status.value} -> {self.to_status.value}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "changed_at": self.changed_at,
            "actor": self.actor.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StatusTransition":
        try:
            return cls(**dict(payload))
        except TypeError as exc:
            raise CampaignContractError(f"invalid StatusTransition: {exc}") from exc


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    account_id: str
    source_note_id: str
    source_note_ref: str
    source_note_hash: str
    activity_type: ActivityType
    classification_confidence: float
    status: CampaignStatus
    created_at: str
    updated_at: str
    active_from: str
    active_until: str
    conversion_goal: str
    allowed_actions: tuple[ActionType, ...]
    required_fact_kinds: tuple[FactKind, ...]
    facts: tuple[AuthorizedFact, ...]
    missing_fact_kinds: tuple[FactKind, ...] = ()
    prohibited_claims: tuple[str, ...] = ()
    transitions: tuple[StatusTransition, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "activity_type", ActivityType(self.activity_type))
        except (TypeError, ValueError) as exc:
            raise CampaignContractError(str(exc)) from exc
        object.__setattr__(self, "status", _enum(CampaignStatus, self.status, "status"))
        object.__setattr__(
            self,
            "allowed_actions",
            tuple(_enum(ActionType, item, "allowed_actions") for item in self.allowed_actions),
        )
        object.__setattr__(
            self,
            "required_fact_kinds",
            tuple(_enum(FactKind, item, "required_fact_kinds") for item in self.required_fact_kinds),
        )
        object.__setattr__(
            self,
            "missing_fact_kinds",
            tuple(_enum(FactKind, item, "missing_fact_kinds") for item in self.missing_fact_kinds),
        )

        for name in (
            "campaign_id",
            "account_id",
            "source_note_id",
        ):
            _safe_id(name, getattr(self, name))
        for name in ("source_note_ref", "source_note_hash", "conversion_goal"):
            _non_empty(name, getattr(self, name))
        if len(self.source_note_hash) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in self.source_note_hash
        ):
            raise CampaignContractError("source_note_hash must be a 64-character SHA-256 hex")
        if type(self.classification_confidence) not in {int, float}:
            raise CampaignContractError("classification_confidence must be numeric")
        if not 0.0 <= self.classification_confidence <= 1.0:
            raise CampaignContractError("classification_confidence must be between 0 and 1")

        created = _timestamp("created_at", self.created_at)
        updated = _timestamp("updated_at", self.updated_at)
        active_from = _timestamp("active_from", self.active_from)
        active_until = _timestamp("active_until", self.active_until)
        if updated < created:
            raise CampaignContractError("updated_at cannot be earlier than created_at")
        if active_until <= active_from:
            raise CampaignContractError("active_until must be later than active_from")

        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise CampaignContractError("allowed_actions cannot contain duplicates")
        if len(set(self.required_fact_kinds)) != len(self.required_fact_kinds):
            raise CampaignContractError("required_fact_kinds cannot contain duplicates")
        if len(set(self.missing_fact_kinds)) != len(self.missing_fact_kinds):
            raise CampaignContractError("missing_fact_kinds cannot contain duplicates")
        if FactKind.CUSTOM in self.required_fact_kinds or FactKind.CUSTOM in self.missing_fact_kinds:
            raise CampaignContractError(
                "custom facts cannot be declared required or missing by generic kind"
            )
        if set(self.required_fact_kinds) & set(self.missing_fact_kinds) != set(
            self.missing_fact_kinds
        ):
            raise CampaignContractError("missing facts must be declared required facts")

        if any(not isinstance(fact, AuthorizedFact) for fact in self.facts):
            raise CampaignContractError("facts must contain AuthorizedFact values")
        fact_ids = [fact.fact_id for fact in self.facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise CampaignContractError("fact_id values must be unique")
        standard_kinds = [fact.kind for fact in self.facts if fact.kind is not FactKind.CUSTOM]
        if len(set(standard_kinds)) != len(standard_kinds):
            raise CampaignContractError("standard fact kinds must be unique")
        custom_keys = [fact.custom_key for fact in self.facts if fact.kind is FactKind.CUSTOM]
        if len(set(custom_keys)) != len(custom_keys):
            raise CampaignContractError("custom fact keys must be unique")

        for fact in self.facts:
            if _timestamp("fact.verified_at", fact.verified_at) > updated:
                raise CampaignContractError("fact verification cannot be newer than Campaign")

        by_kind = {fact.kind: fact for fact in self.facts}
        if FactKind.CAPACITY in by_kind and FactKind.REMAINING_CAPACITY in by_kind:
            if by_kind[FactKind.REMAINING_CAPACITY].value > by_kind[FactKind.CAPACITY].value:
                raise CampaignContractError("remaining_capacity cannot exceed capacity")

        present_kinds = {fact.kind for fact in self.facts}
        if present_kinds & set(self.missing_fact_kinds):
            raise CampaignContractError("a fact cannot be both present and missing")

        ready_statuses = {CampaignStatus.READY, CampaignStatus.ACTIVE}
        if self.status in ready_statuses:
            absent = set(self.required_fact_kinds) - present_kinds
            if absent:
                names = ", ".join(sorted(item.value for item in absent))
                raise CampaignContractError(f"ready Campaign missing required facts: {names}")
            unusable = [
                fact.kind.value
                for fact in self.facts
                if fact.kind in self.required_fact_kinds
                and (not fact.approved_for_public or not fact.is_valid_at(self.updated_at))
            ]
            if unusable:
                raise CampaignContractError(
                    f"ready Campaign has unusable required facts: {', '.join(sorted(unusable))}"
                )
            if not self.allowed_actions:
                raise CampaignContractError("ready Campaign requires at least one allowed action")

        if self.activity_type == ActivityType.NOT_AN_ACTIVITY and self.status not in {
            CampaignStatus.DRAFT,
            CampaignStatus.ANALYZED,
            CampaignStatus.BLOCKED,
        }:
            raise CampaignContractError("not_an_activity cannot become ready or active")

        if self.status is CampaignStatus.ACTIVE and not (active_from <= updated <= active_until):
            raise CampaignContractError("active Campaign must be within its active date range")
        if self.status is CampaignStatus.READY and updated > active_until:
            raise CampaignContractError("ready Campaign cannot be past active_until")
        if self.status is CampaignStatus.EXPIRED and updated <= active_until:
            raise CampaignContractError("expired Campaign must be past active_until")

        previous_to: CampaignStatus | None = None
        previous_time: datetime | None = None
        if any(not isinstance(item, StatusTransition) for item in self.transitions):
            raise CampaignContractError("transitions must contain StatusTransition values")
        for transition in self.transitions:
            changed = _timestamp("transition.changed_at", transition.changed_at)
            if previous_to is not None and transition.from_status is not previous_to:
                raise CampaignContractError("Campaign transition history is disconnected")
            if previous_time is not None and changed < previous_time:
                raise CampaignContractError("Campaign transition history is not chronological")
            if changed < created:
                raise CampaignContractError("Campaign transition cannot predate created_at")
            previous_to = transition.to_status
            previous_time = changed
        if self.transitions and self.transitions[-1].to_status is not self.status:
            raise CampaignContractError("last transition must end at Campaign.status")
        if previous_time is not None and previous_time > updated:
            raise CampaignContractError("transition cannot be newer than updated_at")

        for claim in self.prohibited_claims:
            _non_empty("prohibited_claim", claim)
        if not isinstance(self.metadata, Mapping):
            raise CampaignContractError("metadata must be a mapping")
        try:
            json.dumps(self.metadata, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise CampaignContractError("metadata must be JSON serializable") from exc

    def transition(
        self,
        to_status: CampaignStatus,
        *,
        changed_at: str,
        actor: StatusActor,
        reason: str,
    ) -> "Campaign":
        transition = StatusTransition(self.status, to_status, changed_at, actor, reason)
        changed = _timestamp("changed_at", changed_at)
        if changed < _timestamp("updated_at", self.updated_at):
            raise CampaignContractError("transition cannot move updated_at backwards")
        return replace(
            self,
            status=transition.to_status,
            updated_at=changed_at,
            transitions=(*self.transitions, transition),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "account_id": self.account_id,
            "source_note_id": self.source_note_id,
            "source_note_ref": self.source_note_ref,
            "source_note_hash": self.source_note_hash,
            "activity_type": self.activity_type.value,
            "classification_confidence": self.classification_confidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_from": self.active_from,
            "active_until": self.active_until,
            "conversion_goal": self.conversion_goal,
            "allowed_actions": [item.value for item in self.allowed_actions],
            "required_fact_kinds": [item.value for item in self.required_fact_kinds],
            "facts": [fact.to_dict() for fact in self.facts],
            "missing_fact_kinds": [item.value for item in self.missing_fact_kinds],
            "prohibited_claims": list(self.prohibited_claims),
            "transitions": [item.to_dict() for item in self.transitions],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Campaign":
        allowed = {
            "campaign_id",
            "account_id",
            "source_note_id",
            "source_note_ref",
            "source_note_hash",
            "activity_type",
            "classification_confidence",
            "status",
            "created_at",
            "updated_at",
            "active_from",
            "active_until",
            "conversion_goal",
            "allowed_actions",
            "required_fact_kinds",
            "facts",
            "missing_fact_kinds",
            "prohibited_claims",
            "transitions",
            "metadata",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise CampaignContractError(
                f"unknown Campaign fields: {', '.join(sorted(unknown))}"
            )
        try:
            values = dict(payload)
            values["allowed_actions"] = tuple(values["allowed_actions"])
            values["required_fact_kinds"] = tuple(values["required_fact_kinds"])
            values["facts"] = tuple(AuthorizedFact.from_dict(item) for item in values["facts"])
            values["missing_fact_kinds"] = tuple(values.get("missing_fact_kinds", ()))
            values["prohibited_claims"] = tuple(values.get("prohibited_claims", ()))
            values["transitions"] = tuple(
                StatusTransition.from_dict(item) for item in values.get("transitions", ())
            )
            return cls(**values)
        except (KeyError, TypeError) as exc:
            raise CampaignContractError(f"invalid Campaign: {exc}") from exc
