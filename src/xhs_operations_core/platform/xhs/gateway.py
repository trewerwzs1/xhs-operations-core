"""Single product gateway for every vendor CLI operation."""

from __future__ import annotations

from typing import Any, Callable

from .capabilities import (
    CapabilityAccess,
    CapabilityRegistry,
    CapabilitySurface,
    XhsCapability,
    XhsCapabilityDeniedError,
)


GatewayTransport = Callable[
    [XhsCapability, list[str], str, int],
    dict[str, Any],
]


class XhsOperationGateway:
    """Authorize first, then invoke one injected transport.

    A gateway constructed without a transport is deliberately non-executable;
    this makes the public object safe by default in tooling and audits.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        transport: GatewayTransport | None = None,
    ) -> None:
        self.registry = registry or CapabilityRegistry.product()
        self._transport = transport

    def execute(
        self,
        operation: str,
        args: list[str],
        *,
        surface: CapabilitySurface,
        access: CapabilityAccess,
        token: str = "",
        timeout: int = 120,
    ) -> dict[str, Any]:
        capability = self.registry.require(operation, surface=surface, access=access)
        if self._transport is None:
            raise XhsCapabilityDeniedError(
                "Xiaohongshu gateway has no bound product transport"
            )
        return self._transport(capability, list(args), token, timeout)

    def execute_vendor_command(
        self,
        vendor_command: str,
        args: list[str],
        *,
        surface: CapabilitySurface,
        access: CapabilityAccess,
        token: str = "",
        timeout: int = 120,
    ) -> dict[str, Any]:
        capability = self.registry.require_vendor_command(
            vendor_command, surface=surface, access=access
        )
        if self._transport is None:
            raise XhsCapabilityDeniedError(
                "Xiaohongshu gateway has no bound product transport"
            )
        return self._transport(capability, list(args), token, timeout)

    def audit(self) -> dict[str, Any]:
        return {
            **self.registry.audit(),
            "transport_bound": self._transport is not None,
            "entrypoint": "XhsOperationGateway.execute_vendor_command",
        }
