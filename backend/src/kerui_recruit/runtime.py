from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kerui_recruit.api.services import AppServices
from kerui_recruit.backup.portable import PortableBackupService
from kerui_recruit.backup.snapshot import REBUILD_MARKER
from kerui_recruit.backup.service import BackupService, apply_pending_restore, finalize_pending_restore
from kerui_recruit.bd_agent.agent import BdAgent
from kerui_recruit.bd_agent.evidence import EvidenceExtractor
from kerui_recruit.bd_agent.fetcher import JinaReaderFetcher
from kerui_recruit.bd_agent.planner import QueryPlanner
from kerui_recruit.bd_agent.synthesis import SynthesisGenerator
from kerui_recruit.bd_search.service import BdSearchService
from kerui_recruit.cases.service import CaseService
from kerui_recruit.core.settings import Settings
from kerui_recruit.core.settings_service import SettingsService
from kerui_recruit.core.settings_store import SettingsStore
from kerui_recruit.correction.service import CorrectionService
from kerui_recruit.daily_followup.service import DailyFollowupService
from kerui_recruit.dashboard.service import DashboardService
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.diagnostics.service import DiagnosticsService
from kerui_recruit.encryption.service import EncryptionService
from kerui_recruit.export.service import ExportService
from kerui_recruit.jd.pipeline import JdPipeline
from kerui_recruit.main import create_app
from kerui_recruit.mail.imap_provider import ImapLibProvider
from kerui_recruit.mail.ingest import MailIngestService
from kerui_recruit.mail.resume_gate import ResumeGate
from kerui_recruit.mail.sender import MailSender
from kerui_recruit.mail.service import MailService
from kerui_recruit.mapping.service import MappingService
from kerui_recruit.match.service import MatchService
from kerui_recruit.match.review import MatchReviewService
from kerui_recruit.migration.service import MigrationService
from kerui_recruit.org.binding import OrgBindingService
from kerui_recruit.org.import_parser import DeepSeekOrgImportParser
from kerui_recruit.org.service import OrgService
from kerui_recruit.duplicates.service import DuplicateReportService, MergePlanService
from kerui_recruit.providers.ai.catalog import CatalogService
from kerui_recruit.providers.ai.circuit_breaker import CircuitBreaker
from kerui_recruit.providers.ai.config_store import AiConfigStore
from kerui_recruit.providers.ai.contracts import ExecutionContext, ModelRole, ReasoningMode, TaskKind
from kerui_recruit.providers.ai.manager import AiProviderManager
from kerui_recruit.providers.ai.probes import AiProbeService
from kerui_recruit.providers.factory import ProviderBundle, build_providers, embedding_identity
from kerui_recruit.providers.connectivity import ProviderConnectivityService
from kerui_recruit.providers.leads import DeepSeekLeadExtractor
from kerui_recruit.providers.websearch import (
    NullWebSearchProvider,
    SerpApiWebSearchProvider,
    TavilyWebSearchProvider,
)
from kerui_recruit.reminders.candidate_service import CandidateReminderService
from kerui_recruit.reminders.mail_service import ReminderMailService
from kerui_recruit.reminders.service import ReminderService
from kerui_recruit.backfill.service import BackfillService, no_progress_error
from kerui_recruit.jd.profile import JdProfileGenerator
from kerui_recruit.resumes.profile import CandidateProfileGenerator
from kerui_recruit.resumes.backfill import backfill_contact_fingerprints
from kerui_recruit.schools.backfill import backfill_school_mappings
from kerui_recruit.schools.seed import seed_schools
from kerui_recruit.resumes.pipeline import ResumePipeline
from kerui_recruit.resumes.work_years_rollover import WorkYearsRollover
from kerui_recruit.scheduler.service import SchedulerService
from kerui_recruit.search.lancedb_index import LanceDBSearchIndex
from kerui_recruit.search.rewrite import SemanticQueryRewriter
from kerui_recruit.search.parse import QueryParser
from kerui_recruit.search.review import SearchReviewService
from kerui_recruit.jd.deletion import JdDeletionService
from kerui_recruit.resumes.deletion import CandidateDeletionService
from kerui_recruit.soft_delete.service import SoftDeleteService
from kerui_recruit.search.service import HybridSearchService
from kerui_recruit.search.upgrade import (
    REBUILD_STATE_FILE,
    RESET,
    IndexRebuildTracker,
    archive_search_index,
    plan_index_upgrade,
)
from kerui_recruit.storage.blobs import BlobStore
from kerui_recruit.tasks.repository import TaskRepository, TaskSpec
from kerui_recruit.tasks.worker import TaskWorker


@dataclass(frozen=True, slots=True)
class RuntimeComponents:
    services: AppServices
    pipeline: ResumePipeline
    worker: TaskWorker
    providers: ProviderBundle


def _mock_structured_payload() -> dict:
    """结构化输出（带 response_format）的 mock 内容。

    为什么不能只回 `{"ok": true}`：候选人解析要过 `resumes/validity.check_parsed_resume`
    的完整性校验（≥3 个内容信号，且「技能 / 有意义的画像 / 有内容的工作经历」至少两项），
    岗位解析要拿得到 `title` 才能把修订推到 READY。只回 `{"ok": true}` 会让两侧都判「不合格」，
    e2e 里「导入 → 解析 → 入索引 → 检索」整条链路根本跑不通（实测：修订 FAILED、
    `E_PARSE_INCOMPLETE`、检索 `index_not_ready`）。

    两个模型都不 forbid extra，所以用一份字段超集同时喂候选人与岗位两套 schema。
    """
    return {
        # —— 候选人侧（满足完整性校验所需的内容信号）
        "name": "测试候选人",
        "total_years": 5,
        "highest_degree": "本科",
        "location": "上海",
        "school": "测试大学",
        "industry": "互联网",
        "current_company": "测试科技有限公司",
        "current_title": "后端工程师",
        "skills": ["Python", "Java", "SQL"],
        "summary": "五年后端开发经验，负责支付系统与数据平台的建设与稳定性优化。",
        "experiences": [
            {"company": "测试科技有限公司", "title": "后端工程师",
             "start_date": "2021-01", "end_date": "至今",
             "summary": "负责支付核心链路的开发与维护，支撑日均千万级交易。"},
        ],
        "projects": [
            {"name": "支付清结算平台", "tech_stack": "Python, Kafka, PostgreSQL",
             "summary": "设计并落地清结算平台，替换旧有批处理流程。"},
        ],
        "educations": [
            {"school": "测试大学", "major": "计算机科学与技术", "degree": "本科"},
        ],
        # —— 岗位侧（title 为必填；其余给到能支撑列表与匹配的最小集合）
        "title": "后端工程师",
        "company": "测试科技有限公司",
        "min_years": 5,
        "core_duties": ["负责支付系统后端开发与维护", "参与系统性能优化与服务治理"],
        "required_skills": ["Python", "Java"],
        "plus_skills": ["Kafka"],
        "ai_category": "NON_AI",
        "direction": "BACKEND",
        "career_directions": ["BACKEND"],
        "business_directions": ["FINANCE"],
        "candidate_profile_narrative": "需要五年以上后端开发经验，熟悉 Python 与 Java，"
                                       "有支付或金融系统开发经验。",
    }


def _mock_ai_http_client() -> httpx.AsyncClient:
    """测试用 MockTransport：KERUI_AI_MOCK=1 时注入，不访问真实供应商。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            # 返回内置目录中各供应商的推荐模型，使推荐模型始终“在发现结果中”。
            return httpx.Response(200, json={"data": [
                {"id": "deepseek-flash"},
                {"id": "deepseek-v4-pro"},
                {"id": "qwen3.8-flash"},
                {"id": "qwen3.8-max"},
                {"id": "glm-4.7-flashx"},
                {"id": "glm-5.3"},
                {"id": "glm-5.3-flash"},
                {"id": "kimi-k2.6"},
                {"id": "kimi-k3"},
                {"id": "k3"},
                {"id": "kimi-for-coding"},
                {"id": "qwen3.7-plus"},
                {"id": "deepseek-ai/DeepSeek-V4-Flash"},
                {"id": "deepseek-ai/DeepSeek-V4-Pro"},
                {"id": "THUDM/GLM-4.1V-9B-Thinking"},
            ]})
        json.loads(request.content)  # 显式要求请求体是合法 JSON，与真实供应商一致
        # 两条分支都回结构化 JSON：上游模型未声明 JSON 能力时，provider 会**不发**
        # `response_format`（见 parameter_mapping 的留痕告警），此时只能靠提示词 + 容错解析，
        # 所以纯文本分支也必须给出 JSON，否则简历解析永远拿不到字段、被判「不合格」。
        content = json.dumps(_mock_structured_payload(), ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _mock_ai_provider_config():
    """`KERUI_AI_MOCK=1` 时可用的 AI 连接（全部模型角色指向同一个 mock 模型）。

    注意：**不要在 `build_runtime` 里自动写入**。e2e 的首启向导用例
    （`desktop/tests/ai-settings.spec.ts`）依赖「一开始没有连接」这个初始态，
    自动注入会让它的「添加 AI 服务」入口消失。这里只作为显式可用的工厂保留，
    需要「带 AI 配置的 e2e」时由调用方自己写进 AiConfigStore。
    """
    from pydantic import SecretStr

    from kerui_recruit.providers.ai.config_models import AiConnection, AiProviderConfig
    from kerui_recruit.providers.ai.contracts import ModelRole

    roles = list(ModelRole)
    return AiProviderConfig(connections=[AiConnection(
        connection_id="mock-fast",
        provider_id="deepseek",
        display_name="Mock",
        api_key=SecretStr("sk-mock"),
        models={role: "deepseek-ai/DeepSeek-V4-Flash" for role in roles},
        probed_roles=frozenset(roles),
    )])


def build_runtime(settings: Settings, ai_http_client: httpx.AsyncClient | None = None) -> RuntimeComponents:
    from kerui_recruit.match.jd_index import JdSearchIndex

    settings.paths.ensure()
    restore_report = apply_pending_restore(
        database_path=settings.paths.database,
        search_dir=settings.paths.search,
        backup_dir=settings.paths.backups,
    )
    rebuild_marker = settings.paths.root / REBUILD_MARKER
    explicit_rebuild = rebuild_marker.exists()
    embedding_model, vector_dimension = embedding_identity(settings)
    # 索引版本不一致不再只告警、也不再保留旧索引当基线：发布新版的前提就是新版向量
    # 口径更优。判定只看 metadata 文件，必须在任何 LanceDB 连接之前完成——归档是目录
    # 改名，Windows 上存在打开的句柄会让改名失败。
    upgrade_plan = plan_index_upgrade(
        settings.paths.search, embedding_model=embedding_model, vector_dimension=vector_dimension
    )
    reset_index = explicit_rebuild or restore_report is not None or (
        upgrade_plan is not None and upgrade_plan.resets
    )
    archived: Path | None = None
    if reset_index and settings.paths.search.exists():
        search = settings.paths.search.resolve()
        if search != settings.paths.root.resolve() / "search":
            raise ValueError("搜索目录指向数据目录之外，无法安全重建")
        archived = archive_search_index(search)
    index = LanceDBSearchIndex(
        settings.paths.search,
        vector_dimension=vector_dimension,
        embedding_model=embedding_model,
    )
    jd_index = JdSearchIndex(settings.paths.search / "jobs", vector_dimension=vector_dimension,
        embedding_model=embedding_model)
    if upgrade_plan is not None and not reset_index:
        # 只加字段 / 只换分块方式：旧行与新行物理同构，补列即可写，旧数据全程可用。
        try:
            index.upgrade_inplace()
            jd_index.index.upgrade_inplace()
        except ValueError as error:
            # 既有列的物理类型变了，无法共存：留标记让下次启动走归档重建。
            logging.getLogger(__name__).warning("索引无法就地升级（%s），已安排下次启动归档重建", error)
            rebuild_marker.write_text("1\n", encoding="ascii")
    elif upgrade_plan is not None:
        logging.getLogger(__name__).warning(
            "索引向量口径已更换（%s），已归档旧索引并全量重建", upgrade_plan.reason
        )
    engine = create_engine_for(settings.paths.database)
    migrate(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    reprojected = 0
    if reset_index or upgrade_plan is not None:
        from kerui_recruit.db.models import Candidate, Jd, TaskRecord
        from kerui_recruit.search.sync import enqueue_sync
        with factory() as reproject_session, reproject_session.begin():
            # 版本升级后索引内容口径必须整体刷新，因此全量重投；重置路径下索引已清空，
            # 就地路径下向量缓存按 vector_text 命中，只有文本真变了的才会重新 embedding。
            candidate_ids = list(reproject_session.scalars(select(Candidate.id)))
            jd_ids = list(reproject_session.scalars(select(Jd.id)))
            for candidate_id in candidate_ids:
                enqueue_sync(reproject_session, "candidate", candidate_id)
            for jd_id in jd_ids:
                enqueue_sync(reproject_session, "jd", jd_id)
            reprojected = len(candidate_ids) + len(jd_ids)
            if explicit_rebuild or restore_report is not None:
                # 恢复出的 outbox 描述的是旧索引，并需回收上次未跑完的任务。
                for task in reproject_session.scalars(select(TaskRecord).where(TaskRecord.status == "RUNNING")):
                    task.status = "QUEUED"
                    task.lease_owner = None
                    task.lease_expires_at = None
        if restore_report is not None:
            finalize_pending_restore(restore_report)
        rebuild_marker.unlink(missing_ok=True)
    rebuild_tracker = IndexRebuildTracker(settings.paths.root / REBUILD_STATE_FILE)
    if upgrade_plan is not None or reset_index:
        rebuild_tracker.begin(
            mode=(upgrade_plan.mode if upgrade_plan is not None and not reset_index else RESET),
            reason=(upgrade_plan.reason if upgrade_plan is not None else "显式要求重建索引"),
            total=reprojected,
            archive=archived,
        )
    from kerui_recruit.cases.state import reconcile_legacy_workflow_state
    reconcile_legacy_workflow_state(factory)
    blob_store = BlobStore(settings.paths.blobs, settings.paths.temp)

    encryption_service = EncryptionService(
        key_path=str(settings.paths.config / "encryption.key"),
    )
    catalog_service = CatalogService(cache_path=settings.paths.config / "ai-catalog.json")
    config_store = AiConfigStore(
        path=settings.paths.config / "ai-providers.json",
        encryption=encryption_service,
        catalog_service=catalog_service,
        legacy_settings_path=settings.paths.config / "settings.json",
    )
    if not config_store.path.exists():
        migrated = config_store.migrate_from_settings(settings)
        if migrated.connections:
            config_store.save(migrated)
    if ai_http_client is None:
        ai_http_client = _mock_ai_http_client() if os.environ.get("KERUI_AI_MOCK") == "1" else httpx.AsyncClient()
    ai_manager = AiProviderManager(
        config_store=config_store,
        catalog_service=catalog_service,
        probe_service=AiProbeService(catalog_service, ai_http_client),
        http_client=ai_http_client,
        circuit_breaker=CircuitBreaker(),
    )

    providers = build_providers(settings, ai_manager)
    rewriter = SemanticQueryRewriter(
        ai_manager.task_client(TaskKind.QUERY_REWRITE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    )
    # AI 智能解析（默认关闭，API 侧 parse_enabled 控制）：一次调用拆出硬条件 + 词条 + 语义查询。
    # 用快速模型：输入是一句查询、输出是结构化字段，不需要推理档。
    # **关思考优先**：能关掉思考的模型一律关掉（阿里 qwen3.8-flash 关思考 3.3 秒），
    # 关不掉时才用 `low`（智谱 glm-5.3-flashx 不指定档位 11.2 秒 → `low` 2.0 秒）。
    # `prefer_off` 同时让 AI 设置里快速槽位配的强度不参与——否则使用者把快速槽位调成高强度后，
    # 每次搜索的解析都要多等十几秒。
    query_parser = QueryParser(
        ai_manager.task_client(
            TaskKind.QUERY_PARSE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE,
            reasoning_effort="low", prefer_off=True,
        )
    )
    from kerui_recruit.search.sync import IndexSyncService
    index_sync_service = IndexSyncService(session_factory=factory, index=index,
        embedding_provider=providers.embedding, jd_index=jd_index, rebuild_tracker=rebuild_tracker)
    repository = TaskRepository(factory)
    search_service = HybridSearchService(
        index=index,
        embedding_provider=providers.embedding,
        reranker_provider=providers.reranker,
        rewriter=rewriter,
    )
    match_service = MatchService(
        session_factory=factory,
        search_service=search_service,
        jd_index=jd_index,
    )
    jd_pipeline = JdPipeline(session_factory=factory, parser=providers.jd_parser)

    export_service = ExportService(session_factory=factory)
    correction_service = CorrectionService(session_factory=factory)
    backup_service = BackupService(
        session_factory=factory,
        engine=engine,
        database_path=settings.paths.database,
        backup_dir=settings.paths.backups,
    )
    portable_backup_service = PortableBackupService(
        current_root=settings.paths.root,
    )
    diagnostics_service = DiagnosticsService(
        session_factory=factory,
        database_path=settings.paths.database,
    )
    mapping_service = MappingService(session_factory=factory)
    reminder_service = ReminderService(session_factory=factory)
    # 候选人提醒与流程提醒是两套东西（无日期、不进邮件、不受流程状态影响）。
    candidate_reminder_service = CandidateReminderService(session_factory=factory)
    org_service = OrgService(session_factory=factory)
    backfill_contact_fingerprints(factory, encryption_service)
    with factory() as session, session.begin():
        seed_schools(session)
    org_binding_service = OrgBindingService(
        session_factory=factory,
        encryption=encryption_service,
    )
    if settings.tavily_api_key:
        web_search_provider = TavilyWebSearchProvider(
            api_key=settings.tavily_api_key.get_secret_value(),
            base_url=settings.tavily_base_url,
        )
    elif settings.serpapi_api_key:
        web_search_provider = SerpApiWebSearchProvider(
            api_key=settings.serpapi_api_key.get_secret_value(),
            base_url=settings.serpapi_base_url,
        )
    else:
        web_search_provider = NullWebSearchProvider()

    lead_extractor = DeepSeekLeadExtractor(
        ai_manager.task_client(TaskKind.LEAD_EXTRACT, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    )
    bd_search_service = BdSearchService(
        session_factory=factory,
        search_provider=web_search_provider,
        encryption=encryption_service,
        extractor=lead_extractor,
    )

    backfill_service = BackfillService(
        factory,
        candidate_generator=CandidateProfileGenerator(
            ai_manager.task_client(TaskKind.CANDIDATE_PROFILE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
        ),
        jd_generator=JdProfileGenerator(
            # 岗位画像用**快速档**（2026-09-22 真机实测后从推理档改回，用户选定）。
            # 原先按「需要跨条件取舍」放在推理档 + `ReasoningMode.REQUIRED`，实测在阿里
            # `qwen3.8-max` 上 **150 秒仍未返回**（7.6 真机：阶段停在 `draft`），
            # `regen-profile` 因此稳定 504 `E_PROFILE_TIMEOUT`，§6.1 的「单次生成 ≤ 90 秒」达不到。
            # 与 1.4 的 BD 综合同一条思路：真正的取舍已由硬条件与重排完成，画像只剩
            # 「压缩 + 逐字引用」；需要更强推理的使用者可以把思考模型填进快速槽位（第 9 点）。
            ai_manager.task_client(TaskKind.JD_PROFILE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
        ),
    )
    # 随年份滚动的字段（工作年限 / 年龄 / 画像里的对应数字）：每日重算一次。
    # 画像只做数字就地替换，不调用模型。
    work_years_rollover = WorkYearsRollover(session_factory=factory)
    match_review_service = MatchReviewService(
        session_factory=factory,
        task_client=ai_manager.task_client(TaskKind.MATCH_REVIEW, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND, ReasoningMode.OFF),
        reasoning_task_client=ai_manager.task_client(TaskKind.MATCH_REVIEW, ModelRole.REASONING_TEXT, ExecutionContext.BACKGROUND, ReasoningMode.REQUIRED),
    )
    search_review_service = SearchReviewService(
        session_factory=factory,
        task_client=ai_manager.task_client(TaskKind.SEARCH_REVIEW, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND, ReasoningMode.OFF),
        reasoning_task_client=ai_manager.task_client(TaskKind.SEARCH_REVIEW, ModelRole.REASONING_TEXT, ExecutionContext.BACKGROUND, ReasoningMode.REQUIRED),
    )
    org_import_parser = DeepSeekOrgImportParser(
        ai_manager.task_client(TaskKind.ORG_PARSE, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE)
    )
    bd_agent = BdAgent(
        session_factory=factory,
        search_provider=web_search_provider,
        fetcher=JinaReaderFetcher(client=providers.http_client),
        encryption=encryption_service,
        planner=QueryPlanner(
            # 规划只产出最多 3 条搜索式，短输入短输出：用快速模型即可，
            # 走推理模型会白等 10~40 秒。
            ai_manager.task_client(TaskKind.BD_PLAN, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE, ReasoningMode.OFF)
        ),
        evidence_extractor=EvidenceExtractor(reranker=providers.reranker),
        synthesizer=SynthesisGenerator(
            # 综合也走快速模型。原先按「需要跨片段取舍」放在推理档，但那条取舍其实
            # 已经由上一步的 reranker（bge-reranker-v2-m3）完成了，综合剩下的只是
            # 「抽取 + 逐字引用」，属于短输出结构化任务。
            # 实测代价：火山引擎 deepseek-v4-pro 在 REQUIRED 思考下综合 10 个证据片段
            # 稳定超过 180 秒，把每一次 BD 检索都掐断（表现为「重排之后就结束、没数据」）。
            # 需要更强推理的用户可以把思考模型填进「快速模型」槽位自行取舍。
            ai_manager.task_client(TaskKind.BD_SYNTHESIS, ModelRole.FAST_TEXT, ExecutionContext.INTERACTIVE, ReasoningMode.OFF)
        ),
        fallback=bd_search_service,
    )
    case_service = CaseService(session_factory=factory)
    dashboard_service = DashboardService(session_factory=factory)

    mail_ingest_service = None
    if settings.mail_enabled:
        mail_provider = ImapLibProvider(
            host=settings.imap_host,  # type: ignore[arg-type]
            account=settings.imap_account,  # type: ignore[arg-type]
            password=settings.imap_auth_code.get_secret_value(),  # type: ignore[union-attr]
        )
        mail_service = MailService(session_factory=factory, imap=mail_provider)
        mail_ingest_service = MailIngestService(
            session_factory=factory,
            blob_store=blob_store,
            mail_service=mail_service,
        )

    reminder_mail_service = None
    mail_sender = None
    if settings.smtp_enabled:
        mail_sender = MailSender(
            host=settings.smtp_host,  # type: ignore[arg-type]
            account=settings.smtp_account,  # type: ignore[arg-type]
            password=settings.smtp_auth_code.get_secret_value(),  # type: ignore[union-attr]
            port=settings.smtp_port,
            ssl=settings.smtp_ssl,
        )
        reminder_mail_service = ReminderMailService(
            reminder_service=reminder_service,
            mail_sender=mail_sender,
            to=settings.reminder_to,
        )

    # 每日待跟进 gather 不依赖 SMTP，始终创建，供 Dashboard 待办卡片与定时报告共用；
    # 是否真正发邮件由 DailyFollowupService.send_due_reports 内部根据 mail_sender/to 判断。
    daily_followup_service = DailyFollowupService(
        session_factory=factory,
        mail_sender=mail_sender,
        to=settings.reminder_to,
    )

    resume_gate = ResumeGate(
        ai_manager.task_client(TaskKind.MAIL_RESUME_GATE, ModelRole.FAST_TEXT, ExecutionContext.BACKGROUND)
    )

    # 回收站过期清理复用单条删除服务：这样流程快照与解除关联的行为和手动删除一致，
    # 不会出现「清理后流程还在、但候选人/岗位信息变成空白」。
    soft_delete_service = SoftDeleteService(
        factory,
        candidate_deletion=CandidateDeletionService(
            factory, index, blob_store=blob_store, task_repository=repository),
        jd_deletion=JdDeletionService(factory, jd_index=jd_index),
    )

    scheduler_service = SchedulerService(
        session_factory=factory,
        match_service=match_service,
        reminder_service=reminder_service,
        mail_ingest_service=mail_ingest_service,
        mail_auto_sync=settings.mail_auto_sync_enabled,
        reminder_mail_service=reminder_mail_service,
        backup_service=backup_service,
        sender_domains=settings.imap_whitelist_domains,
        resume_gate=resume_gate,
        daily_followup_service=daily_followup_service if settings.daily_followup_enabled else None,
        soft_delete_service=soft_delete_service,
        work_years_rollover=work_years_rollover,
    )

    settings_service = SettingsService(
        store=SettingsStore(settings.paths.config / "settings.json"),
        encryption=encryption_service,
    )
    migration_service = MigrationService(
        session_factory=factory,
        current_root=settings.paths.root,
    )
    duplicates_service = DuplicateReportService(
        session_factory=factory,
        encryption=encryption_service,
        exports_dir=settings.paths.exports,
    )
    merge_plan_service = MergePlanService(session_factory=factory)

    services = AppServices(
        settings=settings,
        session_factory=factory,
        blob_store=blob_store,
        task_repository=repository,
        search_service=search_service,
        query_parser=query_parser,
        match_service=match_service,
        jd_pipeline=jd_pipeline,
        export_service=export_service,
        correction_service=correction_service,
        backup_service=backup_service,
        portable_backup_service=portable_backup_service,
        diagnostics_service=diagnostics_service,
        mapping_service=mapping_service,
        reminder_service=reminder_service,
        candidate_reminder_service=candidate_reminder_service,
        org_service=org_service,
        org_import_parser=org_import_parser,
        org_binding_service=org_binding_service,
        bd_search_service=bd_search_service,
        bd_agent=bd_agent,
        encryption_service=encryption_service,
        case_service=case_service,
        dashboard_service=dashboard_service,
        daily_followup_service=daily_followup_service,
        ai_manager=ai_manager,
        scheduler_service=scheduler_service,
        settings_service=settings_service,
        migration_service=migration_service,
        duplicates_service=duplicates_service,
        merge_plan_service=merge_plan_service,
        provider_connectivity=ProviderConnectivityService(
            settings=settings,
            providers=providers,
            web_search=web_search_provider,
            ai_manager=ai_manager,
        ),
        index_sync_service=index_sync_service,
        backfill_service=backfill_service,
        match_review_service=match_review_service,
        search_review_service=search_review_service,
    )
    pipeline = ResumePipeline(
        session_factory=factory,
        blob_store=blob_store,
        parser=providers.parser,
        embedding_provider=providers.embedding,
        ocr_provider=providers.ocr,
        vision_parser=providers.vision_parser,
        search_index=None,
        defer_indexing=True,
        encryption_service=encryption_service,
        task_repository=repository,
    )

    async def parse_resume(payload: dict[str, Any], report=None) -> str:
        result = await pipeline.run(
            str(payload["revision_id"]),
            force_ocr=bool(payload.get("force_ocr", False)),
            use_vision=bool(payload.get("use_vision", False)),
        )
        await index_sync_service.run_once(entity_type="candidate", entity_id=result.candidate_id)
        if payload.get("passive_match") and services.match_service is not None:
            repository.enqueue(TaskSpec(task_type="MATCH_CANDIDATE", queue_name="normal", priority=10,
                payload={"candidate_id": result.candidate_id},
                idempotency_key=f"passive-candidate:{result.revision_id}"))
        return result.revision_id

    async def parse_jd(payload: dict[str, Any], report=None) -> str:
        result = await jd_pipeline.run(str(payload["revision_id"]))
        await index_sync_service.run_once(entity_type="jd", entity_id=result.jd_id)
        if payload.get("passive_match") and services.match_service is not None:
            repository.enqueue(TaskSpec(task_type="MATCH_JD", queue_name="normal", priority=10,
                payload={"revision_id": result.revision_id},
                idempotency_key=f"passive-jd:{result.revision_id}"))
        return result.revision_id

    async def match_candidate(payload: dict[str, Any], report=None) -> str | None:
        candidate_id = str(payload["candidate_id"])
        records = await match_service.reverse_match_candidate(candidate_id)
        return match_service.record_reverse_run(candidate_id=candidate_id, records=records).run_id

    async def match_job(payload: dict[str, Any], report=None) -> str | None:
        # Matching retries are separate from parsing and visible in the task list.
        return (await match_service.match_and_record(revision_id=str(payload["revision_id"]))).run_id

    async def backfill_school_mappings_handler(payload: dict[str, Any], report=None) -> str:
        result = await asyncio.to_thread(backfill_school_mappings, factory, report)
        return json.dumps(result, ensure_ascii=False)

    async def backfill_candidate_profiles_handler(payload: dict[str, Any], report=None) -> str:
        result = await backfill_service.backfill_candidate_profiles(report=report, force=bool(payload.get("force", False)))
        error = no_progress_error(result, kind="候选人")
        if error is not None:
            raise error
        return json.dumps(result, ensure_ascii=False)

    async def backfill_jd_profiles_handler(payload: dict[str, Any], report=None) -> str:
        result = await backfill_service.backfill_jd_profiles(report=report, force=bool(payload.get("force", False)))
        error = no_progress_error(result, kind="JD")
        if error is not None:
            raise error
        return json.dumps(result, ensure_ascii=False)

    async def reparse_failed_handler(payload: dict[str, Any], report=None) -> str:
        """对不合格（FAILED）的简历/JD 版本批量入队重新解析任务。

        简历走视觉模型直接结构化解析；JD 仅有文本来源，重新走文本解析。
        """
        from kerui_recruit.db.models import JdRevision, ResumeRevision

        with factory() as session:
            resume_ids = list(session.scalars(
                select(ResumeRevision.id).where(ResumeRevision.status == "FAILED")
            ))
            jd_ids = list(session.scalars(
                select(JdRevision.id).where(JdRevision.status == "FAILED")
            ))
        for revision_id in resume_ids:
            repository.enqueue(TaskSpec(
                task_type="PARSE_RESUME",
                queue_name="batch",
                priority=5,
                payload={"revision_id": revision_id, "force_ocr": False, "use_vision": True},
                idempotency_key=f"REPARSE_FAILED:resume:{revision_id}",
            ))
        for revision_id in jd_ids:
            repository.enqueue(TaskSpec(
                task_type="PARSE_JD",
                queue_name="batch",
                priority=5,
                payload={"revision_id": revision_id},
                idempotency_key=f"REPARSE_FAILED:jd:{revision_id}",
            ))
        return json.dumps({"resumes": len(resume_ids), "jds": len(jd_ids)}, ensure_ascii=False)

    async def blob_cleanup_handler(payload: dict[str, Any], report=None) -> str | None:
        """幂等清理候选人物理文件与搜索索引实体（文件不存在视为成功）。"""
        candidate_id = payload.get("candidate_id")
        if candidate_id:
            index.delete_candidate(candidate_id)
        root = blob_store.root
        for raw in payload.get("storage_paths") or []:
            target = root / str(raw)
            # 文件不存在视为成功（幂等）；权限等 OSError 抛出，任务进入可重试失败状态。
            target.unlink(missing_ok=True)
            for parent in (target.parent, target.parent.parent):
                try:
                    if parent != root and parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    break
        return None

    async def match_review_handler(payload: dict[str, Any], report=None) -> str:
        """对一次匹配 run 的**全部**合格配对执行 AI 深度复核（默认关闭思考，用户触发才入队）。

        不设条数与整批时长上限：匹配到的每一个都要有结论。长任务靠 worker 每 30 秒续租保活，
        用户可随时取消；单条调用仍有超时，失败/超时条目会显式带标记返回。
        """
        reasoning = ReasoningMode.REQUIRED if payload.get("reasoning") else ReasoningMode.OFF
        verdicts = await match_review_service.review_run(
            payload["run_id"],
            report=report,
            reasoning=reasoning,
        )
        return json.dumps(verdicts, ensure_ascii=False)

    async def search_review_handler(payload: dict[str, Any], report=None) -> str:
        """对「一次搜索条件 + 一批候选人」执行 AI 复核，产出亮点/风险点并落表。

        不设条数与整批时长上限：勾选到的人都要有结论。长任务靠 worker 每 30 秒续租保活，
        用户可随时取消；结论逐条落库，取消后已出的部分依然可查。
        """
        reasoning = ReasoningMode.REQUIRED if payload.get("reasoning") else ReasoningMode.OFF
        summary = await search_review_service.review_query(
            query_key=payload["query_key"],
            conditions=payload.get("conditions") or "",
            candidate_ids=list(payload.get("candidate_ids") or []),
            report=report,
            reasoning=reasoning,
        )
        return json.dumps(summary, ensure_ascii=False)

    async def backfill_direction_handler(payload: dict[str, Any], report=None) -> str:
        """对缺失/仅 OTHER 方向的历史 revision 做异步重判（候选人或 JD），补齐多值方向。"""
        from kerui_recruit.direction.backfill import backfill_directions

        result = await asyncio.to_thread(
            backfill_directions,
            factory,
            dry_run=False,
            entity_type=payload.get("entity_type", "candidate"),
            report=report,
        )
        return json.dumps(result, ensure_ascii=False)

    worker = TaskWorker(
        repository=repository,
        worker_id=f"desktop-{uuid4()}",
        queues=("interactive", "normal", "batch", "export"),
        handlers={"PARSE_RESUME": parse_resume, "PARSE_JD": parse_jd,
                  "MATCH_CANDIDATE": match_candidate, "MATCH_JD": match_job,
                  "BACKFILL_SCHOOL_MAPPING": backfill_school_mappings_handler,
                  "BACKFILL_CANDIDATE_PROFILE": backfill_candidate_profiles_handler,
                  "BACKFILL_JD_PROFILE": backfill_jd_profiles_handler,
                  "REPARSE_FAILED": reparse_failed_handler,
                  "BLOB_CLEANUP": blob_cleanup_handler,
                  "MATCH_REVIEW": match_review_handler,
                  "SEARCH_REVIEW": search_review_handler,
                  "BACKFILL_DIRECTION": backfill_direction_handler},
    )
    return RuntimeComponents(services=services, pipeline=pipeline, worker=worker, providers=providers)


def create_runtime_app(settings: Settings, ai_http_client: httpx.AsyncClient | None = None) -> FastAPI:
    runtime = build_runtime(settings, ai_http_client=ai_http_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.services.task_repository.recover_expired_leases()
        # 索引预热放到后台线程，避免阻塞 /health/ready 与首个请求。
        warmup_task = asyncio.create_task(asyncio.to_thread(runtime.services.search_service.warmup))
        worker_task = asyncio.create_task(
            _worker_after_index_maintenance(
                runtime.worker,
                runtime.services.search_service,
            )
        )
        lease_recovery_task = asyncio.create_task(
            _lease_recovery_loop(runtime.services.task_repository)
        )
        scheduler_task = asyncio.create_task(
            runtime.services.scheduler_service.run_forever(interval_seconds=300)
        )
        index_sync_task = asyncio.create_task(runtime.services.index_sync_service.run_forever())
        try:
            yield
        finally:
            warmup_task.cancel()
            worker_task.cancel()
            lease_recovery_task.cancel()
            scheduler_task.cancel()
            index_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await warmup_task
            with suppress(asyncio.CancelledError):
                await worker_task
            with suppress(asyncio.CancelledError):
                await lease_recovery_task
            with suppress(asyncio.CancelledError):
                await scheduler_task
            with suppress(asyncio.CancelledError):
                await index_sync_task
            if runtime.providers.http_client is not None:
                await runtime.providers.http_client.aclose()
            if runtime.services.ai_manager is not None:
                await runtime.services.ai_manager.close()

    return create_app(runtime.services, lifespan=lifespan)


async def _worker_loop(worker: TaskWorker) -> None:
    while True:
        worked = await worker.run_once()
        # Local parsers and embeddings may complete without a network await.
        # Always yield after a claimed task so a large batch cannot starve API
        # responses and desktop health checks on the same event loop.
        await asyncio.sleep(0 if worked else 0.25)


async def _worker_after_index_maintenance(
    worker: TaskWorker,
    search_service: HybridSearchService,
    *,
    concurrency: int = 8,
) -> None:
    try:
        await asyncio.to_thread(search_service.optimize_pending)
    except Exception:
        # The index is a rebuildable projection. A maintenance failure must not
        # stop durable SQLite tasks from continuing on the next worker cycle.
        pass
    loops = [asyncio.create_task(_worker_loop(worker)) for _ in range(concurrency)]
    await asyncio.gather(*loops)


async def _lease_recovery_loop(
    repository: TaskRepository,
    *,
    interval_seconds: float = 30,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await asyncio.to_thread(repository.recover_expired_leases)
