"""Read-only login evidence collection and deterministic evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .config import BrowserConfig
from .profiles import BrowserProfileManager
from xhs_operations_core.storage import read_json, write_json_atomic


READONLY_CONFIRMATION = "I_CONFIRM_XHS_READONLY_LOGIN_CHECK"
MANUAL_LOGIN_CONFIRMATION = "I_CONFIRM_XHS_MANUAL_LOGIN_SESSION"
RUN_AGENT_LOGIN_CONFIRMATION = "I_CONFIRM_LOGGED_IN_OWN_PROFILE"


class BrowserLoginError(RuntimeError):
    pass


def sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"}:
        return ""
    hostname = parts.hostname or ""
    if not hostname:
        return ""
    safe_host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parts.port
    except ValueError:
        return ""
    if port is not None:
        safe_host = f"{safe_host}:{port}"
    return urlunsplit((parts.scheme, safe_host, parts.path, "", ""))


@dataclass(frozen=True)
class LoginEvidence:
    current_url: str
    positive_selector_counts: dict[str, int]
    positive_text_counts: dict[str, int]
    negative_selector_counts: dict[str, int]
    negative_text_counts: dict[str, int]
    risk_text_counts: dict[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_url", sanitize_url(self.current_url))
        for mapping in (
            self.positive_selector_counts,
            self.positive_text_counts,
            self.negative_selector_counts,
            self.negative_text_counts,
            self.risk_text_counts,
        ):
            if not isinstance(mapping, dict) or any(
                not isinstance(key, str) or not key for key in mapping
            ):
                raise BrowserLoginError("login evidence counts must be string-keyed objects")
            if any(type(value) is not int or value < 0 for value in mapping.values()):
                raise BrowserLoginError("login evidence counts must be non-negative integers")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LoginEvidence":
        allowed = {
            "current_url",
            "positive_selector_counts",
            "positive_text_counts",
            "negative_selector_counts",
            "negative_text_counts",
            "risk_text_counts",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise BrowserLoginError(f"unknown login evidence fields: {sorted(unknown)}")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise BrowserLoginError(f"invalid login evidence: {exc}") from exc


@dataclass(frozen=True)
class LoginCheckResult:
    ok: bool
    logged_in: bool
    runtime_ready: bool
    error_code: str | None
    positive_hit: bool
    negative_hit: bool
    risk_signals: tuple[str, ...]
    current_url: str
    calibration_status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LoginCalibrationReceipt:
    account_id: str
    profile_name: str
    signature_fingerprint: str
    verified_at: str
    valid_until: str
    confirmation_ref: str

    def __post_init__(self) -> None:
        for name, value in (
            ("account_id", self.account_id),
            ("profile_name", self.profile_name),
            ("signature_fingerprint", self.signature_fingerprint),
            ("confirmation_ref", self.confirmation_ref),
        ):
            if not isinstance(value, str) or not value.strip():
                raise BrowserLoginError(f"calibration {name} is required")
        if len(self.signature_fingerprint) != 64:
            raise BrowserLoginError("invalid calibration signature fingerprint")
        if any(item not in "0123456789abcdef" for item in self.signature_fingerprint.lower()):
            raise BrowserLoginError("invalid calibration signature fingerprint")
        try:
            verified = datetime.fromisoformat(self.verified_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(self.valid_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrowserLoginError("invalid calibration timestamp") from exc
        if verified.tzinfo is None or expires.tzinfo is None or expires <= verified:
            raise BrowserLoginError("calibration timestamps must be aware and increasing")

    def is_valid(self, *, config: BrowserConfig, checked_at: str) -> bool:
        try:
            now = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(self.valid_until.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (
            now.tzinfo is not None
            and expires.tzinfo is not None
            and datetime.fromisoformat(self.verified_at.replace("Z", "+00:00")) <= now <= expires
            and self.account_id == config.account_id
            and self.profile_name == config.profile_name
            and self.signature_fingerprint == config.login_signature.fingerprint()
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "LoginCalibrationReceipt":
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise BrowserLoginError(f"invalid login calibration receipt: {exc}") from exc


def calibration_path(runtime_dir: Path, config: BrowserConfig) -> Path:
    return runtime_dir / "browser" / "calibrations" / f"{config.account_id}.json"


def load_calibration_status(
    runtime_dir: Path,
    config: BrowserConfig,
    *,
    checked_at: str,
) -> bool:
    payload = read_json(calibration_path(runtime_dir, config), default=None)
    if not isinstance(payload, dict):
        return False
    try:
        receipt = LoginCalibrationReceipt.from_dict(payload)
    except BrowserLoginError:
        return False
    return receipt.is_valid(config=config, checked_at=checked_at)


def record_run_agent_login_calibration(
    *, runtime_dir: Path, config: BrowserConfig, platform_user_id: str,
    confirmation: str, verified_at: str,
) -> LoginCalibrationReceipt:
    """Record login only after Run Agent read the current account's own profile."""
    if confirmation != RUN_AGENT_LOGIN_CONFIRMATION:
        raise BrowserLoginError("exact own-profile login confirmation is required")
    if config.fixture_only or not config.allow_platform_access:
        raise BrowserLoginError("browser config does not permit platform access")
    if not isinstance(platform_user_id, str) or not platform_user_id.strip():
        raise BrowserLoginError("Run Agent own-profile identity evidence is missing")
    try:
        moment = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrowserLoginError("verified_at must be ISO-8601") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise BrowserLoginError("verified_at must include a timezone")
    valid_until = moment.astimezone(timezone.utc) + timedelta(days=30)
    import hashlib
    identity_ref = hashlib.sha256(platform_user_id.strip().encode("utf-8")).hexdigest()[:16]
    receipt = LoginCalibrationReceipt(
        account_id=config.account_id,
        profile_name=config.profile_name,
        signature_fingerprint=config.login_signature.fingerprint(),
        verified_at=moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
        valid_until=valid_until.isoformat(timespec="seconds"),
        confirmation_ref=f"run_agent_own_profile:{identity_ref}",
    )
    write_json_atomic(calibration_path(runtime_dir, config), receipt.to_dict())
    return receipt


class LoginProbe(Protocol):
    def start(self, *, profile_path: Path, config: BrowserConfig) -> None: ...
    def open_readonly(self, url: str, *, timeout_seconds: int) -> None: ...
    def count_selector(self, selector: str) -> int: ...
    def count_text(self, text: str) -> int: ...
    def current_url(self) -> str: ...
    def close(self) -> None: ...


class FakeLoginProbe:
    def __init__(
        self,
        *,
        url: str,
        selector_counts: dict[str, int] | None = None,
        text_counts: dict[str, int] | None = None,
    ) -> None:
        self.url = url
        self.selector_counts = selector_counts or {}
        self.text_counts = text_counts or {}
        self.started = False
        self.opened_urls: list[str] = []

    def start(self, *, profile_path: Path, config: BrowserConfig) -> None:
        self.started = True

    def open_readonly(self, url: str, *, timeout_seconds: int) -> None:
        self.opened_urls.append(url)

    def count_selector(self, selector: str) -> int:
        return self.selector_counts.get(selector, 0)

    def count_text(self, text: str) -> int:
        return self.text_counts.get(text, 0)

    def current_url(self) -> str:
        return self.url

    def close(self) -> None:
        self.started = False


class PlaywrightLoginProbe:
    """Headed persistent browser used only for read-only login evidence."""

    def __init__(self) -> None:
        self._playwright = None
        self._context = None
        self._page = None
        self._timeout_error = None

    def start(self, *, profile_path: Path, config: BrowserConfig) -> None:
        # Historical implementation retained only as a frozen reference.  It
        # must fail before importing or starting Playwright.
        from xhs_operations_core.platform.xhs.method_lock import (
            require_approved_xhs_call_method,
        )

        require_approved_xhs_call_method("legacy_playwright")
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise BrowserLoginError(
                "playwright is not installed; install xhs-operations-core[browser]"
            ) from exc
        self._timeout_error = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                channel=None if config.channel == "chromium" else config.channel,
                headless=False,
                slow_mo=config.slow_mo_ms,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception as exc:
            self.close()
            raise BrowserLoginError(f"unable to launch dedicated browser profile: {exc}") from exc

    def open_readonly(self, url: str, *, timeout_seconds: int) -> None:
        if self._page is None:
            raise BrowserLoginError("browser probe is not started")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        except self._timeout_error as exc:
            raise BrowserLoginError("browser login check timed out") from exc

    def count_selector(self, selector: str) -> int:
        return int(self._page.locator(selector).count())

    def count_text(self, text: str) -> int:
        return int(self._page.get_by_text(text, exact=False).count())

    def current_url(self) -> str:
        return str(self._page.url) if self._page is not None else ""

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._playwright = None
        self._page = None


def collect_login_evidence(probe: LoginProbe, config: BrowserConfig) -> LoginEvidence:
    signature = config.login_signature
    try:
        return LoginEvidence(
            current_url=probe.current_url(),
            positive_selector_counts={
                item: probe.count_selector(item) for item in signature.positive_selectors
            },
            positive_text_counts={item: probe.count_text(item) for item in signature.positive_texts},
            negative_selector_counts={
                item: probe.count_selector(item) for item in signature.negative_selectors
            },
            negative_text_counts={item: probe.count_text(item) for item in signature.negative_texts},
            risk_text_counts={item: probe.count_text(item) for item in signature.risk_texts},
        )
    except BrowserLoginError:
        raise
    except Exception as exc:
        raise BrowserLoginError(f"unable to collect login evidence: {type(exc).__name__}") from exc


def evaluate_login_evidence(
    evidence: LoginEvidence,
    config: BrowserConfig,
    *,
    calibration_verified: bool = False,
) -> LoginCheckResult:
    expected_mappings = (
        (set(config.login_signature.positive_selectors), set(evidence.positive_selector_counts)),
        (set(config.login_signature.positive_texts), set(evidence.positive_text_counts)),
        (set(config.login_signature.negative_selectors), set(evidence.negative_selector_counts)),
        (set(config.login_signature.negative_texts), set(evidence.negative_text_counts)),
        (set(config.login_signature.risk_texts), set(evidence.risk_text_counts)),
    )
    if any(expected != actual for expected, actual in expected_mappings):
        raise BrowserLoginError("login evidence does not cover the complete configured signature")
    positive_hit = any(evidence.positive_selector_counts.values()) or any(
        evidence.positive_text_counts.values()
    )
    negative_hit = any(evidence.negative_selector_counts.values()) or any(
        evidence.negative_text_counts.values()
    )
    risk_signals = tuple(
        key for key, count in evidence.risk_text_counts.items() if count > 0
    )
    current_host = urlsplit(evidence.current_url).hostname
    expected_page = current_host in {"www.xiaohongshu.com", "xiaohongshu.com"}
    logged_in = positive_hit and not negative_hit and not risk_signals and expected_page
    locally_verified = calibration_verified
    runtime_ready = logged_in and locally_verified
    error_code = (
        None
        if runtime_ready
        else "risk_signal"
        if risk_signals
        else "login_not_confirmed"
        if not logged_in and expected_page
        else "unexpected_page"
        if not expected_page
        else "signature_not_locally_verified"
    )
    return LoginCheckResult(
        ok=runtime_ready,
        logged_in=logged_in,
        runtime_ready=runtime_ready,
        error_code=error_code,
        positive_hit=positive_hit,
        negative_hit=negative_hit,
        risk_signals=risk_signals,
        current_url=evidence.current_url,
        calibration_status=config.login_signature.calibration_status,
    )


def run_readonly_login_check(
    *,
    probe: LoginProbe,
    manager: BrowserProfileManager,
    config: BrowserConfig,
    run_id: str,
    confirmation: str,
    calibration_verified: bool = False,
) -> tuple[LoginCheckResult, LoginEvidence]:
    if confirmation != READONLY_CONFIRMATION:
        raise BrowserLoginError("exact read-only confirmation phrase is required")
    if config.fixture_only or not config.allow_platform_access:
        raise BrowserLoginError("browser config does not permit platform access")
    with manager.lease(config, run_id=run_id) as profile_path:
        try:
            probe.start(profile_path=profile_path, config=config)
            probe.open_readonly(config.homepage_url, timeout_seconds=config.timeout_seconds)
            evidence = collect_login_evidence(probe, config)
            return evaluate_login_evidence(
                evidence,
                config,
                calibration_verified=calibration_verified,
            ), evidence
        finally:
            probe.close()


def run_manual_login_authorization(
    *,
    probe: LoginProbe,
    manager: BrowserProfileManager,
    runtime_dir: Path,
    config: BrowserConfig,
    run_id: str,
    confirmation: str,
    verified_at: str,
    input_func=input,
) -> tuple[LoginCheckResult, LoginEvidence, LoginCalibrationReceipt]:
    if confirmation != MANUAL_LOGIN_CONFIRMATION:
        raise BrowserLoginError("exact manual-login confirmation phrase is required")
    if config.fixture_only or not config.allow_platform_access:
        raise BrowserLoginError("browser config does not permit platform access")
    with manager.lease(config, run_id=run_id) as profile_path:
        try:
            probe.start(profile_path=profile_path, config=config)
            probe.open_readonly(config.homepage_url, timeout_seconds=config.timeout_seconds)
            input_func(
                "Complete Xiaohongshu login in the dedicated browser, then press Enter to verify: "
            )
            evidence = collect_login_evidence(probe, config)
            result = evaluate_login_evidence(
                evidence,
                config,
                calibration_verified=True,
            )
            if not result.logged_in or result.risk_signals:
                raise BrowserLoginError("manual login could not be safely confirmed")
            try:
                moment = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise BrowserLoginError("verified_at must be ISO-8601") from exc
            if moment.tzinfo is None:
                raise BrowserLoginError("verified_at must include a timezone")
            valid_until = moment.astimezone(timezone.utc) + timedelta(days=30)
            receipt = LoginCalibrationReceipt(
                account_id=config.account_id,
                profile_name=config.profile_name,
                signature_fingerprint=config.login_signature.fingerprint(),
                verified_at=moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
                valid_until=valid_until.isoformat(timespec="seconds"),
                confirmation_ref=f"manual_login:{run_id}",
            )
            write_json_atomic(calibration_path(runtime_dir, config), receipt.to_dict())
            return result, evidence, receipt
        finally:
            probe.close()
