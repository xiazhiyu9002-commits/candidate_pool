"""模型发现与无隐私能力探测。

- 发现仅确立模型存在性，不确立能力；``/models`` 不可用时回退目录。
- 探测只使用固定虚构文本、固定 JSON 与内置小图，绝不使用真实候选人数据。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
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
from kerui_recruit.providers.ai.pacing import THROTTLE_ERROR_CODES
from kerui_recruit.providers.errors import ProviderError

logger = logging.getLogger(__name__)

# 探测口径版本。口径变化（如能力项、探测载荷）时递增，让已保存的旧探测结果失效、
# 强制重探；否则用户会一直看到按旧口径算出的「可用」。
PROBE_SCHEMA_VERSION = 2

# 单次能力探测的超时预算（秒）。探测载荷都很小，不该按业务调用的 300 秒算：
# 原先每项最坏挂 300 秒、四项串行，最坏 20 分钟，这就是「配置 API 时检测太慢」。
# 现在四项并发 + 45 秒封顶，整轮最坏 ≈ 模型发现 10 秒 + 45 秒。
#
# **这 45 秒是并发首发与串行重试共用的，不从中切一块留给重试**：切了会让本来就慢的
# 供应商从「通过」变成「超时」——阿里 qwen3.8-flash 关思考后做文本探测实测 34.9 秒，
# 把首发砍到 30 秒就正好裁掉它（这是改动中真实踩过的坑）。而需要重试的限流/繁忙
# 都是**快速失败**，首发根本花不了几秒，预算天然留着。
_PROBE_TIMEOUT_SECONDS = 45.0

# 并发首发下「不能直接当结论」的错误码 —— 必须串行重试一次再下定论。
#
# 只收**快速失败**的两种（限流/繁忙，其定义与生成链路的限速器共用同一常量）：
# 它们都是对端立刻回绝，重试几乎不花预算，而并发本身就是限流的诱因，
# 所以必须给它们一次平反机会。
#
# **刻意不含 `E_API_TIMEOUT`**：超时意味着这次调用已经烧掉了几十秒，重试只会再烧一遍，
# 而实测表明这种慢是供应商固有的、不是并发造成的（阿里 qwen3.8-flash 做文本单发 73 秒、
# 并发 77~87 秒，只差约 10%）。把它当可重试会白等一轮，且掩盖真实结论。
_RETRYABLE_PROBE_ERROR_CODES = THROTTLE_ERROR_CODES
_RETRY_BACKOFF_SECONDS = 2.0
# 串行重试至少要有这么多剩余预算；否则重试只会把检测拖到上限，还不如如实报限流。
_RETRY_MIN_REMAINING_SECONDS = 8.0

_PROBE_TEXT = (
    "测试候选人\n"
    "技能：Python、SQL\n"
    "工作经历：软件工程师，负责业务系统开发与维护，5 年工作经验。\n"
    "教育经历：计算机科学与技术，本科。\n"
    "请只用一句话说明你收到了这份资料，不要展开、不要分析。"
)

# JSON 能力探测载荷提示词。要的是「能回业务同形的 JSON」，不是「能回 JSON」。
_PROBE_JSON_PROMPT = (
    "只输出一个 JSON 对象，不要 markdown 代码块，不要任何多余文字。格式：\n"
    '{"name": "张三", "skills": ["Python", "SQL"], "total_years": 5}'
)


class _ProbeJson(BaseModel):
    """JSON 能力探测的最小业务同形对象。

    原先只要求模型回 ``{"ok": true}``：能回这个不代表能回简历/JD 那种大 schema，
    于是出现「检测显示可用、真实解析一直失败」的错位。这里改成与真实解析同形的小对象
    （一个必填标量 + 一个数组 + 一个可空数值），把「能回 JSON」与「能回业务 JSON」对齐。
    """

    name: str
    skills: list[str] = []
    total_years: float | None = None


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

        # 三项能力互不依赖（json 需要 text 成功，串在 text 之后），并发发起：
        # 串行时最坏 4×300 秒，是「检测时间太长」的直接原因。
        # 共享同一个 deadline：并发意味着总耗时约等于其中最慢的一项，而不是各项之和。
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS

        text_task = (
            asyncio.create_task(
                self._probe_capability(
                    adapter, fast_model, fast_profile, _text_request(), response_model=None, deadline=deadline
                )
            )
            if fast_model
            else None
        )
        reasoning_task = (
            asyncio.create_task(
                self._probe_capability(
                    adapter, reasoning_model, reasoning_profile, _reasoning_request(), response_model=None, deadline=deadline
                )
            )
            if reasoning_model
            else None
        )
        vision_task = (
            asyncio.create_task(
                self._probe_capability(
                    adapter, vision_model, vision_profile, _vision_request(), response_model=None, deadline=deadline
                )
            )
            if vision_model
            else None
        )

        # fast_text：文本与 JSON 两项都必须通过；json 依赖 text 成功，故串行。
        if text_task is not None:
            text = await text_task
            if text.ok:
                json_probe = await self._probe_capability(
                    adapter, fast_model, fast_profile, _json_request(), response_model=_ProbeJson, deadline=deadline
                )

        if reasoning_task is not None:
            reasoning = await reasoning_task

        if vision_task is not None:
            vision = await vision_task

        # 并发只用来省时间，**不能拿并发结果当结论**：低 RPM 的供应商（实测 Kimi 开放平台）
        # 会在并发下直接回 429，把 reasoning / vision 判成「不可用」，而紧接着的真实解析
        # 5/5 全部成功（含 2 份 PDF）——矩阵与真实结果自相矛盾，正是问题 #8 那一类误导。
        # 所以限流类失败必须**串行重试一次**再下定论。
        probes = {"text": text, "reasoning": reasoning, "vision": vision}
        specs = {
            "text": (fast_model, fast_profile, _text_request()),
            "reasoning": (reasoning_model, reasoning_profile, _reasoning_request()),
            "vision": (vision_model, vision_profile, _vision_request()),
        }
        for name, (model_id, model_profile, request) in specs.items():
            current = probes[name]
            if model_id is None or current.error_code not in _RETRYABLE_PROBE_ERROR_CODES:
                continue
            if deadline - time.monotonic() < _RETRY_MIN_REMAINING_SECONDS:
                logger.warning("检测遇限流但剩余预算不足，不再串行重试：%s", name)
                continue
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            retried = await self._probe_capability(
                adapter, model_id, model_profile, request, response_model=None, deadline=deadline
            )
            if retried.ok:
                logger.info("并发探测遇限流，串行重试后通过：%s", name)
                probes[name] = retried
        text, reasoning, vision = probes["text"], probes["reasoning"], probes["vision"]
        # 文本是 JSON 探测的前置：文本这一项是**重试后**才通过的，说明当初根本没轮到 JSON 探测。
        # 这里补上，否则矩阵会显示 json=false，又是一个「矩阵与真实不一致」。
        if text.ok and not json_probe.ok and json_probe.error_code is None:
            json_probe = await self._probe_capability(
                adapter, fast_model, fast_profile, _json_request(), response_model=_ProbeJson, deadline=deadline
            )

        # auth 取「实际跑过的第一个能力」的结果，且必须用**重试后**的值：
        # 用重试前的旧值会把「限流→串行重试成功」判成鉴权失败。
        first_request = (
            probes["text"] if text_task is not None
            else probes["reasoning"] if reasoning_task is not None
            else probes["vision"]
        )
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
        deadline: float,
    ) -> CapabilityProbe:
        req = GenerationRequest(
            messages=request.messages,
            role=request.role,
            task_kind=request.task_kind,
            execution_context=ExecutionContext.INTERACTIVE,
            output_mode=OutputMode.JSON if response_model is not None else OutputMode.TEXT,
            reasoning=request.reasoning,
            response_model=response_model,
            # 探测封顶：不传 deadline 时适配器会按业务调用的 300 秒算，一项就够拖垮整轮检测。
            deadline_monotonic=deadline,
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
        messages=[{"role": "user", "content": _PROBE_JSON_PROMPT}],
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
