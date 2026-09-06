from __future__ import annotations

from app.automation.adapters.generic import GenericAdapter
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.adapters.lever import LeverAdapter
from app.automation.adapters.workday import WorkdayAdapter
from app.automation.adapters.smartrecruiters import SmartRecruitersAdapter
from app.automation.adapters.workable import WorkableAdapter
from app.automation.targets import TargetResolution, classify_target, trusted_provider_for_url
from app.domain.targets import TargetKind
from urllib.parse import urlsplit


def _exact_loopback_synthetic(resolution: TargetResolution) -> bool:
    try:
        parts = urlsplit(resolution.final_url)
        host = (parts.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"} and resolution.evidence.get("synthetic_lab") is True


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters = (GreenhouseAdapter, LeverAdapter, WorkdayAdapter, SmartRecruitersAdapter, WorkableAdapter)

    def detect(self, target: TargetResolution | str, html: str = "") -> GenericAdapter:
        resolution = (
            target
            if isinstance(target, TargetResolution)
            else classify_target(target, target, html=html)
        )
        if resolution.kind in {
            TargetKind.NON_HTML,
            TargetKind.MISMATCH,
            TargetKind.BLOCKED,
            TargetKind.AUTH_WALL,
            TargetKind.HUMAN_CHALLENGE,
            TargetKind.LISTING,
            TargetKind.MULTIPLE_CANDIDATE_ROLES,
        }:
            return GenericAdapter(resolution)
        # A provider adapter is an automation capability.  It is available
        # only after the resolution has passed the complete identity gate;
        # a hosted ATS URL by itself is review-only and must stay generic.
        if not resolution.verified_for_automation:
            return GenericAdapter(resolution)
        by_provider = {
            "greenhouse": GreenhouseAdapter,
            "lever": LeverAdapter,
            "workday": WorkdayAdapter,
            "smartrecruiters": SmartRecruitersAdapter,
            "workable": WorkableAdapter,
        }
        # Never honour a forged provider label when the final navigation host
        # is not the exact trusted host/domain for that provider.  Custom
        # domains may pass only through the positive identity bundle enforced
        # by TargetResolution.verified_for_automation; a provider hint/source
        # label alone cannot satisfy that property.
        trusted_provider = trusted_provider_for_url(resolution.final_url)
        if trusted_provider and trusted_provider != resolution.provider:
            return GenericAdapter(resolution)
        # SmartRecruiters and Workable are production-authorized only on their
        # exact HTTPS provider hosts.  The sole exception is an exact local
        # loopback synthetic fixture carrying explicit lab evidence.
        if resolution.provider in {"smartrecruiters", "workable"} and (
            trusted_provider != resolution.provider
            and not _exact_loopback_synthetic(resolution)
        ):
            return GenericAdapter(resolution)
        adapter_type = by_provider.get(resolution.provider)
        if adapter_type is not None:
            return adapter_type(resolution)
        return GenericAdapter(resolution)
