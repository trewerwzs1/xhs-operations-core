"""Stable Xiaohongshu platform boundary errors.

This module is intentionally independent from the frozen Playwright adapter so
normal product imports never load that legacy implementation.
"""

from __future__ import annotations


class LiveInteractionError(RuntimeError):
    """Fail-closed live interaction error with a stable machine code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
