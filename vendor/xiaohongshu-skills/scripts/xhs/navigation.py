"""Verified visible navigation for the supported Tonyredbook page graph."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .user_profile import _extract_user_profile_data
from .comment import _find_and_scroll_to_comment
from .current_page import require_current_note


PROFILE_PATH_RE = re.compile(r"^/user/profile/([A-Za-z0-9_-]+)(?:/|$)")


def _healthy(context: object) -> bool:
    return (
        isinstance(context, dict)
        and isinstance(context.get("riskSignals"), list)
        and not context["riskSignals"]
    )


def _wait_for_profile(page, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        context = page.get_page_context()
        if isinstance(context, dict):
            latest = context
            match = PROFILE_PATH_RE.match(str(context.get("pathname") or ""))
            if match and _healthy(context):
                return {**context, "profileId": match.group(1)}
            if context.get("riskSignals"):
                raise RuntimeError("risk signal detected while opening own profile")
        time.sleep(0.3)
    raise RuntimeError(
        "own-profile navigation did not reach a verified profile page: "
        + json.dumps(latest, ensure_ascii=False, sort_keys=True)
    )


def _visible_own_profile_link(page) -> dict[str, object]:
    """Find the visible sidebar link whose rendered label is exactly 我."""
    value = page.evaluate(
        r"""
        (() => {
          const selector = 'a[href*="/user/profile/"]';
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((node) => {
            const text = String(node.innerText || node.textContent || '')
              .replace(/\\s+/g, '')
              .trim();
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            const visible = rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
            return visible && text === '我';
          });
          const match = index >= 0
            ? String(nodes[index].getAttribute('href') || nodes[index].href || '').match(/\/user\/profile\/([A-Za-z0-9_-]+)/)
            : null;
          return index >= 0 && match
            ? { ok: true, selector, index, profileId: match[1] }
            : { ok: false, selector, index: -1 };
        })()
        """
    )
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("visible own-profile sidebar link was not found")
    return value


def read_current_account_identity(page) -> dict[str, object]:
    """Read the visible exact 我 sidebar link without navigating."""
    context = page.get_page_context()
    if not _healthy(context):
        raise RuntimeError("current account identity requires a healthy XHS page")
    target = _visible_own_profile_link(page)
    platform_user_id = str(target.get("profileId") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", platform_user_id) is None:
        raise RuntimeError("visible own-account identity is invalid")
    return {
        "platformUserId": platform_user_id,
        "pageType": context.get("pageType"),
        "pathname": str(context.get("pathname") or ""),
        "boundTabId": context.get("boundTabId"),
        "riskSignals": [],
        "verified": True,
        "platform_actions_executed": 0,
    }


def _stable_profile_user_id(page, *, path_profile_id: str) -> str:
    profile = _extract_user_profile_data(page)
    author_ids = {
        item.note_card.user.user_id
        for item in profile.feeds
        if item.note_card.user.user_id
    }
    if len(author_ids) != 1:
        raise RuntimeError("own-profile identity is missing or ambiguous")
    platform_user_id = next(iter(author_ids))
    if path_profile_id and path_profile_id != platform_user_id:
        raise RuntimeError("own-profile URL and visible note owner identity differ")
    return platform_user_id


def open_own_profile(page) -> dict[str, object]:
    """Open the logged-in account profile through one visible sidebar click.

    The operation never navigates by a guessed direct URL.  It verifies the
    profile pathname and the stable owner ID extracted from visible profile
    notes before returning.
    """
    before = page.get_page_context()
    if not _healthy(before):
        raise RuntimeError("own-profile navigation requires a healthy XHS page")
    initial_path = str(before.get("pathname") or "")
    initial_match = PROFILE_PATH_RE.match(initial_path)
    clicked = False
    returned_via_history = False
    if initial_match is None:
        if before.get("pageType") == "note_detail":
            # A Xiaohongshu note detail can be rendered as a layer over its
            # source profile.  The visible sidebar control may accept a pointer
            # event without dismissing that layer.  Use one audited history
            # return and require it to land on a healthy profile; never guess a
            # profile URL or fall back after an ambiguous return.
            context = page.return_to_profile("")
            if (
                not _healthy(context)
                or context.get("pageType") != "profile"
                or PROFILE_PATH_RE.match(str(context.get("pathname") or "")) is None
            ):
                raise RuntimeError("note history did not return to a verified profile page")
            returned_via_history = True
        else:
            target = _visible_own_profile_link(page)
            selector = str(target["selector"])
            index = int(target["index"])
            # _visible_own_profile_link already proves the exact sidebar control is
            # inside the viewport and unobscured enough for the audited pointer
            # click.  A second semantic-scroll here is redundant, adds another
            # 10-15 second action delay, and increases the chance that a responsive
            # sidebar moves before the verified click.
            page.click_nth_element(selector, index)
            clicked = True
            context = _wait_for_profile(page)
    else:
        context = _wait_for_profile(page)
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    page.simulate_reading_mouse(10_000)
    platform_user_id = _stable_profile_user_id(
        page, path_profile_id=str(context.get("profileId") or "")
    )
    after = page.get_page_context()
    if not _healthy(after) or PROFILE_PATH_RE.match(str(after.get("pathname") or "")) is None:
        raise RuntimeError("own-profile page changed during identity verification")
    return {
        "pageType": "own_profile",
        "pathname": str(after.get("pathname") or ""),
        "profileId": str(context.get("profileId") or ""),
        "platformUserId": platform_user_id,
        "boundTabId": after.get("boundTabId"),
        "riskSignals": [],
        "clicked": clicked,
        "returnedViaHistory": returned_via_history,
        "readingDwellSeconds": 10,
        "verified": True,
        "platform_actions_executed": 0,
    }


def open_commenter_profile(
    page, *, feed_id: str, comment_id: str, expected_user_id: str
) -> dict[str, object]:
    """Open the exact visible comment author and verify the destination ID."""
    for name, value in (
        ("feed_id", feed_id),
        ("comment_id", comment_id),
        ("expected_user_id", expected_user_id),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError(f"{name} contains unsupported characters")
    require_current_note(page, feed_id)
    before = page.get_page_context()
    if not _healthy(before):
        raise RuntimeError("commenter-profile navigation requires a healthy note page")
    if not _find_and_scroll_to_comment(page, comment_id, ""):
        raise RuntimeError("exact comment target is not visible")
    selector = (
        f'#comment-{comment_id} '
        f'a[href*="/user/profile/{expected_user_id}"]'
    )
    target = page.evaluate(
        rf"""
        (() => {{
          const selector = {json.dumps(selector)};
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            const match = String(node.getAttribute('href') || node.href || '')
              .match(/\/user\/profile\/([A-Za-z0-9_-]+)/);
            return match?.[1] === {json.dumps(expected_user_id)}
              && rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
          }});
          return index >= 0 ? {{ ok: true, index }} : {{ ok: false, index: -1 }};
        }})()
        """
    )
    if not isinstance(target, dict) or target.get("ok") is not True:
        raise RuntimeError("exact visible commenter profile link was not found")
    page.scroll_nth_element_into_view(selector, int(target["index"]))
    page.click_nth_element(selector, int(target["index"]))
    context = _wait_for_profile(page)
    if str(context.get("profileId") or "") != expected_user_id:
        raise RuntimeError("opened profile identity differs from exact comment author")
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    page.simulate_reading_mouse(10_000)
    return {
        "pageType": "other_user_profile",
        "pathname": str(context.get("pathname") or ""),
        "profileId": expected_user_id,
        "sourceNoteId": feed_id,
        "sourceCommentId": comment_id,
        "boundTabId": context.get("boundTabId"),
        "riskSignals": [],
        "readingDwellSeconds": 10,
        "verified": True,
        "platform_actions_executed": 0,
    }


def _wait_for_dm_conversation(page, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        context = page.get_page_context()
        if isinstance(context, dict):
            latest = context
            if context.get("riskSignals"):
                raise RuntimeError("risk signal detected while opening DM conversation")
            if (
                context.get("pageType") == "dm_conversation"
                and context.get("messageEditorVisible") is True
                and _healthy(context)
            ):
                return context
        time.sleep(0.3)
    raise RuntimeError(
        "DM navigation did not reach a verified visible conversation editor: "
        + json.dumps(latest, ensure_ascii=False, sort_keys=True)
    )


def open_dm_conversation(page) -> dict[str, object]:
    """Open the visible exact 私信 control on the current verified profile."""
    before = page.get_page_context()
    profile_id = str(before.get("profileId") or "") if isinstance(before, dict) else ""
    if (
        not _healthy(before)
        or before.get("pageType") != "profile"
        or re.fullmatch(r"[A-Za-z0-9_-]+", profile_id) is None
    ):
        raise RuntimeError("DM navigation requires a healthy verified user profile")
    target = page.evaluate(
        """
        (() => {
          const selector = 'button,a';
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((node) => {
            const text = String(node.innerText || node.textContent || '')
              .replace(/\\s+/g, '')
              .trim();
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return text === '私信'
              && rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
          });
          return index >= 0 ? { ok: true, selector, index } : { ok: false, index: -1 };
        })()
        """
    )
    if not isinstance(target, dict) or target.get("ok") is not True:
        raise RuntimeError("visible exact 私信 control was not found on current profile")
    selector = str(target["selector"])
    index = int(target["index"])
    page.scroll_nth_element_into_view(selector, index)
    page.click_nth_element(selector, index)
    context = _wait_for_dm_conversation(page)
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    page.simulate_reading_mouse(10_000)
    after = page.get_page_context()
    if (
        after.get("pageType") != "dm_conversation"
        or after.get("messageEditorVisible") is not True
        or not _healthy(after)
    ):
        raise RuntimeError("DM conversation changed during verification")
    return {
        "pageType": "dm_conversation",
        "pathname": str(after.get("pathname") or context.get("pathname") or ""),
        "profileId": profile_id,
        "boundTabId": after.get("boundTabId"),
        "riskSignals": [],
        "readingDwellSeconds": 10,
        "verified": True,
        "platform_actions_executed": 0,
    }


def return_to_source_comment(
    page, *, feed_id: str, comment_id: str
) -> dict[str, object]:
    """Return once to the exact source note and re-anchor the source comment."""
    for name, value in (("feed_id", feed_id), ("comment_id", comment_id)):
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError(f"{name} contains unsupported characters")
    before = page.get_page_context()
    if not _healthy(before) or before.get("pageType") not in {
        "profile", "dm_conversation", "message_surface",
    }:
        raise RuntimeError("return-to-source requires a healthy profile or DM page")
    returned_from_dm = before.get("pageType") in {"dm_conversation", "message_surface"}
    if returned_from_dm:
        profile = page.return_to_profile("")
        if profile.get("pageType") != "profile" or not _healthy(profile):
            raise RuntimeError("DM return did not reach the source user profile")
    context = page.return_to_source_note(feed_id)
    if (
        context.get("pageType") != "note_detail"
        or str(context.get("noteId") or "") != feed_id
        or not _healthy(context)
    ):
        raise RuntimeError("history return did not reach the exact source note")
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    if not _find_and_scroll_to_comment(page, comment_id, ""):
        raise RuntimeError("source comment changed or is no longer visible after return")
    page.simulate_reading_mouse(10_000)
    after = page.get_page_context()
    if (
        after.get("pageType") != "note_detail"
        or str(after.get("noteId") or "") != feed_id
        or not _healthy(after)
    ):
        raise RuntimeError("source note changed during comment re-anchoring")
    return {
        "pageType": "note_detail",
        "pathname": str(after.get("pathname") or ""),
        "noteId": feed_id,
        "commentId": comment_id,
        "boundTabId": after.get("boundTabId"),
        "riskSignals": [],
        "readingDwellSeconds": 10,
        "verified": True,
        "returnedFromDm": returned_from_dm,
        "platform_actions_executed": 0,
    }
