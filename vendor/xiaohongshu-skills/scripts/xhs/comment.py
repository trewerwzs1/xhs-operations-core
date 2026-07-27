"""评论操作，对应 Go xiaohongshu/comment_feed.go。"""

from __future__ import annotations

import logging
import json
import re
import time

from .cdp import Page
from .feed_detail import _check_end_container, _check_page_accessible, _get_comment_count
from .human import sleep_random
from .selectors import (
    COMMENT_INPUT_FIELD,
    COMMENT_INPUT_TRIGGER,
    COMMENT_SUBMIT_BUTTON,
    PARENT_COMMENT,
    REPLY_BUTTON,
)
from .urls import make_feed_detail_url, redact_url
from .types import ActionResult
from .current_page import require_current_note

logger = logging.getLogger(__name__)

_PLATFORM_WRITE_ALERT_SELECTOR = ".reds-alert-wrapper"
_PLATFORM_WRITE_RESTRICTION_MARKERS = (
    "禁言",
    "违反社区规范",
    "功能受限",
    "账号存在风险",
    "操作频繁",
)


class CurrentPageActionError(RuntimeError):
    def __init__(self, message: str, *, action_dispatched: bool, failure_code: str) -> None:
        super().__init__(message)
        self.action_dispatched = action_dispatched
        self.failure_code = failure_code


def _visible_platform_write_restriction(page: Page) -> str:
    """Return a visible platform write restriction without mutating the page."""
    text = (page.get_element_text(_PLATFORM_WRITE_ALERT_SELECTOR) or "").strip()
    if text and any(marker in text for marker in _PLATFORM_WRITE_RESTRICTION_MARKERS):
        return text
    return ""


def _require_platform_write_available(page: Page, *, action_name: str) -> None:
    restriction = _visible_platform_write_restriction(page)
    if restriction:
        raise CurrentPageActionError(
            f"{action_name}已停止：平台明确限制当前账号写入（{restriction}）",
            action_dispatched=False,
            failure_code="platform_account_muted",
        )


def _comment_editor_failure_code(exc: Exception) -> str:
    """Map Bridge editor failures to non-sensitive, retry-safe diagnostics."""
    message = str(exc).lower()
    if "visible editor does not exist" in message:
        return "comment_editor_not_visible"
    if "exact readback" in message:
        return "comment_editor_readback_mismatch"
    if "debugger" in message or "target closed" in message:
        return "comment_editor_debugger_unavailable"
    if "90s" in message or "执行超时" in message:
        return "comment_editor_bridge_timeout"
    return "comment_editor_pre_dispatch_failure"


def _first_visible_control_index(page: Page, selector: str) -> int | None:
    """Return the first viewport-visible control without assuming index zero.

    Xiaohongshu can retain a hidden editor from an earlier note overlay while
    mounting the current note's editor later in the DOM.  The Bridge input
    contract is index based, so blindly using index zero can target that stale
    hidden node even though a usable control is present.
    """
    result = page.evaluate(
        f"""
        (() => {{
          const nodes = Array.from(document.querySelectorAll({json.dumps(selector)}));
          return nodes.findIndex((node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            if (rect.width <= 0 || rect.height <= 0
                || rect.bottom <= 0 || rect.right <= 0
                || rect.top >= innerHeight || rect.left >= innerWidth
                || style.display === 'none' || style.visibility === 'hidden') return false;
            const hit = document.elementFromPoint(
              rect.left + rect.width / 2, rect.top + rect.height / 2
            );
            return Boolean(hit && (node === hit || node.contains(hit) || hit.contains(node)));
          }});
        }})()
        """
    )
    return result if isinstance(result, int) and result >= 0 else None


def _first_rendered_control_index(page: Page, selector: str) -> int | None:
    """Return a rendered control even when it is just outside the viewport."""
    result = page.evaluate(
        f"""
        (() => {{
          const nodes = Array.from(document.querySelectorAll({json.dumps(selector)}));
          return nodes.findIndex((node) => {{
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0
              && style.display !== 'none' && style.visibility !== 'hidden';
          }});
        }})()
        """
    )
    return result if isinstance(result, int) and result >= 0 else None


def _wait_for_visible_enabled_submit(
    page: Page, selector: str, index: int, *, timeout: float = 5.0
) -> bool:
    """Wait for React to turn the exact visible submit control active."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = page.evaluate(
            f"""
            (() => {{
              const __tonyredbook_comment_submit_ready = true;
              const node = document.querySelectorAll({json.dumps(selector)})[{index}];
              if (!node) return false;
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              const hit = document.elementFromPoint(
                rect.left + rect.width / 2, rect.top + rect.height / 2
              );
              const className = String(node.className || '');
              return rect.width > 0 && rect.height > 0
                && rect.bottom > 0 && rect.right > 0
                && rect.top < innerHeight && rect.left < innerWidth
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Boolean(hit && (node === hit || node.contains(hit) || hit.contains(node)))
                && !node.disabled && node.getAttribute('aria-disabled') !== 'true'
                && !/(^|[\\s_-])(gray|disabled)([\\s_-]|$)/i.test(className);
            }})()
            """
        )
        if state is True:
            return True
        time.sleep(0.25)
    return False


def inspect_current_comment_controls(
    page: Page, feed_id: str, *, comment_id: str = ""
) -> dict[str, object]:
    """Return sanitized, read-only diagnostics for current-note comment controls."""
    require_current_note(page, feed_id)
    if comment_id and re.fullmatch(r"[A-Za-z0-9_-]+", comment_id) is None:
        raise ValueError("comment_id contains unsupported selector characters")
    if comment_id and not _find_and_scroll_to_comment(page, comment_id, ""):
        return {
            "noteId": feed_id,
            "commentId": comment_id,
            "targetRootFound": False,
            "likeControls": [],
            "error": "comment_like_target_unavailable",
            "platform_actions_executed": 0,
        }
    root_selector = f"#comment-{comment_id}" if comment_id else ""
    like_selector = _comment_like_control_selector(root_selector) if root_selector else ""
    expression = f"""
    (() => {{
      const visible = (node) => {{
        if (!node) return false;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return rect.width > 0 && rect.height > 0
          && rect.bottom > 0 && rect.right > 0
          && rect.top < innerHeight && rect.left < innerWidth
          && style.display !== 'none' && style.visibility !== 'hidden';
      }};
      const describe = (node, selector, index) => {{
        if (!node) return null;
        const rect = node.getBoundingClientRect();
        const hit = document.elementFromPoint(
          rect.left + rect.width / 2, rect.top + rect.height / 2
        );
        return {{
          selector, index,
          tag: String(node.localName || ''),
          className: String(node.className || '').slice(0, 160),
          text: String(node.textContent || '').trim().slice(0, 80),
          visible: visible(node),
          rect: {{
            left: Math.round(rect.left), top: Math.round(rect.top),
            width: Math.round(rect.width), height: Math.round(rect.height),
          }},
          hit: hit ? {{
            tag: String(hit.localName || ''),
            className: String(hit.className || '').slice(0, 160),
            text: String(hit.textContent || '').trim().slice(0, 80),
          }} : null,
        }};
      }};
      const describeLike = (node, selector, index) => {{
        const base = describe(node, selector, index);
        if (!base) return null;
        const nodes = [node, ...Array.from(node.querySelectorAll('*'))];
        const classNames = nodes.map((item) => String(item.className || ''))
          .filter(Boolean).slice(0, 24);
        const ariaPressed = nodes.map((item) => item.getAttribute?.('aria-pressed'))
          .filter((value) => value !== null && value !== undefined).slice(0, 12);
        const dataStates = nodes.map((item) => item.getAttribute?.('data-state'))
          .filter((value) => value !== null && value !== undefined).slice(0, 12);
        const colors = nodes.map((item) => {{
          const style = window.getComputedStyle(item);
          return `${{style.color}}|${{style.fill}}|${{style.stroke}}`.toLowerCase();
        }}).filter((value, position, all) => all.indexOf(value) === position).slice(0, 12);
        const explicitLikedClassNodes = classNames.filter((value) =>
          /(^|[\\s_-])(liked|selected)([\\s_-]|$)/i.test(value)
        );
        const ambiguousActiveClassNodes = classNames.filter((value) =>
          /(^|[\\s_-])active([\\s_-]|$)/i.test(value)
        );
        const redColorNodes = colors.filter((value) =>
          value.includes('rgb(255, 36, 66)') || value.includes('#ff2442')
        );
        return {{
          ...base,
          ariaPressed,
          dataStates,
          classNames,
          colors,
          activeSignals: {{
            ariaPressedTrue: ariaPressed.includes('true'),
            activeClassNodes: explicitLikedClassNodes,
            explicitLikedClassNodes,
            ambiguousActiveClassNodes,
            redColorNodes,
          }},
        }};
      }};
      const rootSelector = {json.dumps(root_selector)};
      const likeSelector = {json.dumps(like_selector)};
      const root = rootSelector ? document.querySelector(rootSelector) : null;
      const selectors = [
        rootSelector ? `${{rootSelector}} .right .interactions .reply` : '',
        rootSelector ? `${{rootSelector}} .reply` : '',
        rootSelector ? `${{rootSelector}} [class*="reply"]` : '',
      ].filter(Boolean);
      const replyControls = [];
      for (const selector of selectors) {{
        Array.from(document.querySelectorAll(selector)).forEach((node, index) => {{
          replyControls.push(describe(node, selector, index));
        }});
      }}
      const textReplies = root ? Array.from(root.querySelectorAll('button,[role="button"],span,div'))
        .filter((node) => String(node.textContent || '').trim() === '回复')
        .slice(0, 10)
        .map((node, index) => describe(node, 'target-root:text=回复', index)) : [];
      const inspectSelector = (selector) => Array.from(document.querySelectorAll(selector))
        .map((node, index) => describe(node, selector, index));
      return {{
        noteId: {json.dumps(feed_id)},
        commentId: {json.dumps(comment_id)},
        targetRootFound: Boolean(root),
        replyControls,
        likeControls: likeSelector
          ? Array.from(document.querySelectorAll(likeSelector))
              .map((node, index) => describeLike(node, likeSelector, index))
          : [],
        textReplies,
        inputTriggers: inspectSelector({json.dumps(COMMENT_INPUT_TRIGGER)}),
        editors: inspectSelector({json.dumps(COMMENT_INPUT_FIELD)}),
        submitButtons: inspectSelector('div.bottom button.' + 'sub' + 'mit'),
        alerts: [
          '.reds-alert', '.reds-alert-wrapper', '.reds-dialog',
          '.reds-modal', '[role="dialog"]', '.reds-alert-mask + *'
        ].flatMap((selector) => inspectSelector(selector)),
        platform_actions_executed: 0,
      }};
    }})()
    """
    result = page.evaluate(expression)
    return result if isinstance(result, dict) else {
        "noteId": feed_id,
        "commentId": comment_id,
        "error": "comment_control_diagnostic_unavailable",
        "platform_actions_executed": 0,
    }


def post_comment_current_note(page: Page, feed_id: str, content: str) -> dict[str, object]:
    """Comment on the already open note; navigation is forbidden."""
    try:
        require_current_note(page, feed_id)
        _check_page_accessible(page)
        _require_platform_write_available(page, action_name="评论")
        baseline_count = _count_exact_visible_text(page, content)
    except CurrentPageActionError:
        raise
    except Exception as exc:
        raise CurrentPageActionError(
            "评论提交前页面或基线校验失败",
            action_dispatched=False,
            failure_code="comment_pre_dispatch_guard_failed",
        ) from exc
    editor_index = _first_visible_control_index(page, COMMENT_INPUT_FIELD)
    trigger_index = (
        None
        if editor_index is not None
        else _first_visible_control_index(page, COMMENT_INPUT_TRIGGER)
    )
    if editor_index is None and trigger_index is None:
        raise CurrentPageActionError(
            "未找到评论输入框，该帖子可能不支持评论或网页端不可访问",
            action_dispatched=False, failure_code="comment_editor_unavailable",
        )
    try:
        # The current XHS note DOM can expose an already-visible editor and a
        # placeholder trigger at the same time. Clicking that placeholder a
        # second time may blur/rebuild the editor before progressive input.
        # Reuse the existing editor when present; only open it when the field
        # has not been mounted yet.
        if editor_index is None:
            page.scroll_nth_element_into_view(COMMENT_INPUT_TRIGGER, trigger_index)
            page.click_nth_element(COMMENT_INPUT_TRIGGER, trigger_index)
            page.wait_for_element(COMMENT_INPUT_FIELD, timeout=5)
            editor_index = _first_visible_control_index(page, COMMENT_INPUT_FIELD)
        if editor_index is None:
            raise RuntimeError("visible editor does not exist after trigger")
        # The trigger click is already constrained to a visible viewport
        # coordinate.  The mounted editor replaces that same control inside
        # XHS's right-hand scroll container.  A second generic wheel scroll is
        # both unnecessary and unsafe here because it may target the page's
        # centre column instead of the nested comment pane.
        page.input_content_editable(COMMENT_INPUT_FIELD, content, index=editor_index)
    except Exception as exc:
        raise CurrentPageActionError(
            "评论提交前编辑器操作失败",
            action_dispatched=False, failure_code=_comment_editor_failure_code(exc),
        ) from exc
    submit_index = _first_rendered_control_index(page, COMMENT_SUBMIT_BUTTON)
    if submit_index is None:
        raise CurrentPageActionError(
            "评论提交前未找到发送按钮",
            action_dispatched=False,
            failure_code="comment_submit_not_visible",
        )
    page.scroll_nth_element_into_view(COMMENT_SUBMIT_BUTTON, submit_index)
    if not _wait_for_visible_enabled_submit(
        page, COMMENT_SUBMIT_BUTTON, submit_index
    ):
        raise CurrentPageActionError(
            "评论提交前发送按钮未进入可用状态",
            action_dispatched=False,
            failure_code="comment_submit_not_ready",
        )
    try:
        page.click_nth_element(COMMENT_SUBMIT_BUTTON, submit_index)
    except Exception as exc:
        raise CurrentPageActionError(
            "评论提交结果未知",
            action_dispatched=True, failure_code="comment_submit_transport_unknown",
        ) from exc
    if not _wait_for_exact_visible_text_increase(page, content, baseline_count):
        restriction = _visible_platform_write_restriction(page)
        if restriction:
            raise CurrentPageActionError(
                f"评论被平台拒绝：当前账号写入受限（{restriction}）",
                action_dispatched=True,
                failure_code="platform_account_muted",
            )
        raise CurrentPageActionError(
            "评论发送结果未知：页面未读回完全相同的可见文本",
            action_dispatched=True, failure_code="comment_text_not_verified",
        )
    return {
        "success": True, "verified": True, "actionDispatched": True,
        "platform_actions_executed": 1,
    }


def reply_current_comment(
    page: Page,
    feed_id: str,
    content: str,
    *,
    comment_id: str,
) -> dict[str, object]:
    """Reply to one exact comment on the already open note."""
    if not comment_id:
        raise ValueError("comment_id is required")
    try:
        require_current_note(page, feed_id)
        _check_page_accessible(page)
        _require_platform_write_available(page, action_name="回复")
        target_found = _find_and_scroll_to_comment(page, comment_id, "")
    except CurrentPageActionError:
        raise
    except Exception as exc:
        raise CurrentPageActionError(
            "回复提交前页面或目标校验失败",
            action_dispatched=False,
            failure_code="reply_pre_dispatch_guard_failed",
        ) from exc
    if not target_found:
        raise CurrentPageActionError(
            f"未找到评论 (commentID: {comment_id})",
            action_dispatched=False, failure_code="reply_target_unavailable",
        )
    try:
        baseline_count = _count_exact_visible_text(
            page, content, parent_comment_id=comment_id
        )
    except Exception as exc:
        raise CurrentPageActionError(
            "回复提交前可见文本基线校验失败",
            action_dispatched=False,
            failure_code="reply_baseline_pre_dispatch_failed",
        ) from exc
    reply_selector = f"#comment-{comment_id} {REPLY_BUTTON}"
    try:
        page.click_element(reply_selector)
        page.wait_for_element(COMMENT_INPUT_FIELD, timeout=5)
        page.input_content_editable(COMMENT_INPUT_FIELD, content)
    except Exception as exc:
        raise CurrentPageActionError(
            "回复提交前编辑器操作失败",
            action_dispatched=False, failure_code="reply_editor_pre_dispatch_failure",
        ) from exc
    try:
        page.click_element(COMMENT_SUBMIT_BUTTON)
    except Exception as exc:
        raise CurrentPageActionError(
            "回复提交结果未知",
            action_dispatched=True, failure_code="reply_submit_transport_unknown",
        ) from exc
    if not _wait_for_exact_visible_text_increase(
        page, content, baseline_count, parent_comment_id=comment_id
    ):
        restriction = _visible_platform_write_restriction(page)
        if restriction:
            raise CurrentPageActionError(
                f"回复被平台拒绝：当前账号写入受限（{restriction}）",
                action_dispatched=True,
                failure_code="platform_account_muted",
            )
        raise CurrentPageActionError(
            "回复发送结果未知：页面未读回完全相同的可见文本",
            action_dispatched=True, failure_code="reply_text_not_verified",
        )
    return {
        "success": True, "verified": True, "actionDispatched": True,
        "platform_actions_executed": 1,
    }


def like_current_comment(
    page: Page,
    feed_id: str,
    *,
    comment_id: str,
) -> ActionResult:
    """Like one exact comment on the already open note."""
    if not comment_id:
        raise ValueError("comment_id is required")
    require_current_note(page, feed_id)
    _check_page_accessible(page)
    if not _find_and_scroll_to_comment(page, comment_id, ""):
        return ActionResult(
            feed_id=feed_id, success=False, message="评论点赞失败：目标评论不存在",
            failure_code="comment_like_target_unavailable",
        )
    selector = _comment_like_control_selector(f"#comment-{comment_id}")
    state_expression = _comment_like_expression(selector)
    before = page.evaluate(state_expression)
    if (
        not isinstance(before, dict)
        or before.get("found") is not True
        or type(before.get("active")) is not bool
        or type(before.get("index")) is not int
        or int(before["index"]) < 0
    ):
        return ActionResult(
            feed_id=feed_id, success=False, message="评论点赞失败：未找到点赞控件",
            failure_code="comment_like_control_unavailable",
        )
    if before.get("active") is True:
        return ActionResult(
            feed_id=feed_id, success=False, message="评论已点赞", verified=True,
            failure_code="comment_like_already_in_target_state",
        )
    page.click_nth_element(selector, int(before["index"]))
    after = page.evaluate(state_expression)
    count_increased = (
        isinstance(after, dict)
        and type(before.get("count")) is int
        and type(after.get("count")) is int
        and int(after["count"]) > int(before["count"])
    )
    if isinstance(after, dict) and (after.get("active") is True or count_increased):
        return ActionResult(
            feed_id=feed_id, success=True, message="评论点赞成功",
            action_dispatched=True, verified=True,
        )
    return ActionResult(
        feed_id=feed_id, success=False, message="评论点赞结果未知：状态未变化",
        action_dispatched=True, failure_code="comment_like_state_not_verified",
    )


def post_comment(page: Page, feed_id: str, xsec_token: str, content: str) -> None:
    """发表评论到 Feed。

    Args:
        page: CDP 页面对象。
        feed_id: Feed ID。
        xsec_token: xsec_token。
        content: 评论内容。

    Raises:
        RuntimeError: 评论失败。
    """
    url = make_feed_detail_url(feed_id, xsec_token)
    logger.info("打开 feed 详情页: %s", redact_url(url))

    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()

    _check_page_accessible(page)
    baseline_count = _count_exact_visible_text(page, content)

    # 点击评论输入触发区域
    if not page.has_element(COMMENT_INPUT_TRIGGER):
        raise RuntimeError("未找到评论输入框，该帖子可能不支持评论或网页端不可访问")

    page.click_element(COMMENT_INPUT_TRIGGER)

    # 输入评论内容（CDP 逐字输入）
    page.wait_for_element(COMMENT_INPUT_FIELD, timeout=5)
    page.input_content_editable(COMMENT_INPUT_FIELD, content)

    # 点击提交
    page.click_element(COMMENT_SUBMIT_BUTTON)

    if not _wait_for_exact_visible_text_increase(page, content, baseline_count):
        raise RuntimeError("评论发送结果未知：页面未读回完全相同的可见文本")

    logger.info("评论发送成功: feed=%s", feed_id)


def reply_comment(
    page: Page,
    feed_id: str,
    xsec_token: str,
    content: str,
    comment_id: str = "",
    user_id: str = "",
) -> None:
    """回复指定评论。

    通过 comment_id 或 user_id 定位评论，然后回复。

    Args:
        page: CDP 页面对象。
        feed_id: Feed ID。
        xsec_token: xsec_token。
        content: 回复内容。
        comment_id: 评论 ID（优先使用）。
        user_id: 用户 ID（备选）。

    Raises:
        RuntimeError: 回复失败。
    """
    if not comment_id:
        raise ValueError("comment_id is required for an exact reply target")

    url = make_feed_detail_url(feed_id, xsec_token)
    logger.info("打开 feed 详情页进行回复: %s", redact_url(url))

    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()

    _check_page_accessible(page)
    sleep_random(1500, 2500)

    # 查找目标评论
    comment_found = _find_and_scroll_to_comment(page, comment_id, user_id)
    if not comment_found:
        raise RuntimeError(f"未找到评论 (commentID: {comment_id}, userID: {user_id})")

    baseline_count = _count_exact_visible_text(
        page, content, parent_comment_id=comment_id
    )

    sleep_random(800, 1500)

    # 点击回复按钮
    reply_selector = f"#comment-{comment_id} {REPLY_BUTTON}"
    page.click_element(reply_selector)

    # 输入回复内容（CDP 逐字输入）
    page.wait_for_element(COMMENT_INPUT_FIELD, timeout=5)
    page.input_content_editable(COMMENT_INPUT_FIELD, content)

    # 点击提交
    page.click_element(COMMENT_SUBMIT_BUTTON)

    if not _wait_for_exact_visible_text_increase(
        page, content, baseline_count, parent_comment_id=comment_id
    ):
        raise RuntimeError("回复发送结果未知：页面未读回完全相同的可见文本")

    logger.info("回复评论成功")


def like_comment(
    page: Page,
    feed_id: str,
    xsec_token: str,
    *,
    comment_id: str,
) -> ActionResult:
    """Like one exact comment and verify its visible active state; never retry."""
    if not comment_id:
        raise ValueError("comment_id is required for exact comment like")
    url = make_feed_detail_url(feed_id, xsec_token)
    logger.info("打开 feed 详情页进行评论点赞: %s", redact_url(url))
    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()
    _check_page_accessible(page)
    if not _find_and_scroll_to_comment(page, comment_id, ""):
        return ActionResult(feed_id=feed_id, success=False, message="评论点赞失败：目标评论不存在")

    selector = _comment_like_control_selector(f"#comment-{comment_id}")
    state_expression = _comment_like_expression(selector)
    before = page.evaluate(state_expression)
    if (
        not isinstance(before, dict)
        or before.get("found") is not True
        or type(before.get("active")) is not bool
        or type(before.get("index")) is not int
        or int(before["index"]) < 0
    ):
        return ActionResult(feed_id=feed_id, success=False, message="评论点赞失败：未找到点赞控件")
    if before.get("active") is True:
        return ActionResult(feed_id=feed_id, success=True, message="评论已点赞")

    page.click_nth_element(selector, int(before["index"]))
    after = page.evaluate(state_expression)
    if isinstance(after, dict) and after.get("active") is True:
        return ActionResult(feed_id=feed_id, success=True, message="评论点赞成功")
    return ActionResult(feed_id=feed_id, success=False, message="评论点赞结果未知：状态未变化")


def _comment_like_control_selector(root_selector: str) -> str:
    return ", ".join(
        f'{root_selector} {suffix}'
        for suffix in (
            '[aria-label*="赞"]',
            'button[class*="like"]',
            '[role="button"][class*="like"]',
            '[class*="like-wrapper"]',
        )
    )


def _comment_like_expression(selector: str) -> str:
    return f"""
    (() => {{
      const candidates = Array.from(document.querySelectorAll({json.dumps(selector)}));
      const index = candidates.findIndex((node) => {{
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
      }});
      if (index < 0) return {{found: false, active: false, index: -1, count: null, countText: ''}};
      const control = candidates[index];
      const nodes = [control, ...Array.from(control.querySelectorAll('*'))];
      const active = nodes.some((node) => {{
        if (node.getAttribute?.('aria-pressed') === 'true') return true;
        const cls = String(node.className || '');
        if (/(^|[\\s_-])(liked|selected)([\\s_-]|$)/i.test(cls)) return true;
        const style = window.getComputedStyle(node);
        const colors = `${{style.color}}|${{style.fill}}|${{style.stroke}}`.toLowerCase();
        return colors.includes('rgb(255, 36, 66)') || colors.includes('#ff2442');
      }});
      const countNode = control.querySelector('.count');
      const countText = String((countNode || control).textContent || '').trim();
      const normalizedCount = countText.replace(/,/g, '');
      let count = null;
      if (normalizedCount === '' || normalizedCount === '赞') count = 0;
      else if (/^\\d+$/.test(normalizedCount)) count = Number(normalizedCount);
      return {{found: true, active, index, count, countText}};
    }})()
    """


def _find_and_scroll_to_comment(
    page: Page,
    comment_id: str,
    user_id: str,
    max_attempts: int = 12,
) -> bool:
    """查找并滚动到目标评论。"""
    if comment_id and re.fullmatch(r"[A-Za-z0-9_-]+", comment_id) is None:
        raise ValueError("comment_id contains unsupported selector characters")
    if user_id and re.fullmatch(r"[A-Za-z0-9_-]+", user_id) is None:
        raise ValueError("user_id contains unsupported selector characters")
    logger.info("开始查找评论 - commentID: %s, userID: %s", comment_id, user_id)

    # 先滚动到评论区
    page.scroll_element_into_view(".comments-container")
    sleep_random(800, 1500)

    # Reading the thread may leave the scroller at the end while the exact
    # requested comment is already present. Match it before the end marker.
    if comment_id:
        selector = f"#comment-{comment_id}"
        if page.has_element(selector):
            page.scroll_element_into_view(selector)
            return True

    last_count = 0
    stagnant = 0

    for attempt in range(max_attempts):
        # 检查是否到底
        if _check_end_container(page):
            logger.info("已到达评论底部，未找到目标评论")
            break

        # 停滞检测
        current_count = _get_comment_count(page)
        if current_count != last_count:
            last_count = current_count
            stagnant = 0
        else:
            stagnant += 1
        if stagnant >= 10:
            logger.info("评论数量停滞超过10次")
            break

        # 滚动到最后一条评论
        if current_count > 0:
            page.scroll_nth_element_into_view(PARENT_COMMENT, current_count - 1)
            sleep_random(200, 500)

        # One bounded semantic scroll per attempt.  The Bridge owns the actual
        # browser input event and pacing; this module never executes scroll JS.
        viewport_height = page.get_viewport_height()
        page.scroll_by(0, max(320, int(viewport_height * 0.7)))

        # 通过 commentID 查找
        if comment_id:
            selector = f"#comment-{comment_id}"
            if page.has_element(selector):
                logger.info("通过 commentID 找到评论 (尝试 %d 次)", attempt + 1)
                page.scroll_element_into_view(selector)
                return True

        # 通过 userID 查找
        if user_id:
            user_selector = f'[data-user-id="{user_id}"]'
            if page.has_element(user_selector):
                page.scroll_element_into_view(user_selector)
                logger.info("通过 userID 找到评论 (尝试 %d 次)", attempt + 1)
                return True

    return False


def _js_str(s: str) -> str:
    """将 Python 字符串转为 JS 字面量（含引号）。"""
    import json

    return json.dumps(s)


def _exact_visible_text_count_expression(
    content: str, *, parent_comment_id: str = ""
) -> str:
    """Count distinct published comment nodes, never editor/draft text."""
    expected = json.dumps(" ".join(content.split()), ensure_ascii=False)
    if parent_comment_id and re.fullmatch(r"[A-Za-z0-9_-]+", parent_comment_id) is None:
        raise ValueError("parent_comment_id contains unsupported selector characters")
    scope_selector = (
        f"#comment-{parent_comment_id}"
        if parent_comment_id
        else ".comments-container"
    )
    scope = json.dumps(scope_selector)
    return f"""
      (() => {{
        const expected = {expected};
        const scope = document.querySelector({scope});
        if (!scope) return -1;
        const nodes = Array.from(scope.querySelectorAll('p,span,div'));
        const publishedRoots = new Set();
        for (const node of nodes) {{
          const style = window.getComputedStyle(node);
          const rect = node.getBoundingClientRect();
          if (style.display === 'none' || style.visibility === 'hidden' || rect.width <= 0 || rect.height <= 0) continue;
          if (node.closest('[contenteditable="true"], textarea, input, .comment-input, .input-box, [class*="editor"]')) continue;
          const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
          if (text !== expected) continue;
          const root = node.closest('[id^="comment-"]');
          if (root && scope.contains(root)) publishedRoots.add(root);
        }}
        return publishedRoots.size;
      }})()
    """


def _count_exact_visible_text(
    page: Page, content: str, *, parent_comment_id: str = ""
) -> int:
    """Return distinct published-comment count or fail closed."""
    result = page.evaluate(_exact_visible_text_count_expression(
        content, parent_comment_id=parent_comment_id
    ))
    if type(result) is not int or result < 0:
        raise RuntimeError("评论可见文本基线读取无效")
    return result


def _wait_for_exact_visible_text_increase(
    page: Page,
    content: str,
    baseline_count: int,
    timeout: float = 8.0,
    parent_comment_id: str = "",
) -> bool:
    """Require a new exact visible occurrence; pre-existing text cannot verify a write."""
    if type(baseline_count) is not int or baseline_count < 0:
        raise ValueError("baseline_count must be a non-negative integer")
    expression = _exact_visible_text_count_expression(
        content, parent_comment_id=parent_comment_id
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_count = page.evaluate(expression)
        if type(current_count) is not int or current_count < 0:
            return False
        if current_count > baseline_count:
            return True
        sleep_random(500, 900)
    return False
