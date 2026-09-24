from __future__ import annotations

import base64
import importlib.resources
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kerui_recruit.providers.ai.catalog import CatalogService, canonical_catalog_json, verify_signed_catalog
from kerui_recruit.providers.ai.catalog_models import ModelProfile, ProviderCatalog, can_serve_role
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, ReasoningMode


def _builtin_raw() -> str:
    return importlib.resources.files("kerui_recruit.providers.ai").joinpath("provider_catalog.builtin.json").read_text(encoding="utf-8")


# 内置目录版本随每次目录改动递增；测试一律相对它取“更高版本”，避免版本号变更造成批量误报。
_BUILTIN_VERSION = int(json.loads(_builtin_raw())["version"])
_NEWER_VERSION = _BUILTIN_VERSION + 1


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


def test_can_serve_role_allows_thinking_only_model_in_fast_slot():
    """强制思考模型可以填进「快速模型」槽位（第 9 点需求）。

    这类模型只声明 reasoning_text，之前会被目录校验直接拒掉（「模型 X 不支持角色
    fast_text」），使用者无法用它换取更高质量。放开快速槽位后按模型自身默认行为运行。
    """
    profile = ModelProfile(
        model_id="thinking-only",
        roles=frozenset({ModelRole.REASONING_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.REQUIRED}),
    )
    assert can_serve_role(profile, ModelRole.FAST_TEXT) is True
    assert can_serve_role(profile, ModelRole.REASONING_TEXT) is True


def test_can_serve_role_rejects_fast_only_model_in_vision_slot():
    """放开快速槽位不得顺手放开视觉槽位。"""
    profile = ModelProfile(
        model_id="text-only",
        roles=frozenset({ModelRole.FAST_TEXT}),
        supported_reasoning_modes=frozenset({ReasoningMode.OFF}),
    )
    assert can_serve_role(profile, ModelRole.VISION) is False


def test_catalog_has_exact_supported_entries(tmp_path):
    catalog = CatalogService(cache_path=tmp_path / "catalog.json").load()
    assert set(catalog.providers) == {
        "deepseek", "kimi_open", "kimi_code", "qwen", "qwen_code",
        "zhipu", "zhipu_code", "siliconflow", "custom_openai",
    }
    assert catalog.default_provider_id == "deepseek"


def test_kimi_platforms_cannot_be_conflated(tmp_path):
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert providers["kimi_open"].base_url == "https://api.moonshot.cn/v1"
    assert providers["kimi_code"].base_url == "https://api.kimi.com/coding/v1"
    assert providers["kimi_open"].allowed_contexts == set(ExecutionContext)
    assert providers["kimi_code"].allowed_contexts == set(ExecutionContext)


def test_kimi_code_subscription_uses_the_coding_endpoint_model_ids(tmp_path):
    """Kimi Code 订阅端点用自己的模型 ID：快速用 kimi-for-coding，思考/视觉用 k3。

    开放平台的 kimi-k2.6 / kimi-k3 不适用于订阅端点，两套名字不可混用
    （用户 2026-09-22 指定）。
    """
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert set(providers["kimi_code"].models) == {"kimi-for-coding", "k3"}
    assert providers["kimi_code"].recommended_models[ModelRole.FAST_TEXT] == "kimi-for-coding"
    assert providers["kimi_code"].recommended_models[ModelRole.REASONING_TEXT] == "k3"
    assert providers["kimi_code"].recommended_models[ModelRole.VISION] == "k3"


def test_zhipu_and_qwen_presets_match_the_coding_plan_model_names(tmp_path):
    """智谱/阿里的预设模型名（用户 2026-09-22 指定，含当日真机复测后的修订）。

    智谱**两侧不同**且这是有意的：开放平台有 `glm-5.3-flashx`，Coding Plan 没有更低的
    快速模型，只能用 `glm-5.3-flash`。两者都是「始终思考」模型，实测做完整简历解析
    （7919 字原文 + 42 字段 schema）：`glm-5.3-flashx` 63.7 秒、结构完整（姓名/经历/画像分点
    都对）；`glm-5.3-flash` 超过 300 秒直接读超时。
    """
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    expected = {
        "zhipu": {"fast_text": "glm-5.3-flashx", "reasoning_text": "glm-5.3",
                  "vision": "glm-5.3-flash"},
        "zhipu_code": {"fast_text": "glm-5.3-flash", "reasoning_text": "glm-5.3",
                       "vision": "glm-5.3-flash"},
        "qwen": {"fast_text": "qwen3.8-flash", "reasoning_text": "qwen3.8-max",
                 "vision": "qwen3.8-flash"},
        "qwen_code": {"fast_text": "qwen3.8-flash", "reasoning_text": "qwen3.8-max",
                      "vision": "qwen3.8-flash"},
    }
    for provider_id, roles in expected.items():
        recommended = providers[provider_id].recommended_models
        assert {role.value: model for role, model in recommended.items()} == roles, provider_id


def test_subscription_plans_have_distinct_endpoints_and_warnings(tmp_path):
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    assert providers["qwen_code"].base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert providers["zhipu_code"].base_url == "https://open.bigmodel.cn/api/coding/paas/v4"
    # 订阅端点必须与同厂商的按量付费端点分离，Key 不互通。
    assert providers["qwen_code"].base_url != providers["qwen"].base_url
    assert providers["zhipu_code"].base_url != providers["zhipu"].base_url
    # 订阅条款限制必须显式提示，向导据此要求用户勾选确认。
    for provider_id in ("kimi_code", "qwen_code", "zhipu_code"):
        assert providers[provider_id].subscription_warning


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


def test_glm_5_3_flash_is_always_thinking(tmp_path):
    """`glm-5.3-flash` / `glm-5.3-flashx` 都是「始终思考」模型：声明支持关思考会让调用直接 400。

    这是 2026-09-22 真机验收抓到的缺陷。预设模型按用户要求换成 `glm-5.3-flash` 后，
    检测矩阵里 `auth` / `text` / `json` / `vision` 全部变成 `E_API_FORMAT`
    （HTTP 400，请求体被拒），只有思考角色 `glm-5.3` 通过。直连复现拿到决定性原文：

        HTTP 400 {"error":{"code":"1210",
                  "message":"该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"}}

    失败原因是请求体里的 `thinking: {"type": "disabled"}`（`apply_reasoning` 只在档案
    同时声明 OFF 与思考模式时才发这个开关），**与模型名无关**：`/models` 列表确认该模型
    存在，直连不带关思考字段时文本与视觉都返回 200（视觉实测能正确识别内置测试图）。
    `glm-5.3-flashx` 同样是这个口径——它虽带 "flashx" 后缀、确实比 `glm-5.3-flash` 快
    （完整简历解析 63.7 秒 vs 超过 300 秒），但一样不接受关思考。
    """
    providers = CatalogService(cache_path=tmp_path / "catalog.json").load().providers
    from kerui_recruit.providers.ai.contracts import ReasoningMode

    targets = (
        ("zhipu", "glm-5.3-flash"),
        ("zhipu", "glm-5.3-flashx"),
        ("zhipu_code", "glm-5.3-flash"),
    )
    for provider_id, model_id in targets:
        profile = providers[provider_id].models[model_id]
        assert ReasoningMode.OFF not in profile.supported_reasoning_modes, (provider_id, model_id)
        assert ReasoningMode.REQUIRED in profile.supported_reasoning_modes, (provider_id, model_id)
        # 对端在 400 里明确给出了可用的强度档位：low / high / max。
        assert "max" in profile.supported_reasoning_efforts, (provider_id, model_id)


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
    data = json.loads(_builtin_raw())
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
    envelope = _sign_envelope(_builtin_dict(version=_NEWER_VERSION), private_key)
    public_b64 = _public_key_b64(private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    result = service.refresh(url="https://example.com/catalog.json", public_key_b64=public_b64, client=client)
    assert result.status == "updated"
    assert service.load().version == _NEWER_VERSION


def test_refresh_rejects_modified_payload(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    envelope = _sign_envelope(_builtin_dict(version=_NEWER_VERSION), private_key)
    envelope["catalog"]["version"] = 99
    public_b64 = _public_key_b64(private_key)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    result = service.refresh(url="https://example.com/catalog.json", public_key_b64=public_b64, client=client)
    assert result.status == "builtin"
    assert service.load().version == _BUILTIN_VERSION


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
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["evil"] = data["providers"]["deepseek"].copy()
    data["providers"]["evil"]["provider_id"] = "evil"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_remove_provider(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    del data["providers"]["qwen"]
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_base_url(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["deepseek"]["base_url"] = "https://evil.example.com"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_parameter_style(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["deepseek"]["parameter_style"] = "standard"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_can_update_model_and_deprecation(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["deepseek"]["models"]["deepseek-v4-flash"]["deprecated"] = True
    data["providers"]["deepseek"]["recommended_models"]["fast_text"] = "deepseek-v4-flash"
    assert _refresh_catalog(tmp_path, data) == "updated"


def test_remote_catalog_cannot_change_default_provider_id(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["default_provider_id"] = "qwen"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_label(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["deepseek"]["label"] = "Evil DeepSeek"
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_kimi_code_allowed_contexts(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
    data["providers"]["kimi_code"]["allowed_contexts"] = ["interactive"]
    assert _refresh_catalog(tmp_path, data) == "builtin"


def test_remote_catalog_cannot_change_subscription_warning(tmp_path):
    data = _builtin_dict(version=_NEWER_VERSION)
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
    _write_cache(tmp_path, version=_NEWER_VERSION, mutate=lambda d: d["providers"]["deepseek"].update(base_url="https://evil.example.com"))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    catalog = service.load()
    assert catalog.version == _BUILTIN_VERSION
    assert catalog.providers["deepseek"].base_url == "https://api.deepseek.com"


def test_cached_catalog_cannot_promote_kimi_code_to_background(tmp_path):
    _write_cache(tmp_path, version=_NEWER_VERSION, mutate=lambda d: d["providers"]["kimi_code"].update(allowed_contexts=["interactive"]))
    service = CatalogService(cache_path=tmp_path / "catalog.json")
    catalog = service.load()
    assert catalog.version == _BUILTIN_VERSION
    assert catalog.providers["kimi_code"].allowed_contexts == set(ExecutionContext)
