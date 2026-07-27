"""Bounded read-only capture of replies written by the current profile owner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time

from .feed_detail import get_current_feed_detail
from .types import CommentLoadConfig
from .user_profile import _extract_user_profile_data


def _iso(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = float(value)
    if seconds > 10_000_000_000:
        seconds /= 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _risk_free(context: object) -> bool:
    return isinstance(context, dict) and isinstance(context.get("riskSignals"), list) and not context["riskSignals"]


def _history_comment_rows(comments: list[object], *, account_user_id: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in comments:
        if not isinstance(raw, dict):
            continue
        parent_id = str(raw.get("id") or "").strip()
        parent_text = str(raw.get("content") or "").strip()
        if not parent_id or not parent_text:
            continue
        rows.append({
            "comment_id": parent_id, "parent_comment_id": None,
            "commenter_role": "visitor", "text": parent_text,
            "published_at": _iso(raw.get("createTime")), "ownership_evidence": None,
        })
        nested = raw.get("subComments", [])
        if not isinstance(nested, list):
            continue
        for reply in nested:
            if not isinstance(reply, dict):
                continue
            reply_id = str(reply.get("id") or "").strip()
            reply_text = str(reply.get("content") or "").strip()
            user = reply.get("user", {})
            reply_user_id = str(user.get("userId") or "") if isinstance(user, dict) else ""
            if not reply_id or not reply_text:
                continue
            is_owner = bool(account_user_id) and reply_user_id == account_user_id
            rows.append({
                "comment_id": reply_id, "parent_comment_id": parent_id,
                "commenter_role": "account_owner" if is_owner else "visitor",
                "text": reply_text, "published_at": _iso(reply.get("createTime")),
                "ownership_evidence": "account_user_id_match" if is_owner else None,
            })
    return rows


def _visible_profile_feed_ids(page) -> list[str]:
    """Return exact note IDs for cards that are currently pointer-clickable.

    The profile state may contain notes that are not rendered in the current
    viewport.  AccountVoice operates on one bounded visible batch, so those
    off-screen state entries must not be compiled into click targets.  The
    click surface is the mature ``section.note-item`` card, not its nested
    anchor: Xiaohongshu may render a mask above the anchor while the complete
    card remains the trusted pointer target.
    """
    result = page.evaluate(
        r"""
        (() => {
          const __tonyredbook_visible_profile_feed_ids = true;
          const ids = [];
          const seen = new Set();
          for (const card of document.querySelectorAll('section.note-item')) {
            try {
              const node = Array.from(card.querySelectorAll('a[href]')).find((candidate) => {
                try {
                  const candidateUrl = new URL(
                    candidate.href || candidate.getAttribute('href') || '', location.origin
                  );
                  return /^\/(?:explore|search_result)\/[A-Za-z0-9_-]+(?:\/|$)/.test(
                    candidateUrl.pathname
                  );
                } catch {
                  return false;
                }
              });
              if (!node) continue;
              const url = new URL(node.href || node.getAttribute('href') || '', location.origin);
              const match = url.pathname.match(/^\/(?:explore|search_result)\/([A-Za-z0-9_-]+)(?:\/|$)/);
              if (!match || seen.has(match[1])) continue;
              const rect = card.getBoundingClientRect();
              const style = getComputedStyle(card);
              if (rect.width <= 0 || rect.height <= 0 ||
                  rect.bottom <= 0 || rect.right <= 0 ||
                  rect.top >= innerHeight || rect.left >= innerWidth ||
                  style.display === 'none' || style.visibility === 'hidden') continue;
              const x = Math.max(1, Math.min(innerWidth - 1, rect.left + rect.width / 2));
              const y = Math.max(1, Math.min(innerHeight - 1, rect.top + rect.height / 2));
              const hit = document.elementFromPoint(x, y);
              if (!hit || !(card === hit || card.contains(hit) || hit.contains(card))) continue;
              seen.add(match[1]);
              ids.push(match[1]);
            } catch {
              continue;
            }
          }
          return ids;
        })()
        """
    )
    if not isinstance(result, list):
        raise RuntimeError("visible own-profile note batch is invalid")
    ids: list[str] = []
    for value in result:
        feed_id = str(value or "")
        if re.fullmatch(r"[A-Za-z0-9_-]+", feed_id) is None:
            raise RuntimeError("visible own-profile note batch contains an invalid note ID")
        if feed_id not in ids:
            ids.append(feed_id)
    return ids


def _open_visible_profile_note(page, *, feed_id: str, profile_id: str) -> dict[str, object]:
    """Open one exact note from the visible own-profile grid.

    AccountVoice must follow the same visible navigation contract as Engage:
    no token URL, no guessed direct navigation, and no batch scan before the
    current note is opened and verified.
    """
    if re.fullmatch(r"[A-Za-z0-9_-]+", feed_id) is None:
        raise RuntimeError("profile note ID is invalid")
    before = page.get_page_context()
    if (
        not _risk_free(before)
        or before.get("pageType") != "profile"
        or str(before.get("profileId") or "") != profile_id
    ):
        raise RuntimeError("profile note navigation requires the exact healthy own profile")
    selector = "section.note-item"
    target = page.evaluate(
        rf"""
        (() => {{
          const selector = {json.dumps(selector)};
          const expected = {json.dumps(feed_id)};
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((card) => {{
            try {{
              const node = Array.from(card.querySelectorAll('a[href]')).find((candidate) => {{
                try {{
                  const candidateUrl = new URL(
                    candidate.href || candidate.getAttribute('href') || '', location.origin
                  );
                  const candidateMatch = candidateUrl.pathname.match(
                    /^\/(?:explore|search_result)\/([A-Za-z0-9_-]+)(?:\/|$)/
                  );
                  return candidateMatch?.[1] === expected;
                }} catch {{
                  return false;
                }}
              }});
              if (!node) return false;
              const url = new URL(node.href || node.getAttribute('href') || '', location.origin);
              const match = url.pathname.match(/^\/(?:explore|search_result)\/([A-Za-z0-9_-]+)(?:\/|$)/);
              if (match?.[1] !== expected) return false;
              const rect = card.getBoundingClientRect();
              const style = getComputedStyle(card);
              if (!(rect.width > 0 && rect.height > 0 &&
                rect.bottom > 0 && rect.right > 0 &&
                rect.top < innerHeight && rect.left < innerWidth &&
                style.display !== 'none' && style.visibility !== 'hidden')) return false;
              const x = Math.max(1, Math.min(innerWidth - 1, rect.left + rect.width / 2));
              const y = Math.max(1, Math.min(innerHeight - 1, rect.top + rect.height / 2));
              const hit = document.elementFromPoint(x, y);
              return Boolean(hit && (card === hit || card.contains(hit) || hit.contains(card)));
            }} catch {{
              return false;
            }}
          }});
          return index >= 0 ? {{ok: true, index}} : {{ok: false}};
        }})()
        """
    )
    if not isinstance(target, dict) or target.get("ok") is not True:
        raise RuntimeError("exact own-profile note is not present in the visible profile grid")
    index = int(target["index"])
    # The DOM probe above proves that this exact card is already inside the
    # viewport.  A separate semantic-scroll is redundant, doubles the audited
    # pointer operations, and can occupy the Bridge command channel for its
    # full timeout even though the card was already clickable.
    page.click_nth_element(selector, index)
    deadline = time.monotonic() + 30.0
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        context = page.get_page_context()
        if isinstance(context, dict):
            latest = context
            if (
                _risk_free(context)
                and context.get("pageType") == "note_detail"
                and str(context.get("noteId") or "") == feed_id
            ):
                page.wait_dom_stable(timeout=15.0, interval=0.5)
                return context
            if context.get("riskSignals"):
                raise RuntimeError("risk signal detected while opening own-profile note")
        time.sleep(0.3)
    raise RuntimeError(
        "own-profile note click did not reach the exact note: "
        + json.dumps(latest, ensure_ascii=False, sort_keys=True)
    )


def capture_own_reply_history(
    page, *, start_position: int = 0, max_notes: int = 10,
    max_comment_items: int = 200,
) -> dict[str, object]:
    if type(start_position) is not int or start_position < 0:
        raise ValueError("start_position must be a non-negative integer")
    if type(max_notes) is not int or not 1 <= max_notes <= 30:
        raise ValueError("max_notes must be 1-30")
    if type(max_comment_items) is not int or not 1 <= max_comment_items <= 200:
        raise ValueError("max_comment_items must be 1-200")
    context = page.get_page_context()
    visible_url = str(context.get("pathname") or context.get("url") or "") if isinstance(context, dict) else ""
    if not _risk_free(context):
        raise RuntimeError("profile history capture requires a risk-free visible page")
    if re.search(r"/user/profile/[A-Za-z0-9_-]+", visible_url) is None:
        raise RuntimeError("open the logged-in account's own profile page before history capture")
    profile_id = str(context.get("profileId") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]+", profile_id) is None:
        raise RuntimeError("visible own-profile ID is invalid")
    profile = _extract_user_profile_data(page)
    feeds = profile.feeds
    if not feeds:
        raise RuntimeError("current profile has no readable published notes")
    author_ids = {item.note_card.user.user_id for item in feeds if item.note_card.user.user_id}
    if len(author_ids) != 1:
        raise RuntimeError("profile note ownership is ambiguous")
    account_user_id = next(iter(author_ids))
    feed_by_id = {item.id: item for item in feeds if item.id}
    visible_feed_ids = _visible_profile_feed_ids(page)
    if not visible_feed_ids:
        raise RuntimeError(
            "current own-profile viewport has no pointer-clickable note cards; "
            "in the dedicated Chrome, scroll until the first note row is visible, "
            "then start one new bounded capture"
        )
    visible_feeds = [feed_by_id[feed_id] for feed_id in visible_feed_ids if feed_id in feed_by_id]
    if not visible_feeds:
        raise RuntimeError("visible own-profile cards do not match the verified profile note state")
    selected = visible_feeds[start_position : start_position + max_notes]
    if not selected:
        raise RuntimeError("start_position is outside the current visible profile note batch")
    config = CommentLoadConfig(
        click_more_replies=True, max_replies_threshold=200,
        max_comment_items=max_comment_items, scroll_speed="slow",
    )
    notes: list[dict[str, object]] = []
    for feed in selected:
        if feed.note_card.user.user_id != account_user_id or not feed.id:
            raise RuntimeError("profile note identity is missing")
        _open_visible_profile_note(page, feed_id=feed.id, profile_id=profile_id)
        has_comment_surface = page.has_element(".comments-container")
        detail = get_current_feed_detail(
            page, feed.id, load_all_comments=has_comment_surface,
            config=config,
        ).to_dict()
        if not _risk_free(page.get_page_context()):
            raise RuntimeError("risk signal detected during profile history capture")
        note = detail.get("note", {})
        comments = detail.get("comments", [])
        if not isinstance(note, dict) or not isinstance(comments, list):
            raise RuntimeError("profile note detail is invalid")
        if str(note.get("noteId") or "") != feed.id:
            raise RuntimeError("profile note detail identity mismatch")
        note_user = note.get("user", {})
        if not isinstance(note_user, dict) or str(note_user.get("userId") or "") != account_user_id:
            raise RuntimeError("opened note does not belong to the current profile")
        notes.append({
            "note_id": feed.id,
            "note_ref": f"https://www.xiaohongshu.com/explore/{feed.id}",
            "title": str(note.get("title") or feed.note_card.display_title or ""),
            "body": str(note.get("body") or note.get("desc") or ""),
            "published_at": _iso(note.get("time")),
            "comments": _history_comment_rows(comments, account_user_id=account_user_id),
        })
        returned = page.return_to_profile(profile_id)
        if (
            not _risk_free(returned)
            or returned.get("pageType") != "profile"
            or str(returned.get("profileId") or "") != profile_id
        ):
            raise RuntimeError("history return did not reach the exact own profile")
        page.wait_dom_stable(timeout=15.0, interval=0.5)
        page.simulate_reading_mouse(10_000)
    next_position = start_position + len(selected)
    has_more = next_position < len(visible_feeds)
    return {
        "account_user_id": account_user_id,
        "capture": {"page_count": 1, "has_more": has_more,
                    "next_note_position": next_position if has_more else None, "notes": notes},
        "captured_note_count": len(notes), "available_note_count": len(visible_feeds),
        "platform_actions_executed": 0,
    }
