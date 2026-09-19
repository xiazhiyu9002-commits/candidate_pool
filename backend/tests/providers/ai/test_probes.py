from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.config_models import AiConnection
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.probes import AiProbeService, probed_roles


def connection(provider_id: str = "deepseek", api_key: str = "k") -> AiConnection:
    return AiConnection(
        connection_id="c1",
        provider_id=provider_id,
        display_name=provider_id,
        api_key=SecretStr(api_key),
    )


def probe_service(
    tmp_path: Path,
    *,
    models_response: httpx.Response | None = None,
    chat_status: int = 200,
    chat_content: str = "ok",
    capture: list | None = None,
) -> AiProbeService:
    async def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append({
                "url": str(request.url),
                "method": request.method,
                "content": request.content.decode("utf-8", errors="ignore"),
            })
        if request.url.path.endswith("/models"):
            return models_response if models_response is not None else httpx.Response(404)
        if chat_status >= 400:
            return httpx.Response(chat_status, json={"error": {"message": "err"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": chat_content}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AiProbeService(CatalogService(cache_path=tmp_path / "catalog.json"), client)


@pytest.mark.asyncio
async def test_discovery_falls_back_to_catalog_when_models_endpoint_is_absent(tmp_path):
    service = probe_service(tmp_path, models_response=httpx.Response(404))
    models = await service.discover(connection("deepseek"))
    assert "deepseek-v4-flash" in {item.model_id for item in models}


@pytest.mark.asyncio
async def test_discovery_uses_models_endpoint_when_available(tmp_path):
    service = probe_service(tmp_path, models_response=httpx.Response(200, json={"data": [{"id": "my-model"}]}))
    models = await service.discover(connection("deepseek"))
    assert {item.model_id for item in models} == {"my-model"}
    assert all(item.source == "discovered" for item in models)


@pytest.mark.asyncio
async def test_probe_never_uses_real_resume_text(tmp_path):
    captured: list = []
    service = probe_service(tmp_path, capture=captured)
    report = await service.probe(connection("deepseek"))
    assert report.text.ok is True
    serialized = json.dumps(captured, ensure_ascii=False)
    assert "测试候选人" in serialized
    assert "真实" not in serialized


@pytest.mark.asyncio
async def test_kimi_code_402_has_membership_action(tmp_path):
    service = probe_service(tmp_path, chat_status=402)
    report = await service.probe(connection("kimi_code"))
    assert report.text.ok is False
    assert report.auth.error_code == "E_KIMI_MEMBERSHIP"
    assert "会员" in report.auth.suggested_action


@pytest.mark.asyncio
async def test_probe_uses_reasoning_model_for_reasoning_probe(tmp_path):
    captured: list = []
    service = probe_service(tmp_path, capture=captured, chat_content='{"ok": true}')
    conn = AiConnection(
        connection_id="c1",
        provider_id="deepseek",
        display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={
            ModelRole.FAST_TEXT: "deepseek-v4-flash",
            ModelRole.REASONING_TEXT: "deepseek-v4-pro",
            ModelRole.VISION: "deepseek-v4-flash-vision-exp",
        },
    )
    report = await service.probe(conn)

    chat_models = []
    for req in captured:
        if req["url"].endswith("/chat/completions"):
            chat_models.append(json.loads(req["content"])["model"])

    assert "deepseek-v4-flash" in chat_models
    assert "deepseek-v4-pro" in chat_models
    assert "deepseek-v4-flash-vision-exp" in chat_models
    assert report.reasoning.ok is True
    assert report.role_models[ModelRole.REASONING_TEXT] == "deepseek-v4-pro"
    assert report.role_models[ModelRole.VISION] == "deepseek-v4-flash-vision-exp"


@pytest.mark.asyncio
async def test_discovery_only_future_model_does_not_probe_deprecated_recommended(tmp_path):
    captured: list = []
    service = probe_service(
        tmp_path,
        models_response=httpx.Response(200, json={"data": [{"id": "future-fast"}]}),
        capture=captured,
    )
    report = await service.probe(connection("deepseek"))
    chat_models = [
        json.loads(req["content"])["model"]
        for req in captured
        if req["url"].endswith("/chat/completions")
    ]
    # /models 成功返回且不含推荐模型 → 不得继续探测已下架的 deepseek-v4-flash。
    assert "deepseek-v4-flash" not in chat_models
    assert report.role_models == {}
    assert report.auth.error_code == "E_AI_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_pure_vision_connection_probes_vision(tmp_path):
    captured: list = []
    service = probe_service(
        tmp_path,
        capture=captured,
        models_response=httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash-vision-exp"}]}),
        chat_content='{"ok": true}',
    )
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={ModelRole.VISION: "deepseek-v4-flash-vision-exp"},
    )
    report = await service.probe(conn)
    assert report.vision.ok is True
    assert report.text.ok is False  # fast_text 未探测
    assert report.json.ok is False
    assert probed_roles(report, conn) == frozenset({ModelRole.VISION})


@pytest.mark.asyncio
async def test_pure_reasoning_connection_probes_reasoning(tmp_path):
    service = probe_service(
        tmp_path,
        models_response=httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]}),
        chat_content='{"ok": true}',
    )
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={ModelRole.REASONING_TEXT: "deepseek-v4-pro"},
    )
    report = await service.probe(conn)
    assert report.reasoning.ok is True
    assert report.text.ok is False  # fast_text 未探测
    assert probed_roles(report, conn) == frozenset({ModelRole.REASONING_TEXT})


@pytest.mark.asyncio
async def test_fast_failure_still_probes_other_roles(tmp_path):
    captured: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-v4-flash"},
                {"id": "deepseek-v4-pro"},
                {"id": "deepseek-v4-flash-vision-exp"},
            ]})
        model = json.loads(request.content)["model"]
        captured.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProbeService(CatalogService(cache_path=tmp_path / "catalog.json"), client)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={
            ModelRole.FAST_TEXT: "deepseek-v4-flash",
            ModelRole.REASONING_TEXT: "deepseek-v4-pro",
            ModelRole.VISION: "deepseek-v4-flash-vision-exp",
        },
    )
    report = await service.probe(conn)
    # fast_text 失败，但 reasoning/vision 仍独立探测成功。
    assert report.text.ok is False
    assert report.reasoning.ok is True
    assert report.vision.ok is True
    assert "deepseek-v4-pro" in captured
    assert "deepseek-v4-flash-vision-exp" in captured


@pytest.mark.asyncio
async def test_discovery_intersection_selects_same_role_catalog_model(tmp_path):
    import importlib.resources
    raw = importlib.resources.files("kerui_recruit.providers.ai").joinpath("provider_catalog.builtin.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    data["version"] = int(data["version"]) + 1
    data["providers"]["deepseek"]["models"]["deepseek-v5-flash"] = {
        "model_id": "deepseek-v5-flash",
        "roles": ["fast_text"],
        "supported_reasoning_modes": ["off"],
        "supported_reasoning_efforts": [],
        "supports_json_schema": True,
        "supports_json_object": True,
        "supports_temperature": True,
        "deprecated": False,
    }
    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps(data), encoding="utf-8")

    captured: list = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-v5-flash"}]})
        captured.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProbeService(CatalogService(cache_path=cache), client)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
    )
    report = await service.probe(conn)
    # 推荐 deepseek-v4-flash 不在 discovered，交集确定性选择 deepseek-v5-flash。
    assert report.role_models[ModelRole.FAST_TEXT] == "deepseek-v5-flash"
    assert "deepseek-v5-flash" in captured
