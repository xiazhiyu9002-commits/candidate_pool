# Candidate Search Retrieval Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic word-level candidate retrieval with a shared lexicon, keyword smart/AND/OR controls, and user-controlled LLM semantic rewriting for vector and hybrid search.

**Architecture:** Candidate documents gain a dedicated whitespace-tokenized FTS field while retaining readable keyword evidence and a non-duplicated semantic vector surface. Keyword Boolean behavior operates on alias-aware concept groups; optional LLM rewriting runs only inside vector/hybrid semantic branches, has a strict sub-budget and cache, and always falls back to the original parsed keywords.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, LanceDB 0.37.x FTS, jieba 0.42.x, SQLAlchemy, React/TypeScript, Vitest, Playwright, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-09-09-search-retrieval-optimization-design.md`

## Global Constraints

- Keep the existing 4.5-second end-to-end search deadline.
- Keyword mode must never call embedding, reranker, or an LLM rewriter.
- LLM rewriting is disabled by default and can only affect vector/hybrid semantic query text.
- Exact filters and exclusions remain deterministic and cannot be changed by rewriting or keyword operators.
- `keyword`, `vector`, and `hybrid` remain separate user-visible modes.
- Never inject every alias into `vector_text`; normalize each skill to one canonical form.
- A rewrite failure is reported through `query_plan.rewrite_status`, not `degraded_reasons`.
- Any physical FTS schema/tokenizer change requires an explicit index version bump and rebuild.
- Preserve existing reverse-match and JD-match behavior by leaving new options at their defaults.
- Every implementation task follows red-green-refactor and ends with its own verification gate.
- This workspace currently has no `.git` directory. Every commit step is conditional: first run `Test-Path .git`; when it is false, do not initialize Git and instead append the task number, changed files, focused-test output and regression-test output to `.trae/search-implementation-progress.md`.

---

## File Map

### New files

- `backend/src/kerui_recruit/search/lexicon.py`: canonical skills, alias groups, Chinese/technical tokenization, document token expansion and query concepts.
- `backend/src/kerui_recruit/search/rewrite.py`: rewriter protocol, structured LLM implementation, validation, TTL/LRU cache and fallback result type.
- `backend/tests/search/test_lexicon.py`: word-boundary, aliases and concept-group unit tests.
- `backend/tests/search/test_rewrite.py`: rewrite validation, cache and failure tests.
- `backend/tests/fixtures/search_golden.json`: fixed synthetic relevance cases for regression metrics.
- `backend/tests/search/test_relevance_golden.py`: golden-set metric gate.
- `desktop/src/pages/TalentPoolPage.test.tsx`: operator/rewrite control behavior.
- `desktop/e2e/search-controls.spec.ts`: real browser mode/operator/rewrite request tests.

### Modified files

- `backend/pyproject.toml`: pin jieba runtime dependency.
- `kerui-recruit-sidecar.spec`: bundle jieba dictionary data.
- `backend/src/kerui_recruit/search/query.py`: consume shared lexicon and return lexical concepts.
- `backend/src/kerui_recruit/search/documents.py`: produce `keyword_index_text`, readable `keyword_text`, and canonical-only `vector_text`.
- `backend/src/kerui_recruit/resumes/pipeline.py`: pass the authoritative school-alias snapshot when building immediate candidate chunks.
- `backend/src/kerui_recruit/search/contracts.py`: add concept/operator/query-plan fields and indexed-text compatibility.
- `backend/src/kerui_recruit/search/lancedb_index.py`: add physical FTS column, whitespace tokenizer, progressive Boolean recall and version 5 metadata.
- `backend/src/kerui_recruit/search/sync.py`: populate the new indexed text during normal synchronization.
- `backend/src/kerui_recruit/search/rebuild_maintenance.py`: migrate/rebuild version 5 while reusing vectors when semantic text is unchanged.
- `backend/src/kerui_recruit/search/service.py`: apply keyword operator and optional semantic rewrite under the shared deadline.
- `backend/src/kerui_recruit/api/search.py`: validate request combinations, pass concepts/options and expose query-plan status.
- `backend/src/kerui_recruit/runtime.py`: build the shared text client early and inject an optional rewriter.
- `backend/src/kerui_recruit/schools/reference.py`: expose one authoritative standard-name/alias group snapshot for document and query normalization.
- `backend/tests/schools/test_schools.py`: verify standard names and imported aliases produce identical groups.
- `backend/tests/search/test_documents.py`: assert the three document surfaces.
- `backend/tests/search/test_query.py`: assert shared normalization and concept parsing.
- `backend/tests/search/test_service.py`: assert mode isolation, Boolean semantics, rewrite routing and deadline fallback.
- `backend/tests/search/test_consistency.py`: assert version-5 FTS behavior and rebuild requirements.
- `backend/tests/search/test_rebuild_maintenance.py`: assert vector reuse and version metadata.
- `backend/tests/api/test_search_consistency.py`: assert API validation and response query plan.
- `desktop/src/App.tsx`: add types/state/localStorage and pass search options.
- `desktop/src/api/client.ts`: send `operator` and `rewrite_enabled`.
- `desktop/src/pages/TalentPoolPage.tsx`: render mutually exclusive controls for keyword versus semantic modes.
- `desktop/src/styles.css`: style the controls and non-blocking rewrite status.

---

### Task 1: Establish a fixed search quality baseline

**Files:**
- Create: `backend/tests/fixtures/search_golden.json`
- Create: `backend/tests/search/test_relevance_golden.py`

**Interfaces:**
- Consumes: existing `LanceDBSearchIndex.search_fts(query, filters, limit)`.
- Produces: `load_golden_cases()`, `precision_at_k()`, `recall_at_k()` and `ndcg_at_k()` helpers used as release evidence.

- [ ] **Step 1: Add the golden fixture with explicit graded judgments**

Use stable synthetic IDs and include at least these cases:

```json
{
  "documents": [
    {"id": "java-backend", "text": "Java Spring Boot 后端 支付系统"},
    {"id": "javascript-frontend", "text": "JavaScript React 前端"},
    {"id": "kubernetes-platform", "text": "Kubernetes 容器平台 云原生"},
    {"id": "cpp-engine", "text": "C++ 搜索引擎"},
    {"id": "csharp-dotnet", "text": "C# .NET 企业应用"},
    {"id": "node-backend", "text": "Node.js 服务端"}
  ],
  "queries": [
    {"query": "Java", "relevance": {"java-backend": 3}},
    {"query": "JS", "relevance": {"javascript-frontend": 3}},
    {"query": "k8s", "relevance": {"kubernetes-platform": 3}},
    {"query": "C++", "relevance": {"cpp-engine": 3}},
    {"query": "C# .NET", "relevance": {"csharp-dotnet": 3}},
    {"query": "Node.js 后端", "relevance": {"node-backend": 3}}
  ]
}
```

- [ ] **Step 2: Write metric tests before changing retrieval**

Implement standard binary `Precision@20`/`Recall@20` and graded `nDCG@10`. Persist the pre-change metric values in the test output as the named baseline constants `BASELINE_PRECISION_20`, `BASELINE_RECALL_20`, and `BASELINE_NDCG_10`; do not set a lower threshold merely to make the new implementation pass.

- [ ] **Step 3: Run the baseline test and save the output in the task log**

Run:

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_relevance_golden.py -vv
```

Expected: PASS for metric calculation itself; output records the current retrieval metrics and the known alias/substring deficiencies.

- [ ] **Step 4: Commit the baseline only**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise write the required checkpoint record described in Global Constraints.

```powershell
git add backend/tests/fixtures/search_golden.json backend/tests/search/test_relevance_golden.py
git commit -m "test: establish candidate search relevance baseline"
```

---

### Task 2: Add the shared lexical model and packaged tokenizer

**Files:**
- Create: `backend/src/kerui_recruit/search/lexicon.py`
- Create: `backend/tests/search/test_lexicon.py`
- Modify: `backend/pyproject.toml`
- Modify: `kerui-recruit-sidecar.spec`
- Modify: `backend/src/kerui_recruit/schools/reference.py`
- Modify: `backend/tests/schools/test_schools.py`

**Interfaces:**
- Produces: `LEXICON_VERSION`, `LexicalConcept`, `normalize_skill`, `tokenize_lexical_text`, `concepts_from_query`, `expand_document_tokens`, and `SchoolReference.alias_groups()`.
- Consumed by: Tasks 3-5.

- [ ] **Step 1: Add failing boundary and alias tests**

```python
def test_skill_aliases_share_one_concept():
    assert concepts_from_query("js")[0] == LexicalConcept(
        canonical="JavaScript", aliases=("JavaScript", "JS")
    )

def test_technical_tokens_keep_boundaries():
    tokens = tokenize_lexical_text("C++ C# .NET Node.js Java JavaScript")
    assert {"c++", "c#", ".net", "node.js", "java", "javascript"} <= set(tokens)

def test_chinese_terms_do_not_create_cross_word_ngram():
    tokens = tokenize_lexical_text("Java后端工程师")
    assert "java" in tokens
    assert "后端" in tokens
    assert "a后" not in tokens

def test_document_alias_expansion_is_deduplicated():
    assert expand_document_tokens(["JS", "JavaScript"]).count("javascript") == 1

def test_dynamic_school_alias_group_is_shared():
    groups = {"北京大学": ("北大", "PKU", "Peking University")}
    assert concepts_from_query("北大 Java", extra_alias_groups=groups)[0].canonical == "北京大学"

def test_longest_dynamic_alias_is_matched_before_segmentation():
    groups = {"北京大学": ("Peking University",)}
    concepts = concepts_from_query("Peking University Java", extra_alias_groups=groups)
    assert [item.canonical for item in concepts] == ["北京大学", "Java"]
```

- [ ] **Step 2: Run the focused tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_lexicon.py -vv
```

Expected: collection fails because `kerui_recruit.search.lexicon` does not exist.

- [ ] **Step 3: Add the dependency and deterministic custom vocabulary**

Add `jieba>=0.42.1,<1` to project dependencies. In `lexicon.py`, initialize one tokenizer instance and register at least `JavaScript`, `TypeScript`, `Kubernetes`, `Spring Boot`, `Node.js`, `C++`, `C#`, `.NET`, `云原生`, `机器学习`, `深度学习`, `大模型`, `后端`, and `服务端`. Keep skill and degree alias data as canonical-to-alias groups so document expansion and query concepts cannot diverge. Match dynamic multi-word school aliases by longest normalized span before generic jieba/whitespace segmentation.

- [ ] **Step 4: Bundle jieba dictionary files into the sidecar**

Add:

```python
datas += collect_data_files('jieba')
hiddenimports += collect_submodules('jieba')
```

to `kerui-recruit-sidecar.spec`.

- [ ] **Step 5: Expose school aliases without duplicating the seed table**

Add `SchoolReference.alias_groups() -> dict[str, tuple[str, ...]]`. It loads each `School.canonical_name` and `School.aliases` from the database once per call and returns canonical-to-alias groups. The result must include user-imported overseas-school aliases as well as built-in aliases. `search/lexicon.py` accepts these groups through the `extra_alias_groups` argument; it must not import `SCHOOL_ALIASES` from `schools/seed.py`.

- [ ] **Step 6: Run lexical, school and existing query tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_lexicon.py tests/search/test_query.py tests/search/test_degrees.py tests/schools/test_schools.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit or record the shared lexical checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/pyproject.toml backend/src/kerui_recruit/search/lexicon.py backend/tests/search/test_lexicon.py backend/src/kerui_recruit/schools/reference.py backend/tests/schools/test_schools.py kerui-recruit-sidecar.spec
git commit -m "feat: add shared lexical normalization"
```

---

### Task 3: Separate indexed, evidence and semantic document surfaces

**Files:**
- Modify: `backend/src/kerui_recruit/search/documents.py`
- Modify: `backend/src/kerui_recruit/search/contracts.py`
- Modify: `backend/src/kerui_recruit/resumes/pipeline.py`
- Modify: `backend/tests/search/test_documents.py`
- Modify: `backend/tests/search/test_query.py`

**Interfaces:**
- Consumes: Task 2 lexical functions.
- Produces: `build_candidate_document(..., school_alias_groups=...)["keyword_index_text"]` and `parse_query(..., school_alias_groups=...).concepts`.

- [ ] **Step 1: Write failing document-surface tests**

```python
def test_candidate_document_separates_three_surfaces():
    doc = build_candidate_document({
        "name": "张三",
        "skills": ["JS", "k8s"],
        "ai_profile_summary": "负责交易系统开发",
    })
    assert "javascript" in doc["keyword_index_text"].split()
    assert "js" in doc["keyword_index_text"].split()
    assert "kubernetes" in doc["keyword_index_text"].split()
    assert "张三" in doc["keyword_text"]
    assert doc["vector_text"].count("JavaScript") == 1
    assert " JS " not in f" {doc['vector_text']} "

def test_java_and_javascript_remain_distinct_tokens():
    doc = build_candidate_document({"skills": ["JavaScript"]})
    assert "javascript" in doc["keyword_index_text"].split()
    assert "java" not in doc["keyword_index_text"].split()

def test_school_aliases_expand_only_the_lexical_surface():
    doc = build_candidate_document(
        {"educations": [{"school": "北京大学"}]},
        school_alias_groups={"北京大学": ("北大", "PKU")},
    )
    assert {"北京大学", "北大", "pku"} <= set(doc["keyword_index_text"].split())
    assert "北大" not in doc["vector_text"]

def test_degree_aliases_share_the_normalized_degree_filter_vocabulary():
    doc = build_candidate_document({"highest_degree": "BACHELOR"})
    assert {"bachelor", "本科", "学士"} <= set(doc["keyword_index_text"].split())
```

- [ ] **Step 2: Write the failing parsed-concept test**

```python
def test_parse_query_returns_filters_keywords_and_concepts():
    parsed = parse_query("JS 后端 5年以上 上海")
    assert parsed.keywords == "JS 后端"
    assert [concept.canonical for concept in parsed.concepts] == ["JavaScript", "后端"]
    assert parsed.filters.min_years == 5
    assert parsed.filters.location == "上海"
```

- [ ] **Step 3: Run focused tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_documents.py tests/search/test_query.py -vv
```

- [ ] **Step 4: Implement the three surfaces without changing field-term projections**

Keep `name_terms`, `school_terms`, `company_terms`, `title_terms`, `location_terms`, `school_tags`, age and years behavior unchanged. Use canonical skills once in `vector_parts`; expand aliases only when creating `keyword_index_text`. Accept optional `school_alias_groups` in both `build_candidate_document` and `parse_query`, so the same `SchoolReference.alias_groups()` snapshot drives document and query normalization.

Add `keyword_index_text: str | None = None` to `SearchChunk` and an `effective_keyword_index_text` property that falls back to `effective_keyword_text` for compatibility. In `ResumePipeline`, obtain one `SchoolReference.alias_groups()` snapshot before building the immediate candidate document and pass it to `build_candidate_document`; this keeps non-deferred indexing consistent with durable outbox synchronization.

- [ ] **Step 5: Move `normalize_skill` imports to the shared lexicon**

Keep `normalize_skill` re-exported from `search/query.py` for one compatibility release so existing match tests and callers do not break immediately.

- [ ] **Step 6: Run document, query, match and pipeline tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_documents.py tests/search/test_query.py tests/resumes/test_pipeline.py tests/match/test_reverse_index.py -q
```

Expected: all pass; reverse matching still uses distinct keyword and vector representations.

- [ ] **Step 7: Commit or record the document-separation checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/search/documents.py backend/src/kerui_recruit/search/contracts.py backend/src/kerui_recruit/search/query.py backend/src/kerui_recruit/resumes/pipeline.py backend/tests/search/test_documents.py backend/tests/search/test_query.py
git commit -m "feat: separate candidate lexical and semantic documents"
```

---

### Task 4: Migrate FTS to whitespace tokens and index version 5

**Files:**
- Modify: `backend/src/kerui_recruit/search/lancedb_index.py`
- Modify: `backend/src/kerui_recruit/search/sync.py`
- Modify: `backend/src/kerui_recruit/search/rebuild_maintenance.py`
- Modify: `backend/tests/search/test_consistency.py`
- Modify: `backend/tests/search/test_rebuild_maintenance.py`
- Modify: `backend/tests/search/test_sync.py`

**Interfaces:**
- Consumes: `SearchChunk.keyword_index_text` and document `keyword_index_text` from Task 3.
- Produces: FTS over the `keyword_index_text` physical column with version `5` metadata.

- [ ] **Step 1: Add failing FTS boundary tests**

Seed separate candidates with `java`, `javascript`, `c++`, `c#`, `.net`, and `node.js` index tokens. Assert that searching `java` does not return the JavaScript-only row and every punctuation-bearing term returns only its intended row.

- [ ] **Step 2: Add failing compatibility tests**

Assert `INDEX_SCHEMA_VERSION == "5"`, `INDEX_CHUNK_VERSION == "5"`, and a version-4 metadata file causes `is_compatible()` to return false with an explicit rebuild message.

- [ ] **Step 3: Run the focused index tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_consistency.py tests/search/test_rebuild_maintenance.py tests/search/test_sync.py -vv
```

- [ ] **Step 4: Add the physical field and change the analyzer**

Create the FTS index with:

```python
FTS(
    base_tokenizer="whitespace",
    lower_case=True,
    stem=False,
    remove_stop_words=False,
)
```

Search `keyword_index_text`; continue returning readable `keyword_text` as `SearchHit.content`.

- [ ] **Step 5: Update normal sync and maintenance rebuild**

Normal writes must populate both fields. `IndexSyncService._snapshot()` loads `SchoolReference.alias_groups()` once inside its existing database session and passes the snapshot to every candidate document built in that batch. Rebuild uses the same alias source and validation compares both fields. Vector reuse is permitted only when the source row's `vector_text` equals the rebuilt document's `vector_text`; otherwise enqueue a fresh embedding instead of copying a stale vector.

- [ ] **Step 6: Run index and rebuild tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_consistency.py tests/search/test_rebuild_maintenance.py tests/search/test_sync.py tests/search/test_index_maintenance.py -q
```

- [ ] **Step 7: Commit or record the index-migration checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/search/lancedb_index.py backend/src/kerui_recruit/search/sync.py backend/src/kerui_recruit/search/rebuild_maintenance.py backend/tests/search/test_consistency.py backend/tests/search/test_rebuild_maintenance.py backend/tests/search/test_sync.py
git commit -m "feat: migrate candidate FTS to word-level index"
```

---

### Task 5: Implement alias-aware smart/AND/OR keyword retrieval

**Files:**
- Modify: `backend/src/kerui_recruit/search/contracts.py`
- Modify: `backend/src/kerui_recruit/search/lancedb_index.py`
- Modify: `backend/src/kerui_recruit/search/service.py`
- Modify: `backend/tests/search/test_service.py`
- Modify: `backend/tests/search/test_filters.py`

**Interfaces:**
- Consumes: `tuple[LexicalConcept, ...]` from Task 3.
- Produces: `HybridSearchService.search(..., operator: str = "smart", concepts: tuple[LexicalConcept, ...] = ())`.

- [ ] **Step 1: Add failing Boolean semantics tests**

```python
@pytest.mark.asyncio
async def test_keyword_and_requires_every_concept(tmp_path):
    page = await service.search(
        "Java 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="and", concepts=concepts_from_query("Java 后端"),
    )
    assert [hit.candidate_id for hit in page.items] == ["java-backend"]

@pytest.mark.asyncio
async def test_keyword_or_accepts_any_concept(tmp_path):
    page = await service.search(
        "Java 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="or", concepts=concepts_from_query("Java 后端"),
    )
    assert {hit.candidate_id for hit in page.items} == {
        "java-backend", "java-non-backend", "python-backend"
    }

@pytest.mark.asyncio
async def test_aliases_are_or_within_and_concepts(tmp_path):
    page = await service.search(
        "JS 后端", CandidateFilters(), limit=20, mode="keyword",
        operator="and", concepts=concepts_from_query("JS 后端"),
    )
    assert "javascript-backend" in {hit.candidate_id for hit in page.items}
```

- [ ] **Step 2: Add a recall-cap regression test**

Seed more than 300 high-scoring OR-only candidates and one lower-ranked candidate satisfying every AND concept. Assert the satisfying candidate is still returned. This test prevents a fixed `top 300 -> Python post-filter` implementation.

- [ ] **Step 3: Run Boolean tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_service.py tests/search/test_filters.py -vv
```

- [ ] **Step 4: Implement Boolean matching over normalized token sets**

For each concept, match any alias token; combine concept matches with AND or OR. Prefer a Lance/Tantivy Boolean query when its behavior passes the punctuation and Chinese tests. If the installed query parser cannot preserve those semantics, progressively expand candidate retrieval until the requested page is full, the table is exhausted, or the shared deadline expires.

- [ ] **Step 5: Preserve smart and non-keyword behavior**

`smart` must execute the current BM25 path without a hard post-filter. `vector` and `hybrid` must not silently accept a non-smart operator; their invalid combinations are rejected by the API in Task 6. Direct service callers retain `operator="smart"` defaults.

- [ ] **Step 6: Run keyword, filter and reverse-match regression tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_service.py tests/search/test_filters.py tests/search/test_hybrid.py tests/match/test_reverse_index.py -q
```

- [ ] **Step 7: Commit or record the keyword-operator checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/search/contracts.py backend/src/kerui_recruit/search/lancedb_index.py backend/src/kerui_recruit/search/service.py backend/tests/search/test_service.py backend/tests/search/test_filters.py
git commit -m "feat: add alias-aware keyword operators"
```

---

### Task 6: Add API validation and query-plan response

**Files:**
- Modify: `backend/src/kerui_recruit/api/search.py`
- Modify: `backend/src/kerui_recruit/search/contracts.py`
- Modify: `backend/tests/api/test_search_consistency.py`
- Modify: `backend/tests/api/test_filter_only_request.py`

**Interfaces:**
- Produces request fields `operator` and `rewrite_enabled` and response field `query_plan`.
- Consumes service arguments from Task 5 and rewrite status from Task 8.

- [ ] **Step 1: Add failing request-combination tests**

Assert these responses:

```text
keyword + smart/and/or + rewrite=false -> 200
keyword + rewrite=true -> 422
vector + operator=and/or -> 422
hybrid + operator=and/or -> 422
vector/hybrid + smart + rewrite=true/false -> 200
```

- [ ] **Step 2: Add failing filter and school-alias preservation tests**

Post `"JS 后端 5年以上 上海"` with `operator="and"`. Capture the service call and assert the service receives keywords `"JS 后端"`, `min_years=5`, `location="上海"`, two lexical concepts and no rewritten filters. Then post `"北大 Java"` and assert the API obtains `SchoolReference.alias_groups()` under the same deadline and passes a `北京大学` concept to the service while retaining `Java` as a separate concept.

- [ ] **Step 3: Implement Pydantic fields and cross-field validation**

Use `Literal` types and a model-level validator; do not accept arbitrary strings and normalize them later.

- [ ] **Step 4: Return a query plan for every code path**

Index-not-ready, no-match, service-error and successful responses must all include a `query_plan`. Before Task 8 is connected, use `disabled` or `not_applicable`; no response branch may omit the field.

- [ ] **Step 5: Run API tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/api/test_search_consistency.py tests/api/test_filter_only_request.py -q
```

- [ ] **Step 6: Commit or record the API-contract checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/api/search.py backend/src/kerui_recruit/search/contracts.py backend/tests/api/test_search_consistency.py backend/tests/api/test_filter_only_request.py
git commit -m "feat: expose candidate search query controls"
```

---

### Task 7: Implement the bounded and cached semantic rewriter

**Files:**
- Create: `backend/src/kerui_recruit/search/rewrite.py`
- Create: `backend/tests/search/test_rewrite.py`
- Read only: `backend/src/kerui_recruit/providers/openai_compatible.py`

**Interfaces:**
- Produces: `QueryRewriter`, `SemanticQueryRewriter`, `RewriteResult(query: str, status: str)`.
- Consumed by: Task 8.

- [ ] **Step 1: Add failing success and safety tests**

Use a fake `OpenAICompatibleClient` and assert:

```python
result = await rewriter.rewrite("JS 交易系统")
assert result.query == "JavaScript 交易系统服务端开发"
assert result.status == "success"
```

Also assert empty output, more than 500 characters, invalid JSON and an output that adds `上海` or `硕士` all return the original query with `status="unavailable"`.

- [ ] **Step 2: Add failing cache tests**

Two identical calls within 600 seconds must call the fake client once. Changing `LEXICON_VERSION`, model identity or normalized source text must miss the cache. The 257th unique entry must evict the least recently used item.

- [ ] **Step 3: Run rewrite tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_rewrite.py -vv
```

- [ ] **Step 4: Implement structured output**

Use `OpenAICompatibleClient.complete_json` with:

```python
class SemanticRewritePayload(BaseModel):
    semantic_query: str = Field(min_length=1, max_length=500)
```

The system prompt must state: only align synonyms and resume terminology; do not add location, education, experience years, school, employer, salary, age, gender or any requirement absent from the input; return JSON only.

- [ ] **Step 5: Validate that rewriting introduced no hard filters**

Compare the filters from `parse_query(original)` and `parse_query(rewritten)`. If the rewritten result contains any non-default hard filter not present in the original, reject it and return the original query as unavailable.

- [ ] **Step 6: Run rewrite and provider tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_rewrite.py tests/providers/test_error_mapping.py -q
```

- [ ] **Step 7: Commit or record the rewriter checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/search/rewrite.py backend/tests/search/test_rewrite.py
git commit -m "feat: add safe semantic query rewriter"
```

---

### Task 8: Route optional rewriting through vector and hybrid search

**Files:**
- Modify: `backend/src/kerui_recruit/search/service.py`
- Modify: `backend/src/kerui_recruit/search/contracts.py`
- Modify: `backend/src/kerui_recruit/runtime.py`
- Modify: `backend/src/kerui_recruit/api/search.py`
- Modify: `backend/tests/search/test_service.py`
- Modify: `backend/tests/api/test_search_consistency.py`

**Interfaces:**
- Consumes: optional `QueryRewriter` from Task 7.
- Produces: `HybridSearchService.search(..., rewrite_enabled: bool = False)` and populated `SearchPage.query_plan`.

- [ ] **Step 1: Add failing mode-isolation tests**

With spy rewriter, embedding and reranker providers, assert:

- keyword mode never calls the rewriter, embedding or reranker;
- vector rewrite disabled sends original parsed keywords to embedding/reranker;
- vector rewrite enabled sends rewritten text to embedding/reranker;
- hybrid rewrite enabled sends deterministic lexical text to FTS and rewritten text to embedding/reranker.

- [ ] **Step 2: Add failing timeout/fallback tests**

A rewriter sleeping beyond its sub-budget must produce `rewrite_status="unavailable"`, call embedding with the original query, and return usable results. In hybrid mode a slow rewriter must not prevent the FTS branch from returning within the 4.5-second overall deadline.

- [ ] **Step 3: Run focused service tests and verify failure**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search/test_service.py tests/api/test_search_consistency.py -vv
```

- [ ] **Step 4: Inject the optional rewriter without duplicating text-provider routing**

Move the existing `text_key/text_url/text_model` and `OpenAICompatibleClient` construction in `runtime.py` to immediately after `build_providers(settings)`. Reuse that same client for profile generators, BD planning and `SemanticQueryRewriter`; do not construct a second HTTP client or repeat capability resolution.

- [ ] **Step 5: Apply the rewrite sub-budget inside the semantic branch**

Calculate the rewrite deadline as:

```python
rewrite_deadline = min(budget, time.monotonic() + min(1.0, max(0.0, (budget - time.monotonic()) * 0.25)))
```

For hybrid search, start FTS immediately and run rewrite -> embedding -> vector in the semantic task. On every rewrite exception or timeout, continue semantic retrieval with the original query.

- [ ] **Step 6: Use the semantic query for reranking**

Carry `semantic_query_used` through the service so the reranker receives the same semantic intent as embedding. Do not overwrite the lexical `query` argument used by FTS or keyword hit reasons.

- [ ] **Step 7: Run all service/API/match regression tests**

```powershell
Set-Location backend
py -3.12 -m pytest tests/search tests/api/test_search_consistency.py tests/match/test_reverse_index.py tests/match/test_match.py -q
```

- [ ] **Step 8: Commit or record the semantic-routing checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/src/kerui_recruit/search/service.py backend/src/kerui_recruit/search/contracts.py backend/src/kerui_recruit/runtime.py backend/src/kerui_recruit/api/search.py backend/tests/search/test_service.py backend/tests/api/test_search_consistency.py
git commit -m "feat: route optional semantic query rewriting"
```

---

### Task 9: Add user controls and persist preferences

**Files:**
- Modify: `desktop/src/App.tsx`
- Modify: `desktop/src/api/client.ts`
- Modify: `desktop/src/pages/TalentPoolPage.tsx`
- Modify: `desktop/src/styles.css`
- Create: `desktop/src/pages/TalentPoolPage.test.tsx`

**Interfaces:**
- Produces: `CandidateKeywordOperator`, `CandidateSearchOptions`, UI state and API payload.
- Consumes: Task 6 API request/response contract.

- [ ] **Step 1: Add failing API-client payload tests**

Assert exact JSON bodies:

```json
{"query":"Java 后端","mode":"keyword","operator":"and","rewrite_enabled":false,"limit":50}
```

and:

```json
{"query":"交易系统后端","mode":"hybrid","operator":"smart","rewrite_enabled":true,"limit":50}
```

- [ ] **Step 2: Add failing component tests**

Assert that keyword mode renders `aria-label="关键词逻辑"` with three Chinese options and does not render the rewrite checkbox. Assert vector/hybrid render `aria-label="AI 语义改写"` and do not render the keyword-operator selector.

- [ ] **Step 3: Add state and stable localStorage keys**

Use:

```text
search-keyword-operator:v1
search-rewrite-enabled:v1
```

Defaults are `smart` and `false`. Invalid stored values fall back to those defaults.

- [ ] **Step 4: Replace positional client arguments with an options object**

```typescript
export type CandidateKeywordOperator = "smart" | "and" | "or";

export interface CandidateSearchOptions {
  mode: "keyword" | "vector" | "hybrid";
  operator: CandidateKeywordOperator;
  rewriteEnabled: boolean;
}

searchCandidates(
  query: string,
  filters?: CandidateSearchFilters,
  options?: CandidateSearchOptions,
): Promise<CandidateSearchResult>;
```

The client serializes `rewriteEnabled` as `rewrite_enabled`.

- [ ] **Step 5: Render mutually exclusive controls**

Use labels `智能排序`, `同时满足`, `满足任一`. The semantic toggle copy is `AI 语义改写` with helper text `可能增加最多约 1 秒`。Do not leave a disabled irrelevant control taking toolbar space.

- [ ] **Step 6: Handle response status without treating it as a service outage**

When requested rewriting returns `unavailable`, show `AI 改写不可用，已使用原搜索词。` as a notice. Do not set the global error and do not append to `degraded_reasons`.

- [ ] **Step 7: Run frontend unit tests and build**

```powershell
Set-Location desktop
npm test -- --run
npm run build
```

Expected: all tests pass and production build succeeds.

- [ ] **Step 8: Commit or record the frontend-controls checkpoint**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add desktop/src/App.tsx desktop/src/api/client.ts desktop/src/pages/TalentPoolPage.tsx desktop/src/pages/TalentPoolPage.test.tsx desktop/src/styles.css
git commit -m "feat: add candidate search query controls"
```

---

### Task 10: Complete automated, packaged and real-API acceptance

**Files:**
- Create: `desktop/e2e/search-controls.spec.ts`
- Modify: `backend/tests/search/test_relevance_golden.py`
- Modify: `.github/workflows/ci.yml`
- Evidence only: `.trae/search-acceptance-2026-09-09.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: final release evidence; no new production behavior.

- [ ] **Step 1: Turn baseline metrics into non-regression gates**

Assert keyword `Precision@20 >= BASELINE_PRECISION_20`, alias `Recall@20 > BASELINE_RECALL_20`, and hybrid `nDCG@10 >= BASELINE_NDCG_10`. Keep the individual mandatory alias and boundary assertions even if aggregate metrics pass.

- [ ] **Step 2: Add browser request and persistence coverage**

Playwright must verify:

1. Keyword + `同时满足` sends `operator="and"`, never sends rewrite true.
2. Vector + rewrite on sends `operator="smart"`, `rewrite_enabled=true`.
3. Hybrid + rewrite off sends `rewrite_enabled=false`.
4. Reload preserves both preferences.
5. Rewrite unavailable displays a notice while candidate results remain visible.

- [ ] **Step 3: Run the complete backend gate**

```powershell
Set-Location backend
py -3.12 -m pytest -q
```

Expected: zero failures; existing skipped/deselected tests are listed and explained in the evidence file.

- [ ] **Step 4: Run the complete frontend gate**

```powershell
Set-Location desktop
npm test -- --run
npm run build
npx playwright test
```

Expected: zero failures.

- [ ] **Step 5: Build and smoke-test the packaged sidecar offline**

```powershell
Set-Location 'C:\Users\Nl\Desktop\安装包+源码\candidate_pool'
py -3.12 -m PyInstaller --clean kerui-recruit-sidecar.spec
```

Disconnect or block outbound provider calls, start the packaged sidecar with a temporary data root, import one Chinese resume and verify keyword search for `Java后端`, `JS`, `C++` and `k8s`. The sidecar must not fail because jieba data is missing.

- [ ] **Step 6: Run a real configured API test with resumes from `1`**

Use the running application's actual session token and base URL. Import at least one representative file from `C:\Users\Nl\Desktop\安装包+源码\candidate_pool\1`, wait until parsing/index synchronization is complete, then execute:

```text
keyword/smart
keyword/and
keyword/or
vector/rewrite off
vector/rewrite on
hybrid/rewrite off
hybrid/rewrite on
```

Record sanitized request fields, candidate IDs/names, `query_plan`, `degraded_reasons`, status code and elapsed milliseconds in `.trae/search-acceptance-2026-09-09.md`. Do not record API keys, session tokens, full phone numbers or resume bodies.

- [ ] **Step 7: Check the release thresholds**

Reject completion if any condition is true:

- Java-only query returns a JavaScript-only candidate.
- Any mandatory alias case fails.
- AND misses a valid candidate because it was beyond a fixed recall cap.
- Keyword mode makes any remote provider call.
- Rewrite off makes an LLM call.
- Rewrite failure removes otherwise available search results.
- Exact filters differ between rewrite on and off.
- Keyword P95 exceeds 500ms.
- Rewrite-enabled search exceeds the 4.5-second total deadline.
- Sidecar cannot tokenize Chinese without internet access.

- [ ] **Step 8: Commit or record final acceptance coverage**

First run `Test-Path .git`. Run the commit only when it returns `True`; otherwise append the final checkpoint evidence to `.trae/search-implementation-progress.md`.

```powershell
git add backend/tests/search/test_relevance_golden.py desktop/e2e/search-controls.spec.ts .github/workflows/ci.yml .trae/search-acceptance-2026-09-09.md
git commit -m "test: verify candidate search optimization end to end"
```

---

## Trae Execution Loop

Trae must execute exactly one task at a time and use this state machine:

```text
READ SPEC + CURRENT TASK
  -> WRITE FAILING TEST
  -> PROVE TEST FAILS FOR EXPECTED REASON
  -> IMPLEMENT MINIMUM CHANGE
  -> RUN FOCUSED TESTS
  -> RUN TASK REGRESSION SET
  -> REVIEW DIFF AGAINST GLOBAL CONSTRAINTS
  -> COMMIT
  -> MARK TASK COMPLETE WITH EVIDENCE
  -> NEXT TASK
```

If the same failure repeats three times, stop modifying code and write a root-cause note containing the failing command, complete error, attempted changes and the smallest unresolved decision. Do not weaken an assertion, increase a timeout, lower a metric threshold or skip a test merely to obtain a green run.

Completion may be claimed only after Task 10 produces fresh evidence from all automated gates, the packaged offline smoke test and the real configured API test.
