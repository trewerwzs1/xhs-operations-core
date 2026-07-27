"""Bounded visible DM conversation capture for the private Run Agent gateway."""

from __future__ import annotations

import json
import hashlib
import re
import time
from typing import Any


_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def capture_current_dm_conversation(page, *, max_messages: int = 50) -> dict[str, Any]:
    """Capture visible message rows only; never reads storage, network, or hidden state."""
    if type(max_messages) is not int or not 1 <= max_messages <= 100:
        raise ValueError("max_messages must be 1-100")
    context = page.get_page_context()
    if (
        not isinstance(context, dict)
        or context.get("pageType") != "dm_conversation"
        or context.get("messageEditorVisible") is not True
        or context.get("riskSignals")
    ):
        raise RuntimeError("DM capture requires a healthy visible conversation editor")
    value = page.evaluate(
        f"""
        (() => {{
          const maxMessages = {max_messages};
          const editorSelector = [
            'textarea[placeholder*="发送消息"]',
            'textarea[placeholder*="发消息"]',
            '[contenteditable="true"][data-placeholder*="发送消息"]',
            '[contenteditable="true"][data-placeholder*="发消息"]',
            '[contenteditable="true"][placeholder*="发送消息"]',
            '[contenteditable="true"][placeholder*="发消息"]'
          ].join(',');
          const visible = (node) => {{
            if (!(node instanceof Element)) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && style.visibility !== 'hidden' && style.display !== 'none';
          }};
          const editor = Array.from(document.querySelectorAll(editorSelector)).find(visible);
          if (!editor) return {{ ok: false, reason: 'visible_dm_editor_missing' }};
          const surface = editor.closest('[role="dialog"],main,[class*="chat"],[class*="Chat"],[class*="message"],[class*="Message"]')
            || document.body;
          const rootRect = surface.getBoundingClientRect();
          const rowSelector = [
            '[data-message-id]', '[data-msg-id]',
            '[class*="message-item"]', '[class*="messageItem"]',
            '[class*="msg-item"]', '[class*="msgItem"]',
            '[class*="chat-item"]', '[class*="chatItem"]'
          ].join(',');
          const rawRows = Array.from(surface.querySelectorAll(rowSelector));
          const roots = [];
          const seen = new Set();
          for (const row of rawRows) {{
            if (!visible(row) || row.contains(editor) || editor.contains(row)) continue;
            const outer = row.closest('[data-message-id],[data-msg-id],[class*="message-item"],[class*="messageItem"],[class*="msg-item"],[class*="msgItem"]') || row;
            if (seen.has(outer)) continue;
            seen.add(outer);
            roots.push(outer);
          }}
          const messages = [];
          for (const [index, row] of roots.slice(-maxMessages).entries()) {{
            const text = String(row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text === '发送') continue;
            const className = String(row.className || '').toLowerCase();
            const rect = row.getBoundingClientRect();
            let direction = '';
            let directionEvidence = '';
            if (/(?:outgoing|sent|self|mine|right)/.test(className)) {{
              direction = 'outgoing'; directionEvidence = 'semantic_class';
            }} else if (/(?:incoming|received|other|left)/.test(className)) {{
              direction = 'incoming'; directionEvidence = 'semantic_class';
            }} else if (rootRect.width > 0) {{
              direction = rect.left + rect.width / 2 >= rootRect.left + rootRect.width / 2
                ? 'outgoing' : 'incoming';
              directionEvidence = 'visual_alignment';
            }}
            if (!direction) continue;
            messages.push({{ index, direction, directionEvidence, text }});
          }}
          const profileIds = Array.from(surface.querySelectorAll('a[href*="/user/profile/"]'))
            .filter(visible)
            .map((node) => String(node.getAttribute('href') || node.href || '').match(/\\/user\\/profile\\/([A-Za-z0-9_-]+)/)?.[1] || '')
            .filter(Boolean);
          return {{
            ok: true,
            profileIds: Array.from(new Set(profileIds)),
            messages,
            visibleRowCount: roots.length,
            capturedMessageCount: messages.length,
            coverage: 'bounded_visible_conversation'
          }};
        }})()
        """
    )
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError(
            "visible DM conversation could not be captured: "
            + json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
    profile_id = str(context.get("profileId") or "")
    visible_ids = [
        str(item) for item in value.get("profileIds", [])
        if isinstance(item, str) and _SAFE_ID.fullmatch(item)
    ]
    if not profile_id and len(set(visible_ids)) == 1:
        profile_id = visible_ids[0]
    if _SAFE_ID.fullmatch(profile_id) is None:
        raise RuntimeError("DM peer identity is missing or ambiguous")
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) > max_messages:
        raise RuntimeError("DM message capture exceeded its bounded contract")
    for item in messages:
        if (
            not isinstance(item, dict)
            or item.get("direction") not in {"incoming", "outgoing"}
            or item.get("directionEvidence") not in {"semantic_class", "visual_alignment"}
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            raise RuntimeError("DM message row is incomplete or ambiguous")
    return {
        "profileId": profile_id,
        "messages": messages,
        "riskSignals": [],
        "coverage": "bounded_visible_conversation",
        "visibleRowCount": int(value.get("visibleRowCount") or 0),
        "capturedMessageCount": len(messages),
        "boundTabId": context.get("boundTabId"),
        "read_only": True,
        "platform_actions_executed": 0,
    }


def _exact_outgoing_count(page, content: str) -> int:
    value = page.evaluate(
        f"""
        (() => {{
          const expected = {json.dumps(content)};
          const editorSelector = '[contenteditable="true"][data-placeholder*="发消息"],[contenteditable="true"][data-placeholder*="发送消息"],[contenteditable="true"][placeholder*="发消息"],[contenteditable="true"][placeholder*="发送消息"]';
          const editor = Array.from(document.querySelectorAll(editorSelector)).find((node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          }});
          if (!editor) return -1;
          const surface = editor.closest('[role="dialog"],main,[class*="chat"],[class*="Chat"],[class*="message"],[class*="Message"]') || document.body;
          const rootRect = surface.getBoundingClientRect();
          const rowSelector = '[data-message-id],[data-msg-id],[class*="message-item"],[class*="messageItem"],[class*="msg-item"],[class*="msgItem"],[class*="chat-item"],[class*="chatItem"]';
          const roots = Array.from(new Set(Array.from(surface.querySelectorAll(rowSelector)).map((row) =>
            row.closest('[data-message-id],[data-msg-id],[class*="message-item"],[class*="messageItem"],[class*="msg-item"],[class*="msgItem"]') || row
          )));
          let count = 0;
          for (const row of roots) {{
            if (row.contains(editor) || editor.contains(row)) continue;
            const className = String(row.className || '').toLowerCase();
            const rect = row.getBoundingClientRect();
            const outgoing = /(?:outgoing|sent|self|mine|right)/.test(className)
              || (!/(?:incoming|received|other|left)/.test(className)
                && rootRect.width > 0
                && rect.left + rect.width / 2 >= rootRect.left + rootRect.width / 2);
            if (!outgoing) continue;
            const leafMatch = Array.from(row.querySelectorAll('span,p,div')).some((node) =>
              !node.matches(editorSelector)
              && !node.querySelector('span,p,div')
              && String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim() === expected
            );
            const rowText = String(row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim();
            if (leafMatch || rowText === expected) count += 1;
          }}
          return count;
        }})()
        """
    )
    if type(value) is not int or value < 0:
        raise RuntimeError("DM outgoing verification surface is missing")
    return value


def send_current_dm_message(
    page, *, expected_peer_hash: str, content: str
) -> dict[str, Any]:
    """Send exactly one approved message on the current identity-bound conversation."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_peer_hash or "") is None:
        raise ValueError("expected_peer_hash must be SHA-256 hex")
    content = " ".join(str(content or "").split())
    if not content or len(content) > 240 or "\ufffd" in content or set(content) == {"?"}:
        raise ValueError("DM content failed length or Unicode validation")
    captured = capture_current_dm_conversation(page, max_messages=100)
    profile_id = str(captured["profileId"])
    if hashlib.sha256(profile_id.encode("utf-8")).hexdigest() != expected_peer_hash:
        raise RuntimeError("current DM peer differs from the approved target")
    baseline = _exact_outgoing_count(page, content)
    editor_selector = (
        '[contenteditable="true"][data-placeholder*="发送消息"],'
        '[contenteditable="true"][data-placeholder*="发消息"],'
        '[contenteditable="true"][placeholder*="发送消息"],'
        '[contenteditable="true"][placeholder*="发消息"]'
    )
    editor = page.evaluate(
        f"""
        (() => {{
          const selector = {json.dumps(editor_selector)};
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
          }});
          return index >= 0 ? {{ ok: true, index }} : {{ ok: false, index: -1 }};
        }})()
        """
    )
    if not isinstance(editor, dict) or editor.get("ok") is not True:
        raise RuntimeError("visible DM contenteditable editor was not found")
    page.input_content_editable(editor_selector, content, index=int(editor["index"]))
    submit = page.evaluate(
        r"""
        (() => {
          const selector = 'button';
          const nodes = Array.from(document.querySelectorAll(selector));
          const index = nodes.findIndex((node) => {
            const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').trim();
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return text === '发送' && !node.disabled
              && rect.width > 0 && rect.height > 0
              && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth
              && style.visibility !== 'hidden' && style.display !== 'none';
          });
          return index >= 0 ? { ok: true, selector, index } : { ok: false, index: -1 };
        })()
        """
    )
    if not isinstance(submit, dict) or submit.get("ok") is not True:
        raise RuntimeError("visible enabled DM send button was not found")
    page.scroll_nth_element_into_view(str(submit["selector"]), int(submit["index"]))
    page.click_nth_element(str(submit["selector"]), int(submit["index"]))
    deadline = time.monotonic() + 20.0
    observed = baseline
    while time.monotonic() < deadline:
        observed = _exact_outgoing_count(page, content)
        if observed > baseline:
            context = page.get_page_context()
            if context.get("riskSignals"):
                raise RuntimeError("risk signal detected after DM send")
            return {
                "success": True,
                "verified": True,
                "profileId": profile_id,
                "contentHash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "baselineExactOutgoingCount": baseline,
                "observedExactOutgoingCount": observed,
                "verification": "scoped_exact_outgoing_visible_increase",
                "platform_actions_executed": 1,
            }
        time.sleep(0.3)
    raise RuntimeError(
        "DM send result is unknown: exact outgoing visible count did not increase; "
        f"baseline={baseline}, observed={observed}"
    )
