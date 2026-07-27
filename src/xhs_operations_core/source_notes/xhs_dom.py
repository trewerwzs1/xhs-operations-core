"""Pure parsers for visible Xiaohongshu source-note evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from .latest import LatestNoteContractError


@dataclass(frozen=True)
class ParsedPublishedAt:
    value: str
    precision: str
    source_text: str


def parse_visible_published_at(text: str, *, captured_at: str) -> ParsedPublishedAt:
    source = " ".join(str(text).split()).strip()
    if not source:
        raise LatestNoteContractError("visible published text is empty")
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LatestNoteContractError("captured_at must be ISO-8601") from exc
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise LatestNoteContractError("captured_at must include a timezone")

    relative = re.search(r"(\d+)\s*(分钟|小时|天)前", source)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
        }[unit]
        value = captured - delta
        precision = "day" if unit == "天" else "minute"
        if precision == "day":
            value = value.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            value = value.replace(second=0, microsecond=0)
        return ParsedPublishedAt(value.isoformat(), precision, source)

    today = re.search(r"今天\s*(\d{1,2}):(\d{2})", source)
    if today:
        value = captured.replace(
            hour=int(today.group(1)), minute=int(today.group(2)), second=0, microsecond=0
        )
        return ParsedPublishedAt(value.isoformat(), "minute", source)

    yesterday = re.search(r"昨天\s*(\d{1,2}):(\d{2})", source)
    if yesterday:
        value = (captured - timedelta(days=1)).replace(
            hour=int(yesterday.group(1)),
            minute=int(yesterday.group(2)),
            second=0,
            microsecond=0,
        )
        return ParsedPublishedAt(value.isoformat(), "minute", source)

    full_date = re.search(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", source)
    if full_date:
        value = captured.replace(
            year=int(full_date.group(1)),
            month=int(full_date.group(2)),
            day=int(full_date.group(3)),
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return ParsedPublishedAt(value.isoformat(), "day", source)

    short_date = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)", source)
    if short_date:
        month, day = int(short_date.group(1)), int(short_date.group(2))
        try:
            value = captured.replace(
                month=month, day=day, hour=0, minute=0, second=0, microsecond=0
            )
        except ValueError as exc:
            raise LatestNoteContractError("visible published date is invalid") from exc
        if value.date() > captured.date():
            value = value.replace(year=value.year - 1)
        return ParsedPublishedAt(value.isoformat(), "day", source)

    raise LatestNoteContractError("visible published text format is unsupported")

