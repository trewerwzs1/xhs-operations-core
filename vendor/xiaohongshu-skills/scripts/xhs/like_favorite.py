"""点赞/收藏操作，对应 Go xiaohongshu/like_favorite.go。"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoFeedDetailError
from .selectors import COLLECT_BUTTON, LIKE_BUTTON
from .types import ActionResult
from .urls import make_feed_detail_url
from .current_page import require_current_note

logger = logging.getLogger(__name__)

_INSPECT_VISIBLE_LIKE_CONTROL_JS = """
(() => {
  const selectors = [
    '.interact-container .left .like-wrapper svg.like-icon',
    '.interact-container .left .like-wrapper .count',
    '.interact-container .left .like-lottie',
    '.interaction-container .left .like-lottie',
    '.engage-bar .like-wrapper',
    '.like-wrapper',
    '[class*="like"] [class*="like"]',
    'button[aria-label*="赞"]'
  ];
  const controls = [];
  for (const selector of selectors) {
    const nodes = Array.from(document.querySelectorAll(selector));
    for (let index = 0; index < nodes.length; index += 1) {
      const node = nodes[index];
      const rect = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      if (!(rect.width > 0 && rect.height > 0
        && rect.bottom > 0 && rect.right > 0
        && rect.top < innerHeight && rect.left < innerWidth
        && style.visibility !== 'hidden' && style.display !== 'none')) continue;
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      if (!hit || !(node === hit || node.contains(hit) || hit.contains(node))) continue;
      controls.push({
        selector,
        index,
        tag: String(node.localName || ''),
        className: String(node.className || '').slice(0, 240),
        ariaLabel: String(node.getAttribute('aria-label') || '').slice(0, 80),
        ariaPressed: String(node.getAttribute('aria-pressed') || ''),
        text: String(node.textContent || '').trim().slice(0, 80),
        computedColor: String(style.color || ''),
        computedFill: String(style.fill || ''),
        rect: {
          left: Math.round(rect.left), top: Math.round(rect.top),
          width: Math.round(rect.width), height: Math.round(rect.height),
        },
        hit: hit ? {
          tag: String(hit.localName || ''),
          className: String(hit.className || '').slice(0, 160),
        } : null,
        svg: (() => {
          const svg = node.matches('svg') ? node : node.querySelector('svg');
          const use = svg?.querySelector('use');
          return svg ? {
            className: String(svg.getAttribute('class') || '').slice(0, 160),
            fill: String(svg.getAttribute('fill') || ''),
            style: String(svg.getAttribute('style') || '').slice(0, 160),
            useHref: String(use?.getAttribute('href') || use?.getAttribute('xlink:href') || '').slice(0, 160),
          } : null;
        })(),
        wrapper: (() => {
          const wrapper = node.closest('.like-wrapper');
          const count = node.closest('.left')?.querySelector('.count');
          return {
            className: String(wrapper?.className || '').slice(0, 160),
            ariaPressed: String(wrapper?.getAttribute('aria-pressed') || ''),
            countText: String(count?.textContent || '').trim().slice(0, 40),
            color: wrapper ? String(getComputedStyle(wrapper).color || '') : '',
          };
        })(),
        ancestors: Array.from({length: 4}, (_, offset) => {
          let current = node;
          for (let step = 0; step <= offset; step += 1) current = current?.parentElement;
          return current ? {
            tag: String(current.localName || ''),
            className: String(current.className || '').slice(0, 240),
          } : null;
        }).filter(Boolean),
      });
    }
  }
  return controls.length
    ? {ok: true, controls}
    : {ok: false, controls: [], error: 'visible_like_control_missing'};
})()
"""

# 从 __INITIAL_STATE__ 读取互动状态的 JS
_GET_INTERACT_STATE_JS = """
(() => {
    if (window.__INITIAL_STATE__ &&
        window.__INITIAL_STATE__.note &&
        window.__INITIAL_STATE__.note.noteDetailMap) {
        return JSON.stringify(window.__INITIAL_STATE__.note.noteDetailMap);
    }
    return "";
})()
"""


def _get_interact_state(page: Page, feed_id: str) -> tuple[bool, bool]:
    """读取笔记的点赞/收藏状态。

    Returns:
        (liked, collected)

    Raises:
        NoFeedDetailError: 无法获取状态。
    """
    result = page.evaluate(_GET_INTERACT_STATE_JS)
    if not result:
        raise NoFeedDetailError()

    note_detail_map = json.loads(result)
    detail = note_detail_map.get(feed_id)
    if not detail and len(note_detail_map) == 1:
        detail = next(iter(note_detail_map.values()))

    if not detail:
        raise NoFeedDetailError()

    interact = detail.get("note", {}).get("interactInfo", {})
    return interact.get("liked", False), interact.get("collected", False)


def _prepare_page(page: Page, feed_id: str, xsec_token: str) -> None:
    """导航到 feed 详情页。"""
    url = make_feed_detail_url(feed_id, xsec_token)
    page.navigate(url)
    page.wait_for_load()
    page.wait_dom_stable()


# ========== 点赞 ==========


def like_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """点赞笔记（幂等：已点赞则跳过）。"""
    _prepare_page(page, feed_id, xsec_token)
    return _toggle_like(page, feed_id, target_liked=True)


def like_current_feed(page: Page, feed_id: str) -> ActionResult:
    """Like the already open note; never navigate and never click on unknown state."""
    require_current_note(page, feed_id)
    return _toggle_like(page, feed_id, target_liked=True)


def inspect_current_like_control(page: Page, feed_id: str) -> dict[str, object]:
    """Return sanitized visible-control diagnostics without clicking."""
    require_current_note(page, feed_id)
    try:
        liked, _ = _get_interact_state(page, feed_id)
        state_available = True
    except NoFeedDetailError:
        liked = False
        state_available = False
    control = page.evaluate(_INSPECT_VISIBLE_LIKE_CONTROL_JS)
    return {
        "noteId": feed_id,
        "stateAvailable": state_available,
        "liked": liked if state_available else None,
        "control": control if isinstance(control, dict) else {"ok": False},
        "platform_actions_executed": 0,
    }


def unlike_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """取消点赞（幂等：未点赞则跳过）。"""
    _prepare_page(page, feed_id, xsec_token)
    return _toggle_like(page, feed_id, target_liked=False)


def _toggle_like(page: Page, feed_id: str, target_liked: bool) -> ActionResult:
    """执行点赞/取消点赞操作。"""
    action_name = "点赞" if target_liked else "取消点赞"

    try:
        liked, _ = _get_interact_state(page, feed_id)
    except NoFeedDetailError:
        logger.error("无法读取互动状态，未知结果不点击")
        return ActionResult(
            feed_id=feed_id,
            success=False,
            message=f"{action_name}结果未知：无法读取当前状态",
            failure_code="like_state_unavailable",
        )

    # 幂等检查
    if liked == target_liked:
        logger.info("feed %s 已%s，跳过", feed_id, action_name)
        return ActionResult(
            feed_id=feed_id, success=False, message=f"已{action_name}",
            verified=True, failure_code="like_already_in_target_state",
        )

    # The note action bar must already be visible. Bind the one exact count
    # inside the like wrapper before dispatch. Its center is directly
    # hit-testable, while the icon is covered by the animation overlay.
    control = page.evaluate(_INSPECT_VISIBLE_LIKE_CONTROL_JS)
    exact_controls = (
        control.get("controls", [])
        if isinstance(control, dict) and control.get("ok") is True
        else []
    )
    exact_controls = [
        item for item in exact_controls
        if item.get("selector") == LIKE_BUTTON and item.get("index") == 0
    ]
    if len(exact_controls) != 1:
        return ActionResult(
            feed_id=feed_id,
            success=False,
            message=f"{action_name}失败：唯一可见点赞控件不可用",
            failure_code="like_control_not_clickable",
        )

    # Dispatch one click; unknown results fail closed and are never retried.
    try:
        page.click_nth_element(LIKE_BUTTON, 0)
    except Exception:
        return ActionResult(
            feed_id=feed_id,
            success=False,
            message=f"{action_name}失败：可见控件无法点击",
            failure_code="like_control_not_clickable",
        )

    # 验证
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            liked, _ = _get_interact_state(page, feed_id)
            if liked == target_liked:
                logger.info("feed %s %s成功", feed_id, action_name)
                return ActionResult(
                    feed_id=feed_id, success=True, message=f"{action_name}成功",
                    action_dispatched=True, verified=True,
                )
        except NoFeedDetailError:
            pass
        time.sleep(0.3)

    logger.error("feed %s %s结果未知，停止", feed_id, action_name)
    return ActionResult(
        feed_id=feed_id, success=False, message=f"{action_name}结果未知：状态未变化",
        action_dispatched=True, failure_code="like_state_not_verified",
    )


# ========== 收藏 ==========


def favorite_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """收藏笔记（幂等：已收藏则跳过）。"""
    _prepare_page(page, feed_id, xsec_token)
    return _toggle_favorite(page, feed_id, target_collected=True)


def unfavorite_feed(page: Page, feed_id: str, xsec_token: str) -> ActionResult:
    """取消收藏（幂等：未收藏则跳过）。"""
    _prepare_page(page, feed_id, xsec_token)
    return _toggle_favorite(page, feed_id, target_collected=False)


def _wait_collect_button(page: Page, timeout: float = 5.0, interval: float = 0.2) -> bool:
    """等待收藏按钮出现。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.has_element(COLLECT_BUTTON):
            return True
        time.sleep(interval)
    return False


def _wait_collected_state(
    page: Page,
    feed_id: str,
    target_collected: bool,
    timeout: float = 3.0,
    interval: float = 0.3,
) -> bool:
    """短轮询验证收藏状态是否达到目标。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, collected = _get_interact_state(page, feed_id)
            if collected == target_collected:
                return True
        except NoFeedDetailError:
            pass
        time.sleep(interval)
    return False


def _toggle_favorite(page: Page, feed_id: str, target_collected: bool) -> ActionResult:
    """执行收藏/取消收藏操作。"""
    action_name = "收藏" if target_collected else "取消收藏"

    try:
        _, collected = _get_interact_state(page, feed_id)
    except NoFeedDetailError:
        logger.warning("无法读取互动状态，直接点击")
        collected = not target_collected

    # 幂等检查
    if collected == target_collected:
        logger.info("feed %s 已%s，跳过", feed_id, action_name)
        return ActionResult(feed_id=feed_id, success=True, message=f"已{action_name}")

    if not _wait_collect_button(page, timeout=5.0):
        logger.error("feed %s 未找到收藏按钮: %s", feed_id, COLLECT_BUTTON)
        return ActionResult(
            feed_id=feed_id, success=False, message=f"{action_name}失败：未找到收藏按钮"
        )

    try:
        page.click_element(COLLECT_BUTTON)
    except Exception as e:
        logger.warning("feed %s 点击收藏按钮失败: %s", feed_id, e)
        return ActionResult(feed_id=feed_id, success=False, message=f"{action_name}失败：点击异常")

    if _wait_collected_state(page, feed_id, target_collected, timeout=3.0, interval=0.3):
        logger.info("feed %s %s成功", feed_id, action_name)
        return ActionResult(feed_id=feed_id, success=True, message=f"{action_name}成功")

    logger.error("feed %s %s未确认成功", feed_id, action_name)
    return ActionResult(feed_id=feed_id, success=False, message=f"{action_name}失败：状态未变化")
