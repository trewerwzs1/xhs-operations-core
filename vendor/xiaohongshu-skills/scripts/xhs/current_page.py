"""Fail-closed identity and risk checks for actions on the already open note."""

from __future__ import annotations

from typing import Any


def require_current_note(page: Any, expected_note_id: str) -> dict[str, Any]:
    if not expected_note_id:
        raise RuntimeError("expected note id is required")
    context = page.get_page_context()
    if not isinstance(context, dict):
        raise RuntimeError("当前页面上下文无效")
    risks = context.get("riskSignals", [])
    if not isinstance(risks, list):
        raise RuntimeError("当前页面风险状态无效")
    if risks:
        raise RuntimeError("页面风险提示: " + ",".join(str(item) for item in risks))
    if context.get("pageType") != "note_detail":
        raise RuntimeError("当前页面不是笔记详情页")
    if str(context.get("noteId", "")) != expected_note_id:
        raise RuntimeError("当前页面笔记与计划目标不匹配")
    return context
