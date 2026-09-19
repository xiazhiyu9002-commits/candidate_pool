from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kerui_recruit.providers.ai.catalog import CatalogService, canonical_catalog_json, verify_signed_catalog
from kerui_recruit.providers.ai.catalog_models import ModelProfile, ProviderCatalog, can_serve_role
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, ReasoningMode


def test_can_serve_role_allows_optional_thinking_model_in_reasoning_slot():
    profile = ModelProfile(
        model_id="deepseek-flash",
        roles=frozenset({ModelRole.FAST_TEXT, ModelRole.VISION}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF, ReasoningMode.AUTO}),
    )
    assert can_serve_role(profile, ModelRole.FAST_TEXT) is True
    assert can_serve_role(profile, ModelRole.VISION) is True
    assert can_serve_role(profile, ModelRole.REASONING_TEXT) is True


def test_can_serve_role_rejects_non_thinking_model_in_reasoning_slot():
    profile = ModelProfile(
        model_id="off-only",
        roles=frozenset({ModelRole.FAST_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF}),
    )
    assert can_serve_role(profile, ModelRole.FAST_TEXT) is True
    assert can_serve_role(profile, ModelRole.REASONING_TEXT) is False


def test_catalog_has_exact_supported_entries(tmp_path):
    catalog = CatalogService(cache_path=tmp_path / "catalog.json").load()
    assert set(catalog.providers) == {
        "deepseek", "kimi_open", "kimi_code", "qwen",
        "zhipu", "siliconflow", "custom_openai",
    }
    assert catalog.default_provider_id == "deepseek"


def test_kimi_platforms_cannot_be_conflated(tmp_path):
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert providers["kimi_open"].base_url == "https://api.moonshot.cn/v1"
    assert providers["kimi_code"].base_url == "https://api.kimi.com/coding/v1"
    assert providers["kimi_open"].allowed_contexts == set(ExecutionContext)
    assert providers["kimi_code"].allowed_contexts == set(ExecutionContext)


def test_every_recommended_model_declares_its_role(tmp_path):
    catalog = CatalogService(cache_path=tmp_path / "catalog.json").load()
    for provider in catalog.providers.values():
        for role, model_id in provider.recommended_models.items():
            assert role in provider.models[model_id].roles
            assert role in set(ModelRole)


def test_kimi_k3_is_always_thinking(tmp_path):
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    from kerui_recruit.providers.ai.contracts import ReasoningMode

    k3 = providers["kimi_open"].models["kimi-k3"]
    assert ReasoningMode.OFF not in k3.supported_reasoning_modes
    assert ReasoningMode.REQUIRED in k3.supported_reasoning_modes


def _sign_envelope(catalog: dict, private_key: Ed25519PrivateKey) -> dict:
    signature = private_key.sign(canonical_catalog_json(catalog))
    return {"catalog": catalog, "signature": base64.b64encode(signature).decode("ascii")}


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    public = private_key.public_key()
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _builtin_dict(version: int) -> dict:
    import importlib.resources

    raw = importlib.resources.files("kerui_recruit.providers.ai").joinpath("provider_catalog.builtin.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    data["version"] = version
    return data


def test_signed_newer_catalog_is_accepted(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(_builtin_dict(version=4), private_key)
    public_b64 = _public_key_b64(private_key)

    parsed = verify_signed_catalog(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        public_b64,
    )
    assert parsed.version == 4


def test_modified_catalog_is_rejected(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(_builtin_dict(version=4), private_key)
    envelope["catalog"]["version"] = 99  # tamper after signing
    public_b64 = _public_key_b64(private_key)

    with pytest.raises(Exception):
        verify_signed_catalog(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            public_b64,
        )


def test_refresh_disabled_without_remote_config(tmp_path):
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    result = service.refresh(url=None, public_key_b64=None)
    assert result.status == "disabled"
    assert result.active_version == service.load().version


def test_refresh_accepts_signed_newer_catalog(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(_builtin_dict(version=6), private_key)
    public_b64 = _public_key_b64(private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    result = service.refresh(url="https://example.com/catalog.json", public_key_b64=public_b64, client=client)
    assert result.status == "updated"
    assert service.load().version == 6


def test_refresh_rejects_modified_payload(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(_builtin_dict(version=6), private_key)
    envelope["catalog"]["version"] = 99
    public_b64 = _public_key_b64(private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    result = service.refresh(url="https://example.com/catalog.json", public_key_b64=public_b64, client=client)
    assert result.status == "builtin"
    assert service.load().version == 5


def _refresh_catalog(tmp_path, catalog: dict) -> str:
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(catalog, private_key)
    public_b64 = _public_key_b64(private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    return service.refresh(url="https://example.com/catalog.json", public_key_b64=public_b64, client=client).status


def test_remote_catalog_cannot_add_provider(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["evil"] = data["providers"]["deepseek"].copy()
    data["providers"]["evil"]["provider_id"] = "evil"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_remove_provider(tmp_path):
    data = _builtin_dict(version=6)
    del data["providers"]["qwen"]
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_base_url(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["deepseek"]["base_url"] = "https://evil.example.com"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_parameter_style(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["deepseek"]["parameter_style"] = "standard"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_can_update_model_and_deprecation(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["deepseek"]["models"]["deepseek-v4-flash"]["deprecated"] = True
    data["providers"]["deepseek"]["recommended_models"]["fast_text"] = "deepseek-v4-flash"
    assert _refresh_catalog(tmp_path, data) == "updated"


def test_remote_catalog_cannot_change_default_provider_id(tmp_path):
    data = _builtin_dict(version=6)
    data["default_provider_id"] = "qwen"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_label(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["deepseek"]["label"] = "Evil DeepSeek"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_kimi_code_allowed_contexts(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["kimi_code"]["allowed_contexts"] = ["interactive"]
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_subscription_warning(tmp_path):
    data = _builtin_dict(version=6)
    data["providers"]["kimi_code"]["subscription_warning"] = "no warning"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def _write_cache(tmp_path, version: int, mutate) -> Path:
    data = _builtin_dict(version)
    mutate(data)
    catalog = ProviderCatalog.model_validate(data)
    cache = tmp_path / "catalog.json"
    cache.write_text(catalog.model_dump_json(), encoding="utf-8")
    return cache


def test_cached_catalog_cannot_redirect_deepseek_base_url(tmp_path):
    _write_cache(tmp_path, version=6, mutate=lambda d: d["providers"]["deepseek"].update(base_url="https://evil.example.com"))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    catalog = service.load()
    assert catalog.version == 5
    assert catalog.providers["deepseek"].base_url == "https://api.deepseek.com"


def test_cached_catalog_cannot_promote_kimi_code_to_background(tmp_path):
    _write_cache(tmp_path, version=6, mutate=lambda d: d["providers"]["kimi_code"].update(allowed_contexts=["interactive"]))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    catalog = service.load()
    assert catalog.version == 5
    assert catalog.providers["kimi_code"].allowed_contexts == set(ExecutionContext)
