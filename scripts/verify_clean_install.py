"""Verify one XHS Operations Core delivery ZIP in a genuinely empty directory.

The verifier is intentionally part of the delivery.  It performs no platform
access: it extracts the exact archive, runs the normal installer, runs the
STOP-on offline UAT, and binds the result to an already-produced packaged
Extension/Bridge DOM trace for the same extension tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile


class CleanInstallError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanInstallError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CleanInstallError(f"{label} must be a JSON object")
    return value


def _safe_archive_members(archive: Path) -> tuple[list[zipfile.ZipInfo], str]:
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
    if not members:
        raise CleanInstallError("delivery archive is empty")
    roots: set[str] = set()
    for info in members:
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise CleanInstallError(f"unsafe archive member: {info.filename}")
        # Unix symlinks are not valid delivery payloads on any recipient OS.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise CleanInstallError(f"archive symlink is forbidden: {info.filename}")
        roots.add(pure.parts[0])
    if len(roots) != 1:
        raise CleanInstallError("delivery archive must contain exactly one root directory")
    return members, next(iter(roots))


def _extract_archive(archive: Path, install_root: Path) -> Path:
    if install_root.exists() and any(install_root.iterdir()):
        raise CleanInstallError("install root must be absent or empty")
    install_root.mkdir(parents=True, exist_ok=True)
    members, root_name = _safe_archive_members(archive)
    with zipfile.ZipFile(archive) as bundle:
        for info in members:
            target = (install_root / PurePosixPath(info.filename)).resolve()
            try:
                target.relative_to(install_root.resolve())
            except ValueError as exc:
                raise CleanInstallError(f"archive member escapes install root: {info.filename}") from exc
        bundle.extractall(install_root)
    project = install_root / root_name
    if not (project / "pyproject.toml").is_file():
        raise CleanInstallError("extracted project root is missing pyproject.toml")
    if (project / ".git").exists():
        raise CleanInstallError("delivery unexpectedly contains Git metadata")
    return project


def _run(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise CleanInstallError(f"{label} failed with exit {completed.returncode}: {detail}")
    return completed


def _powershell() -> str:
    for name in ("powershell.exe", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    raise CleanInstallError("Windows PowerShell is required for the recipient installer")


def _codex() -> str:
    configured = os.environ.get("XHS_OPERATIONS_CORE_CODEX_PATH")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    for name in ("codex.exe", "codex"):
        path = shutil.which(name)
        if path:
            return path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        if candidate.is_file():
            return str(candidate)
    raise CleanInstallError("Codex Desktop CLI is required for Plugin clean-install verification")


def verify(
    *,
    archive: Path,
    install_root: Path,
    extension_trace: Path,
    output: Path,
    account_id: str,
    profile_name: str,
) -> dict[str, object]:
    archive = archive.resolve()
    install_root = install_root.resolve()
    if not archive.is_file():
        raise CleanInstallError(f"delivery archive does not exist: {archive}")
    archive_hash = _sha256(archive)
    project = _extract_archive(archive, install_root)

    recipient_home = install_root / "recipient-home"
    recipient_localappdata = install_root / "recipient-localappdata"
    recipient_home.mkdir(parents=True, exist_ok=False)
    recipient_localappdata.mkdir(parents=True, exist_ok=False)
    installer_environment = os.environ.copy()
    installer_environment.update(
        {
            "USERPROFILE": str(recipient_home),
            "HOME": str(recipient_home),
            "LOCALAPPDATA": str(recipient_localappdata),
            "CODEX_HOME": str(recipient_home / ".codex"),
            "XHS_OPERATIONS_CORE_CODEX_PATH": _codex(),
        }
    )

    _run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "scripts" / "install.ps1"),
            "-AccountId",
            account_id,
            "-ProfileName",
            profile_name,
        ],
        cwd=project,
        label="recipient install",
        timeout=600,
        env=installer_environment,
    )

    packaged_skill = project / "skills" / "xhs-operations-core"
    installed_skill = recipient_home / ".codex" / "skills" / "xhs-operations-core"
    packaged_extension = project / "vendor" / "xiaohongshu-skills" / "extension"
    staged_extension = recipient_localappdata / "XhsOperationsCore" / "xhs-bridge-extension"
    skill_hash = _tree_sha256(packaged_skill)
    installed_skill_hash = _tree_sha256(installed_skill)
    extension_hash = _tree_sha256(packaged_extension)
    staged_extension_hash = _tree_sha256(staged_extension)
    skill_install_ok = bool(skill_hash and skill_hash == installed_skill_hash)
    extension_staging_ok = bool(extension_hash and extension_hash == staged_extension_hash)
    if not skill_install_ok:
        raise CleanInstallError("recipient Codex Skill installation does not match the packaged Skill")
    if not extension_staging_ok:
        raise CleanInstallError("recipient staged extension does not match the packaged extension")

    plugin_manifest = _load_object(
        project / "plugins" / "xhs-operations-core" / ".codex-plugin" / "plugin.json",
        "packaged Plugin manifest",
    )
    plugin_version = str(plugin_manifest.get("version") or "")
    plugin_list_result = _run(
        [_codex(), "plugin", "list", "--json"],
        cwd=project,
        label="recipient Plugin inspection",
        timeout=60,
        env=installer_environment,
    )
    try:
        plugin_list = json.loads(plugin_list_result.stdout)
    except json.JSONDecodeError as exc:
        raise CleanInstallError(f"recipient Plugin list is invalid JSON: {exc}") from exc
    installed_entries = plugin_list.get("installed", []) if isinstance(plugin_list, dict) else []
    plugin_state_ok = any(
        isinstance(item, dict)
        and item.get("pluginId") == "xhs-operations-core@xhs-operations-core-local"
        and item.get("version") == plugin_version
        and item.get("installed") is True
        and item.get("enabled") is True
        for item in installed_entries
    )
    packaged_plugin = project / "plugins" / "xhs-operations-core"
    installed_plugin = (
        recipient_home
        / ".codex"
        / "plugins"
        / "cache"
        / "xhs-operations-core-local"
        / "xhs-operations-core"
        / plugin_version
    )
    packaged_plugin_hash = _tree_sha256(packaged_plugin)
    installed_plugin_hash = _tree_sha256(installed_plugin)
    plugin_install_ok = bool(
        plugin_state_ok
        and packaged_plugin_hash
        and packaged_plugin_hash == installed_plugin_hash
    )
    if not plugin_install_ok:
        raise CleanInstallError("recipient Codex Plugin does not match the packaged enabled Plugin")

    uat_path = project / "work" / "clean-install-offline-uat.json"
    python = project / ".venv" / "Scripts" / "python.exe"
    _run(
        [
            str(python),
            str(project / "scripts" / "offline_uat.py"),
            "--project-root",
            str(project),
            "--isolated-development-sandbox",
            "--output",
            str(uat_path),
        ],
        cwd=project,
        label="recipient offline UAT",
        timeout=300,
    )
    uat = _load_object(uat_path, "offline UAT report")
    offline_ok = (
        uat.get("ok") is True
        and uat.get("platform_actions_executed") == 0
        and uat.get("browser_started") is False
    )
    if not offline_ok:
        raise CleanInstallError("recipient offline UAT did not prove STOP-on zero-platform execution")

    trace = _load_object(extension_trace.resolve(), "extension DOM trace")
    trace_ok = (
        trace.get("ok") is True
        and trace.get("driver") == "packaged_extension_bridge_dom"
        and trace.get("mocked") is False
        and trace.get("fixture_only") is True
        and trace.get("platform_network_accessed") is False
        and trace.get("extension_tree_sha256") == extension_hash
    )
    if not trace_ok:
        raise CleanInstallError("extension DOM trace does not match the extracted extension tree")

    fingerprint = hashlib.sha256(str(install_root).casefold().encode("utf-8")).hexdigest()
    result: dict[str, object] = {
        "schema_version": 1,
        "report_type": "xhs_operations_core_clean_install",
        "ok": True,
        "install_root_fingerprint": fingerprint,
        "archive_sha256": archive_hash,
        "offline_uat_ok": True,
        "offline_uat_check_count": uat.get("check_count"),
        "bridge_fixture_trace_ok": True,
        "skill_install_ok": True,
        "skill_tree_sha256": skill_hash,
        "plugin_install_ok": True,
        "plugin_version": plugin_version,
        "plugin_tree_sha256": packaged_plugin_hash,
        "extension_staging_ok": True,
        "extension_tree_sha256": extension_hash,
        "installer_environment_isolated": True,
        "git_dependency": False,
        "donor_dependency": False,
        "platform_actions_executed": 0,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--extension-trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account-id", default="clean_install_fixture")
    parser.add_argument("--profile-name", default="clean_install_profile")
    args = parser.parse_args()
    try:
        result = verify(
            archive=args.archive,
            install_root=args.install_root,
            extension_trace=args.extension_trace,
            output=args.output,
            account_id=args.account_id,
            profile_name=args.profile_name,
        )
    except (CleanInstallError, OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
