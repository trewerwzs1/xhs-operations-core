"""Visible inbox navigation and bounded capture for the V2 service workflow."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any


_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
_CONTACT_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:微信|vx|V信|wxid)[：:\s_-]*[A-Za-z0-9_-]{4,}", re.I),
)


def _visible_context(page) -> dict[str, Any]:
    context = page.get_page_context()
    if not isinstance(context, dict) or not isinstance(context.get("riskSignals"), list):
        raise RuntimeError("service page context is invalid")
    if context["riskSignals"]:
        raise RuntimeError("risk signal detected on service surface")
    return context


def _wait_page_type(page, allowed: set[str], *, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _visible_context(page)
        if str(latest.get("pageType") or "") in allowed:
            return latest
        time.sleep(0.3)
    raise RuntimeError(
        "service navigation did not reach the expected visible page: "
        + json.dumps(latest, ensure_ascii=False, sort_keys=True)
    )


def _find_text_control(page, labels: list[str]) -> dict[str, Any] | None:
    result = page.evaluate(
        f"""
        (() => {{
          const __tonyredbook_service_text_control_v1 = true;
          const labels = {json.dumps(labels, ensure_ascii=False)};
          const selector = 'a,button,[role="button"],[role="tab"]';
          const nodes = Array.from(document.querySelectorAll(selector));
          const visible = (node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
          }};
          for (const label of labels) {{
            const index = nodes.findIndex((node) =>
              visible(node)
              && String(node.innerText || node.textContent || '').replace(/\\s+/g, '').trim() === label
            );
            if (index >= 0) return {{ok: true, selector, index, label}};
          }}
          return {{ok: false}};
        }})()
        """
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    return result


def _click_text_control(page, labels: list[str]) -> str:
    target = _find_text_control(page, labels)
    if target is None:
        raise RuntimeError("required visible service navigation control was not found")
    selector = str(target["selector"])
    index = int(target["index"])
    page.scroll_nth_element_into_view(selector, index)
    page.click_nth_element(selector, index)
    return str(target["label"])


def open_service_inbox(page, *, channel: str) -> dict[str, Any]:
    """Reach one inbox through visible controls only; never navigate by URL."""

    if channel not in {"comments", "dm"}:
        raise ValueError("service channel must be comments or dm")
    before = _visible_context(page)
    current_type = str(before.get("pageType") or "")
    clicked: list[str] = []
    if current_type not in {"message_inbox", "message_surface"}:
        clicked.append(_click_text_control(page, ["通知", "消息"]))
        _wait_page_type(page, {"message_inbox", "message_surface"})
    labels = ["评论和@", "评论与@", "评论"] if channel == "comments" else ["私信", "消息"]
    target = _find_text_control(page, labels)
    if target is not None:
        selector = str(target["selector"])
        index = int(target["index"])
        page.scroll_nth_element_into_view(selector, index)
        page.click_nth_element(selector, index)
        clicked.append(str(target["label"]))
    allowed = {"message_inbox"} if channel == "comments" else {"message_inbox", "message_surface"}
    context = _wait_page_type(page, allowed)
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    page.simulate_reading_mouse(10_000)
    return {
        "channel": channel,
        "pageType": context.get("pageType"),
        "pathname": str(context.get("pathname") or ""),
        "boundTabId": context.get("boundTabId"),
        "riskSignals": [],
        "visibleControlsUsed": clicked,
        "readingDwellSeconds": 10,
        "verified": True,
        "platform_actions_executed": 0,
    }


def _redact(value: str) -> tuple[str, bool]:
    text = " ".join(str(value or "").split())
    redacted = False
    for pattern in _CONTACT_PATTERNS:
        if pattern.search(text):
            redacted = True
            text = pattern.sub("[已脱敏]", text)
    return text[:500], redacted


def _capture_dom(page, channel: str, max_items: int) -> dict[str, Any]:
    return page.evaluate(
        f"""
        (() => {{
          const __tonyredbook_service_inbox_capture_v1 = true;
          const channel = {json.dumps(channel)};
          const maxItems = {max_items};
          const visible = (node) => {{
            if (!(node instanceof Element)) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && style.visibility !== 'hidden' && style.display !== 'none';
          }};
          const profileId = (root) => {{
            const anchor = Array.from(root.querySelectorAll('a[href*="/user/profile/"]')).find(visible);
            return String(anchor?.getAttribute('href') || anchor?.href || '')
              .match(/\\/user\\/profile\\/([A-Za-z0-9_-]+)/)?.[1] || '';
          }};
          const dataId = (root, names) => {{
            for (const name of names) {{
              const value = String(root.getAttribute(name) || '').trim();
              if (/^[A-Za-z0-9_-]+$/.test(value)) return value;
            }}
            return '';
          }};
          const rows = [];
          const seen = new Set();
          if (channel === 'comments') {{
            const openSelector = 'a[href*="/explore/"],a[href*="/discovery/item/"]';
            const anchors = Array.from(document.querySelectorAll(openSelector));
            for (const [openIndex, anchor] of anchors.entries()) {{
              if (!visible(anchor)) continue;
              const root = anchor.closest('[data-notification-id],[class*="notification-item"],[class*="interaction-item"],li') || anchor.parentElement;
              if (!root || !visible(root) || seen.has(root)) continue;
              const text = String(root.innerText || root.textContent || '').replace(/\\s+/g, ' ').trim();
              if (!text || text.length > 1000 || !/(评论|回复|提到|@)/.test(text)) continue;
              const href = String(anchor.getAttribute('href') || anchor.href || '');
              const noteId = href.match(/\\/(?:explore|discovery\\/item)\\/([A-Za-z0-9_-]+)/)?.[1] || '';
              if (!noteId) continue;
              seen.add(root);
              rows.push({{
                channel, openSelector, openIndex, noteId,
                commentId: dataId(root, ['data-comment-id','data-commentid']),
                conversationId: '', peerProfileId: profileId(root), text,
                unread: Boolean(root.querySelector('[class*="unread"],[data-unread="true"]'))
              }});
              if (rows.length >= maxItems) break;
            }}
          }} else {{
            const openSelector = 'a[href*="/messages/"],a[href*="/im/"],a[href*="/chat/"]';
            const anchors = Array.from(document.querySelectorAll(openSelector));
            for (const [openIndex, anchor] of anchors.entries()) {{
              if (!visible(anchor)) continue;
              const root = anchor.closest('[data-conversation-id],[class*="conversation-item"],[class*="chat-item"],li') || anchor.parentElement;
              if (!root || !visible(root) || seen.has(root)) continue;
              const href = String(anchor.getAttribute('href') || anchor.href || '');
              const conversationId = dataId(root, ['data-conversation-id','data-chat-id'])
                || href.match(/\\/(?:messages|im|chat)\\/([A-Za-z0-9_-]+)/)?.[1] || '';
              const text = String(root.innerText || root.textContent || '').replace(/\\s+/g, ' ').trim();
              if (!conversationId || !text || text.length > 1000) continue;
              seen.add(root);
              rows.push({{
                channel, openSelector, openIndex, noteId: '', commentId: '',
                conversationId, peerProfileId: profileId(root), text,
                unread: Boolean(root.querySelector('[class*="unread"],[data-unread="true"]'))
              }});
              if (rows.length >= maxItems) break;
            }}
          }}
          return {{ok: true, rows, visibleItemCount: rows.length}};
        }})()
        """
    )


def _normalized_rows(page, *, channel: str, max_items: int) -> list[dict[str, Any]]:
    raw = _capture_dom(page, channel, max_items)
    if not isinstance(raw, dict) or raw.get("ok") is not True or not isinstance(raw.get("rows"), list):
        raise RuntimeError("visible service inbox could not be captured")
    result: list[dict[str, Any]] = []
    for row in raw["rows"][:max_items]:
        if not isinstance(row, dict) or row.get("channel") != channel:
            raise RuntimeError("service inbox row is invalid")
        note_id = str(row.get("noteId") or "")
        comment_id = str(row.get("commentId") or "")
        conversation_id = str(row.get("conversationId") or "")
        peer_id = str(row.get("peerProfileId") or "")
        for value in (note_id, comment_id, conversation_id, peer_id):
            if value and _SAFE_ID.fullmatch(value) is None:
                raise RuntimeError("service inbox identity contains unsupported characters")
        if channel == "comments" and not note_id:
            continue
        if channel == "dm" and not conversation_id:
            continue
        text, privacy_redacted = _redact(str(row.get("text") or ""))
        if not text:
            continue
        identity = {
            "channel": channel,
            "note_id": note_id,
            "comment_id": comment_id,
            "conversation_id": conversation_id,
            "peer_profile_id": peer_id,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        item_hash = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        result.append({
            **row,
            "text": text,
            "privacyRedacted": privacy_redacted,
            "textHash": identity["text_hash"],
            "itemHash": item_hash,
        })
    return result


def capture_service_inbox(page, *, channel: str, max_items: int = 20) -> dict[str, Any]:
    if channel not in {"comments", "dm"}:
        raise ValueError("service channel must be comments or dm")
    if type(max_items) is not int or not 1 <= max_items <= 50:
        raise ValueError("service max_items must be 1-50")
    context = _visible_context(page)
    allowed = {"message_inbox"} if channel == "comments" else {"message_inbox", "message_surface"}
    if context.get("pageType") not in allowed:
        raise RuntimeError("service inbox capture requires the verified channel surface")
    rows = _normalized_rows(page, channel=channel, max_items=max_items)
    output = []
    for index, row in enumerate(rows):
        output.append({
            "index": index,
            "channel": channel,
            "itemHash": row["itemHash"],
            "noteId": row.get("noteId", ""),
            "commentId": row.get("commentId", ""),
            "conversationId": row.get("conversationId", ""),
            "peerProfileId": row.get("peerProfileId", ""),
            "incomingText": row["text"],
            "incomingTextHash": row["textHash"],
            "privacyRedacted": row["privacyRedacted"],
            "unread": row.get("unread") is True,
        })
    return {
        "channel": channel,
        "items": output,
        "capturedItemCount": len(output),
        "coverage": "bounded_visible_service_inbox",
        "boundTabId": context.get("boundTabId"),
        "read_only": True,
        "platform_actions_executed": 0,
    }


def open_service_item(page, *, channel: str, expected_item_hash: str) -> dict[str, Any]:
    if channel not in {"comments", "dm"}:
        raise ValueError("service channel must be comments or dm")
    if re.fullmatch(r"[0-9a-f]{64}", expected_item_hash or "") is None:
        raise ValueError("expected_item_hash must be SHA-256 hex")
    _visible_context(page)
    rows = _normalized_rows(page, channel=channel, max_items=50)
    matches = [row for row in rows if row["itemHash"] == expected_item_hash]
    if len(matches) != 1:
        raise RuntimeError("exact service inbox item is missing or ambiguous")
    target = matches[0]
    selector = str(target.get("openSelector") or "")
    index = target.get("openIndex")
    if not selector or type(index) is not int or index < 0:
        raise RuntimeError("exact service inbox item has no stable visible control")
    page.scroll_nth_element_into_view(selector, index)
    page.click_nth_element(selector, index)
    expected_types = {"note_detail"} if channel == "comments" else {"dm_conversation"}
    context = _wait_page_type(page, expected_types)
    if channel == "comments" and str(context.get("noteId") or "") != str(target.get("noteId") or ""):
        raise RuntimeError("opened service comment note identity mismatch")
    page.wait_dom_stable(timeout=15.0, interval=0.5)
    page.simulate_reading_mouse(10_000)
    return {
        "channel": channel,
        "itemHash": expected_item_hash,
        "pageType": context.get("pageType"),
        "noteId": str(context.get("noteId") or ""),
        "profileId": str(context.get("profileId") or ""),
        "conversationId": str(target.get("conversationId") or ""),
        "boundTabId": context.get("boundTabId"),
        "riskSignals": [],
        "readingDwellSeconds": 10,
        "verified": True,
        "platform_actions_executed": 0,
    }
