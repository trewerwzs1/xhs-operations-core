"""Atomic project-local Campaign repository."""

from __future__ import annotations

from pathlib import Path

from xhs_operations_core.storage import StorageCorruptionError, read_json, update_json_object

from .models import Campaign, CampaignContractError


class CampaignRepositoryError(RuntimeError):
    """Base repository error."""


class CampaignAlreadyExistsError(CampaignRepositoryError):
    pass


class CampaignNotFoundError(CampaignRepositoryError):
    pass


class CampaignConflictError(CampaignRepositoryError):
    pass


class CampaignRepository:
    def __init__(self, runtime_dir: str | Path) -> None:
        self.directory = Path(runtime_dir) / "campaigns"

    def path_for(self, campaign_id: str) -> Path:
        # Parsing a minimal Campaign would be inappropriate here; enforce the same safe id shape.
        import re

        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", campaign_id) is None:
            raise CampaignRepositoryError("unsafe campaign_id")
        return self.directory / f"{campaign_id}.json"

    def create(self, campaign: Campaign) -> Path:
        path = self.path_for(campaign.campaign_id)

        def create_only(current: dict[str, object]) -> dict[str, object]:
            if path.exists():
                raise CampaignAlreadyExistsError(campaign.campaign_id)
            return campaign.to_dict()

        update_json_object(path, create_only)
        return path

    def get(self, campaign_id: str) -> Campaign:
        path = self.path_for(campaign_id)
        try:
            payload = read_json(path)
        except FileNotFoundError as exc:
            raise CampaignNotFoundError(campaign_id) from exc
        if not isinstance(payload, dict):
            raise StorageCorruptionError(f"Campaign state must be an object: {path}")
        try:
            return Campaign.from_dict(payload)
        except CampaignContractError as exc:
            raise StorageCorruptionError(f"invalid Campaign state: {path}: {exc}") from exc

    def update(self, campaign: Campaign, *, expected_updated_at: str) -> Path:
        path = self.path_for(campaign.campaign_id)

        def compare_and_replace(current: dict[str, object]) -> dict[str, object]:
            if not path.exists():
                raise CampaignNotFoundError(campaign.campaign_id)
            if not current:
                raise StorageCorruptionError(f"empty Campaign state: {path}")
            try:
                stored = Campaign.from_dict(current)
            except CampaignContractError as exc:
                raise StorageCorruptionError(
                    f"invalid Campaign state before update: {path}: {exc}"
                ) from exc
            if stored.campaign_id != campaign.campaign_id:
                raise StorageCorruptionError(f"Campaign id/path mismatch: {path}")
            actual = stored.updated_at
            if actual != expected_updated_at:
                raise CampaignConflictError(
                    f"Campaign changed: expected {expected_updated_at}, found {actual}"
                )
            return campaign.to_dict()

        update_json_object(path, compare_and_replace)
        return path

    def list(self) -> list[Campaign]:
        if not self.directory.exists():
            return []
        campaigns = [self.get(path.stem) for path in sorted(self.directory.glob("*.json"))]
        return sorted(campaigns, key=lambda item: (item.updated_at, item.campaign_id), reverse=True)
