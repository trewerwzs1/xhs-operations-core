"""BridgePage - 通过浏览器扩展 Bridge 实现与 CDP Page 相同的接口。

CLI 命令通过 WebSocket 发送到 bridge_server.py，
bridge_server 转发给浏览器扩展执行，结果原路返回。

每次调用都是一次短连接（发一条命令 → 收一条回复），
不需要维护持久连接。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any

import websockets.sync.client as ws_client

from .errors import CDPError, ElementNotFoundError
from .human import visible_action_delay

BRIDGE_URL = "ws://localhost:9333"


class BridgePage:
    """与 CDP Page 接口兼容的 Extension Bridge 实现。"""

    def __init__(self, bridge_url: str = BRIDGE_URL) -> None:
        self._bridge_url = bridge_url

    # ─── 内部通信 ───────────────────────────────────────────────

    def _call(self, method: str, params: dict | None = None) -> Any:
        """向 bridge server 发送一条命令并等待结果。"""
        msg: dict[str, Any] = {"role": "cli", "method": method}
        if params:
            msg["params"] = params
        try:
            connection = ws_client.connect(
                self._bridge_url,
                open_timeout=10,
                max_size=50 * 1024 * 1024,
            )
        except OSError as e:
            raise CDPError(f"无法连接到 bridge server（{self._bridge_url}）: {e}") from e
        try:
            with connection as ws:
                ws.send(json.dumps(msg, ensure_ascii=False))
                try:
                    raw = ws.recv(timeout=90)
                except TimeoutError as e:
                    raise CDPError(
                        f"Bridge 命令执行超时（90s）: {method}; "
                        "禁止立即重试，先按动作类型执行只读恢复或未知写入对账"
                    ) from e
        except OSError as e:
            raise CDPError(f"Bridge 传输中断（{self._bridge_url}）: {e}") from e

        resp = json.loads(raw)
        if "error" in resp and resp["error"]:
            raise CDPError(f"Bridge 错误: {resp['error']}")
        return resp.get("result")

    # ─── 导航 ───────────────────────────────────────────────────

    def navigate(self, url: str) -> None:
        self._call("navigate", {"url": url})
        visible_action_delay()

    def wait_for_load(self, timeout: float = 60.0) -> None:
        self._call("wait_for_load", {"timeout": int(timeout * 1000)})

    def wait_dom_stable(self, timeout: float = 10.0, interval: float = 0.5) -> None:
        self._call(
            "wait_dom_stable",
            {
                "timeout": int(timeout * 1000),
                "interval": int(interval * 1000),
            },
        )

    def get_page_context(self) -> dict[str, Any]:
        """Return only visible, non-sensitive page identity and risk state."""
        result = self._call("get_page_context")
        if not isinstance(result, dict):
            raise CDPError("Bridge 页面上下文无效")
        return result

    def bind_active_xhs_tab(self) -> dict[str, Any]:
        """Bind this Bridge session to the user's foreground Xiaohongshu tab."""
        result = self._call("bind_active_xhs_tab")
        if not isinstance(result, dict):
            raise CDPError("Bridge 活动标签绑定结果无效")
        return result

    def list_xhs_tabs(self) -> dict[str, Any]:
        """List only sanitized Xiaohongshu tab contexts without navigation."""
        result = self._call("list_xhs_tabs")
        if not isinstance(result, dict) or not isinstance(result.get("tabs"), list):
            raise CDPError("Bridge 小红书标签诊断结果无效")
        return result

    def go_back_and_verify(self, expected_query: str) -> dict[str, Any]:
        """Return once through browser history and verify the original search page."""
        result = self._call("go_back_and_verify", {"expectedQuery": expected_query})
        if not isinstance(result, dict):
            raise CDPError("Bridge 返回搜索页结果无效")
        visible_action_delay()
        return result

    def return_to_source_note(self, expected_note_id: str) -> dict[str, Any]:
        """Return once and verify the exact source note without URL fallback."""
        if re.fullmatch(r"[A-Za-z0-9_-]+", expected_note_id) is None:
            raise CDPError("Bridge 原帖ID无效")
        result = self._call(
            "return_to_source_note", {"expectedNoteId": expected_note_id}
        )
        if not isinstance(result, dict):
            raise CDPError("Bridge 返回原帖结果无效")
        visible_action_delay()
        return result

    def return_to_profile(self, expected_profile_id: str) -> dict[str, Any]:
        """Return once and verify the exact user profile without URL fallback."""
        if expected_profile_id and re.fullmatch(r"[A-Za-z0-9_-]+", expected_profile_id) is None:
            raise CDPError("Bridge 用户主页ID无效")
        result = self._call(
            "return_to_profile", {"expectedProfileId": expected_profile_id}
        )
        if not isinstance(result, dict):
            raise CDPError("Bridge 返回用户主页结果无效")
        visible_action_delay()
        return result

    def open_search_result(self, expected_note_id: str) -> dict[str, Any]:
        """Click one known candidate in the current search batch and verify it."""
        if re.fullmatch(r"[0-9a-fA-F]{24}", expected_note_id) is None:
            raise CDPError("Bridge 搜索候选ID无效")
        before = self.get_page_context()
        if before.get("pageType") != "search_results" or before.get("riskSignals"):
            raise CDPError("Bridge 当前页面不是健康搜索结果页")
        selector = f'a[href*="{expected_note_id}"]'
        target = self.evaluate(
            f"""
            (() => {{
                const __tonyredbook_find_exact_candidate = true;
                const selector = {json.dumps(selector)};
                const nodes = Array.from(document.querySelectorAll(selector));
                const index = nodes.findIndex((node) => {{
                    const url = new URL(node.href || node.getAttribute('href') || '', location.origin);
                    const match = url.pathname.match(/^\\/(?:explore|search_result)\\/([0-9a-fA-F]{{24}})(?:\\/|$)/);
                    const rect = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return match?.[1] === {json.dumps(expected_note_id)}
                        && rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden' && style.display !== 'none';
                }});
                return index >= 0
                    ? {{ok: true, index, href: String(nodes[index].href || '')}}
                    : {{ok: false}};
            }})()
            """
        )
        if not isinstance(target, dict) or target.get("ok") is not True:
            raise CDPError("Bridge 当前搜索批次中未找到可见目标候选")
        target_index = int(target["index"])
        self.scroll_nth_element_into_view(selector, target_index)
        self.click_nth_element(selector, target_index)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            context = self.get_page_context()
            if (
                context.get("pageType") == "note_detail"
                and str(context.get("noteId", "")) == expected_note_id
            ):
                return context
            time.sleep(0.3)
        raise CDPError("Bridge 真实点击后页面身份未能验证；禁止改用直链重试")

    def get_navigation_count(self) -> dict[str, int]:
        result = self._call("get_navigation_count")
        if not isinstance(result, dict):
            raise CDPError("Bridge 导航计数无效")
        return {
            "forward": int(result.get("forward", 0)),
            "back": int(result.get("back", 0)),
        }

    # ─── JavaScript 执行 ────────────────────────────────────────

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        return self._call("evaluate", {"expression": expression})

    def evaluate_function(self, function_body: str, *args: Any) -> Any:
        return self._call("evaluate", {"expression": f"({function_body})()"})

    # ─── 元素查询 ────────────────────────────────────────────────

    def query_selector(self, selector: str) -> str | None:
        """返回 "found" 表示元素存在，None 表示不存在（兼容 CDP 的 objectId 语义）。"""
        found = self._call("has_element", {"selector": selector})
        return "found" if found else None

    def query_selector_all(self, selector: str) -> list[str]:
        count = self.get_elements_count(selector)
        return ["found"] * count

    def has_element(self, selector: str) -> bool:
        return bool(self._call("has_element", {"selector": selector}))

    def wait_for_element(self, selector: str, timeout: float = 30.0) -> str:
        found = self._call(
            "wait_for_selector",
            {
                "selector": selector,
                "timeout": int(timeout * 1000),
            },
        )
        if not found:
            raise ElementNotFoundError(selector)
        return "found"

    # ─── 元素操作 ────────────────────────────────────────────────

    def click_element(self, selector: str) -> None:
        self._call("click_element", {"selector": selector})
        visible_action_delay()

    def click_nth_element(self, selector: str, index: int) -> None:
        if type(index) is not int or index < 0:
            raise ValueError("click_nth_element index must be a non-negative integer")
        self._call("click_nth_element", {"selector": selector, "index": index})
        visible_action_delay()

    def click_element_by_text(self, selector: str, text: str) -> None:
        self._call("click_element_by_text", {"selector": selector, "text": text})
        visible_action_delay()

    def input_text(self, selector: str, text: str) -> None:
        self.input_content_editable(selector, text)

    def input_content_editable(self, selector: str, text: str, *, index: int = 0) -> None:
        if not isinstance(text, str) or not text:
            raise ValueError("contenteditable text must be a non-empty string")
        if type(index) is not int or index < 0:
            raise ValueError("contenteditable index must be non-negative")
        result = self._call(
            "input_content_editable_progressive",
            {"selector": selector, "index": index, "text": text},
        )
        if not isinstance(result, dict) or result.get("verified") is not True:
            raise CDPError("Bridge progressive input did not verify the exact editor value")
        visible_action_delay()

    def get_element_text(self, selector: str) -> str | None:
        return self._call("get_element_text", {"selector": selector})

    def get_element_attribute(self, selector: str, attr: str) -> str | None:
        return self._call("get_element_attribute", {"selector": selector, "attr": attr})

    def get_elements_count(self, selector: str) -> int:
        result = self._call("get_elements_count", {"selector": selector})
        return int(result) if result is not None else 0

    def remove_element(self, selector: str) -> None:
        self._call("remove_element", {"selector": selector})

    def hover_element(self, selector: str) -> None:
        self._call("hover_element", {"selector": selector})
        visible_action_delay()

    def select_all_text(self, selector: str) -> None:
        self._call("select_all_text", {"selector": selector})
        visible_action_delay()

    # ─── 滚动 ────────────────────────────────────────────────────

    def scroll_by(self, x: int, y: int) -> None:
        self._call("semantic_scroll", {"mode": "delta", "x": x, "y": y})
        visible_action_delay()

    def scroll_to(self, x: int, y: int) -> None:
        self._call("semantic_scroll", {"mode": "absolute", "x": x, "y": y})
        visible_action_delay()

    def scroll_to_bottom(self) -> None:
        self._call("semantic_scroll", {"mode": "bottom"})
        visible_action_delay()

    def scroll_element_into_view(self, selector: str) -> None:
        self._call("semantic_scroll", {"mode": "element", "selector": selector})
        visible_action_delay()

    def scroll_nth_element_into_view(self, selector: str, index: int) -> None:
        if type(index) is not int or index < 0:
            raise ValueError("scroll_nth_element_into_view index must be non-negative")
        self._call(
            "semantic_scroll",
            {"mode": "nth_element", "selector": selector, "index": index},
        )
        visible_action_delay()

    def get_scroll_top(self) -> int:
        result = self._call("get_scroll_top")
        return int(result) if result is not None else 0

    def get_viewport_height(self) -> int:
        result = self._call("get_viewport_height")
        return int(result) if result is not None else 768

    # ─── 输入事件 ────────────────────────────────────────────────

    def press_key(self, key: str) -> None:
        self._call("press_key", {"key": key})
        visible_action_delay()

    def type_text(self, text: str, delay_ms: int = 50) -> None:
        self._call("type_text", {"text": text, "delayMs": delay_ms})
        visible_action_delay()

    def mouse_move(self, x: float, y: float) -> None:
        self._call("pointer_event", {"mode": "move", "x": x, "y": y})
        visible_action_delay()

    def mouse_click(self, x: float, y: float, button: str = "left") -> None:
        if button != "left":
            raise ValueError("only the approved left-button pointer action is supported")
        self._call("pointer_event", {"mode": "click", "x": x, "y": y})
        visible_action_delay()

    def dispatch_wheel_event(self, delta_y: float) -> None:
        self._call("semantic_scroll", {"mode": "delta", "x": 0, "y": delta_y})
        visible_action_delay()

    def simulate_reading_mouse(self, duration_ms: int = 12_000) -> None:
        """Run one bounded visible reading step through the extension.

        Reading is a required semantic action.  A missing extension capability
        is therefore an error instead of an optional best-effort decoration.
        """
        if type(duration_ms) is not int or not 10_000 <= duration_ms <= 15_000:
            raise ValueError("reading duration must be between 10000 and 15000 ms")
        result = self._call("simulate_reading", {"durationMs": duration_ms})
        if not isinstance(result, dict) or result.get("completed") is not True:
            raise CDPError("Bridge reading step did not complete")

    # ─── 文件上传 ────────────────────────────────────────────────

    def set_file_input(self, selector: str, files: list[str]) -> None:
        """通过 chrome.debugger + DOM.setFileInputFiles 上传本地文件。
        传递绝对路径给扩展，由扩展调用 CDP 完成上传（与原 CDP 方式等价）。
        """
        # 统一转换为绝对路径（兼容 Windows 反斜杠）
        abs_paths = [os.path.abspath(path) for path in files]
        self._call("set_file_input", {"selector": selector, "files": abs_paths})

    # ─── 截图 ────────────────────────────────────────────────────

    def screenshot_element(self, selector: str, padding: int = 0) -> bytes:
        result = self._call("screenshot_element", {"selector": selector, "padding": padding})
        if result and result.get("data"):
            return base64.b64decode(result["data"])
        return b""


    # ─── 无操作（原 CDP 专有功能，扩展模式不需要） ─────────────────

    def inject_stealth(self) -> None:
        """Compatibility no-op; this Bridge does not inject page scripts."""

    # ─── 兼容性辅助方法 ──────────────────────────────────────────

    def is_server_running(self) -> bool:
        """检查 bridge server 是否在运行（不需要 extension 已连接）。"""
        try:
            with ws_client.connect(self._bridge_url, open_timeout=3) as ws:
                ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                raw = ws.recv(timeout=5)
            resp = json.loads(raw)
            return "result" in resp
        except Exception:
            return False

    def is_extension_connected(self) -> bool:
        """检查浏览器扩展是否已连接到 bridge server。"""
        try:
            with ws_client.connect(self._bridge_url, open_timeout=3) as ws:
                ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                raw = ws.recv(timeout=5)
            resp = json.loads(raw)
            return bool(resp.get("result", {}).get("extension_connected"))
        except Exception:
            return False

    @property
    def target_id(self) -> str:
        """兼容旧代码对 page.target_id 的引用。"""
        return "extension-bridge"
