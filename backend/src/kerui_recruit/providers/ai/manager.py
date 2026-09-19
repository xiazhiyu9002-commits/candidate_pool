"""生成式 AI 管理器：配置热加载、不可变快照、主备路由与生命周期。

- 持有当前不可变快照（路由 + 解析出的目标 + 无密钥 cache_identity）。
- ``generate()`` 每次请求开始时读取一个快照引用；进行中的请求保留其本地旧快照。
- ``update_config()`` 校验并保存候选配置后在锁内原子替换快照，下一次请求立即生效。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.catalog_models import ModelProfile, can_serve_role
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker, RouterStatus
from kerui_recruit.providers.ai.config_models import AiProviderConfig
from kerui_recruit.providers.ai.config_store import AiConfigStore
from kerui_recruit.providers.ai.contracts import (
    ExecutionContext,
    GenerationRequest,
    GenerationResult,
    ModelRole,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.openai_chat import OpenAIChatAdapter
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers.ai.router import AiProviderRouter, RouterTarget
from kerui_recruit.providers.ai.task_client import TaskGenerationClient


@dataclass(frozen=True, slots=True)
class AiProviderSnapshot:
    config: AiProviderConfig
    router: AiProviderRouter
    cache_identity: str


def _resolved_profile(model_id: str, role: ModelRole) -> ModelProfile:
    """目录外模型的保守能力档案：只声明已探测角色，不做 JSON/温度/思考强度假设。"""
    reasoning_modes = (
        frozenset({ReasoningMode.REQUIRED})
        if role == ModelRole.REASONING_TEXT
        else frozenset({ReasoningMode.OFF})
    )
    return ModelProfile(
        model_id=model_id,
        roles=frozenset({role}),
        supported_reasoning_modes=reasoning_modes,
        supports_json_schema=False,
        supports_json_object=False,
        supports_temperature=False,
    )


class AiProviderManager:
    def __init__(
        self,
        *,
        config_store: AiConfigStore,
        catalog_service: CatalogService,
        probe_service: AiProbeService,
        http_client: httpx.AsyncClient,
        circuit_breaker: CircuitBreaker | None = None,
        clock=None,
    ) -> None:
        self._config_store = config_store
        self._catalog_service = catalog_service
        self._probe_service = probe_service
        self._http_client = http_client
        self._circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._revision = 0
        self._snapshot = self._build_snapshot(config_store.load())

    @property
    def cache_identity(self) -> str:
        return self._snapshot.cache_identity

    @property
    def config(self) -> AiProviderConfig:
        return self._snapshot.config

    @property
    def llm_enabled(self) -> bool:
        """是否存在至少一个实际可路由的目标（替代旧 settings.llm_enabled）。"""
        return bool(self._snapshot.router.targets)

    def serving_roles(self) -> dict[str, frozenset[ModelRole]]:
        """按 connection_id 返回实际可路由角色（依据 RouterTarget，而非 config.probed_roles）。"""
        result: dict[str, set[ModelRole]] = {}
        for target in self._snapshot.router.targets:
            result.setdefault(target.connection_id, set()).add(target.role)
        return {cid: frozenset(roles) for cid, roles in result.items()}

    def task_client(
        self,
        task_kind: TaskKind,
        role: ModelRole,
        execution_context: ExecutionContext,
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> TaskGenerationClient:
        return TaskGenerationClient(
            self,
            task_kind=task_kind,
            role=role,
            execution_context=execution_context,
            reasoning=reasoning,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return await self._snapshot.router.generate(request)

    async def update_config(self, config: AiProviderConfig) -> None:
        async with self._lock:
            # 构建、持久化、revision 更新与快照替换全部串行，避免并发保存导致
            # 磁盘配置与内存快照不一致。
            self._config_store.save(config)
            self._revision += 1
            snapshot = self._build_snapshot(config)
            self._circuit_breaker.clear_model_unavailable()
            self._snapshot = snapshot

    def status(self) -> RouterStatus:
        return self._circuit_breaker.status()

    def has_role(self, role: ModelRole) -> bool:
        """是否存在至少一个实际可路由的该角色目标（以快照解析结果为准，而非 probed_roles）。"""
        return any(target.role == role for target in self._snapshot.router.targets)

    def public_view(self):
        return self._config_store.public_view()

    def catalog(self):
        return self._catalog_service.load()

    def refresh_catalog(self):
        return self._catalog_service.refresh()

    def validate_connection(self, connection) -> None:
        """探测/保存前对单个连接做统一安全校验（预设不可覆盖 Base URL/风格等）。"""
        self._config_store.validate_connection(connection)

    async def probe(self, connection) -> object:
        return await self._probe_service.probe(connection)

    async def probe_cached(self, connection):
        return await self._probe_service.probe_cached(connection)

    def cached_probe(self, connection, token):
        return self._probe_service.cached_probe(connection, token)

    async def discover(self, connection) -> list:
        return await self._probe_service.discover(connection)

    async def close(self) -> None:
        await self._http_client.aclose()

    def _build_snapshot(self, config: AiProviderConfig) -> AiProviderSnapshot:
        catalog = self._catalog_service.load()
        targets: list[RouterTarget] = []
        resolved: list[str] = []
        for connection in config.connections:
            if not connection.enabled:
                continue
            preset = catalog.providers.get(connection.provider_id)
            if preset is None:
                continue
            base_url = connection.base_url_override or preset.base_url
            for role, model_id in connection.models.items():
                if role not in connection.probed_roles:
                    continue
                profile = preset.models.get(model_id)
                if profile is None:
                    # 目录外模型（custom_openai / 未来模型）：按已探测角色构建保守档案。
                    profile = _resolved_profile(model_id, role)
                if not can_serve_role(profile, role):
                    continue
                adapter = OpenAIChatAdapter(
                    base_url=base_url,
                    api_key=connection.api_key.get_secret_value(),
                    parameter_style=connection.parameter_style or preset.parameter_style,
                    http_client=self._http_client,
                    connection_id=connection.connection_id,
                    provider_id=connection.provider_id,
                )
                targets.append(RouterTarget(
                    connection_id=connection.connection_id,
                    provider_id=connection.provider_id,
                    role=role,
                    model=model_id,
                    profile=profile,
                    adapter=adapter,
                    allowed_contexts=preset.allowed_contexts,
                ))
                resolved.append(f"{connection.connection_id}:{connection.provider_id}:{role.value}:{model_id}")
        router = AiProviderRouter(targets, circuit_breaker=self._circuit_breaker, clock=self._clock)
        identity = f"rev{self._revision}|{','.join(sorted(resolved))}"
        return AiProviderSnapshot(config=config, router=router, cache_identity=identity)
