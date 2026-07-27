"""搜索 Feeds，对应 Go xiaohongshu/search.go。"""

from __future__ import annotations

import json
import logging
import re
import time

from .cdp import Page
from .errors import NoFeedsError
from .selectors import FILTER_BUTTON, FILTER_PANEL
from .types import Feed, FilterOption
from .urls import make_search_url

logger = logging.getLogger(__name__)

HOME_URL = "https://www.xiaohongshu.com/explore"
VISIBLE_SEARCH_SELECTOR = 'input[type="search"], input[placeholder*="搜索"], textarea.textarea, textarea[placeholder*="搜索"]'
VISIBLE_NOTE_LINK_SELECTOR = (
    "section.note-item a.title[href*='/explore/'], "
    "section.note-item a.title[href*='/search_result/'], "
    "a.title[href*='/explore/'], "
    "a.title[href*='/search_result/']"
)
_SAFE_NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

# 筛选选项映射表：{筛选组索引: [文本, ...]}
_FILTER_OPTIONS: dict[int, list[str]] = {
    1: ["综合", "最新", "最多点赞", "最多评论", "最多收藏"],
    2: ["不限", "视频", "图文"],
    3: ["不限", "一天内", "一周内", "半年内"],
    4: ["不限", "已看过", "未看过", "已关注"],
    5: ["不限", "同城", "附近"],
}

# 从 __INITIAL_STATE__ 提取搜索结果的 JS
_EXTRACT_SEARCH_JS = """
(() => {
    const s = window.__INITIAL_STATE__?.search;
    if (!s?.feeds) return "";
    const data = s.feeds.value !== undefined ? s.feeds.value : s.feeds._value;
    return data ? JSON.stringify(data) : "";
})()
"""

# The live session must follow the cards the user can actually see.  The
# internal search state may contain stale, duplicated or synthetic rows that
# have no corresponding card on the current result surface.
_EXTRACT_VISIBLE_SEARCH_JS = rf"""
(() => {{
    const selector = {json.dumps(VISIBLE_NOTE_LINK_SELECTOR)};
    const nodes = Array.from(document.querySelectorAll(selector));
    const seen = new Set();
    const rows = [];
    for (let selectorIndex = 0; selectorIndex < nodes.length; selectorIndex += 1) {{
        const node = nodes[selectorIndex];
        let url;
        try {{
            url = new URL(node.href || node.getAttribute('href') || '', location.origin);
        }} catch {{
            continue;
        }}
        const match = url.pathname.match(/^\/(?:explore|search_result)\/([0-9a-fA-F]{{24}})(?:\/|$)/);
        if (!match || seen.has(match[1])) continue;
        seen.add(match[1]);
        const title = String(node.getAttribute('title') || node.innerText || node.textContent || '')
            .replace(/\s+/g, ' ')
            .trim();
        rows.push({{
            id: match[1],
            index: selectorIndex,
            modelType: 'visible_dom_card',
            noteCard: {{ displayTitle: title }},
        }});
    }}
    return JSON.stringify(rows);
}})()
"""

_FIND_VISIBLE_SEARCH_INPUT_JS = rf"""
(() => {{
    const __tonyredbook_find_search_input = true;
    const selector = {json.dumps(VISIBLE_SEARCH_SELECTOR)};
    const nodes = Array.from(document.querySelectorAll(selector));
    const index = nodes.findIndex((node) => {{
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        if (!(rect.width > 50 && rect.height > 10
            && style.visibility !== 'hidden' && style.display !== 'none'
            && rect.bottom > 0 && rect.right > 0
            && rect.top < innerHeight && rect.left < innerWidth)) return false;
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(x, y);
        return Boolean(hit && (node === hit || node.contains(hit) || hit.contains(node)));
    }});
    if (index < 0) return {{ok: false, error: 'visible_search_input_missing'}};
    const node = nodes[index];
    const tag = String(node.localName || 'input');
    const candidates = [];
    if (node.id) candidates.push(`#${{CSS.escape(node.id)}}`);
    for (const attr of ['name', 'placeholder', 'aria-label']) {{
        const value = node.getAttribute(attr);
        if (value) candidates.push(`${{tag}}[${{attr}}=${{JSON.stringify(value)}}]`);
    }}
    const classes = Array.from(node.classList || []).filter(Boolean);
    if (classes.length) {{
        candidates.push(tag + classes.map((value) => `.${{CSS.escape(value)}}`).join(''));
    }}
    candidates.push(selector);
    for (const stableSelector of candidates) {{
        let stableNodes;
        try {{ stableNodes = Array.from(document.querySelectorAll(stableSelector)); }}
        catch {{ continue; }}
        const stableIndex = stableNodes.indexOf(node);
        if (stableIndex >= 0) {{
            return {{ok: true, selector: stableSelector, index: stableIndex}};
        }}
    }}
    return {{ok: false, error: 'stable_search_input_selector_missing'}};
}})()
"""

_READ_ACTIVE_SEARCH_INPUT_JS = rf"""
(() => {{
    const __tonyredbook_read_active_search = true;
    const selector = {json.dumps(VISIBLE_SEARCH_SELECTOR)};
    const el = document.activeElement;
    if (!el || !el.matches(selector)) return {{ok: false, error: 'search_input_not_focused'}};
    return {{ok: true, value: String(el.value || '')}};
}})()
"""


_FIND_VISIBLE_SEARCH_BUTTON_JS = """
(() => {
    const __tonyredbook_find_search_button = true;
    const selector = '.search-icon, [class*="search"] svg, button:has(svg)';
    const nodes = Array.from(document.querySelectorAll(selector));
    for (let index = 0; index < nodes.length; index += 1) {
        const node = nodes[index];
        {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            if (rect.width > 0 && rect.height > 0
                && style.visibility !== 'hidden' && style.display !== 'none') {
                return {ok: true, selector, index};
            }
        }
    }
    return {ok: false, error: 'visible_search_button_missing'};
})()
"""


def _find_internal_option(group_index: int, text: str) -> tuple[int, str]:
    """查找内部筛选选项。

    Returns:
        (filters_index, text)

    Raises:
        ValueError: 未找到匹配的选项。
    """
    options = _FILTER_OPTIONS.get(group_index)
    if not options:
        raise ValueError(f"筛选组 {group_index} 不存在")

    if text in options:
        return group_index, text

    raise ValueError(f"在筛选组 {group_index} 中未找到 '{text}'，有效值: {options}")


def _convert_filters(filter_opt: FilterOption) -> list[tuple[int, str]]:
    """将 FilterOption 转换为内部 (filters_index, text) 列表。"""
    result: list[tuple[int, str]] = []

    if filter_opt.sort_by:
        result.append(_find_internal_option(1, filter_opt.sort_by))
    if filter_opt.note_type:
        result.append(_find_internal_option(2, filter_opt.note_type))
    if filter_opt.publish_time:
        result.append(_find_internal_option(3, filter_opt.publish_time))
    if filter_opt.search_scope:
        result.append(_find_internal_option(4, filter_opt.search_scope))
    if filter_opt.location:
        result.append(_find_internal_option(5, filter_opt.location))

    return result


def search_feeds(
    page: Page,
    keyword: str,
    filter_option: FilterOption | None = None,
) -> list[Feed]:
    """搜索 Feeds。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        filter_option: 可选筛选条件。

    Raises:
        NoFeedsError: 没有捕获到搜索结果。
        ValueError: 筛选选项无效。
    """
    search_url = make_search_url(keyword)
    page.navigate(search_url)
    page.wait_for_load()
    page.wait_dom_stable()

    # 等待 __INITIAL_STATE__.search.feeds 有数据
    _wait_for_search_feeds(page)

    # 应用筛选条件（若有）
    if filter_option:
        internal_filters = _convert_filters(filter_option)
        if internal_filters:
            _apply_filters(page, internal_filters)

    # 提取搜索结果
    result = page.evaluate(_EXTRACT_SEARCH_JS)
    if not result:
        raise NoFeedsError()

    feeds_data = json.loads(result)
    if not feeds_data:
        raise NoFeedsError()

    return [Feed.from_dict(f) for f in feeds_data]


def search_feeds_visible(page: Page, keyword: str) -> list[Feed]:
    """Search once through the visible homepage input with Unicode-safe typing."""
    feeds, _ = search_feeds_visible_with_evidence(page, keyword)
    return feeds


def search_feeds_visible_with_evidence(
    page: Page, keyword: str
) -> tuple[list[Feed], dict[str, object]]:
    """Run one visible search and return ordinary-page normalization evidence."""
    value = keyword.strip()
    if not value or "\ufffd" in value or set(value) == {"?"}:
        raise ValueError("search keyword failed Unicode validation")
    context = page.get_page_context()
    risks = context.get("riskSignals", []) if isinstance(context, dict) else None
    if not isinstance(risks, list) or risks:
        raise RuntimeError("current page contains risk or unknown state; search is blocked")
    if context.get("pageType") != "home":
        page.navigate(HOME_URL)
        page.wait_for_load()
        page.wait_dom_stable()
        context = page.get_page_context()
        risks = context.get("riskSignals", []) if isinstance(context, dict) else None
        if not isinstance(risks, list) or risks:
            raise RuntimeError("homepage contains risk or unknown state")
    input_target = page.evaluate(_FIND_VISIBLE_SEARCH_INPUT_JS)
    if not isinstance(input_target, dict) or input_target.get("ok") is not True:
        raise RuntimeError("visible search input is unavailable")
    input_selector = str(input_target.get("selector") or VISIBLE_SEARCH_SELECTOR)
    input_index = int(input_target["index"])
    # The progressive editor primitive performs its own visible hit-test and
    # trusted focus click immediately before clearing and typing.  A separate
    # click here used to add a 10-15 second human pacing delay between locating
    # and editing; React can replace the search input during that delay and
    # make the previously visible nth index stale.
    page.input_content_editable(input_selector, value, index=input_index)
    typed_context = page.evaluate(_READ_ACTIVE_SEARCH_INPUT_JS)
    if (
        not isinstance(typed_context, dict)
        or typed_context.get("ok") is not True
        or str(typed_context.get("value", "")) != value
    ):
        raise RuntimeError("visible search input value mismatch before submit")
    # Submit through the focused editor.  The homepage contains several
    # unrelated visible SVG buttons, so guessing a broad search-button index
    # can click the wrong control after a layout change.  A trusted Enter key
    # is the stable, user-visible submission path used by the mature flow.
    page.press_key("Enter")
    page.wait_for_load()
    page.wait_dom_stable()
    submitted_context = page.get_page_context()
    if isinstance(submitted_context, dict) and submitted_context.get("pageType") == "home":
        raise RuntimeError("single visible search submission did not leave the homepage")
    return _read_current_search_results(page, value, allow_ai_normalization=True)


def adopt_current_search_results(page: Page, keyword: str) -> tuple[list[Feed], dict[str, object]]:
    """Adopt the already visible query without typing it again.

    Xiaohongshu may route a visible Enter search to ``/search_result_ai``.  The
    proven Ranfang flow does not process that page; it normalizes the same
    query, in the same bound tab, to the ordinary ``/search_result`` page.
    """
    return _read_current_search_results(page, keyword, allow_ai_normalization=True)


def _read_current_search_results(
    page: Page,
    keyword: str,
    *,
    allow_ai_normalization: bool,
) -> tuple[list[Feed], dict[str, object]]:
    value = keyword.strip()
    if not value or "\ufffd" in value or set(value) == {"?"}:
        raise ValueError("search keyword failed Unicode validation")
    context = page.get_page_context()
    if not isinstance(context, dict):
        raise RuntimeError("current search page context is unknown")
    risks = context.get("riskSignals", [])
    if not isinstance(risks, list) or risks:
        raise RuntimeError("current search page contains risk or unknown state")
    if str(context.get("query", "")) != value:
        raise RuntimeError("visible search query readback mismatch")

    pathname = str(context.get("pathname", ""))
    normalized_from_ai = (
        context.get("pageType") == "ai_search_results"
        or pathname == "/search_result_ai"
        or pathname.startswith("/search_result_ai/")
    )
    if normalized_from_ai:
        if not allow_ai_normalization:
            raise RuntimeError("AI search results are not an approved discovery surface")
        page.navigate(make_search_url(value))
        page.wait_for_load()
        page.wait_dom_stable()
        context = page.get_page_context()

    if not isinstance(context, dict) or context.get("pageType") != "search_results":
        page_type = str(context.get("pageType", "")) if isinstance(context, dict) else "unknown"
        pathname = str(context.get("pathname", "")) if isinstance(context, dict) else "unknown"
        raise RuntimeError(
            "visible search did not reach the ordinary search results page: "
            f"page_type={page_type}; pathname={pathname}"
        )
    if str(context.get("query", "")) != value:
        raise RuntimeError("ordinary search query readback mismatch")
    risks = context.get("riskSignals", [])
    if not isinstance(risks, list) or risks:
        raise RuntimeError("ordinary search results contain risk or unknown state")

    feeds = _read_visible_search_feeds(page)
    return feeds, {
        "normalized_from_ai": normalized_from_ai,
        "page_context": context,
        "candidate_source": "visible_dom_cards",
    }


def _read_visible_search_feeds(page: Page, timeout: float = 15.0) -> list[Feed]:
    """Read ordered, de-duplicated candidates from visible result cards only."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = page.evaluate(_EXTRACT_VISIBLE_SEARCH_JS)
        if result:
            try:
                rows = json.loads(result)
            except json.JSONDecodeError:
                rows = []
            if isinstance(rows, list):
                feeds: list[Feed] = []
                seen: set[str] = set()
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    note_id = str(item.get("id", ""))
                    if not _SAFE_NOTE_ID_RE.fullmatch(note_id) or note_id in seen:
                        continue
                    seen.add(note_id)
                    feeds.append(Feed.from_dict(item))
                if feeds:
                    return feeds
        time.sleep(0.3)
    raise NoFeedsError()


def _wait_for_search_feeds(page: Page, timeout: float = 15.0) -> None:
    """等待 __INITIAL_STATE__.search.feeds 有数据。

    Raises:
        NoFeedsError: 超时仍无数据。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = page.evaluate(_EXTRACT_SEARCH_JS)
        if result:
            try:
                if json.loads(result):
                    return
            except json.JSONDecodeError:
                pass
        time.sleep(0.3)
    raise NoFeedsError()


def _apply_filters(page: Page, filters: list[tuple[int, str]]) -> None:
    """Apply one visible filter action at a time with the live pacing gate."""
    for group_index, text in filters:
        page.click_element("div.filter")
        page.wait_for_element("div.filters-wrapper", timeout=5)
        expression = f"""
        (() => {{
          const wrapper = document.querySelector('div.filters-wrapper');
          if (!wrapper) return {{ok: false, error: '筛选面板不存在'}};
          const group = wrapper.querySelectorAll('div.filters')[{group_index - 1}];
          if (!group) return {{ok: false, error: '筛选分组不存在'}};
          const all = Array.from(wrapper.querySelectorAll('div.tags'));
          const option = Array.from(group.querySelectorAll('div.tags'))
            .find((node) => node.textContent.trim() === {json.dumps(text)});
          const index = all.indexOf(option);
          return index >= 0 ? {{ok: true, index}} : {{ok: false, error: '筛选选项不存在'}};
        }})()
        """
        result = page.evaluate(expression)
        if not isinstance(result, dict) or not result.get('ok'):
            raise ValueError(f"应用筛选失败: {(result or {}).get('error', '未知错误')}")
        option_index = int(result["index"])
        page.scroll_nth_element_into_view("div.filters-wrapper div.tags", option_index)
        page.click_nth_element("div.filters-wrapper div.tags", option_index)
        _wait_for_search_feeds(page)
