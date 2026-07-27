"""Append-only query-funnel evidence and deterministic daily aggregation."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xhs_operations_core.storage import append_jsonl, read_jsonl

from .daily_review import QueryRunMetrics, ReviewError


METRIC_KINDS = {
    "search",
    "note_open",
    "candidate",
    "message",
    "approval",
    "stop",
    "exhausted",
}


class QueryMetricsStore:
    """Store counts and safe codes only; never note, comment, or message prose."""

    def __init__(self, runtime_dir: str | Path) -> None:
        self.path = Path(runtime_dir) / "reporting" / "query_metric_events.jsonl"

    @staticmethod
    def _safe_id(name: str, value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}", value
        ) is None:
            raise ReviewError(f"query metric {name} is invalid")
        return value

    @staticmethod
    def _moment(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ReviewError("query metric occurred_at must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReviewError("query metric occurred_at must include timezone")
        return parsed

    def record(
        self,
        *,
        campaign_id: str,
        account_id: str,
        run_id: str,
        query_id: str,
        kind: str,
        ref: str,
        occurred_at: str,
        count: int = 1,
        level: str = "",
        outcome: str = "",
        reason_code: str = "",
    ) -> str:
        for name, value in (
            ("campaign_id", campaign_id),
            ("account_id", account_id),
            ("run_id", run_id),
            ("query_id", query_id),
            ("ref", ref),
        ):
            self._safe_id(name, value)
        if kind not in METRIC_KINDS:
            raise ReviewError("query metric kind is unsupported")
        if type(count) is not int or count < 0:
            raise ReviewError("query metric count must be a non-negative integer")
        if level and level not in {"A", "B", "C", "X"}:
            raise ReviewError("query metric candidate level is invalid")
        if outcome and outcome not in {"valid", "blocked"}:
            raise ReviewError("query metric message outcome is invalid")
        if reason_code and re.fullmatch(r"[a-z0-9][a-z0-9_:.-]{0,127}", reason_code) is None:
            raise ReviewError("query metric reason code is invalid")
        self._moment(occurred_at)
        identity = {
            "campaign_id": campaign_id,
            "account_id": account_id,
            "run_id": run_id,
            "query_id": query_id,
            "kind": kind,
            "ref": ref,
            "count": count,
            "level": level,
            "outcome": outcome,
            "reason_code": reason_code,
        }
        event_id = "metric_" + sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:20]
        existing = [row for row in read_jsonl(self.path) if row.get("event_id") == event_id]
        if existing:
            if existing[-1].get("identity") != identity:
                raise ReviewError("query metric event ID collision")
            return event_id
        append_jsonl(self.path, {
            "schema_version": 1,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "identity": identity,
            "stores_raw_text": False,
        })
        return event_id

    def aggregate_daily(
        self,
        *,
        campaign_id: str,
        account_id: str,
        plan_date: str,
        timezone_name: str,
        query_ids: set[str] | None = None,
    ) -> list[QueryRunMetrics]:
        try:
            zone = ZoneInfo(timezone_name)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ReviewError("query metric timezone is not recognized") from exc
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in read_jsonl(self.path):
            identity = row.get("identity")
            if (
                row.get("schema_version") != 1
                or row.get("stores_raw_text") is not False
                or not isinstance(identity, dict)
            ):
                raise ReviewError("query metric event store is corrupt")
            if identity.get("campaign_id") != campaign_id or identity.get("account_id") != account_id:
                continue
            moment = self._moment(str(row.get("occurred_at", ""))).astimezone(zone)
            if moment.date().isoformat() != plan_date:
                continue
            query_id = str(identity.get("query_id", ""))
            if query_ids is not None and query_id not in query_ids:
                continue
            key = (str(identity.get("run_id", "")), query_id)
            groups.setdefault(key, []).append(identity)

        results: list[QueryRunMetrics] = []
        for (run_id, query_id), events in sorted(groups.items()):
            totals = {
                "searched_notes": 0,
                "opened_notes": 0,
                "visible_comments": 0,
                "candidates_a": 0,
                "candidates_b": 0,
                "candidates_c": 0,
                "candidates_x": 0,
                "messages_valid": 0,
                "messages_blocked": 0,
                "human_approved": 0,
            }
            exhausted = False
            stop_reasons: set[str] = set()
            for event in events:
                kind = event["kind"]
                count = int(event["count"])
                if kind == "search":
                    totals["searched_notes"] += count
                elif kind == "note_open":
                    totals["opened_notes"] += 1
                    totals["visible_comments"] += count
                elif kind == "candidate":
                    totals[f"candidates_{str(event['level']).lower()}"] += count
                elif kind == "message":
                    totals[f"messages_{event['outcome']}"] += count
                elif kind == "approval":
                    totals["human_approved"] += count
                elif kind == "stop" and event["reason_code"]:
                    stop_reasons.add(str(event["reason_code"]))
                elif kind == "exhausted":
                    exhausted = True
            results.append(QueryRunMetrics.from_dict({
                "run_id": run_id,
                "query_id": query_id,
                **totals,
                "exhausted": exhausted,
                "stop_reasons": sorted(stop_reasons),
            }))
        return results
