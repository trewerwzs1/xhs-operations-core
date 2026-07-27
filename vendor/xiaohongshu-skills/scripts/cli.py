"""统一 CLI 入口（Extension Bridge 版本）

通过浏览器扩展 Bridge 连接用户已打开的浏览器，无需 Chrome 调试端口。
先启动 bridge_server.py，并在浏览器中安装 XHS Bridge 扩展，再运行此 CLI。

输出: JSON（ensure_ascii=False）
退出码: 0=成功, 1=未登录, 2=错误
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from urllib.parse import urlsplit, urlunsplit

# Windows 控制台默认编码（如 cp1252）不支持中文，强制 UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("xhs-cli")

XSEC_TOKEN_ENV = "TONYREDBOOK_XSEC_TOKEN"
PRIVATE_PIPE_ENV = "TONYREDBOOK_PRIVATE_PIPE"
CAPABILITY_COMMAND_ENV = "TONYREDBOOK_CAPABILITY_COMMAND"
_APPROVED_GATEWAY_COMMANDS = frozenset({
    "search-feeds-visible",
    "adopt-search-results",
    "capture-own-reply-history",
    "current-account-identity",
    "open-own-profile",
    "open-commenter-profile",
    "open-dm-conversation",
    "capture-current-dm-conversation",
    "send-current-dm-message",
    "open-service-inbox",
    "capture-service-inbox",
    "open-service-item",
    "return-to-source-comment",
    "page-context",
    "bind-active-xhs-tab",
    "list-xhs-tabs",
    "get-current-feed-detail",
    "inspect-current-like-control",
    "inspect-current-comment-controls",
    "go-back-and-verify",
    "open-search-result",
    "like-current-feed",
    "post-comment-current",
    "like-current-comment",
    "reply-current-comment",
})
_SENSITIVE_OUTPUT_KEYS = {
    "xsectoken", "xsec_token", "cookie", "cookies", "web_session", "a1", "xs",
    "authorization", "requestheaders", "request_headers", "responseheaders", "response_headers",
}


def _token_default() -> str:
    """Read the transient token from the environment, never process argv."""
    return os.environ.get(XSEC_TOKEN_ENV, "")


def _require_token(args: argparse.Namespace) -> str:
    token = str(getattr(args, "xsec_token", "") or "")
    if not token:
        raise ValueError(f"missing transient token in {XSEC_TOKEN_ENV}")
    return token


def _require_product_gateway(args: argparse.Namespace) -> None:
    """Reject every direct or legacy vendor CLI invocation before connection.

    The product capability registry is the only supported public operation
    surface.  The vendor CLI is retained solely as its private subprocess
    transport and must receive the exact command selected by that gateway.
    """
    command = str(getattr(args, "command", "") or "")
    if os.environ.get(PRIVATE_PIPE_ENV) != "1":
        raise PermissionError("vendor CLI is private; use the product XHS operation gateway")
    authorized = os.environ.get(CAPABILITY_COMMAND_ENV, "")
    if command not in _APPROVED_GATEWAY_COMMANDS:
        raise PermissionError(f"vendor command is frozen or unsupported: {command}")
    if authorized != command:
        raise PermissionError("vendor capability authorization mismatch")


def _sanitize_output(value):
    if isinstance(value, dict):
        clean = {}
        sensitive = {key.replace("_", "").replace("-", "") for key in _SENSITIVE_OUTPUT_KEYS}
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in sensitive:
                continue
            clean[key] = _sanitize_output(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_output(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


# ─── 输出工具 ────────────────────────────────────────────────────────────────


def _output(data: dict, exit_code: int = 0) -> None:
    output = data if os.environ.get(PRIVATE_PIPE_ENV) == "1" else _sanitize_output(data)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _open_file_if_display(path: str) -> None:
    """有桌面时用系统默认程序打开文件。"""
    import platform
    import subprocess

    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)
        elif system == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        logger.debug("无法自动打开文件: %s", path)


# ─── Bridge 连接 ──────────────────────────────────────────────────────────────


class _DummyBrowser:
    """空 browser 对象，保持与旧代码的兼容性。"""

    def close(self) -> None:
        pass

    def close_page(self, page) -> None:
        pass


def _ensure_bridge_ready(bridge_url: str) -> None:
    """确保 bridge server 在运行、浏览器扩展已连接。若未就绪则自动启动。"""
    import subprocess
    import time
    from pathlib import Path

    from xhs.bridge import BridgePage

    page = BridgePage(bridge_url)

    # ── 1. 检查 bridge server ────────────────────────────────────────
    if not page.is_server_running():
        logger.info("Bridge server 未运行，正在启动...")
        scripts_dir = Path(__file__).parent
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [sys.executable, str(scripts_dir / "bridge_server.py")],
            **kwargs,
        )
        for _ in range(10):
            time.sleep(1)
            if page.is_server_running():
                logger.info("Bridge server 已启动")
                break
        else:
            logger.warning("Bridge server 启动超时，请手动运行 bridge_server.py")
            return

    # ── 2. 检查扩展是否连接 ──────────────────────────────────────────
    if page.is_extension_connected():
        return

    logger.info("浏览器扩展未连接，正在打开 Chrome...")
    _open_chrome()

    for _ in range(20):
        time.sleep(1)
        if page.is_extension_connected():
            logger.info("浏览器扩展已连接")
            return
    logger.warning("等待扩展连接超时，请确认 Chrome 已安装 XHS Bridge 扩展并已启用")


def _open_chrome() -> None:
    """启动绑定的 Tonyredbook Chrome profile，并加载包内 XHS Bridge。"""
    import subprocess
    from pathlib import Path

    vendor_root = Path(__file__).resolve().parents[1]
    extension_dir = Path(
        os.environ.get("TONYREDBOOK_EXTENSION_DIR", str(vendor_root / "extension"))
    ).resolve()
    profile_value = os.environ.get("TONYREDBOOK_CHROME_PROFILE", "").strip()
    profile_dir = Path(profile_value).resolve() if profile_value else None
    if not (extension_dir / "manifest.json").is_file():
        raise RuntimeError(f"XHS Bridge extension is missing: {extension_dir}")

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            # XHS Bridge is installed once through chrome://extensions and is
            # persisted in the dedicated profile. Current stable Chrome can
            # suppress an unpacked extension's UI/service worker when it is
            # relaunched with the legacy extension command-line flags.
            args = [path, "--no-first-run"]
            if profile_dir is not None:
                profile_dir.mkdir(parents=True, exist_ok=True)
                args.append(f"--user-data-dir={profile_dir}")
            args.append("https://www.xiaohongshu.com/explore")
            subprocess.Popen(args)
            return
    # macOS / Linux fallback
    for cmd in [["open", "-a", "Google Chrome"], ["google-chrome"], ["chromium-browser"]]:
        try:
            subprocess.Popen(cmd)
            return
        except FileNotFoundError:
            continue
    logger.warning("找不到 Chrome，请手动打开浏览器")


def _connect(args: argparse.Namespace):
    """返回 (browser, page)，browser 为空对象，page 通过 Extension Bridge 操作浏览器。"""
    from xhs.bridge import BridgePage

    bridge_url = getattr(args, "bridge_url", "ws://localhost:9333")
    _ensure_bridge_ready(bridge_url)
    return _DummyBrowser(), BridgePage(bridge_url)


# _connect_saved_tab / _connect_existing 在 bridge 模式下与 _connect 等价
_connect_saved_tab = _connect
_connect_existing = _connect


# ─── 子命令实现 ───────────────────────────────────────────────────────────────


def _qrcode_fallback(browser, page, args: argparse.Namespace) -> None:
    """频率限制时刷新页面返回二维码。"""
    from xhs.login import fetch_qrcode, make_qrcode_url, save_qrcode_to_file
    from xhs.urls import EXPLORE_URL

    page.navigate(EXPLORE_URL)
    page.wait_for_load()

    png_bytes, _b64_orig, already = fetch_qrcode(page)
    if already:
        _output({"logged_in": True, "message": "已登录"})
        return

    qrcode_path = save_qrcode_to_file(png_bytes)
    image_url, login_url = make_qrcode_url(png_bytes)
    _open_file_if_display(qrcode_path)

    result: dict = {
        "logged_in": False,
        "login_method": "qrcode",
        "qrcode_path": qrcode_path,
        "qrcode_image_url": image_url,
        "message": "验证码发送受限，已切换为二维码登录，请扫码。扫码后运行 wait-login 等待登录结果。",
    }
    if login_url:
        result["qr_login_url"] = login_url
    _output(result, exit_code=1)


def cmd_check_login(args: argparse.Namespace) -> None:
    """检查登录状态，未登录时自动获取二维码。"""
    from xhs.login import fetch_qrcode, make_qrcode_url, save_qrcode_to_file

    browser, page = _connect(args)
    try:
        png_bytes, _b64_orig, already = fetch_qrcode(page)
        if already:
            _output({"logged_in": True}, exit_code=0)
            return

        qrcode_path = save_qrcode_to_file(png_bytes)
        image_url, login_url = make_qrcode_url(png_bytes)
        _open_file_if_display(qrcode_path)

        result: dict = {
            "logged_in": False,
            "login_method": "qrcode",
            "qrcode_path": qrcode_path,
            "qrcode_image_url": image_url,
            "hint": "未登录，二维码已自动生成。扫码后运行 wait-login 等待登录结果",
        }
        if login_url:
            result["qr_login_url"] = login_url
        _output(result, exit_code=1)
    finally:
        browser.close()


def cmd_login(args: argparse.Namespace) -> None:
    """登录（扫码，阻塞等待完成）。"""
    from xhs.login import fetch_qrcode, make_qrcode_url, save_qrcode_to_file, wait_for_login

    browser, page = _connect(args)
    try:
        png_bytes, _b64_orig, already = fetch_qrcode(page)
        if already:
            _output({"logged_in": True, "message": "已登录"})
            return

        qrcode_path = save_qrcode_to_file(png_bytes)
        image_url, login_url = make_qrcode_url(png_bytes)
        _open_file_if_display(qrcode_path)

        result: dict = {"qrcode_path": qrcode_path, "qrcode_image_url": image_url}
        if login_url:
            result["qr_login_url"] = login_url
        logger.info("二维码已生成，等待扫码...")

        success = wait_for_login(page, timeout=120)
        _output(
            {"logged_in": success, "message": "登录成功" if success else "等待超时"},
            exit_code=0 if success else 2,
        )
    finally:
        browser.close()


def cmd_get_qrcode(args: argparse.Namespace) -> None:
    """获取登录二维码截图并立即返回（非阻塞）。"""
    from xhs.login import fetch_qrcode, make_qrcode_url, save_qrcode_to_file

    browser, page = _connect(args)
    try:
        png_bytes, _b64_orig, already = fetch_qrcode(page)
        if already:
            browser.close_page(page)
            browser.close()
            _output({"logged_in": True, "message": "已登录"})
            return

        qrcode_path = save_qrcode_to_file(png_bytes)
        image_url, login_url = make_qrcode_url(png_bytes)
        _open_file_if_display(qrcode_path)
        browser.close()

        result: dict = {
            "qrcode_path": qrcode_path,
            "qrcode_image_url": image_url,
            "message": "二维码已生成，请扫码登录。扫码后运行 wait-login 等待登录结果。",
        }
        if login_url:
            result["qr_login_url"] = login_url
        _output(result)
    finally:
        pass


def cmd_wait_login(args: argparse.Namespace) -> None:
    """等待扫码登录完成（配合 get-qrcode 使用）。"""
    from xhs.login import wait_for_login

    browser, page = _connect_saved_tab(args)
    try:
        success = wait_for_login(page, timeout=args.timeout)
        _output(
            {
                "logged_in": success,
                "message": "登录成功" if success else "等待超时，请重新运行 get-qrcode 获取新二维码",
            },
            exit_code=0 if success else 2,
        )
    finally:
        browser.close()


def cmd_phone_login(args: argparse.Namespace) -> None:
    """手机号+验证码登录（交互式）。"""
    from xhs.errors import RateLimitError
    from xhs.login import send_phone_code, submit_phone_code

    browser, page = _connect(args)
    try:
        sent = send_phone_code(page, args.phone)
        if not sent:
            _output({"logged_in": True, "message": "已登录，无需重新登录"})
            return

        code = args.code
        if not code:
            code = input("请输入收到的短信验证码: ").strip()

        success = submit_phone_code(page, code)
        _output(
            {"logged_in": success, "message": "登录成功" if success else "验证码错误或超时"},
            exit_code=0 if success else 2,
        )
    except RateLimitError:
        _qrcode_fallback(browser, page, args)
    finally:
        browser.close()


def cmd_send_code(args: argparse.Namespace) -> None:
    """分步登录第一步：发送手机验证码。"""
    from xhs.errors import RateLimitError
    from xhs.login import send_phone_code

    browser, page = _connect(args)
    try:
        sent = send_phone_code(page, args.phone)
        if not sent:
            _output({"logged_in": True, "message": "已登录，无需重新登录"})
            return
        _output({
            "status": "code_sent",
            "message": (
                f"验证码已发送至 {args.phone[:3]}****{args.phone[-4:]}，"
                "请运行 verify-code --code <验证码>"
            ),
        })
    except RateLimitError:
        _qrcode_fallback(browser, page, args)
    finally:
        browser.close()


def cmd_verify_code(args: argparse.Namespace) -> None:
    """分步登录第二步：填写验证码并提交。"""
    from xhs.login import submit_phone_code

    browser, page = _connect_saved_tab(args)
    try:
        success = submit_phone_code(page, args.code)
        _output(
            {"logged_in": success, "message": "登录成功" if success else "验证码错误或超时"},
            exit_code=0 if success else 2,
        )
    finally:
        browser.close()




def cmd_list_feeds(args: argparse.Namespace) -> None:
    """获取首页 Feed 列表。"""
    from xhs.feeds import list_feeds

    browser, page = _connect(args)
    try:
        feeds = list_feeds(page)
        _output({"feeds": [f.to_dict() for f in feeds], "count": len(feeds)})
    finally:
        browser.close()


def cmd_search_feeds(args: argparse.Namespace) -> None:
    """搜索 Feeds。"""
    from xhs.search import search_feeds
    from xhs.types import FilterOption

    filter_opt = FilterOption(
        sort_by=args.sort_by or "",
        note_type=args.note_type or "",
        publish_time=args.publish_time or "",
        search_scope=args.search_scope or "",
        location=args.location or "",
    )

    browser, page = _connect(args)
    try:
        feeds = search_feeds(page, args.keyword, filter_opt)
        _output({"feeds": [f.to_dict() for f in feeds], "count": len(feeds)})
    finally:
        browser.close()


def cmd_search_feeds_visible(args: argparse.Namespace) -> None:
    from xhs.search import search_feeds_visible_with_evidence

    browser, page = _connect(args)
    try:
        feeds, evidence = search_feeds_visible_with_evidence(page, args.keyword)
        _output({"feeds": [item.to_dict() for item in feeds], "count": len(feeds), **evidence})
    finally:
        browser.close()


def cmd_adopt_search_results(args: argparse.Namespace) -> None:
    """Read the current query batch without typing or submitting it again."""
    from xhs.search import adopt_current_search_results

    browser, page = _connect(args)
    try:
        feeds, evidence = adopt_current_search_results(page, args.keyword)
        _output(
            {
                "feeds": [item.to_dict() for item in feeds],
                "count": len(feeds),
                "adopted_current_query": True,
                **evidence,
            }
        )
    finally:
        browser.close()


def cmd_get_feed_detail(args: argparse.Namespace) -> None:
    """获取 Feed 详情。"""
    from xhs.feed_detail import get_feed_detail
    from xhs.types import CommentLoadConfig

    config = CommentLoadConfig(
        click_more_replies=args.click_more_replies,
        max_replies_threshold=args.max_replies_threshold,
        max_comment_items=args.max_comment_items,
        scroll_speed=args.scroll_speed,
    )

    browser, page = _connect(args)
    try:
        detail = get_feed_detail(
            page,
            args.feed_id,
            _require_token(args),
            load_all_comments=args.load_all_comments,
            config=config,
            keyword=getattr(args, "keyword", "篮球"),
        )
        _output(detail.to_dict())
    except Exception as e:
        # 附带 404 诊断事件，帮助定位根因
        diagnostics: list = []
        try:
            diagnostics = page.get_404_diagnostics() or []
        except Exception:
            pass
        err_data: dict = {"success": False, "error": str(e)}
        if diagnostics:
            latest = diagnostics[-1]
            err_data["diagnosis"] = {
                "root_cause": latest.get("diagnosis", {}).get("root_cause"),
                "cause_category": latest.get("diagnosis", {}).get("cause_category"),
                "detail": latest.get("diagnosis", {}).get("detail"),
                "how_xhs_decides": latest.get("diagnosis", {}).get("how_xhs_decides"),
                "url": latest.get("url"),
                "final_url": latest.get("final_url"),
            }
        _output(err_data, exit_code=2)
    finally:
        browser.close()


def cmd_user_profile(args: argparse.Namespace) -> None:
    """获取用户主页。"""
    from xhs.user_profile import get_user_profile

    browser, page = _connect(args)
    try:
        profile = get_user_profile(page, args.user_id, _require_token(args))
        _output(profile.to_dict())
    finally:
        browser.close()


def cmd_page_context(args: argparse.Namespace) -> None:
    """Read visible page identity and risk state without navigation."""
    browser, page = _connect(args)
    try:
        _output(page.get_page_context())
    finally:
        browser.close()


def cmd_capture_own_reply_history(args: argparse.Namespace) -> None:
    """Read a bounded batch of replies from the currently visible own profile."""
    from xhs.own_reply_history import capture_own_reply_history

    browser, page = _connect(args)
    try:
        _output(capture_own_reply_history(
            page,
            start_position=args.start_position,
            max_notes=args.max_notes,
            max_comment_items=args.max_comment_items,
        ))
    finally:
        browser.close()


def cmd_open_own_profile(args: argparse.Namespace) -> None:
    """Open and verify the logged-in account's own profile."""
    from xhs.navigation import open_own_profile

    browser, page = _connect(args)
    try:
        _output(open_own_profile(page))
    finally:
        browser.close()


def cmd_current_account_identity(args: argparse.Namespace) -> None:
    """Read the visible exact own-account sidebar identity."""
    from xhs.navigation import read_current_account_identity

    browser, page = _connect(args)
    try:
        _output(read_current_account_identity(page))
    finally:
        browser.close()


def cmd_open_commenter_profile(args: argparse.Namespace) -> None:
    """Open and verify the exact visible comment author profile."""
    from xhs.navigation import open_commenter_profile

    browser, page = _connect(args)
    try:
        _output(open_commenter_profile(
            page,
            feed_id=args.feed_id,
            comment_id=args.comment_id,
            expected_user_id=args.expected_user_id,
        ))
    finally:
        browser.close()


def cmd_return_to_source_comment(args: argparse.Namespace) -> None:
    """Return from one profile to its exact source note/comment."""
    from xhs.navigation import return_to_source_comment

    browser, page = _connect(args)
    try:
        _output(return_to_source_comment(
            page,
            feed_id=args.feed_id,
            comment_id=args.comment_id,
        ))
    finally:
        browser.close()


def cmd_open_dm_conversation(args: argparse.Namespace) -> None:
    """Open and verify one DM conversation from the current profile."""
    from xhs.navigation import open_dm_conversation

    browser, page = _connect(args)
    try:
        _output(open_dm_conversation(page))
    finally:
        browser.close()


def cmd_capture_current_dm_conversation(args: argparse.Namespace) -> None:
    """Capture one bounded visible DM conversation without writing."""
    from xhs.dm import capture_current_dm_conversation

    browser, page = _connect(args)
    try:
        _output(capture_current_dm_conversation(page, max_messages=args.max_messages))
    finally:
        browser.close()


def cmd_send_current_dm_message(args: argparse.Namespace) -> None:
    """Send one exact pre-approved DM on the current verified conversation."""
    from xhs.dm import send_current_dm_message

    browser, page = _connect(args)
    try:
        _output(send_current_dm_message(
            page,
            expected_peer_hash=args.expected_peer_hash,
            content=args.content,
        ))
    finally:
        browser.close()


def cmd_open_service_inbox(args: argparse.Namespace) -> None:
    """Open one visible service inbox channel without direct URL navigation."""
    from xhs.service import open_service_inbox

    browser, page = _connect(args)
    try:
        _output(open_service_inbox(page, channel=args.channel))
    finally:
        browser.close()


def cmd_capture_service_inbox(args: argparse.Namespace) -> None:
    """Capture one bounded visible comment or DM inbox."""
    from xhs.service import capture_service_inbox

    browser, page = _connect(args)
    try:
        _output(capture_service_inbox(
            page,
            channel=args.channel,
            max_items=args.max_items,
        ))
    finally:
        browser.close()


def cmd_open_service_item(args: argparse.Namespace) -> None:
    """Open one exact hash-bound item from the current service inbox."""
    from xhs.service import open_service_item

    browser, page = _connect(args)
    try:
        _output(open_service_item(
            page,
            channel=args.channel,
            expected_item_hash=args.expected_item_hash,
        ))
    finally:
        browser.close()


def cmd_bind_active_xhs_tab(args: argparse.Namespace) -> None:
    """Bind subsequent commands to the foreground Xiaohongshu tab."""
    browser, page = _connect(args)
    try:
        _output(page.bind_active_xhs_tab())
    finally:
        browser.close()


def cmd_list_xhs_tabs(args: argparse.Namespace) -> None:
    """List sanitized XHS tab contexts without navigation."""
    browser, page = _connect(args)
    try:
        _output(page.list_xhs_tabs())
    finally:
        browser.close()


def cmd_go_back_and_verify(args: argparse.Namespace) -> None:
    """Return through browser history and verify the original search page."""
    browser, page = _connect(args)
    try:
        _output(page.go_back_and_verify(args.expected_query))
    finally:
        browser.close()


def cmd_open_search_result(args: argparse.Namespace) -> None:
    browser, page = _connect(args)
    try:
        _output(page.open_search_result(args.expected_note_id))
    finally:
        browser.close()


def cmd_get_current_feed_detail(args: argparse.Namespace) -> None:
    from xhs.feed_detail import get_current_feed_detail
    from xhs.types import CommentLoadConfig

    config = CommentLoadConfig(
        click_more_replies=False,
        max_replies_threshold=10,
        max_comment_items=args.max_comment_items,
        scroll_speed="normal",
    )
    browser, page = _connect(args)
    try:
        detail = get_current_feed_detail(page, args.feed_id, config=config)
        _output(detail.to_dict())
    finally:
        browser.close()


def cmd_like_current_feed(args: argparse.Namespace) -> None:
    from xhs.like_favorite import like_current_feed

    browser, page = _connect(args)
    try:
        result = like_current_feed(page, args.feed_id)
        _output(result.to_dict(), exit_code=0 if result.success else 2)
    finally:
        browser.close()


def cmd_inspect_current_like_control(args: argparse.Namespace) -> None:
    from xhs.like_favorite import inspect_current_like_control

    browser, page = _connect(args)
    try:
        _output(inspect_current_like_control(page, args.feed_id))
    finally:
        browser.close()


def cmd_inspect_current_comment_controls(args: argparse.Namespace) -> None:
    from xhs.comment import inspect_current_comment_controls

    browser, page = _connect(args)
    try:
        _output(inspect_current_comment_controls(
            page, args.feed_id, comment_id=args.comment_id
        ))
    finally:
        browser.close()


def cmd_post_comment_current(args: argparse.Namespace) -> None:
    from xhs.comment import CurrentPageActionError, post_comment_current_note

    browser, page = _connect(args)
    try:
        try:
            result = post_comment_current_note(page, args.feed_id, args.content)
        except CurrentPageActionError as exc:
            _output({
                "success": False,
                "verified": False,
                "actionDispatched": exc.action_dispatched,
                "failureCode": exc.failure_code,
                "message": str(exc),
                "platform_actions_executed": 0,
            }, exit_code=2)
        _output(result)
    finally:
        browser.close()


def cmd_like_current_comment(args: argparse.Namespace) -> None:
    from xhs.comment import like_current_comment

    browser, page = _connect(args)
    try:
        result = like_current_comment(page, args.feed_id, comment_id=args.comment_id)
        _output(result.to_dict(), exit_code=0 if result.success else 2)
    finally:
        browser.close()


def cmd_reply_current_comment(args: argparse.Namespace) -> None:
    from xhs.comment import CurrentPageActionError, reply_current_comment

    browser, page = _connect(args)
    try:
        try:
            result = reply_current_comment(
                page,
                args.feed_id,
                args.content,
                comment_id=args.comment_id,
            )
        except CurrentPageActionError as exc:
            _output({
                "success": False,
                "verified": False,
                "actionDispatched": exc.action_dispatched,
                "failureCode": exc.failure_code,
                "message": str(exc),
                "platform_actions_executed": 0,
            }, exit_code=2)
        _output(result)
    finally:
        browser.close()


def cmd_post_comment(args: argparse.Namespace) -> None:
    """发表评论。"""
    from xhs.comment import post_comment

    browser, page = _connect(args)
    try:
        post_comment(page, args.feed_id, _require_token(args), args.content)
        _output({"success": True, "message": "评论发送成功"})
    finally:
        browser.close()


def cmd_reply_comment(args: argparse.Namespace) -> None:
    """回复评论。"""
    from xhs.comment import reply_comment

    browser, page = _connect(args)
    try:
        reply_comment(
            page,
            args.feed_id,
            _require_token(args),
            args.content,
            comment_id=args.comment_id or "",
            user_id=args.user_id or "",
        )
        _output({"success": True, "message": "回复成功"})
    finally:
        browser.close()


def cmd_like_comment(args: argparse.Namespace) -> None:
    """点赞一条精确评论。"""
    from xhs.comment import like_comment

    browser, page = _connect(args)
    try:
        result = like_comment(
            page,
            args.feed_id,
            _require_token(args),
            comment_id=args.comment_id,
        )
        _output(result.to_dict(), exit_code=0 if result.success else 2)
    finally:
        browser.close()


def cmd_like_feed(args: argparse.Namespace) -> None:
    """点赞/取消点赞。"""
    from xhs.like_favorite import like_feed, unlike_feed

    browser, page = _connect(args)
    try:
        if args.unlike:
            result = unlike_feed(page, args.feed_id, _require_token(args))
        else:
            result = like_feed(page, args.feed_id, _require_token(args))
        _output(result.to_dict())
    finally:
        browser.close()


def cmd_favorite_feed(args: argparse.Namespace) -> None:
    """收藏/取消收藏。"""
    from xhs.like_favorite import favorite_feed, unfavorite_feed

    browser, page = _connect(args)
    try:
        if args.unfavorite:
            result = unfavorite_feed(page, args.feed_id, _require_token(args))
        else:
            result = favorite_feed(page, args.feed_id, _require_token(args))
        _output(result.to_dict())
    finally:
        browser.close()


def cmd_publish(args: argparse.Namespace) -> None:
    """发布图文内容。"""
    from image_downloader import process_images
    from xhs.publish import publish_image_content
    from xhs.types import PublishImageContent

    with open(args.title_file, encoding="utf-8") as f:
        title = f.read().strip()
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read().strip()

    image_paths = process_images(args.images) if args.images else []
    if not image_paths:
        _output({"success": False, "error": "没有有效的图片"}, exit_code=2)

    browser, page = _connect(args)
    try:
        publish_image_content(
            page,
            PublishImageContent(
                title=title,
                content=content,
                tags=args.tags or [],
                image_paths=image_paths,
                schedule_time=args.schedule_at,
                is_original=args.original,
                visibility=args.visibility or "",
            ),
        )
        _output({"success": True, "title": title, "images": len(image_paths), "status": "发布完成"})
    finally:
        browser.close()


def cmd_fill_publish(args: argparse.Namespace) -> None:
    """只填写图文表单，不发布。"""
    from image_downloader import process_images
    from xhs.publish import fill_publish_form
    from xhs.types import PublishImageContent

    with open(args.title_file, encoding="utf-8") as f:
        title = f.read().strip()
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read().strip()

    image_paths = process_images(args.images) if args.images else []
    if not image_paths:
        _output({"success": False, "error": "没有有效的图片"}, exit_code=2)

    browser, page = _connect(args)
    try:
        fill_publish_form(
            page,
            PublishImageContent(
                title=title,
                content=content,
                tags=args.tags or [],
                image_paths=image_paths,
                schedule_time=args.schedule_at,
                is_original=args.original,
                visibility=args.visibility or "",
            ),
        )
        _output({"success": True, "title": title, "images": len(image_paths), "status": "表单已填写，等待确认发布"})
    finally:
        browser.close()


def cmd_fill_publish_video(args: argparse.Namespace) -> None:
    """只填写视频表单，不发布。"""
    from xhs.publish_video import fill_publish_video_form
    from xhs.types import PublishVideoContent

    with open(args.title_file, encoding="utf-8") as f:
        title = f.read().strip()
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read().strip()

    browser, page = _connect(args)
    try:
        fill_publish_video_form(
            page,
            PublishVideoContent(
                title=title,
                content=content,
                tags=args.tags or [],
                video_path=args.video,
                schedule_time=args.schedule_at,
                visibility=args.visibility or "",
            ),
        )
        _output({"success": True, "title": title, "video": args.video, "status": "视频表单已填写，等待确认发布"})
    finally:
        browser.close()


def cmd_click_publish(args: argparse.Namespace) -> None:
    """点击发布按钮（在用户确认后调用）。"""
    from xhs.publish import click_publish_button

    browser, page = _connect_existing(args)
    try:
        click_publish_button(page)
        _output({"success": True, "status": "发布完成"})
    finally:
        browser.close()


def cmd_save_draft(args: argparse.Namespace) -> None:
    """保存为草稿。"""
    from xhs.publish import save_as_draft

    browser, page = _connect_existing(args)
    try:
        save_as_draft(page)
        _output({"success": True, "status": "内容已保存到草稿箱"})
    finally:
        browser.close()


def cmd_long_article(args: argparse.Namespace) -> None:
    """长文模式：填写内容 + 一键排版，返回模板列表。"""
    from xhs.publish_long_article import publish_long_article

    with open(args.title_file, encoding="utf-8") as f:
        title = f.read().strip()
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read().strip()

    browser, page = _connect(args)
    try:
        template_names = publish_long_article(
            page,
            title=title,
            content=content,
            image_paths=args.images,
        )
        _output({"success": True, "templates": template_names, "status": "长文已填写，请选择模板"})
    finally:
        browser.close()


def cmd_select_template(args: argparse.Namespace) -> None:
    """选择排版模板。"""
    from xhs.publish_long_article import select_template

    browser, page = _connect_existing(args)
    try:
        selected = select_template(page, args.name)
        if selected:
            _output({"success": True, "template": args.name, "status": "模板已选择"})
        else:
            _output({"success": False, "error": f"未找到模板: {args.name}"}, exit_code=2)
    finally:
        browser.close()


def cmd_next_step(args: argparse.Namespace) -> None:
    """点击下一步 + 填写发布页描述。"""
    from xhs.publish_long_article import click_next_and_fill_description

    with open(args.content_file, encoding="utf-8") as f:
        description = f.read().strip()

    browser, page = _connect_existing(args)
    try:
        click_next_and_fill_description(page, description)
        _output({"success": True, "status": "已进入发布页，等待确认发布"})
    finally:
        browser.close()




def cmd_publish_video(args: argparse.Namespace) -> None:
    """发布视频内容。"""
    from xhs.publish_video import publish_video_content
    from xhs.types import PublishVideoContent

    with open(args.title_file, encoding="utf-8") as f:
        title = f.read().strip()
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read().strip()

    browser, page = _connect(args)
    try:
        publish_video_content(
            page,
            PublishVideoContent(
                title=title,
                content=content,
                tags=args.tags or [],
                video_path=args.video,
                schedule_time=args.schedule_at,
                visibility=args.visibility or "",
            ),
        )
        _output({"success": True, "title": title, "video": args.video, "status": "发布完成"})
    finally:
        browser.close()


def _publish_current_error(exc: Exception, *, dispatched: bool) -> None:
    _output(
        {
            "success": False,
            "verified": False,
            "actionDispatched": dispatched,
            "platform_actions_executed": 0,
            "failureCode": "publish_result_unknown" if dispatched else "publish_pre_dispatch_failure",
            "error": str(exc),
        },
        exit_code=2,
    )


def cmd_publish_image_current(args: argparse.Namespace) -> None:
    """Publish one exact local-image plan through visible Bridge actions."""
    from xhs.publish_current import (
        PublishDispatchedUnknownError,
        PublishPreDispatchError,
        publish_image_current,
    )

    try:
        browser, page = _connect(args)
    except Exception as exc:
        _publish_current_error(exc, dispatched=False)
    try:
        try:
            result = publish_image_current(
                page,
                plan_hash=args.plan_hash,
                title=args.title,
                content=args.content,
                tags=args.tags or [],
                image_paths=args.images,
                media_hashes=args.media_hashes,
            )
        except PublishPreDispatchError as exc:
            _publish_current_error(exc, dispatched=False)
        except PublishDispatchedUnknownError as exc:
            _publish_current_error(exc, dispatched=True)
        _output(result)
    finally:
        browser.close()


def cmd_publish_video_current(args: argparse.Namespace) -> None:
    """Publish one exact local-video plan through visible Bridge actions."""
    from xhs.publish_current import (
        PublishDispatchedUnknownError,
        PublishPreDispatchError,
        publish_video_current,
    )

    try:
        browser, page = _connect(args)
    except Exception as exc:
        _publish_current_error(exc, dispatched=False)
    try:
        try:
            result = publish_video_current(
                page,
                plan_hash=args.plan_hash,
                title=args.title,
                content=args.content,
                tags=args.tags or [],
                video_path=args.video,
                media_hash=args.media_hash,
            )
        except PublishPreDispatchError as exc:
            _publish_current_error(exc, dispatched=False)
        except PublishDispatchedUnknownError as exc:
            _publish_current_error(exc, dispatched=True)
        _output(result)
    finally:
        browser.close()


# ─── 参数解析 ──────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xhs-cli",
        description="小红书自动化 CLI（Extension Bridge 版）",
    )
    parser.add_argument(
        "--bridge-url",
        default="ws://localhost:9333",
        help="Bridge server WebSocket 地址 (default: ws://localhost:9333)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # check-login
    sub = subparsers.add_parser("check-login", help="检查登录状态")
    sub.set_defaults(func=cmd_check_login)

    # login
    sub = subparsers.add_parser("login", help="登录（扫码，阻塞等待）")
    sub.set_defaults(func=cmd_login)

    # get-qrcode
    sub = subparsers.add_parser("get-qrcode", help="获取登录二维码截图（非阻塞）")
    sub.set_defaults(func=cmd_get_qrcode)

    # wait-login
    sub = subparsers.add_parser("wait-login", help="等待扫码登录完成（配合 get-qrcode）")
    sub.add_argument("--timeout", type=float, default=120.0, help="等待超时秒数 (default: 120)")
    sub.set_defaults(func=cmd_wait_login)

    # phone-login
    sub = subparsers.add_parser("phone-login", help="手机号+验证码登录（交互式）")
    sub.add_argument("--phone", required=True, help="手机号")
    sub.add_argument("--code", default="", help="短信验证码（省略则交互式输入）")
    sub.set_defaults(func=cmd_phone_login)

    # send-code
    sub = subparsers.add_parser("send-code", help="分步登录第一步：发送手机验证码")
    sub.add_argument("--phone", required=True, help="手机号")
    sub.set_defaults(func=cmd_send_code)

    # verify-code
    sub = subparsers.add_parser("verify-code", help="分步登录第二步：填写验证码")
    sub.add_argument("--code", required=True, help="短信验证码")
    sub.set_defaults(func=cmd_verify_code)

    # list-feeds
    sub = subparsers.add_parser("list-feeds", help="获取首页 Feed 列表")
    sub.set_defaults(func=cmd_list_feeds)

    # search-feeds
    sub = subparsers.add_parser("search-feeds", help="搜索 Feeds")
    sub.add_argument("--keyword", required=True, help="搜索关键词")
    sub.add_argument("--sort-by", help="排序: 综合|最新|最多点赞|最多评论|最多收藏")
    sub.add_argument("--note-type", help="类型: 不限|视频|图文")
    sub.add_argument("--publish-time", help="时间: 不限|一天内|一周内|半年内")
    sub.add_argument("--search-scope", help="范围: 不限|已看过|未看过|已关注")
    sub.add_argument("--location", help="位置: 不限|同城|附近")
    sub.set_defaults(func=cmd_search_feeds)

    sub = subparsers.add_parser("search-feeds-visible", help="从首页可见搜索框逐字搜索")
    sub.add_argument("--keyword", required=True)
    sub.set_defaults(func=cmd_search_feeds_visible)

    sub = subparsers.add_parser("adopt-search-results", help="接管当前搜索结果，不重复输入关键词")
    sub.add_argument("--keyword", required=True)
    sub.set_defaults(func=cmd_adopt_search_results)

    # get-feed-detail
    sub = subparsers.add_parser("get-feed-detail", help="获取 Feed 详情")
    sub.add_argument("--feed-id", required=True, help="Feed ID")
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--load-all-comments", action="store_true", help="加载全部评论")
    sub.add_argument("--click-more-replies", action="store_true", help="展开更多回复")
    sub.add_argument("--max-replies-threshold", type=int, default=10)
    sub.add_argument("--max-comment-items", type=int, default=0)
    sub.add_argument("--scroll-speed", default="normal", help="slow|normal|fast")
    sub.add_argument("--keyword", default="篮球", help="风控重试时的搜索关键词")
    sub.set_defaults(func=cmd_get_feed_detail)

    # user-profile
    sub = subparsers.add_parser("user-profile", help="获取用户主页")
    sub.add_argument("--user-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.set_defaults(func=cmd_user_profile)

    sub = subparsers.add_parser("capture-own-reply-history", help="读取当前账号主页的历史回复")
    sub.add_argument("--start-position", type=int, default=0)
    sub.add_argument("--max-notes", type=int, default=10)
    sub.add_argument("--max-comment-items", type=int, default=200)
    sub.set_defaults(func=cmd_capture_own_reply_history)

    sub = subparsers.add_parser("open-own-profile", help="通过可见侧边栏进入并核验本人主页")
    sub.set_defaults(func=cmd_open_own_profile)

    sub = subparsers.add_parser("current-account-identity", help="读取并核验当前登录账号身份")
    sub.set_defaults(func=cmd_current_account_identity)

    sub = subparsers.add_parser("open-commenter-profile", help="从精确评论进入并核验评论者主页")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--comment-id", required=True)
    sub.add_argument("--expected-user-id", required=True)
    sub.set_defaults(func=cmd_open_commenter_profile)

    sub = subparsers.add_parser("return-to-source-comment", help="返回并重验原帖原评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--comment-id", required=True)
    sub.set_defaults(func=cmd_return_to_source_comment)

    sub = subparsers.add_parser("open-dm-conversation", help="从当前用户主页进入并核验私信会话")
    sub.set_defaults(func=cmd_open_dm_conversation)

    sub = subparsers.add_parser("capture-current-dm-conversation", help="只读抓取当前可见私信会话")
    sub.add_argument("--max-messages", type=int, default=50)
    sub.set_defaults(func=cmd_capture_current_dm_conversation)

    sub = subparsers.add_parser("send-current-dm-message", help="发送一条精确授权的当前会话私信")
    sub.add_argument("--expected-peer-hash", required=True)
    sub.add_argument("--content", required=True)
    sub.set_defaults(func=cmd_send_current_dm_message)

    sub = subparsers.add_parser("open-service-inbox", help="通过可见控件进入评论或私信收件箱")
    sub.add_argument("--channel", choices=("comments", "dm"), required=True)
    sub.set_defaults(func=cmd_open_service_inbox)

    sub = subparsers.add_parser("capture-service-inbox", help="读取当前可见客服收件箱")
    sub.add_argument("--channel", choices=("comments", "dm"), required=True)
    sub.add_argument("--max-items", type=int, default=20)
    sub.set_defaults(func=cmd_capture_service_inbox)

    sub = subparsers.add_parser("open-service-item", help="打开客服收件箱中的精确哈希项目")
    sub.add_argument("--channel", choices=("comments", "dm"), required=True)
    sub.add_argument("--expected-item-hash", required=True)
    sub.set_defaults(func=cmd_open_service_item)

    # current visible page context (no navigation)
    sub = subparsers.add_parser("page-context", help="读取当前页面身份和风险状态")
    sub.set_defaults(func=cmd_page_context)

    sub = subparsers.add_parser("bind-active-xhs-tab", help="绑定当前前台小红书标签")
    sub.set_defaults(func=cmd_bind_active_xhs_tab)

    sub = subparsers.add_parser("list-xhs-tabs", help="列出脱敏的小红书标签上下文")
    sub.set_defaults(func=cmd_list_xhs_tabs)

    # controlled return to the existing search result page
    sub = subparsers.add_parser("go-back-and-verify", help="返回并验证原搜索结果页")
    sub.add_argument("--expected-query", required=True)
    sub.set_defaults(func=cmd_go_back_and_verify)

    sub = subparsers.add_parser("open-search-result", help="打开当前批次中的已知候选")
    sub.add_argument("--expected-note-id", required=True)
    sub.set_defaults(func=cmd_open_search_result)

    sub = subparsers.add_parser("get-current-feed-detail", help="读取当前已打开笔记")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--max-comment-items", type=int, default=20)
    sub.set_defaults(func=cmd_get_current_feed_detail)

    sub = subparsers.add_parser("like-current-feed", help="点赞当前已打开笔记")
    sub.add_argument("--feed-id", required=True)
    sub.set_defaults(func=cmd_like_current_feed)

    sub = subparsers.add_parser("inspect-current-like-control", help="只读检查当前笔记点赞控件")
    sub.add_argument("--feed-id", required=True)
    sub.set_defaults(func=cmd_inspect_current_like_control)

    sub = subparsers.add_parser("inspect-current-comment-controls", help="只读检查当前评论控件")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--comment-id", default="")
    sub.set_defaults(func=cmd_inspect_current_comment_controls)

    sub = subparsers.add_parser("post-comment-current", help="评论当前已打开笔记")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--content", required=True)
    sub.set_defaults(func=cmd_post_comment_current)

    sub = subparsers.add_parser("like-current-comment", help="点赞当前页精确评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--comment-id", required=True)
    sub.set_defaults(func=cmd_like_current_comment)

    sub = subparsers.add_parser("reply-current-comment", help="回复当前页精确评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--comment-id", required=True)
    sub.add_argument("--content", required=True)
    sub.set_defaults(func=cmd_reply_current_comment)

    # post-comment
    sub = subparsers.add_parser("post-comment", help="发表评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--content", required=True)
    sub.set_defaults(func=cmd_post_comment)

    # reply-comment
    sub = subparsers.add_parser("reply-comment", help="回复评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--content", required=True)
    sub.add_argument("--comment-id")
    sub.add_argument("--user-id")
    sub.set_defaults(func=cmd_reply_comment)

    # like-comment
    sub = subparsers.add_parser("like-comment", help="点赞指定评论")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--comment-id", required=True)
    sub.set_defaults(func=cmd_like_comment)

    # like-feed
    sub = subparsers.add_parser("like-feed", help="点赞")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--unlike", action="store_true")
    sub.set_defaults(func=cmd_like_feed)

    # favorite-feed
    sub = subparsers.add_parser("favorite-feed", help="收藏")
    sub.add_argument("--feed-id", required=True)
    sub.add_argument("--xsec-token", default=_token_default(), help=argparse.SUPPRESS)
    sub.add_argument("--unfavorite", action="store_true")
    sub.set_defaults(func=cmd_favorite_feed)

    # publish
    sub = subparsers.add_parser("publish-image-current", help="发布一条已批准的本地图文")
    sub.add_argument("--plan-hash", required=True)
    sub.add_argument("--title", required=True)
    sub.add_argument("--content", required=True)
    sub.add_argument("--images", nargs="+", required=True)
    sub.add_argument("--media-hashes", nargs="+", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.set_defaults(func=cmd_publish_image_current)

    sub = subparsers.add_parser("publish-video-current", help="发布一条已批准的本地视频")
    sub.add_argument("--plan-hash", required=True)
    sub.add_argument("--title", required=True)
    sub.add_argument("--content", required=True)
    sub.add_argument("--video", required=True)
    sub.add_argument("--media-hash", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.set_defaults(func=cmd_publish_video_current)

    # frozen legacy publish commands retained for upstream reference
    sub = subparsers.add_parser("publish", help="发布图文")
    sub.add_argument("--title-file", required=True)
    sub.add_argument("--content-file", required=True)
    sub.add_argument("--images", nargs="+", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.add_argument("--schedule-at")
    sub.add_argument("--original", action="store_true")
    sub.add_argument("--visibility")
    sub.set_defaults(func=cmd_publish)

    # publish-video
    sub = subparsers.add_parser("publish-video", help="发布视频")
    sub.add_argument("--title-file", required=True)
    sub.add_argument("--content-file", required=True)
    sub.add_argument("--video", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.add_argument("--schedule-at")
    sub.add_argument("--visibility")
    sub.set_defaults(func=cmd_publish_video)

    # fill-publish
    sub = subparsers.add_parser("fill-publish", help="填写图文表单（不发布）")
    sub.add_argument("--title-file", required=True)
    sub.add_argument("--content-file", required=True)
    sub.add_argument("--images", nargs="+", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.add_argument("--schedule-at")
    sub.add_argument("--original", action="store_true")
    sub.add_argument("--visibility")
    sub.set_defaults(func=cmd_fill_publish)

    # fill-publish-video
    sub = subparsers.add_parser("fill-publish-video", help="填写视频表单（不发布）")
    sub.add_argument("--title-file", required=True)
    sub.add_argument("--content-file", required=True)
    sub.add_argument("--video", required=True)
    sub.add_argument("--tags", nargs="*")
    sub.add_argument("--schedule-at")
    sub.add_argument("--visibility")
    sub.set_defaults(func=cmd_fill_publish_video)

    # click-publish
    sub = subparsers.add_parser("click-publish", help="点击发布按钮")
    sub.set_defaults(func=cmd_click_publish)

    # save-draft
    sub = subparsers.add_parser("save-draft", help="保存为草稿")
    sub.set_defaults(func=cmd_save_draft)

    # long-article
    sub = subparsers.add_parser("long-article", help="长文模式：填写 + 一键排版")
    sub.add_argument("--title-file", required=True)
    sub.add_argument("--content-file", required=True)
    sub.add_argument("--images", nargs="*")
    sub.set_defaults(func=cmd_long_article)

    # select-template
    sub = subparsers.add_parser("select-template", help="选择排版模板")
    sub.add_argument("--name", required=True)
    sub.set_defaults(func=cmd_select_template)

    # next-step
    sub = subparsers.add_parser("next-step", help="点击下一步 + 填写描述")
    sub.add_argument("--content-file", required=True)
    sub.set_defaults(func=cmd_next_step)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        _require_product_gateway(args)
        args.func(args)
    except Exception as e:
        logger.error("执行失败: %s", e, exc_info=True)
        _output({"success": False, "error": str(e)}, exit_code=2)


if __name__ == "__main__":
    main()
