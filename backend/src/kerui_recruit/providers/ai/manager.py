"""生成式 AI 管理器：配置热加载、不可变快照、主备路由与生命周期。

- 持有当前不可变快照（路由 + 解析出的目标 + 无密钥 cache_identity）。
- ``generate()`` 每次请求开始时读取一个快照引用；进行中的请求保留其本地旧快照。
- ``update_config()`` 校验并保存候选配置后在锁内原子替换快照，下一次请求立即生效。
"""
from __future__ import annotations

import asyncio
import time
import weakref
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
    ReasoningEffort,
    ReasoningMode,
    TaskKind,
)
from kerui_recruit.providers.ai.openai_chat import OpenAIChatAdapter
from kerui_recruit.providers.ai.pacing import THROTTLE_ERROR_CODES, GenerationPacer
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers.ai.router import AiProviderRouter, RouterTarget
from kerui_recruit.providers.ai.task_client import TaskGenerationClient
from kerui_recruit.providers.errors import FailureCategory, ProviderError


@dataclass(frozen=True, slots=True)
class AiProviderSnapshot:
    config: AiProviderConfig
    router: AiProviderRouter
    cache_identity: str


# 「没有限流」与「限流但没给 Retry-After」需要区分，所以不能直接用 None 当哨兵。
_NO_THROTTLE = object()

# 限流在生成链路内部消化的次数上限。取 2：低配额账号（实测 3 RPM，即 20 秒一次）
# 多等两轮就够跨过窗口；再多只是把已经注定失败的请求拖长。
_MAX_THROTTLE_RETRIES = 2

# 哪些限流允许在链路内部消化：**只收 429（配额/频率）**。
#
# 为什么不收 `E_API_BUSY`（503）：503 的含义是「对端临时繁忙」，路由自身的重试与
# 熔断已经覆盖它；而且第一次失败后熔断往往已经打开，紧接着重试只会拿到
# `E_AI_NO_PROVIDER`，把真实的 503 掩盖成一个更含糊的「没有可用服务」
# （`tests/providers/ai/test_failover_e2e.py` 里「两边都 503」那条就这么暴露了）。
# 429 不同：它明确说明「配额窗口还没过」，而任务层的重试间隔（1s/5s/30s/2min）
# 与窗口长度（实测该账号 3 RPM = 20 秒）根本不匹配，必须在这里等。
_ABSORBED_THROTTLE_CODES = frozenset({"E_API_RATE_LIMIT"})


def _throttled_retry_after(error: ProviderError):
    """从错误里提取限流信号：返回 Retry-After 秒数（可能是 None），未限流返回 `_NO_THROTTLE`。

    **必须看 `details` 而不能只看外层错误码**：路由在全部目标失败后抛的是聚合错误
    `E_AI_ALL_PROVIDERS_FAILED`，逐次尝试的真实错误码（如 `E_API_RATE_LIMIT`）只在
    `details` 里。单供应商是最常见情形，只看外层等于永远检测不到限流。
    """
    if error.code in THROTTLE_ERROR_CODES:
        return error.retry_after_seconds
    for detail in error.details or ():
        if isinstance(detail, dict) and detail.get("error_code") in THROTTLE_ERROR_CODES:
            return error.retry_after_seconds
    return _NO_THROTTLE


def _absorbable_retry_after(error: ProviderError):
    """能被链路内部消化的限流：返回 Retry-After（可能 None），否则 `_NO_THROTTLE`。

    与 `_throttled_retry_after` 的区别是集合更窄（只看 429）——限速器要对 503 也降速，
    但重试只对 429 做。判据同样必须看 `details`，理由见上。
    """
    if error.code in _ABSORBED_THROTTLE_CODES:
        return error.retry_after_seconds
    for detail in error.details or ():
        if isinstance(detail, dict) and detail.get("error_code") in _ABSORBED_THROTTLE_CODES:
            return error.retry_after_seconds
    return _NO_THROTTLE


def _rate_limit_cooldown_remaining(circuit_breaker: CircuitBreaker, now: float) -> float:
    """熔断里由 429 造成的剩余冷却时间（秒）。

    必须把它算进内部重试的等待：一次 429 会**同时**触发熔断（连接级冷却 60 秒，
    对端给了 Retry-After 就按它的值）与限速器降速。只等限速器的话，重试会立刻撞上
    「熔断打开」→ 路由选不出目标 → `E_AI_NO_PROVIDER`，把「限流还没过去」
    误报成「没有可用的 AI 服务」，反而更难排查。
    """
    return max(
        (health.cooldown_until - now
         for health in circuit_breaker.status().connections
         if health.last_error_code == FailureCategory.RATE_LIMIT.value),
        default=0.0,
    )


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
        # 按事件循环隔离的客户端表（键是循环对象，弱引用：循环被回收时自动消失）。
        # 详见 `_http_client_for_current_loop` 的理由说明。
        self._loop_clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._base_client_claimed = False
        self._circuit_breaker = circuit_breaker or CircuitBreaker(clock=clock)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._revision = 0
        # 生成链路的并发 + 自适应限速，所有 worker 共用这一份。
        self._pacer = GenerationPacer()
        self._snapshot = self._build_snapshot(config_store.load())

    @property
    def cache_identity(self) -> str:
        return self._snapshot.cache_identity

    def _http_client_for_current_loop(self) -> httpx.AsyncClient:
        """按**当前运行的事件循环**取一个 http 客户端。

        为什么要这么绕：httpx 客户端不能跨事件循环复用。同步路径里的 `asyncio.run(...)`
        （`providers/leads.py:extract`、`mail/resume_gate.py`）会现开一个短命循环，而调度器
        又在 `asyncio.to_thread` 里跑这些同步函数；连接池里那些建立在短命循环上的连接，
        在循环关闭后被主循环复用就会抛 `Event loop is closed`——实测表现为
        `org.import_parse` 偶发 500，以及探针退出阶段 `ai_manager.close()` 报同一句错。

        规则：**第一个来认领的循环拿到注入的那个客户端**（生产里就是主循环；测试里就是注入
        `MockTransport` 的那个），之后每个新循环各拿一份自己的。这样跨循环调用不再互相污染，
        而正常路径仍然复用同一个长连接池。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的循环（同步上下文直接调用）：只能用注入的那个。
            return self._http_client
        existing = self._loop_clients.get(loop)
        if existing is not None:
            return existing
        if not self._base_client_claimed:
            self._base_client_claimed = True
            self._loop_clients[loop] = self._http_client
            return self._http_client
        client = httpx.AsyncClient()
        self._loop_clients[loop] = client
        return client

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
        reasoning_effort: ReasoningEffort | None = None,
        prefer_off: bool = False,
    ) -> TaskGenerationClient:
        """构造任务客户端。

        ``reasoning_effort`` 是该任务的**显式档位**，``prefer_off=True`` 表示「宁可关思考」：
        能关掉思考的模型一律关掉、槽位配置的强度不参与，档位只在关不掉时用。
        只有「已实测提高强度不改变结果」的机械任务才应该这么传（见 `search/parse.py`）。
        """
        return TaskGenerationClient(
            self,
            task_kind=task_kind,
            role=role,
            execution_context=execution_context,
            reasoning=reasoning,
            reasoning_effort=reasoning_effort,
            prefer_off=prefer_off,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """所有生成调用的唯一入口：在自适应限速器后面执行，并在链路内部消化限流。

        放在这里而不是各个调用方：生成链路（解析/画像/BD/复核）与检索链路不同，
        原先**完全没有限速**，8 个 worker 会直接齐发；对着只有 3 RPM 的账号
        （实测 Kimi 开放平台）这不叫并发、只叫互相踩。限速器按对端反馈自适应，
        对高配额账号不会平白拖慢。

        **为什么限流不能直接上抛给任务层**：任务重试间隔是 1s / 5s / 30s / 2min / 10min，
        而限流窗口由对端配额决定（实测该 Kimi 账号 3 RPM，即 20 秒才轮到一次）。
        429 直接上抛会让 5 次尝试全部落在同一个窗口里耗尽、整批判失败——2026-09-22 实测
        就是「5 份里 4 份失败」，而同一批里唯一撑到第 4 次尝试的那份是成功的。
        这里按限速器的节奏等待后重试，让低配额账号表现为「慢但成功」而不是「失败」。
        """
        for _ in range(_MAX_THROTTLE_RETRIES):
            try:
                result = await self._generate_once(request)
            except ProviderError as error:
                retry_after = _throttled_retry_after(error)
                if retry_after is not _NO_THROTTLE:
                    # 等待取「对端说的 Retry-After」与「熔断剩余冷却」的较大者：
                    # 只等前者会立刻撞上熔断，重试还没发出去就变成 E_AI_NO_PROVIDER。
                    now = time.monotonic()
                    self._pacer.record_throttled(max(
                        retry_after or 0.0,
                        _rate_limit_cooldown_remaining(self._circuit_breaker, now),
                    ))
                if _absorbable_retry_after(error) is _NO_THROTTLE:
                    raise
                continue
            self._pacer.record_success()
            return result
        # 最后一次不再兜：照实抛出，但仍然把限流反馈给限速器。
        try:
            result = await self._generate_once(request)
        except ProviderError as error:
            retry_after = _throttled_retry_after(error)
            if retry_after is not _NO_THROTTLE:
                self._pacer.record_throttled(retry_after)
            raise
        self._pacer.record_success()
        return result

    async def _generate_once(self, request: GenerationRequest) -> GenerationResult:
        try:
            async with self._pacer.slot(request.deadline_monotonic):
                return await self._snapshot.router.generate(request)
        except TimeoutError as error:
            # 限速器的等待吃掉了整个请求预算。必须转成 ProviderError：否则会以一个裸
            # TimeoutError 逃到只捕获 ProviderError 的调用方，被当成「未知异常」处理，
            # 而真实含义是「预算耗尽，别再重试了」。
            raise ProviderError(
                code="E_AI_DEADLINE",
                retryable=False,
                user_message="请求预算已耗尽",
                category=FailureCategory.DEADLINE,
                switchable=False,
            ) from error

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
        """关闭注入的客户端。

        按循环新建的那些**只丢弃引用、不关闭**：它们的循环（`asyncio.run` 的短命循环）早已
        关闭，在别的循环里 `await aclose()` 只会再次触发 `Event loop is closed`——这正是
        实测里探针退出阶段那条报错的来源。它们的连接随循环一起作废，进程退出时由操作系统回收。
        """
        self._loop_clients.clear()
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
                    client_provider=self._http_client_for_current_loop,
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
                    reasoning_effort=connection.reasoning_efforts.get(role),
                ))
                resolved.append(f"{connection.connection_id}:{connection.provider_id}:{role.value}:{model_id}")
        router = AiProviderRouter(targets, circuit_breaker=self._circuit_breaker, clock=self._clock)
        identity = f"rev{self._revision}|{','.join(sorted(resolved))}"
        return AiProviderSnapshot(config=config, router=router, cache_identity=identity)
