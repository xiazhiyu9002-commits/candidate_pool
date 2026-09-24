from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.jd.structured import JdParser
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, TaskKind
from kerui_recruit.providers.contracts import EmbeddingProvider, OCRProvider, RerankerProvider
from kerui_recruit.providers.errors import ProviderError
from kerui_recruit.providers.generation_tasks import AiJdParser, AiResumeParser
from kerui_recruit.providers.local import (
    LocalHashEmbeddingProvider,
    LocalJdParser,
    LocalKeywordReranker,
    LocalResumeParser,
)
from kerui_recruit.providers.ocr import OpenAICompatibleOCRProvider
from kerui_recruit.providers.siliconflow import (
    SiliconFlowEmbeddingProvider,
    SiliconFlowRerankerProvider,
)
from kerui_recruit.providers.vision_parse import VisionStructuredParser
from kerui_recruit.resumes.structured import ResumeParser

LOCAL_VECTOR_DIMENSION = 64


@dataclass(slots=True)
class ProviderBundle:
    parser: ResumeParser
    jd_parser: JdParser
    embedding: EmbeddingProvider
    reranker: RerankerProvider
    ocr: OCRProvider | None
    vision_parser: object | None
    vector_dimension: int
    http_client: httpx.AsyncClient | None


class _VisionClient:
    """每次业务请求检查 manager 当前快照的视觉代理。

    无视觉路由时抛出降级错误，避免视觉配置保存/停用后仍需重启才能生效。
    """

    def __init__(self, manager, task_client, *, no_route_code: str, no_route_message: str) -> None:
        self._manager = manager
        self._task_client = task_client
        self._no_route_code = no_route_code
        self._no_route_message = no_route_message

    async def complete_text(self, messages, temperature=None, *, execution_context=None, deadline_monotonic=None):
        if not self._manager.has_role(ModelRole.VISION):
            raise ProviderError(
                code=self._no_route_code, retryable=False,
                user_message=self._no_route_message, switchable=False,
            )
        return await self._task_client.complete_text(
            messages, temperature=temperature,
            execution_context=execution_context, deadline_monotonic=deadline_monotonic,
        )


class _RoutedResumeParser:
    """远程解析优先，无可选路由时回退到本地确定性解析。"""

    def __init__(self, manager, local: ResumeParser) -> None:
        self._manager = manager
        self._remote = AiResumeParser(manager.task_client(
            TaskKind.RESUME_PARSE, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND,
        ))
        self._local = local

    def uses_remote_ai(self) -> bool:
        """当前是否真有可用的远程快速解析路由。

        没有时 `parse_resume` 会回退本地确定性解析（不调用任何模型）。调用方
        （`resumes/pipeline.py`）据此把实际路线写进抽取诊断：**兜底不能静默**，
        否则「界面显示解析成功、画像却空着」会被误读成供应商解析质量差。
        """
        return self._manager.has_role(ModelRole.FAST_TEXT)

    async def parse_resume(self, text: str):
        try:
            return await self._remote.parse_resume(text)
        except ProviderError as error:
            if error.code == "E_AI_NO_PROVIDER":
                return await self._local.parse_resume(text)
            raise


class _RoutedJdParser:
    def __init__(self, manager, local: JdParser) -> None:
        self._remote = AiJdParser(manager.task_client(
            TaskKind.JD_PARSE, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND,
        ))
        self._local = local

    async def parse_jd(self, text: str):
        try:
            return await self._remote.parse_jd(text)
        except ProviderError as error:
            if error.code == "E_AI_NO_PROVIDER":
                return await self._local.parse_jd(text)
            raise

    async def split_jds(self, text: str) -> list[str]:
        try:
            return await self._remote.split_jds(text)
        except ProviderError as error:
            if error.code == "E_AI_NO_PROVIDER":
                return await self._local.split_jds(text)
            raise


def embedding_identity(settings: Settings) -> tuple[str, int]:
    """当前代码实际使用的 embedding 模型名与向量维度。

    与 ``build_providers`` 用同一判据（是否配置 SiliconFlow 密钥）。索引 metadata
    的口径校验与自动重建判定都依赖这个值，两处各写一份会漂移，因此收敛到这里。
    """
    if settings.siliconflow_api_key is not None:
        return settings.siliconflow_embedding_model, SiliconFlowEmbeddingProvider.dimension
    return "local-hash-v1", LOCAL_VECTOR_DIMENSION


def _fallback_models(fallback: str | None, primary: str) -> tuple[str, ...]:
    """备份通道：留空或与主模型相同时视为未配置（即关闭备份）。"""
    model = (fallback or "").strip()
    return (model,) if model and model != primary else ()


def build_providers(settings: Settings, ai_manager=None) -> ProviderBundle:
    """构建供应商能力包。

    Embedding/Rerank 仍固定走 SiliconFlow 或本地；生成式解析由 ``ai_manager`` 提供的
    主备路由代理承载（无路由时回退本地）。生成式路由的变化不影响 Embedding/Rerank 与向量维度。
    """
    client = httpx.AsyncClient() if settings.siliconflow_api_key is not None else None

    # Embedding / Rerank 固定走 SiliconFlow（搜索能力，不在本次 AI 改造范围）。
    embedding_provider: EmbeddingProvider
    reranker: RerankerProvider
    if settings.siliconflow_api_key is not None and client is not None:
        embedding_provider = SiliconFlowEmbeddingProvider(
            api_key=settings.siliconflow_api_key.get_secret_value(),
            client=client,
            base_url=settings.siliconflow_base_url,
            model=settings.siliconflow_embedding_model,
            fallback_models=_fallback_models(
                settings.siliconflow_embedding_fallback_model,
                settings.siliconflow_embedding_model,
            ),
        )
        vector_dimension = SiliconFlowEmbeddingProvider.dimension
    else:
        embedding_provider = LocalHashEmbeddingProvider(dimension=LOCAL_VECTOR_DIMENSION)
        vector_dimension = LOCAL_VECTOR_DIMENSION

    if settings.siliconflow_api_key is not None and client is not None:
        reranker = SiliconFlowRerankerProvider(
            api_key=settings.siliconflow_api_key.get_secret_value(),
            client=client,
            base_url=settings.siliconflow_base_url,
            model=settings.siliconflow_reranker_model,
            fallback_models=_fallback_models(
                settings.siliconflow_reranker_fallback_model,
                settings.siliconflow_reranker_model,
            ),
        )
    else:
        reranker = LocalKeywordReranker()

    parser: ResumeParser
    jd_parser: JdParser
    ocr: OCRProvider | None = None
    vision_parser: object | None = None
    if ai_manager is not None:
        parser = _RoutedResumeParser(ai_manager, LocalResumeParser())
        jd_parser = _RoutedJdParser(ai_manager, LocalJdParser())
        # 视觉代理每次请求检查 manager 当前快照：保存/停用视觉连接后无需重启。
        ocr = OpenAICompatibleOCRProvider(_VisionClient(
            ai_manager,
            ai_manager.task_client(TaskKind.OCR, ModelRole.VISION, ExecutionContext.BACKGROUND),
            no_route_code="E_OCR_REQUIRED",
            no_route_message="该文件需要 OCR 识别，但尚未配置视觉服务",
        ))
        vision_parser = VisionStructuredParser(_VisionClient(
            ai_manager,
            ai_manager.task_client(TaskKind.VISION_PARSE, ModelRole.VISION, ExecutionContext.BACKGROUND),
            no_route_code="E_VISION_UNAVAILABLE",
            no_route_message="尚未配置视觉模型，无法进行视觉重解析",
        ))
    else:
        parser = LocalResumeParser()
        jd_parser = LocalJdParser()

    return ProviderBundle(
        parser=parser,
        jd_parser=jd_parser,
        embedding=embedding_provider,
        reranker=reranker,
        ocr=ocr,
        vision_parser=vision_parser,
        vector_dimension=vector_dimension,
        http_client=client,
    )
