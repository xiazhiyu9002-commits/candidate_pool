"""模型发现与无隐私能力探测。

- 发现仅确立模型存在性，不确立能力；``/models`` 不可用时回退目录。
- 探测只使用固定虚构文本、固定 JSON 与内置小图，绝不使用真实候选人数据。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel

from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.catalog_models import ModelProfile, ProviderPreset
from kerui_recruit.providers.ai.config_models import AiConnection
from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationRequest,
    ModelRole,
    OutputMode,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.openai_chat import OpenAIChatAdapter
from kerui_recruit.providers.errors import ProviderError

_PROBE_TEXT = (
    "测试候选人\n"
    "技能：Python、SQL\n"
    "工作经历：软件工程师，负责业务系统开发与维护，5 年工作经验。\n"
    "教育经历：计算机科学与技术，本科。"
)


class _ProbeJson(BaseModel):
    ok: bool


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    model_id: str
    source: str = "catalog"


@dataclass(frozen=True, slots=True)
class CapabilityProbe:
    ok: bool
    error_code: str | None = None
    suggested_action: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionProbeReport:
    auth: CapabilityProbe
    text: CapabilityProbe
    json: CapabilityProbe
    reasoning: CapabilityProbe
    vision: CapabilityProbe
    models: tuple[DiscoveredModel, ...] = field(default_factory=tuple)
    role_models: dict[ModelRole, str] = field(default_factory=dict)


def probed_roles(report: ConnectionProbeReport, connection: AiConnection) -> frozenset[ModelRole]:
    """根据探测报告计算连接上真正可用的角色（仅限连接已分配模型的能力）。"""
    roles: set[ModelRole] = set()
    for role in connection.models:
        if role == ModelRole.FAST_TEXT and report.text.ok and report.json.ok:
            roles.add(role)
        elif role == ModelRole.REASONING_TEXT and report.reasoning.ok:
            roles.add(role)
        elif role == ModelRole.VISION and report.vision.ok:
            roles.add(role)
    return frozenset(roles)


def _credential_fingerprint(api_key: str) -> str:
    """凭据指纹：仅用于服务端比对，绝不返回前端或出现在 token/响应中。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _ProbeReceipt:
    """服务端短期探测回执，绑定连接身份与探测结果。"""
    provider_id: str
    credential_fingerprint: str
    resolved_base_url: str
    parameter_style: str
    role_models: dict[ModelRole, str]
    report: ConnectionProbeReport
    expires_at: float


_MAX_CACHE_ENTRIES = 256


class AiProbeService:
    def __init__(self, catalog_service: CatalogService, http_client: httpx.AsyncClient) -> None:
        self.catalog_service = catalog_service
        self.http_client = http_client
        # 短期探测回执缓存：随机不透明 token → 回执。
        self._cache: dict[str, _ProbeReceipt] = {}
        self._cache_ttl = 120.0

    def _receipt_key(self, connection: AiConnection) -> tuple[str, str, str, str]:
        preset = self._preset(connection.provider_id)
        style = connection.parameter_style or (preset.parameter_style if preset else "standard")
        return (
            connection.provider_id,
            _credential_fingerprint(connection.api_key.get_secret_value()),
            self._base_url(connection),
            style,
        )

    async def probe_cached(self, connection: AiConnection) -> tuple[str, ConnectionProbeReport]:
        """探测并生成随机不透明回执；返回 (token, report)。"""
        report = await self.probe(connection)
        provider_id, fp, base_url, style = self._receipt_key(connection)
        token = secrets.token_hex(32)
        self._cache[token] = _ProbeReceipt(
            provider_id=provider_id,
            credential_fingerprint=fp,
            resolved_base_url=base_url,
            parameter_style=style,
            role_models=dict(report.role_models),
            report=report,
            expires_at=time.monotonic() + self._cache_ttl,
        )
        self._evict()
        return token, report

    def cached_probe(self, connection: AiConnection, token: str) -> ConnectionProbeReport | None:
        """校验回执与连接身份绑定，且提交的 role→model 与回执探测映射一致。"""
        receipt = self._cache.get(token)
        if receipt is None or receipt.expires_at <= time.monotonic():
            return None
        if self._receipt_key(connection) != (
            receipt.provider_id,
            receipt.credential_fingerprint,
            receipt.resolved_base_url,
            receipt.parameter_style,
        ):
            return None
        # 提交的每个 role→model 必须与回执实际探测一致；可保存子集，但不得换模型/换角色。
        for role, model in connection.models.items():
            if receipt.role_models.get(role) != model:
                return None
        return receipt.report

    def _evict(self) -> None:
        """过期清理 + 数量上限（按最早过期淘汰）。"""
        now = time.monotonic()
        for token in [t for t, r in self._cache.items() if r.expires_at <= now]:
            self._cache.pop(token, None)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            oldest = min(self._cache.items(), key=lambda kv: kv[1].expires_at)[0]
            self._cache.pop(oldest, None)

    def _preset(self, provider_id: str) -> ProviderPreset | None:
        return self.catalog_service.load().providers.get(provider_id)

    def _base_url(self, connection: AiConnection) -> str:
        if connection.base_url_override:
            return connection.base_url_override.rstrip("/")
        preset = self._preset(connection.provider_id)
        return (preset.base_url if preset else "").rstrip("/")

    async def discover(self, connection: AiConnection) -> list[DiscoveredModel]:
        base_url = self._base_url(connection)
        if base_url:
            try:
                response = await self.http_client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {connection.api_key.get_secret_value()}"},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                )
                if response.status_code == 200:
                    data = response.json()
                    ids = [
                        item["id"]
                        for item in data.get("data", [])
                        if isinstance(item, dict) and item.get("id")
                    ]
                    if ids:
                        return [DiscoveredModel(model_id=item, source="discovered") for item in ids]
            except Exception:
                pass
        preset = self._preset(connection.provider_id)
        if preset is None:
            return []
        return [DiscoveredModel(model_id=model_id, source="catalog") for model_id in preset.models]

    async def probe(self, connection: AiConnection) -> ConnectionProbeReport:
        preset = self._preset(connection.provider_id)
        style = connection.parameter_style or (preset.parameter_style if preset else "standard")
        base_url = self._base_url(connection)
        api_key = connection.api_key.get_secret_value()
        discovered = await self.discover(connection)
        discovered_ids = {item.model_id for item in discovered}
        discovery_succeeded = any(item.source == "discovered" for item in discovered)

        def _select(role: ModelRole) -> str:
            # 优先级：用户明确指定 → 发现列表中仍存在且未弃用的推荐 → 目录同角色交集。
            if role in connection.models:
                return connection.models[role]
            if preset is None:
                return ""
            recommended = preset.recommended_models.get(role, "")
            if recommended:
                rec_profile = preset.models.get(recommended)
                if rec_profile is not None and not rec_profile.deprecated:
                    if not discovery_succeeded or recommended in discovered_ids:
                        return recommended
            # 推荐模型缺失/下架：在 discovered_ids 与目录同角色未弃用模型的交集中做确定性选择。
            candidates = sorted(
                mid for mid in discovered_ids
                if (profile := preset.models.get(mid)) is not None
                and role in profile.roles
                and not profile.deprecated
            )
            return candidates[0] if candidates else ""

        fast_model = _select(ModelRole.FAST_TEXT)
        reasoning_model = _select(ModelRole.REASONING_TEXT)
        vision_model = _select(ModelRole.VISION)

        role_models: dict[ModelRole, str] = {}
        if fast_model:
            role_models[ModelRole.FAST_TEXT] = fast_model
        if reasoning_model:
            role_models[ModelRole.REASONING_TEXT] = reasoning_model
        if vision_model:
            role_models[ModelRole.VISION] = vision_model

        fast_profile = self._profile(preset, fast_model) if preset else self._generic_profile(fast_model)
        reasoning_profile = self._profile(preset, reasoning_model) if preset else self._generic_profile(reasoning_model)
        vision_profile = self._profile(preset, vision_model) if preset else self._generic_profile(vision_model)

        # 认证探测：由该连接首次实际能力请求确定。
        auth = CapabilityProbe(ok=True)
        text = CapabilityProbe(ok=False)
        json_probe = CapabilityProbe(ok=False)
        reasoning = CapabilityProbe(ok=False)
        vision = CapabilityProbe(ok=False)

        if not (fast_model or reasoning_model or vision_model) or not base_url:
            auth = CapabilityProbe(ok=False, error_code="E_AI_NOT_CONFIGURED", suggested_action="请填写 API Key 并选择模型")
            return ConnectionProbeReport(auth=auth, text=text, json=json_probe, reasoning=reasoning, vision=vision, role_models=role_models, models=tuple(discovered))

        adapter = OpenAIChatAdapter(
            base_url=base_url, api_key=api_key, parameter_style=style,
            http_client=self.http_client, connection_id=connection.connection_id,
            provider_id=connection.provider_id,
        )

        first_request: CapabilityProbe | None = None

        # fast_text：文本与 JSON 两项都必须通过。
        if fast_model:
            text = await self._probe_capability(adapter, fast_model, fast_profile, _text_request(), response_model=None)
            first_request = text
            if text.ok:
                json_probe = await self._probe_capability(adapter, fast_model, fast_profile, _json_request(), response_model=_ProbeJson)

        # 思考探测：独立进行，不因 fast_text 缺失或失败而跳过。
        if reasoning_model:
            reasoning = await self._probe_capability(adapter, reasoning_model, reasoning_profile, _reasoning_request(), response_model=None)
            if first_request is None:
                first_request = reasoning

        # 视觉探测：独立进行。
        if vision_model:
            vision = await self._probe_capability(adapter, vision_model, vision_profile, _vision_request(), response_model=None)
            if first_request is None:
                first_request = vision

        if first_request is not None and not first_request.ok:
            auth = self._classify(connection, first_request)

        return ConnectionProbeReport(auth=auth, text=text, json=json_probe, reasoning=reasoning, vision=vision, role_models=role_models, models=tuple(discovered))

    async def _probe_capability(
        self,
        adapter: OpenAIChatAdapter,
        model: str,
        profile: ModelProfile,
        request: GenerationRequest,
        *,
        response_model: type[BaseModel] | None,
    ) -> CapabilityProbe:
        req = GenerationRequest(
            messages=request.messages,
            role=request.role,
            task_kind=request.task_kind,
            execution_context=ExecutionContext.INTERACTIVE,
            output_mode=OutputMode.JSON if response_model is not None else OutputMode.TEXT,
            reasoning=request.reasoning,
            response_model=response_model,
        )
        try:
            await adapter.generate(req, model=model, profile=profile)
            return CapabilityProbe(ok=True)
        except ProviderError as error:
            return CapabilityProbe(ok=False, error_code=error.code, suggested_action=error.user_message)

    def _classify(self, connection: AiConnection, probe: CapabilityProbe) -> CapabilityProbe:
        if connection.provider_id == "kimi_code" and probe.error_code == "E_API_BALANCE":
            return CapabilityProbe(
                ok=False,
                error_code="E_KIMI_MEMBERSHIP",
                suggested_action="会员状态或订阅额度异常，请检查 Kimi Code 会员资格与用量",
            )
        return probe

    def _profile(self, preset: ProviderPreset | None, model_id: str) -> ModelProfile:
        if not model_id:
            return self._generic_profile("")
        if preset is not None and model_id in preset.models:
            return preset.models[model_id]
        return self._generic_profile(model_id)

    def _generic_profile(self, model_id: str) -> ModelProfile:
        # 未知模型不做任何能力假设：不默认支持 JSON Object、温度或思考强度。
        return ModelProfile(
            model_id=model_id,
            roles=frozenset({ModelRole.FAST_TEXT}),
            supported_reasoning_modes=frozenset({ReasoningMode.OFF}),
            supports_json_object=False,
            supports_temperature=False,
        )


def _text_request() -> GenerationRequest:
    return GenerationRequest(
        messages=[{"role": "user", "content": _PROBE_TEXT}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.RESUME_PARSE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.OFF,
    )


def _json_request() -> GenerationRequest:
    return GenerationRequest(
        messages=[{"role": "user", "content": '只回复 JSON，格式 {"ok": true}'}],
        role=ModelRole.FAST_TEXT,
        task_kind=TaskKind.RESUME_PARSE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.JSON,
        reasoning=ReasoningMode.OFF,
        response_model=_ProbeJson,
    )


def _reasoning_request() -> GenerationRequest:
    return GenerationRequest(
        messages=[{"role": "user", "content": _PROBE_TEXT}],
        role=ModelRole.REASONING_TEXT,
        task_kind=TaskKind.BD_PLAN,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.REQUIRED,
    )


def _vision_request() -> GenerationRequest:
    # 内置 64x64 纯色 PNG 测试图，不含任何真实候选人资料。
    # 尺寸需 ≥ 28x28：GLM-VL 系列会拒绝过小的图片（height/width must be larger than 28）。
    tiny_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAT0lEQVR42u3PQQkAAAgEsMtpEoMZ0Ai+hcEKLNP1WgQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQELgtHTGFpNwapEwAAAABJRU5ErkJggg=="
    )
    return GenerationRequest(
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "请确认你能看到图片"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}},
            ],
        }],
        role=ModelRole.VISION,
        task_kind=TaskKind.VISION_PARSE,
        execution_context=ExecutionContext.INTERACTIVE,
        output_mode=OutputMode.TEXT,
        reasoning=ReasoningMode.OFF,
    )
