# Phase 0 Search Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an installable local desktop foundation that imports resumes, persists crash-safe tasks, creates a hybrid search projection, and returns 120,000-candidate search results within the agreed latency budget.

**Architecture:** A Tauri/React desktop shell launches a packaged FastAPI sidecar. SQLite is the fact store and durable queue, LanceDB is a rebuildable search projection, and originals live in a content-addressed directory.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, LanceDB, PyMuPDF, python-docx, cryptography, pytest, React, TypeScript, Vite, Tauri 2, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-29-embedded-recruitment-desktop-design.md`

## Global Constraints

- Production targets are Windows 10/11 x64 and macOS Apple Silicon with 16GB RAM and SSD.
- Production installation must not require Docker, Python, Node.js, Java, or a database server.
- SQLite is the only business fact source; LanceDB is always rebuildable.
- Services bind only to `127.0.0.1` and require a per-launch session token.
- Candidate capacity is 100,000; performance tests use 120,000 candidates and 2–4 search chunks each.
- READY search target is P95 <= 1.5 seconds and P99 <= 2.5 seconds.
- Tests never require live API keys; fake providers are deterministic.

---

### Task 1: Repository and backend bootstrap

**Files:**
- Create: `.gitignore`
- Create: `backend/pyproject.toml`
- Create: `backend/src/kerui_recruit/__init__.py`
- Create: `backend/src/kerui_recruit/main.py`
- Create: `backend/tests/test_health.py`

**Interfaces:**
- Produces: `kerui_recruit.main.create_app() -> FastAPI`
- Produces: `GET /health/live -> {"status": "alive"}`

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient
from kerui_recruit.main import create_app


def test_liveness_endpoint() -> None:
    response = TestClient(create_app()).get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
```

- [ ] **Step 2: Run the test and verify import failure**

Run: `uv run --project backend pytest backend/tests/test_health.py -q`  
Expected: FAIL because `kerui_recruit.main` does not exist.

- [ ] **Step 3: Add the package and application factory**

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="KeRui Recruit", version="0.1.0")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    return app
```

- [ ] **Step 4: Run the health test**

Run: `uv run --project backend pytest backend/tests/test_health.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit the bootstrap**

```powershell
git add .gitignore backend
git commit -m "build: bootstrap local backend"
```

### Task 2: Platform data paths and runtime settings

**Files:**
- Create: `backend/src/kerui_recruit/core/paths.py`
- Create: `backend/src/kerui_recruit/core/settings.py`
- Create: `backend/tests/core/test_paths.py`

**Interfaces:**
- Produces: `AppPaths.from_root(root: Path) -> AppPaths`
- Produces: `AppPaths.ensure() -> None`
- Produces: `Settings(data_root: Path, session_token: SecretStr)`

- [ ] **Step 1: Write path-layout tests**

```python
def test_app_paths_create_only_expected_directories(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    paths.ensure()
    assert paths.database == tmp_path / "data/db/recruit.sqlite3"
    assert paths.search == tmp_path / "data/search"
    assert paths.blobs == tmp_path / "data/blobs"
    assert {p.name for p in (tmp_path / "data").iterdir()} == {
        "db", "search", "blobs", "exports", "backups", "logs", "temp", "config"
    }
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run --project backend pytest backend/tests/core/test_paths.py -q`  
Expected: FAIL because `AppPaths` is undefined.

- [ ] **Step 3: Implement immutable path and settings models**

```python
@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    search: Path
    blobs: Path
    exports: Path
    backups: Path
    logs: Path
    temp: Path
    config: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        root = root.expanduser().resolve()
        return cls(
            root=root,
            database=root / "db" / "recruit.sqlite3",
            search=root / "search",
            blobs=root / "blobs",
            exports=root / "exports",
            backups=root / "backups",
            logs=root / "logs",
            temp=root / "temp",
            config=root / "config",
        )

    def ensure(self) -> None:
        for path in (self.database.parent, self.search, self.blobs, self.exports,
                     self.backups, self.logs, self.temp, self.config):
            path.mkdir(parents=True, exist_ok=True)
```

The default root resolver must use `%LOCALAPPDATA%/KeRuiRecruit` on Windows and `~/Library/Application Support/KeRuiRecruit` on macOS. It must reject cloud-sync and network paths with a stable `E_CONFIG_UNSAFE_DATA_ROOT` error.

- [ ] **Step 4: Run path tests**

Run: `uv run --project backend pytest backend/tests/core/test_paths.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit platform settings**

```powershell
git add backend/src/kerui_recruit/core backend/tests/core
git commit -m "feat: add cross-platform data paths"
```

### Task 3: SQLite engine, migrations, and core schema

**Files:**
- Create: `backend/src/kerui_recruit/db/base.py`
- Create: `backend/src/kerui_recruit/db/session.py`
- Create: `backend/src/kerui_recruit/db/models.py`
- Create: `backend/src/kerui_recruit/db/migrate.py`
- Create: `backend/tests/db/test_schema.py`

**Interfaces:**
- Produces: `create_engine_for(path: Path) -> Engine`
- Produces: `session_factory(engine: Engine) -> sessionmaker[Session]`
- Produces: `migrate(engine: Engine) -> None`
- Produces: SQLAlchemy models `Candidate`, `ResumeDocument`, `ResumeRevision`, `Blob`, `TaskRecord`, `TaskEvent`

- [ ] **Step 1: Write schema and pragma tests**

```python
def test_engine_enables_required_sqlite_guards(tmp_path: Path) -> None:
    engine = create_engine_for(tmp_path / "recruit.sqlite3")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one().lower() == "wal"


def test_candidate_soft_delete_and_revision_relationship(session: Session) -> None:
    candidate = Candidate(display_name="张三")
    document = ResumeDocument(candidate=candidate)
    revision = ResumeRevision(document=document, content_sha256="a" * 64, status="PENDING")
    session.add(candidate)
    session.commit()
    assert revision.document.candidate.id == candidate.id
    assert candidate.deleted_at is None
```

- [ ] **Step 2: Verify schema tests fail**

Run: `uv run --project backend pytest backend/tests/db/test_schema.py -q`  
Expected: FAIL because the database modules do not exist.

- [ ] **Step 3: Implement the engine and models**

Use UUIDv7-compatible string identifiers, UTC timestamps, explicit enum check constraints, foreign keys, indexes on all lookup and soft-delete fields, and a connection hook that sets `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, and `busy_timeout=5000`.

- [ ] **Step 4: Run database tests**

Run: `uv run --project backend pytest backend/tests/db/test_schema.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit the fact-store foundation**

```powershell
git add backend/src/kerui_recruit/db backend/tests/db
git commit -m "feat: add sqlite fact-store schema"
```

### Task 4: Content-addressed blob storage and resume ingest transaction

**Files:**
- Create: `backend/src/kerui_recruit/storage/blobs.py`
- Create: `backend/src/kerui_recruit/resumes/ingest.py`
- Create: `backend/src/kerui_recruit/resumes/schemas.py`
- Create: `backend/tests/storage/test_blobs.py`
- Create: `backend/tests/resumes/test_ingest.py`

**Interfaces:**
- Produces: `BlobStore.put(source: BinaryIO, suffix: str) -> StoredBlob`
- Produces: `BlobStore.open(sha256: str) -> BinaryIO`
- Produces: `ResumeIngestService.ingest(command: IngestResume) -> IngestResult`

- [ ] **Step 1: Write atomic storage and deduplication tests**

```python
def test_blob_store_deduplicates_and_uses_hash_shards(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    first = store.put(BytesIO(b"resume"), ".pdf")
    second = store.put(BytesIO(b"resume"), ".pdf")
    assert first.sha256 == second.sha256
    assert first.path == tmp_path / first.sha256[:2] / first.sha256[2:4] / f"{first.sha256}.pdf"
    assert list(tmp_path.rglob("*.pdf")) == [first.path]
```

```python
def test_ingest_reuses_blob_but_creates_version(session: Session, blob_store: BlobStore) -> None:
    service = ResumeIngestService(session, blob_store)
    one = service.ingest(IngestResume(filename="a.pdf", content=b"same"))
    two = service.ingest(IngestResume(filename="b.pdf", content=b"same", candidate_id=one.candidate_id))
    assert one.blob_id == two.blob_id
    assert one.revision_id != two.revision_id
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run --project backend pytest backend/tests/storage/test_blobs.py backend/tests/resumes/test_ingest.py -q`  
Expected: FAIL because storage and ingest services are missing.

- [ ] **Step 3: Implement staged writes and idempotent ingest**

Write content to `temp`, flush and fsync, then use an atomic rename into the shard path. Create the database records in one transaction and remove staged files on rollback. Accept only `.pdf`, `.doc`, and `.docx`; return `E_FILE_TYPE_UNSUPPORTED` for other suffixes.

- [ ] **Step 4: Run ingest tests**

Run: `uv run --project backend pytest backend/tests/storage/test_blobs.py backend/tests/resumes/test_ingest.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit blob ingest**

```powershell
git add backend/src/kerui_recruit/storage backend/src/kerui_recruit/resumes backend/tests
git commit -m "feat: add atomic resume ingest"
```

### Task 5: Durable task leases and worker recovery

**Files:**
- Create: `backend/src/kerui_recruit/tasks/models.py`
- Create: `backend/src/kerui_recruit/tasks/repository.py`
- Create: `backend/src/kerui_recruit/tasks/worker.py`
- Create: `backend/tests/tasks/test_repository.py`
- Create: `backend/tests/tasks/test_worker.py`

**Interfaces:**
- Produces: `TaskRepository.enqueue(spec: TaskSpec) -> str`
- Produces: `TaskRepository.claim(worker_id: str, queues: tuple[str, ...]) -> ClaimedTask | None`
- Produces: `TaskRepository.heartbeat(task_id: str, worker_id: str) -> None`
- Produces: `TaskRepository.complete(task_id: str, result_ref: str | None) -> None`
- Produces: `recover_expired_leases(now: datetime) -> int`

- [ ] **Step 1: Write lease, priority, and crash-recovery tests**

```python
def test_claim_prefers_priority_and_excludes_live_lease(repo: TaskRepository) -> None:
    low = repo.enqueue(TaskSpec(type="parse", queue="batch", priority=10, payload={}))
    high = repo.enqueue(TaskSpec(type="parse", queue="interactive", priority=100, payload={}))
    claimed = repo.claim("worker-1", ("interactive", "batch"))
    assert claimed and claimed.id == high
    assert repo.claim("worker-2", ("interactive",)).id != high


def test_expired_running_task_returns_to_queue(repo: TaskRepository, clock: FakeClock) -> None:
    task_id = repo.enqueue(TaskSpec(type="parse", queue="batch", priority=1, payload={}))
    repo.claim("dead-worker", ("batch",))
    clock.advance(minutes=6)
    assert repo.recover_expired_leases(clock.now()) == 1
    assert repo.claim("new-worker", ("batch",)).id == task_id
```

- [ ] **Step 2: Verify task tests fail**

Run: `uv run --project backend pytest backend/tests/tasks -q`  
Expected: FAIL because task services are missing.

- [ ] **Step 3: Implement compare-and-swap leases**

Claims must execute in a short `BEGIN IMMEDIATE` transaction, update one eligible task, create a `task_event`, and commit before work begins. Retry delays are `1s, 5s, 30s, 2m, 10m` with deterministic jitter injection in tests.

- [ ] **Step 4: Run task tests**

Run: `uv run --project backend pytest backend/tests/tasks -q`  
Expected: PASS.

- [ ] **Step 5: Commit durable tasks**

```powershell
git add backend/src/kerui_recruit/tasks backend/tests/tasks
git commit -m "feat: add durable local task worker"
```

### Task 6: Provider contracts and deterministic fakes

**Files:**
- Create: `backend/src/kerui_recruit/providers/contracts.py`
- Create: `backend/src/kerui_recruit/providers/errors.py`
- Create: `backend/src/kerui_recruit/providers/fakes.py`
- Create: `backend/src/kerui_recruit/providers/openai_compatible.py`
- Create: `backend/tests/providers/test_contracts.py`
- Create: `backend/tests/providers/test_error_mapping.py`

**Interfaces:**
- Produces: `LLMProvider`, `EmbeddingProvider`, `RerankerProvider`, `OCRProvider` protocols
- Produces: `ProviderError(code: str, retryable: bool, user_message: str)`
- Produces: deterministic fake implementations for all contracts

- [ ] **Step 1: Write provider contract tests**

```python
async def test_fake_embedding_is_stable_and_has_requested_dimension() -> None:
    provider = FakeEmbeddingProvider(dimension=16)
    first = await provider.embed_documents(["Python 金融"])
    second = await provider.embed_documents(["Python 金融"])
    assert first == second
    assert len(first[0]) == 16


@pytest.mark.parametrize("status,code,retryable", [
    (401, "E_API_AUTH", False),
    (402, "E_API_BALANCE", False),
    (429, "E_API_RATE_LIMIT", True),
    (500, "E_API_UPSTREAM", True),
    (503, "E_API_BUSY", True),
])
def test_http_status_mapping(status: int, code: str, retryable: bool) -> None:
    error = map_http_error(status)
    assert (error.code, error.retryable) == (code, retryable)
```

- [ ] **Step 2: Verify provider tests fail**

Run: `uv run --project backend pytest backend/tests/providers -q`  
Expected: FAIL because provider contracts are missing.

- [ ] **Step 3: Implement contracts, fakes, and error mapping**

The OpenAI-compatible implementation must validate structured JSON with Pydantic, use explicit connect/read timeouts, omit secrets from exceptions, and attach a non-sensitive request ID.

- [ ] **Step 4: Run provider tests**

Run: `uv run --project backend pytest backend/tests/providers -q`  
Expected: PASS.

- [ ] **Step 5: Commit provider foundation**

```powershell
git add backend/src/kerui_recruit/providers backend/tests/providers
git commit -m "feat: add configurable model providers"
```

### Task 7: Resume extraction, normalization, and parse pipeline

**Files:**
- Create: `backend/src/kerui_recruit/resumes/extract.py`
- Create: `backend/src/kerui_recruit/resumes/normalize.py`
- Create: `backend/src/kerui_recruit/resumes/pipeline.py`
- Create: `backend/src/kerui_recruit/resumes/structured.py`
- Create: `backend/tests/resumes/test_extract.py`
- Create: `backend/tests/resumes/test_pipeline.py`
- Create: `backend/tests/fixtures/resume.docx`
- Create: `backend/tests/fixtures/resume.pdf`

**Interfaces:**
- Produces: `extract_text(path: Path) -> ExtractedText`
- Produces: `normalize_resume(parsed: ParsedResume) -> NormalizedResume`
- Produces: `ResumePipeline.run(revision_id: str) -> PipelineResult`

- [ ] **Step 1: Write extraction and pipeline tests**

```python
def test_docx_and_pdf_extract_nonempty_text(fixtures: Path) -> None:
    assert "Python" in extract_text(fixtures / "resume.docx").text
    assert "Python" in extract_text(fixtures / "resume.pdf").text


async def test_pipeline_persists_facts_and_search_chunks(pipeline: ResumePipeline, session: Session) -> None:
    result = await pipeline.run(seed_revision(session, text="5年 Python 金融风控"))
    assert result.status == "READY"
    assert result.chunk_count >= 2
    candidate = session.get(Candidate, result.candidate_id)
    assert candidate.total_years == Decimal("5.0")
```

- [ ] **Step 2: Verify pipeline tests fail**

Run: `uv run --project backend pytest backend/tests/resumes/test_extract.py backend/tests/resumes/test_pipeline.py -q`  
Expected: FAIL because extraction and pipeline modules are missing.

- [ ] **Step 3: Implement typed extraction and normalization**

Use PyMuPDF for text PDFs and python-docx for DOCX. Return `requires_ocr=True` when extracted text is below the configured threshold. Normalize dates, degree levels, locations, skills, phone/email, total years and confidence without inventing unknown values.

- [ ] **Step 4: Run resume tests**

Run: `uv run --project backend pytest backend/tests/resumes -q`  
Expected: PASS.

- [ ] **Step 5: Commit the parse pipeline**

```powershell
git add backend/src/kerui_recruit/resumes backend/tests/resumes backend/tests/fixtures
git commit -m "feat: add resume parsing pipeline"
```

### Task 8: LanceDB search projection and hybrid retrieval

**Files:**
- Create: `backend/src/kerui_recruit/search/contracts.py`
- Create: `backend/src/kerui_recruit/search/lancedb_index.py`
- Create: `backend/src/kerui_recruit/search/query.py`
- Create: `backend/src/kerui_recruit/search/service.py`
- Create: `backend/tests/search/test_hybrid.py`
- Create: `backend/tests/search/test_filters.py`

**Interfaces:**
- Produces: `SearchIndex.upsert(chunks: Sequence[SearchChunk]) -> None`
- Produces: `SearchIndex.delete_revision(revision_id: str) -> None`
- Produces: `SearchIndex.search(request: SearchRequest) -> list[SearchHit]`
- Produces: `HybridSearchService.search(query: str, filters: CandidateFilters, limit: int) -> SearchPage`

- [ ] **Step 1: Write hybrid and hard-filter tests**

```python
async def test_hybrid_search_combines_keyword_and_semantic_hits(search_service: HybridSearchService) -> None:
    await seed_search(search_service, [
        chunk("a", "Java 支付系统", [1.0, 0.0]),
        chunk("b", "金融结算平台", [0.9, 0.1]),
        chunk("c", "平面设计", [0.0, 1.0]),
    ])
    page = await search_service.search("Java 金融", CandidateFilters(), 20)
    assert [hit.candidate_id for hit in page.items[:2]] == ["a", "b"]


async def test_hard_filters_never_leak_nonmatching_candidates(search_service: HybridSearchService) -> None:
    page = await search_service.search("Python", CandidateFilters(min_years=5, degree="MASTER"), 100)
    assert all(hit.total_years >= 5 and hit.degree == "MASTER" for hit in page.items)
```

- [ ] **Step 2: Verify search tests fail**

Run: `uv run --project backend pytest backend/tests/search -q`  
Expected: FAIL because search interfaces are missing.

- [ ] **Step 3: Implement projection, BM25, ANN, and RRF**

Create scalar, FTS, and vector indexes. Execute BM25 and ANN concurrently, apply structured prefilters, merge by RRF with `k=60`, request external reranking only for the top 100, and return RRF results when the reranker fails or times out.

- [ ] **Step 4: Run search tests**

Run: `uv run --project backend pytest backend/tests/search -q`  
Expected: PASS.

- [ ] **Step 5: Commit search projection**

```powershell
git add backend/src/kerui_recruit/search backend/tests/search
git commit -m "feat: add embedded hybrid search"
```

### Task 9: Secured local API for ingest, tasks, and search

**Files:**
- Create: `backend/src/kerui_recruit/api/auth.py`
- Create: `backend/src/kerui_recruit/api/errors.py`
- Create: `backend/src/kerui_recruit/api/resumes.py`
- Create: `backend/src/kerui_recruit/api/tasks.py`
- Create: `backend/src/kerui_recruit/api/search.py`
- Modify: `backend/src/kerui_recruit/main.py`
- Create: `backend/tests/api/test_auth.py`
- Create: `backend/tests/api/test_resume_flow.py`

**Interfaces:**
- Produces: `POST /api/resumes/import`
- Produces: `GET /api/tasks/{task_id}`
- Produces: `POST /api/search/candidates`
- Produces: stable `{code, message, request_id, details}` error envelope

- [ ] **Step 1: Write authentication and end-to-end API tests**

```python
def test_api_rejects_missing_session_token(client: TestClient) -> None:
    response = client.post("/api/search/candidates", json={"query": "Python"})
    assert response.status_code == 401
    assert response.json()["code"] == "E_LOCAL_SESSION"


def test_resume_import_reaches_search_with_fake_providers(client: TestClient, token_headers: dict[str, str]) -> None:
    imported = client.post(
        "/api/resumes/import",
        files={"file": ("resume.pdf", make_resume_pdf("Python 金融"), "application/pdf")},
        headers=token_headers,
    ).json()
    run_worker_until_idle()
    result = client.post(
        "/api/search/candidates",
        json={"query": "Python 金融", "limit": 20},
        headers=token_headers,
    ).json()
    assert result["items"][0]["candidate_id"] == imported["candidate_id"]
```

- [ ] **Step 2: Verify API tests fail**

Run: `uv run --project backend pytest backend/tests/api -q`  
Expected: FAIL because routers and local auth are missing.

- [ ] **Step 3: Implement routers and error middleware**

Use streaming upload limits, request IDs, typed request/response schemas, session-token middleware and Chinese user messages. Never include paths, secrets, stack traces or candidate plaintext in API errors.

- [ ] **Step 4: Run API tests**

Run: `uv run --project backend pytest backend/tests/api -q`  
Expected: PASS.

- [ ] **Step 5: Commit the secured API**

```powershell
git add backend/src/kerui_recruit/api backend/src/kerui_recruit/main.py backend/tests/api
git commit -m "feat: expose secured local recruitment api"
```

### Task 10: Tauri/React desktop shell and task/search UI

**Files:**
- Create: `desktop/package.json`
- Create: `desktop/vite.config.ts`
- Create: `desktop/src/main.tsx`
- Create: `desktop/src/App.tsx`
- Create: `desktop/src/api/client.ts`
- Create: `desktop/src/features/search/SearchPage.tsx`
- Create: `desktop/src/features/tasks/TaskCenter.tsx`
- Create: `desktop/src-tauri/Cargo.toml`
- Create: `desktop/src-tauri/tauri.conf.json`
- Create: `desktop/src-tauri/src/lib.rs`
- Create: `desktop/tests/search.spec.ts`

**Interfaces:**
- Consumes: local API routes from Task 9
- Produces: sidecar lifecycle and session-token injection
- Produces: candidate search page and persistent task center

- [ ] **Step 1: Write the desktop E2E test**

```typescript
test("imports a resume and finds it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "导入简历" }).click();
  await page.getByLabel("选择文件").setInputFiles("../backend/tests/fixtures/resume.pdf");
  await expect(page.getByText("解析完成")).toBeVisible();
  await page.getByPlaceholder("搜索人才").fill("Python");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("row", { name: /Python/ })).toBeVisible();
});
```

- [ ] **Step 2: Verify the UI test fails**

Run: `npm --prefix desktop run test:e2e -- search.spec.ts`  
Expected: FAIL because the desktop application is absent.

- [ ] **Step 3: Implement the shell and minimum complete workflow**

The Rust shell must select a random loopback port, generate a 256-bit token, launch the backend sidecar, wait for `/health/ready`, and terminate it on explicit application exit. The UI must expose import progress, task failures with retry, filters, result table, details drawer and original preview.

- [ ] **Step 4: Run frontend and E2E tests**

Run: `npm --prefix desktop test`  
Expected: PASS.  
Run: `npm --prefix desktop run test:e2e -- search.spec.ts`  
Expected: PASS.

- [ ] **Step 5: Commit the desktop workflow**

```powershell
git add desktop
git commit -m "feat: add desktop import and search workflow"
```

### Task 11: 120,000-candidate benchmark and Phase 0 gate

**Files:**
- Create: `backend/src/kerui_recruit/bench/generate.py`
- Create: `backend/src/kerui_recruit/bench/search_benchmark.py`
- Create: `backend/tests/performance/test_search_budget.py`
- Create: `docs/verification/phase0.md`

**Interfaces:**
- Produces: `generate_dataset(candidate_count: int, seed: int) -> Iterable[BenchmarkCandidate]`
- Produces: JSON benchmark report with p50, p95, p99, error rate, Recall@300 and nDCG@10

- [ ] **Step 1: Write a scaled CI budget test**

```python
@pytest.mark.performance
def test_hybrid_search_scaled_budget(benchmark_app: BenchmarkApp) -> None:
    report = benchmark_app.run(candidate_count=12_000, query_count=200, concurrency=5)
    assert report.error_rate == 0
    assert report.p95_ms <= 1_500
    assert report.p99_ms <= 2_500
    assert report.recall_at_300 >= 0.98
```

- [ ] **Step 2: Verify the gate fails before tuning**

Run: `uv run --project backend pytest backend/tests/performance/test_search_budget.py -q -m performance`  
Expected: FAIL until the generator and report exist.

- [ ] **Step 3: Implement deterministic generation and benchmark reporting**

The local release gate must run 120,000 candidates, 800 labeled queries, 10 concurrent clients and 5 QPS while a normal import worker runs. Store machine details, dependency versions and index parameters in the report.

- [ ] **Step 4: Run all Phase 0 checks**

Run: `uv run --project backend pytest backend/tests -q`  
Expected: PASS.  
Run: `npm --prefix desktop test`  
Expected: PASS.  
Run: `npm --prefix desktop run test:e2e`  
Expected: PASS.  
Run: `uv run --project backend python -m kerui_recruit.bench.search_benchmark --candidates 120000 --queries 800 --concurrency 10 --qps 5`  
Expected: p95 <= 1500ms, p99 <= 2500ms, Recall@300 >= 0.98 and nDCG@10 >= 0.85.

- [ ] **Step 5: Record evidence and commit the Phase 0 gate**

```powershell
git add backend/src/kerui_recruit/bench backend/tests/performance docs/verification/phase0.md
git commit -m "test: add phase zero acceptance gate"
```
