from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.config_models import AiConnection
from kerui_recruit.providers.ai.contracts import ModelRole
from kerui_recruit.providers.ai.probes import AiProbeService, probed_roles

# JSON 能力探测现在要求「业务同形对象」，不再接受裸 {"ok": true}。
# 夹具统一用这个载荷，避免测试在不知不觉中走「json 探测失败」的分支。
PROBE_JSON_OK = '{"name": "张三", "skills": ["Python"], "total_years": 5}'


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
    chat_content: str = PROBE_JSON_OK,
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
    service = probe_service(tmp_path, capture=captured, chat_content=PROBE_JSON_OK)
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
    assert report.json.ok is True
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
        chat_content=PROBE_JSON_OK,
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
        chat_content=PROBE_JSON_OK,
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
        return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})

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
        return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})

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
    assert report.json.ok is True


@pytest.mark.asyncio
async def test_json_probe_requires_business_shaped_payload(tmp_path):
    """JSON 探测要的是「能回业务同形 JSON」，不是「能回 JSON」。

    原先只要求回 ``{"ok": true}``：能回它不代表能回简历/JD 那种大 schema，
    于是出现「检测显示可用、真实解析一直失败」的错位。这里断言弱载荷不再放行。
    """
    service = probe_service(tmp_path, chat_content='{"ok": true}')
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={ModelRole.FAST_TEXT: "deepseek-v4-flash"},
    )

    report = await service.probe(conn)

    assert report.text.ok is True
    assert report.json.ok is False
    # fast_text 需要 text + json 双通过：弱 JSON 载荷下这个角色不该被判为可用。
    assert probed_roles(report, conn) == frozenset()


@pytest.mark.asyncio
async def test_capability_probes_run_concurrently(tmp_path):
    """三项能力探测必须并发发起。

    串行是「配置 API 时检测太慢」的直接原因（每项最坏 300 秒，四项串行最坏 20 分钟）。
    这里让 handler 必须等齐 3 个请求才放行：只要有一项是等前一项结束后才发起的，
    它就会等不到同伴并最终失败，测试随之失败。
    """
    arrived: set[str] = set()
    all_arrived = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "m-fast"}, {"id": "m-pro"}, {"id": "m-vision"},
            ]})
        arrived.add(json.loads(request.content)["model"])
        if len(arrived) >= 3:
            all_arrived.set()
        try:
            await asyncio.wait_for(all_arrived.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            # 串行实现会走到这里；不抛错，让断言去指出问题。
            pass
        return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProbeService(CatalogService(cache_path=tmp_path / "catalog.json"), client)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"),
        models={
            ModelRole.FAST_TEXT: "m-fast",
            ModelRole.REASONING_TEXT: "m-pro",
            ModelRole.VISION: "m-vision",
        },
    )

    report = await service.probe(conn)

    assert arrived == {"m-fast", "m-pro", "m-vision"}
    assert report.text.ok and report.reasoning.ok and report.vision.ok


@pytest.mark.asyncio
async def test_throttled_concurrent_probe_is_retried_serially(tmp_path, monkeypatch):
    """并发探测被限流时必须**串行重试**再下定论，不能把 429 当成「能力不可用」。

    实测（2026-09-22）：Kimi 开放平台在并发探测下 reasoning 与 vision 双双返回 429，
    矩阵显示两项不可用；而紧接着的 5 份简历（含 2 份 PDF）全部解析成功——矩阵与真实
    结果自相矛盾，属于问题 #8 那一类误导。
    """
    monkeypatch.setattr("kerui_recruit.providers.ai.probes._RETRY_BACKOFF_SECONDS", 0.0)
    chat_calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "m-fast"}, {"id": "m-pro"}, {"id": "m-vision"},
            ]})
        chat_calls.append(1)
        # 并发首发的三个请求全被限流；之后的串行重试放行。
        if len(chat_calls) <= 3:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": PROBE_JSON_OK}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProbeService(CatalogService(cache_path=tmp_path / "catalog.json"), client)
    conn = AiConnection(
        connection_id="c1", provider_id="kimi_open", display_name="Kimi",
        api_key=SecretStr("k"),
        models={
            ModelRole.FAST_TEXT: "m-fast",
            ModelRole.REASONING_TEXT: "m-pro",
            ModelRole.VISION: "m-vision",
        },
    )

    report = await service.probe(conn)

    assert report.text.ok and report.reasoning.ok and report.vision.ok
    # 文本是重试后才通过的 → json 探测当时根本没轮到，必须补跑，否则矩阵又会显示 json=false。
    assert report.json.ok
    # 鉴权取的是重试后的结果，不能被重试前的 429 判成鉴权失败。
    assert report.auth.ok


@pytest.mark.asyncio
async def test_persistent_rate_limit_still_reports_unavailable(tmp_path, monkeypatch):
    """限流一直不解除时仍如实报「不可用」——串行重试是为了纠假阴性，不是掩盖真失败。"""
    monkeypatch.setattr("kerui_recruit.providers.ai.probes._RETRY_BACKOFF_SECONDS", 0.0)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m-fast"}]})
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProbeService(CatalogService(cache_path=tmp_path / "catalog.json"), client)
    conn = AiConnection(
        connection_id="c1", provider_id="deepseek", display_name="DeepSeek",
        api_key=SecretStr("k"), models={ModelRole.FAST_TEXT: "m-fast"},
    )

    report = await service.probe(conn)

    assert report.text.ok is False
    assert report.text.error_code == "E_API_RATE_LIMIT"
    assert report.auth.ok is False
