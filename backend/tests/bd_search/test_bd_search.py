from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kerui_recruit.bd_search.service import (
    BdSearchService,
    LeadInfo,
    WebSearchProvider,
    WebSearchResult,
    _enhance_query,
    _extract_company,
    _extract_job_title,
)
from kerui_recruit.db.migrate import migrate
from kerui_recruit.db.models import Blob, Candidate, ResumeDocument, ResumeRevision
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.encryption.service import EncryptionService


class FakeWebSearch(WebSearchProvider):
    def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
        return [
            WebSearchResult(
                title="字节跳动科技有限公司 — Java 高级工程师",
                url="https://example.com/1",
                snippet="字节跳动科技有限公司招聘 Java 高级工程师，北京...",
                source="web",
            ),
            WebSearchResult(
                title="腾讯科技（深圳）有限公司 — 前端开发工程师",
                url="https://example.com/2",
                snippet="腾讯科技（深圳）有限公司 前端开发工程师 深圳...",
                source="web",
            ),
        ]


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_search_leads_persists_encrypted(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = BdSearchService(
        session_factory=session_factory,
        search_provider=FakeWebSearch(),
        encryption=encryption,
    )
    leads = service.search_leads("Java 工程师 招聘", limit=5)
    assert len(leads) == 2
    assert leads[0].status == "新线索"

    # Encrypted company_name should not be plaintext
    assert "字节跳动" not in leads[0].company_name
    # Decrypt should recover original
    company = encryption.decrypt(leads[0].company_name)
    assert "字节跳动" in company

    # Second lead
    company2 = encryption.decrypt(leads[1].company_name)
    assert "腾讯" in company2


def test_regex_extracts_company_and_job() -> None:
    company = _extract_company("阿里云计算有限公司 招聘", "阿里云计算有限公司")
    assert company is not None
    assert "阿里" in company

    job = _extract_job_title("招聘 Java 高级工程师", "Java 高级工程师")
    assert job is not None
    assert "工程师" in job


def test_enhance_query_appends_recruitment_intent() -> None:
    assert _enhance_query("Java 架构师") == "Java 架构师 招聘 公司直招 官网"


def test_enhance_query_does_not_duplicate_intent() -> None:
    assert _enhance_query("Java 架构师 招聘") == "Java 架构师 招聘"
    assert _enhance_query("Java 架构师 直招") == "Java 架构师 直招"


def test_search_leads_passes_raw_content_to_extractor(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    class RawContentSearch(WebSearchProvider):
        def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
            return [
                WebSearchResult(
                    title="某科技公司 高级工程师",
                    url="https://example.com/1",
                    snippet="short",
                    source="web",
                    raw_content="某科技公司 招聘 高级 Java 工程师 北京",
                )
            ]

    captured: dict[str, str | None] = {}

    class CapturingExtractor:
        def extract(self, title, snippet, raw_content=None):
            captured["raw_content"] = raw_content
            return LeadInfo(company="某科技公司", job_title="高级 Java 工程师")

    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = BdSearchService(
        session_factory=session_factory,
        search_provider=RawContentSearch(),
        encryption=encryption,
        extractor=CapturingExtractor(),  # type: ignore[arg-type]
    )
    service.search_leads("高级工程师", limit=5)

    assert captured["raw_content"] == "某科技公司 招聘 高级 Java 工程师 北京"


def test_search_leads_deduplicates_within_ttl(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    calls: list[str] = []

    class CountingSearch(WebSearchProvider):
        def search(self, query: str, limit: int = 10) -> list[WebSearchResult]:
            calls.append(query)
            return [
                WebSearchResult(
                    title="字节跳动科技有限公司 — Java 高级工程师",
                    url="https://example.com/1",
                    snippet="字节跳动招聘 Java",
                    source="web",
                )
            ]

    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = BdSearchService(
        session_factory=session_factory,
        search_provider=CountingSearch(),
        encryption=encryption,
    )

    first = service.search_leads("Java 工程师")
    second = service.search_leads("Java 工程师")

    assert len(first) == 1
    # Second search must reuse cached results, not hit the provider again.
    assert len(calls) == 1
    assert len(second) == 1
    assert second[0].id == first[0].id


def test_update_status_changes_lead(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    encryption = EncryptionService(key_path=str(tmp_path / "encryption.key"))
    service = BdSearchService(
        session_factory=session_factory,
        search_provider=FakeWebSearch(),
        encryption=encryption,
    )
    lead = service.search_leads("Java 工程师")[0]
    assert lead.status == "新线索"

    updated = service.update_status(lead.id, "已联系", note="已电话联系")
    assert updated.status == "已联系"
    assert updated.note == "已电话联系"


def test_build_search_queries_from_candidate(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with session_factory() as session:
        candidate = Candidate(display_name="张三")
        session.add(candidate)
        session.flush()
        document = ResumeDocument(candidate_id=candidate.id)
        session.add(document)
        session.flush()
        blob = Blob(
            content_sha256="sha",
            suffix="pdf",
            size_bytes=100,
            storage_path="blobs/sha.pdf",
        )
        session.add(blob)
        session.flush()
        revision = ResumeRevision(
            document_id=document.id,
            blob_id=blob.id,
            content_sha256="sha",
            original_filename="张三.pdf",
            status="READY",
            is_current=True,
            parsed_data={"skills": ["Java", "金融风控"], "location": "上海", "summary": "金融科技后端"},
        )
        session.add(revision)
        session.commit()
        candidate_id = candidate.id

    service = BdSearchService(
        session_factory=session_factory,
        search_provider=FakeWebSearch(),
        encryption=EncryptionService(key_path=str(tmp_path / "ignored.key")),
    )
    queries = service.build_search_queries(candidate_id)
    assert len(queries) >= 1
    assert "Java" in queries[0]
    assert "招聘" in queries[0]