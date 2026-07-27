"""Audited immediate image/video publishing through visible XHS Bridge actions."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .errors import PublishError, UploadTimeoutError
from .human import visible_action_delay
from .selectors import (
    CONTENT_EDITOR,
    CREATOR_TAB,
    FILE_INPUT,
    IMAGE_PREVIEW,
    PUBLISH_BUTTON,
    TITLE_INPUT,
)
from .urls import PUBLISH_URL


class PublishPreDispatchError(PublishError):
    """The public submit action was not entered."""


class PublishDispatchedUnknownError(PublishError):
    """Submit was clicked but no exact visible terminal result was verified."""


def _file_sha256(path: str) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_common(
    *,
    plan_hash: str,
    title: str,
    content: str,
    tags: list[str],
    media_paths: list[str],
    media_hashes: list[str],
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", plan_hash or "") is None:
        raise PublishPreDispatchError("publish plan hash is invalid")
    if not title or not content or not isinstance(tags, list) or len(tags) > 10:
        raise PublishPreDispatchError("publish title, content or tags are invalid")
    if len(media_paths) != len(media_hashes) or not media_paths:
        raise PublishPreDispatchError("publish media binding is incomplete")
    for path, expected_hash in zip(media_paths, media_hashes, strict=True):
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash or "") is None:
            raise PublishPreDispatchError("publish media hash is invalid")
        if not Path(path).is_file() or _file_sha256(path) != expected_hash:
            raise PublishPreDispatchError("publish media changed before dispatch")


def _navigate_to_publish(page) -> None:
    page.navigate(PUBLISH_URL)
    page.wait_for_load(timeout=60)
    page.wait_dom_stable(timeout=15)
    url = str(page.evaluate("location.href") or "")
    parsed = urlsplit(url)
    if parsed.hostname != "creator.xiaohongshu.com" or not parsed.path.startswith("/publish"):
        raise PublishPreDispatchError("creator publish page identity was not verified")


def _select_visible_tab(page, tab_name: str) -> None:
    index = page.evaluate(
        f"""
        (() => {{
            const __tonyredbook_publish_tab_index = true;
            const nodes = Array.from(document.querySelectorAll({json.dumps(CREATOR_TAB)}));
            return nodes.findIndex((node) => {{
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                const title = node.querySelector('span.title');
                const x = Math.max(1, Math.min(innerWidth - 1, rect.left + rect.width / 2));
                const y = Math.max(1, Math.min(innerHeight - 1, rect.top + rect.height / 2));
                const hit = document.elementFromPoint(x, y);
                return title && title.textContent.trim() === {json.dumps(tab_name)}
                    && rect.width > 0 && rect.height > 0
                    && rect.bottom > 0 && rect.right > 0
                    && rect.top < innerHeight && rect.left < innerWidth
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && hit && (node === hit || node.contains(hit) || hit.contains(node));
            }});
        }})()
        """
    )
    if type(index) is not int or index < 0:
        raise PublishPreDispatchError(f"visible publish tab was not found: {tab_name}")
    # The probe above proves this exact tab is already inside the viewport and
    # pointer-clickable.  A separate semantic scroll is redundant and has been
    # proven to occupy the Bridge command channel in the live environment.
    page.click_nth_element(CREATOR_TAB, index)
    active = page.evaluate(
        f"""
        (() => {{
            const __tonyredbook_active_publish_tab = true;
            const nodes = Array.from(document.querySelectorAll({json.dumps(CREATOR_TAB)}));
            for (const node of nodes) {{
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                if (!node.classList.contains('active') || rect.width <= 0 || rect.height <= 0
                    || style.display === 'none' || style.visibility === 'hidden') continue;
                return (node.querySelector('span.title')?.textContent || '').trim();
            }}
            return '';
        }})()
        """
    )
    if active != tab_name:
        raise PublishPreDispatchError("publish tab did not become visibly active")


def _wait_for_image_upload(page, expected_count: int) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if page.get_elements_count(IMAGE_PREVIEW) >= expected_count:
            return
        time.sleep(0.5)
    raise UploadTimeoutError("image upload did not produce the expected visible previews")


def _publish_button_ready(page) -> bool:
    return page.evaluate(
        f"""
        (() => {{
            const __tonyredbook_publish_button_ready = true;
            const button = document.querySelector({json.dumps(PUBLISH_BUTTON)});
            if (!button) return false;
            const rect = button.getBoundingClientRect();
            const style = getComputedStyle(button);
            const x = Math.max(1, Math.min(innerWidth - 1, rect.left + rect.width / 2));
            const y = Math.max(1, Math.min(innerHeight - 1, rect.top + rect.height / 2));
            const hit = document.elementFromPoint(x, y);
            return rect.width > 0 && rect.height > 0 && !button.disabled
                && rect.bottom > 0 && rect.right > 0
                && rect.top < innerHeight && rect.left < innerWidth
                && style.display !== 'none' && style.visibility !== 'hidden'
                && hit && (button === hit || button.contains(hit) || hit.contains(button));
        }})()
        """
    ) is True


def _wait_for_video_ready(page) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if _publish_button_ready(page):
            return
        time.sleep(1)
    raise UploadTimeoutError("video upload or processing did not become ready")


def _fill_text(page, *, title: str, content: str, tags: list[str]) -> str:
    final_content = content
    if tags:
        final_content = content.rstrip() + "\n" + " ".join(f"#{tag}" for tag in tags)
    page.input_text(TITLE_INPUT, title)
    page.input_content_editable(CONTENT_EDITOR, final_content)
    return sha256(
        json.dumps(
            {"title": title, "content": content, "tags": tags},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _visible_publish_state(page) -> dict[str, str]:
    value = page.evaluate(
        """
        (() => {
            const __tonyredbook_visible_publish_result = true;
            const text = String(document.body?.innerText || '');
            const riskMarkers = ['验证码', '账号异常', '操作频繁', '违反社区规范', '禁止发布', '禁止发笔记'];
            const successMarkers = ['发布成功', '发布完成', '正在审核', '审核中'];
            return {
                url: String(location.href || ''),
                riskMarker: riskMarkers.find((item) => text.includes(item)) || '',
                successMarker: successMarkers.find((item) => text.includes(item)) || '',
            };
        })()
        """
    )
    if not isinstance(value, dict):
        return {"url": "", "riskMarker": "unknown_state", "successMarker": ""}
    return {
        "url": str(value.get("url", "")),
        "riskMarker": str(value.get("riskMarker", "")),
        "successMarker": str(value.get("successMarker", "")),
    }


def _submit_and_verify(page) -> dict[str, str]:
    try:
        if not _publish_button_ready(page):
            raise PublishPreDispatchError("visible publish button is missing or disabled")
    except PublishPreDispatchError:
        raise
    except Exception as exc:
        raise PublishPreDispatchError("publish button preflight failed") from exc
    try:
        page.click_element(PUBLISH_BUTTON)
    except Exception as exc:
        raise PublishDispatchedUnknownError(
            "publish click transport result is unknown; do not retry automatically"
        ) from exc
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = _visible_publish_state(page)
        if state["riskMarker"]:
            raise PublishDispatchedUnknownError(
                "publish stopped on visible platform risk: " + state["riskMarker"]
            )
        if state["successMarker"]:
            parsed = urlsplit(state["url"])
            return {
                "successMarker": state["successMarker"],
                "pageHost": parsed.hostname or "",
                "pagePath": parsed.path,
            }
        time.sleep(1)
    raise PublishDispatchedUnknownError(
        "publish submit result is unknown; do not retry automatically"
    )


def publish_image_current(
    page,
    *,
    plan_hash: str,
    title: str,
    content: str,
    tags: list[str],
    image_paths: list[str],
    media_hashes: list[str],
) -> dict[str, object]:
    _validate_common(
        plan_hash=plan_hash,
        title=title,
        content=content,
        tags=tags,
        media_paths=image_paths,
        media_hashes=media_hashes,
    )
    if not 1 <= len(image_paths) <= 9:
        raise PublishPreDispatchError("image publish requires 1-9 images")
    try:
        _navigate_to_publish(page)
        _select_visible_tab(page, "上传图文")
        page.set_file_input(FILE_INPUT, image_paths)
        visible_action_delay()
        _wait_for_image_upload(page, len(image_paths))
        content_hash = _fill_text(page, title=title, content=content, tags=tags)
    except (PublishPreDispatchError, UploadTimeoutError):
        raise
    except Exception as exc:
        raise PublishPreDispatchError("image publish form preparation failed") from exc
    evidence = _submit_and_verify(page)
    return {
        "success": True,
        "verified": True,
        "actionDispatched": True,
        "platform_actions_executed": 1,
        "planHash": plan_hash,
        "contentHash": content_hash,
        "mediaHashes": list(media_hashes),
        "evidence": evidence,
    }


def publish_video_current(
    page,
    *,
    plan_hash: str,
    title: str,
    content: str,
    tags: list[str],
    video_path: str,
    media_hash: str,
) -> dict[str, object]:
    _validate_common(
        plan_hash=plan_hash,
        title=title,
        content=content,
        tags=tags,
        media_paths=[video_path],
        media_hashes=[media_hash],
    )
    try:
        _navigate_to_publish(page)
        _select_visible_tab(page, "上传视频")
        page.set_file_input(FILE_INPUT, [video_path])
        visible_action_delay()
        _wait_for_video_ready(page)
        content_hash = _fill_text(page, title=title, content=content, tags=tags)
    except (PublishPreDispatchError, UploadTimeoutError):
        raise
    except Exception as exc:
        raise PublishPreDispatchError("video publish form preparation failed") from exc
    evidence = _submit_and_verify(page)
    return {
        "success": True,
        "verified": True,
        "actionDispatched": True,
        "platform_actions_executed": 1,
        "planHash": plan_hash,
        "contentHash": content_hash,
        "mediaHashes": [media_hash],
        "evidence": evidence,
    }
