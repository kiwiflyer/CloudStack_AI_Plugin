"""Assembling the engine's services from the environment (SPEC.md section 10)."""

from __future__ import annotations

import os
from pathlib import Path

from aisvc.api.deps import Services
from aisvc.db import create_session_factory
from aisvc.hf import HuggingFaceHub, ModelSourcing
from aisvc.registry import ModelRegistry, S3ObjectStore
from aisvc.spec import DEFAULT_LICENSES, load_licenses, load_spec

__all__ = ["build_services"]


def build_services() -> Services:  # pragma: no cover - needs real services
    """Everything the handlers need, read once at startup."""
    spec_path = os.environ.get("AISVC_SPEC", "platform.yaml")
    licenses_path = os.environ.get("AISVC_LICENSES", "")
    spec = load_spec(spec_path, licenses_path=licenses_path or None)
    allowed = load_licenses(licenses_path) if licenses_path else DEFAULT_LICENSES

    bucket = os.environ.get("AISVC_REGISTRY_BUCKET", "models")
    registry = ModelRegistry(
        S3ObjectStore(bucket=bucket, endpoint_url=os.environ.get("AISVC_REGISTRY_ENDPOINT")),
        bucket=bucket,
    )

    sourcing = None
    token = os.environ.get("AISVC_HF_TOKEN")
    if token or os.environ.get("AISVC_HF_ENDPOINT"):
        sourcing = ModelSourcing(
            HuggingFaceHub(token=token, endpoint=os.environ.get("AISVC_HF_ENDPOINT")),
            registry,
            allowed_licenses=allowed,
            settings=spec.document.huggingface,
        )

    _ = Path(spec_path)  # the spec repository lands with Git writeback
    return Services(
        spec=spec,
        registry=registry,
        session_factory=create_session_factory(),
        sourcing=sourcing,
        networks_for=_networks_lookup(),
    )


def _networks_lookup() -> object | None:  # pragma: no cover - needs CloudStack
    """Ask CloudStack which networks an account owns, or nothing.

    Without CloudStack credentials there is no lookup, and every
    attempt to attach a private endpoint to a network is refused. That
    is the safe direction: the alternative is trusting a network id a
    caller supplied.
    """
    endpoint = os.environ.get("CLOUDSTACK_API_URL", "")
    api_key = os.environ.get("CLOUDSTACK_API_KEY", "")
    secret_key = os.environ.get("CLOUDSTACK_SECRET_KEY", "")
    if not (endpoint and api_key and secret_key):
        return None

    from aisvc.cloudstack import CloudStackClient, Credentials
    from aisvc.cloudstack.networks import TenantNetworks

    credentials = Credentials(endpoint=endpoint, api_key=api_key, secret_key=secret_key)

    async def lookup(account: str, domain_path: str) -> list[str]:
        # A client per call: attaching an endpoint is rare, and a
        # long-lived client here would outlive the request that needs it.
        async with CloudStackClient(credentials) as client:
            return await TenantNetworks(client).for_account(account, domain_path)

    return lookup
