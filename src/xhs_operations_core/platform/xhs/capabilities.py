"""Closed-by-default capability registry for Xiaohongshu platform calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class XhsCapabilityDeniedError(RuntimeError):
    """Raised before connection when an operation is outside the product surface."""


class CapabilityAccess(str, Enum):
    READ = "read"
    WRITE = "write"


class CapabilitySurface(str, Enum):
    SETUP_READ = "setup_read"
    SESSION_CURRENT_PAGE = "session_current_page"
    PUBLISH_CURRENT_PAGE = "publish_current_page"
    SERVICE_INBOX = "service_inbox"
    LEGACY_RAW = "legacy_raw"


@dataclass(frozen=True)
class XhsCapability:
    operation: str
    vendor_command: str
    access: CapabilityAccess
    surface: CapabilitySurface
    enabled: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "vendor_command": self.vendor_command,
            "access": self.access.value,
            "surface": self.surface.value,
            "enabled": self.enabled,
            "reason": self.reason,
        }


_PRODUCT_CAPABILITIES = (
    XhsCapability("search_feeds_visible", "search-feeds-visible", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("adopt_current_search_results", "adopt-search-results", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("capture_own_reply_history", "capture-own-reply-history", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("open_own_profile", "open-own-profile", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("current_account_identity", "current-account-identity", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("open_commenter_profile", "open-commenter-profile", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("return_to_source_comment", "return-to-source-comment", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("open_dm_conversation", "open-dm-conversation", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("capture_current_dm_conversation", "capture-current-dm-conversation", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("open_service_inbox", "open-service-inbox", CapabilityAccess.READ, CapabilitySurface.SERVICE_INBOX),
    XhsCapability("capture_service_inbox", "capture-service-inbox", CapabilityAccess.READ, CapabilitySurface.SERVICE_INBOX),
    XhsCapability("open_service_item", "open-service-item", CapabilityAccess.READ, CapabilitySurface.SERVICE_INBOX),
    XhsCapability("page_context", "page-context", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("bind_active_xhs_tab", "bind-active-xhs-tab", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("list_xhs_tabs", "list-xhs-tabs", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("get_current_feed_detail", "get-current-feed-detail", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("inspect_current_like_control", "inspect-current-like-control", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("inspect_current_comment_controls", "inspect-current-comment-controls", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("go_back_and_verify", "go-back-and-verify", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("open_search_result", "open-search-result", CapabilityAccess.READ, CapabilitySurface.SETUP_READ),
    XhsCapability("like_current_feed", "like-current-feed", CapabilityAccess.WRITE, CapabilitySurface.SESSION_CURRENT_PAGE),
    XhsCapability("post_comment_current", "post-comment-current", CapabilityAccess.WRITE, CapabilitySurface.SESSION_CURRENT_PAGE),
    XhsCapability("like_current_comment", "like-current-comment", CapabilityAccess.WRITE, CapabilitySurface.SESSION_CURRENT_PAGE),
    XhsCapability("reply_current_comment", "reply-current-comment", CapabilityAccess.WRITE, CapabilitySurface.SESSION_CURRENT_PAGE),
    XhsCapability("send_current_dm_message", "send-current-dm-message", CapabilityAccess.WRITE, CapabilitySurface.SESSION_CURRENT_PAGE),
)

_FROZEN_CAPABILITIES = (
    XhsCapability("legacy_search_feeds", "search-feeds", CapabilityAccess.READ, CapabilitySurface.LEGACY_RAW, False, "superseded_by_visible_single_search_session"),
    XhsCapability("legacy_get_feed_detail", "get-feed-detail", CapabilityAccess.READ, CapabilitySurface.LEGACY_RAW, False, "superseded_by_current_page_detail"),
    XhsCapability("legacy_post_comment", "post-comment", CapabilityAccess.WRITE, CapabilitySurface.LEGACY_RAW, False, "superseded_by_current_page_session"),
    XhsCapability("legacy_reply_comment", "reply-comment", CapabilityAccess.WRITE, CapabilitySurface.LEGACY_RAW, False, "superseded_by_current_page_session"),
    XhsCapability("legacy_like_feed", "like-feed", CapabilityAccess.WRITE, CapabilitySurface.LEGACY_RAW, False, "superseded_by_current_page_session"),
    XhsCapability("legacy_like_comment", "like-comment", CapabilityAccess.WRITE, CapabilitySurface.LEGACY_RAW, False, "superseded_by_current_page_session"),
    XhsCapability("publish_image_current", "publish-image-current", CapabilityAccess.WRITE, CapabilitySurface.PUBLISH_CURRENT_PAGE, False, "v2_publish_live_uat_pending"),
    XhsCapability("publish_video_current", "publish-video-current", CapabilityAccess.WRITE, CapabilitySurface.PUBLISH_CURRENT_PAGE, False, "v2_publish_live_uat_pending"),
)


class CapabilityRegistry:
    """Exact allow-list; unknown, disabled and cross-surface calls all fail closed."""

    schema_version = 1
    default_policy = "deny"

    def __init__(self, capabilities: Iterable[XhsCapability] = ()) -> None:
        rows = tuple(capabilities)
        self._by_operation = {item.operation: item for item in rows}
        if len(self._by_operation) != len(rows):
            raise ValueError("duplicate Xiaohongshu capability operation")
        commands = [item.vendor_command for item in rows]
        if len(set(commands)) != len(commands):
            raise ValueError("duplicate Xiaohongshu vendor command")
        self._by_vendor_command = {item.vendor_command: item for item in rows}

    @classmethod
    def product(cls) -> "CapabilityRegistry":
        return cls((*_PRODUCT_CAPABILITIES, *_FROZEN_CAPABILITIES))

    def require(
        self,
        operation: str,
        *,
        surface: CapabilitySurface,
        access: CapabilityAccess,
    ) -> XhsCapability:
        capability = self._by_operation.get(operation)
        if capability is None:
            raise XhsCapabilityDeniedError(
                f"Xiaohongshu capability is not registered: {operation}"
            )
        if not capability.enabled:
            raise XhsCapabilityDeniedError(
                f"Xiaohongshu capability is frozen: {operation}; {capability.reason}"
            )
        if capability.surface is not surface or capability.access is not access:
            raise XhsCapabilityDeniedError(
                "Xiaohongshu capability surface mismatch: "
                f"{operation} requires {capability.surface.value}/{capability.access.value}"
            )
        return capability

    def require_vendor_command(
        self,
        vendor_command: str,
        *,
        surface: CapabilitySurface,
        access: CapabilityAccess,
    ) -> XhsCapability:
        capability = self._by_vendor_command.get(vendor_command)
        if capability is None:
            raise XhsCapabilityDeniedError(
                f"Xiaohongshu vendor command is not registered: {vendor_command}"
            )
        return self.require(capability.operation, surface=surface, access=access)

    def audit(self) -> dict[str, Any]:
        rows = sorted(self._by_operation.values(), key=lambda item: item.operation)
        return {
            "schema_version": self.schema_version,
            "default_policy": self.default_policy,
            "allowed": [item.to_dict() for item in rows if item.enabled],
            "frozen": [item.to_dict() for item in rows if not item.enabled],
            "unknown_operations_allowed": False,
        }
