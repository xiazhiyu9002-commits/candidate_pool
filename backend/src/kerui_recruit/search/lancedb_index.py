from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
import json
import threading
import time
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from kerui_recruit.search.contracts import (
    CandidateFilters,
    SearchChunk,
    SearchHit,
    SearchRequest,
)
from kerui_recruit.search.lexicon import is_curated_concept, tokenize_lexical_text


SEARCH_DEADLINE: ContextVar[float | None] = ContextVar("search_execution_deadline", default=None)

# 索引契约版本：物理字段（keyword_text/keyword_index_text/vector_text/字段词项）与分块方式均改变，
# 必须整体提升，旧索引不兼容时走显式重建。
# schema 9 -> 10 / chunk 7 -> 8：新增多值方向三列（career_directions / career_specializations /
# business_directions），旧索引缺列且检索路径不依赖它们，但筛选与匹配需要，故要求显式重建。
INDEX_SCHEMA_VERSION = "10"
INDEX_CHUNK_VERSION = "8"
# 可显式迁移的旧索引版本（只读回退 → 补列升级为可写）。
LEGACY_SCHEMA_VERSION = "8"
LEGACY_CHUNK_VERSION = "6"


def _join_terms(terms: tuple[str, ...]) -> str:
    """Space-joined, casefolded term text for deterministic field contains matching."""
    return " ".join(str(t) for t in terms if t).casefold()


def _sanitize_fts_query(query: str) -> str:
    """剥离引号等 tantivy 查询语法字符，让关键词按字面 token 匹配。

    LanceDB FTS 底层是 tantivy：双引号被当作短语查询语法（中文短语会解析失败
    抛异常），单引号则作为字面字符匹配不到任何 token。检索正文只应把输入当作
    普通词袋，去掉这些语法字符（含全角引号）。
    """
    for char in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        query = query.replace(char, " ")
    return " ".join(query.split())


def _concept_token_sets(concepts) -> tuple[set[str], ...]:
    return tuple({alias.casefold() for alias in concept.aliases} for concept in concepts)


def _matches_concepts(text: str, token_sets: tuple[set[str], ...], operator: str) -> bool:
    tokens = set(text.split())
    if operator == "and":
        return all(tokens & concept_tokens for concept_tokens in token_sets)
    return any(tokens & concept_tokens for concept_tokens in token_sets)


def _matched_concept_indices(text: str, token_sets: tuple[set[str], ...]) -> set[int]:
    """该 chunk 命中的 concept 下标集合（候选人级 AND 需按候选人跨 chunk 聚合）。"""
    tokens = set(text.split())
    return {index for index, concept_tokens in enumerate(token_sets) if tokens & concept_tokens}


class LanceDBSearchIndex:
    table_name = "candidate_chunks"
    rrf_k = 60
    optimize_every = 20

    def __init__(self, root: Path, *, vector_dimension: int,
                 embedding_model: str = "unspecified", schema_version: str = INDEX_SCHEMA_VERSION,
                 chunk_version: str = INDEX_CHUNK_VERSION) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.vector_dimension = vector_dimension
        self.database = lancedb.connect(str(root))
        self._pending_modifications = 0
        self._dirty_marker = root / ".fts-dirty"
        self._metadata_path = root / "candidate-index-metadata.json"
        self.metadata = {"schema_version": str(schema_version), "embedding_model": embedding_model,
                         "vector_dimension": vector_dimension, "chunk_version": str(chunk_version)}
        self._write_lock = threading.RLock()

    @property
    def compatibility_error(self) -> str | None:
        if not self._table_exists():
            return None
        try:
            actual = self._read_metadata()
        except (OSError, ValueError):
            return "Index incompatible: missing or invalid metadata; explicit rebuild required"
        if actual != self.metadata:
            return "Index incompatible: schema, model, dimension or chunk version changed; explicit rebuild required"
        schema = getattr(self.database.open_table(self.table_name), "schema", None)
        if schema is not None and ("keyword_text" not in schema.names or
                                   "keyword_index_text" not in schema.names or
                                   "body_index_text" not in schema.names or
                                   "chunk_type" not in schema.names or
                                   "kind" not in schema.names or
                                   "specializations" not in schema.names or
                                   "career_directions" not in schema.names or
                                   "career_specializations" not in schema.names or
                                   "business_directions" not in schema.names or
                                   "name_text" not in schema.names or
                                   "preferred_locations" not in schema.names or
                                   schema.field("vector").type.list_size != self.vector_dimension):
            return "Index incompatible: physical schema differs; explicit rebuild required"
        return None

    def is_compatible(self) -> bool:
        return self.compatibility_error is None

    def _require_compatible(self) -> None:
        error = self.compatibility_error
        if error:
            raise ValueError(error)

    # 旧索引（schema 8 / chunk 6）与新代码（schema 10 / chunk 8）在物理上几乎一致，
    # 只缺若干写侧字段（kind / sequence / evidence_path / specializations 与多值方向三列）；
    # 检索所需的全部列（含 keyword_index_text / vector / chunk_type / 各过滤列）均存在。因此把
    # 「可读」与「可写」拆开：旧索引可安全只读检索，写入仍需显式重建（staging）。
    _READ_COLUMNS = (
        "id", "candidate_id", "revision_id",
        "keyword_text", "keyword_index_text", "vector_text", "body_index_text",
        "chunk_type", "vector",
    )

    @property
    def read_compatibility_error(self) -> str | None:
        if not self._table_exists():
            return None
        try:
            actual = self._read_metadata()
        except (OSError, ValueError):
            return "Index not readable: missing or invalid metadata; explicit rebuild required"
        # 检索正确性只依赖 embedding 模型与向量维度；schema/chunk 版本可放宽，以允许旧索引只读回退。
        if actual.get("embedding_model") != self.metadata["embedding_model"]:
            return "Index not readable: embedding model changed; explicit rebuild required"
        if actual.get("vector_dimension") != self.metadata["vector_dimension"]:
            return "Index not readable: vector dimension changed; explicit rebuild required"
        schema = getattr(self.database.open_table(self.table_name), "schema", None)
        if schema is None:
            return None
        missing = [c for c in self._READ_COLUMNS if c not in schema.names]
        if missing:
            return f"Index not readable: missing columns {missing}; explicit rebuild required"
        if schema.field("vector").type.list_size != self.vector_dimension:
            return "Index not readable: vector dimension mismatch; explicit rebuild required"
        return None

    def is_readable(self) -> bool:
        return self.read_compatibility_error is None

    def _require_readable(self) -> None:
        error = self.read_compatibility_error
        if error:
            raise ValueError(error)

    def _has_column(self, name: str) -> bool:
        if not self._table_exists():
            return False
        schema = getattr(self.database.open_table(self.table_name), "schema", None)
        return schema is not None and name in schema.names

    def _write_metadata(self) -> None:
        temporary = self._metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.metadata, sort_keys=True), encoding="utf-8")
        temporary.replace(self._metadata_path)

    def _read_metadata(self) -> dict:
        """读 metadata 时用 utf-8-sig 容忍 BOM。

        Windows 上的编辑器与 PowerShell 写 JSON 常带 BOM，当成解析失败会让一次
        「版本落后、可原地修复」的升级被误判为索引损坏，白白丢弃整个索引。
        """
        return json.loads(self._metadata_path.read_text(encoding="utf-8-sig"))

    def legacy_upgrade_error(self) -> str | None:
        """迁移前校验（不修改索引）：仅已知旧版本 schema 8 / chunk 6 且模型/维度一致才可迁移。"""
        if not self._table_exists():
            return "Index not upgradable: table missing"
        try:
            actual = self._read_metadata()
        except (OSError, ValueError):
            return "Index not upgradable: missing or invalid metadata"
        if str(actual.get("schema_version")) != LEGACY_SCHEMA_VERSION \
                or str(actual.get("chunk_version")) != LEGACY_CHUNK_VERSION:
            return "Index not upgradable: only schema 8 / chunk 6 legacy index is supported"
        if actual.get("embedding_model") != self.metadata["embedding_model"]:
            return "Index not upgradable: embedding model changed"
        if actual.get("vector_dimension") != self.metadata["vector_dimension"]:
            return "Index not upgradable: vector dimension changed"
        table = self.database.open_table(self.table_name)
        schema = getattr(table, "schema", None)
        if schema is None:
            return None
        if schema.field("vector").type.list_size != self.vector_dimension:
            return "Index not upgradable: physical vector dimension mismatch"
        return None

    def upgrade_legacy_schema(self) -> bool:
        """把旧索引（schema 8 / chunk 6）就地升级为可写当前 schema。

        补上当前 schema 中缺失的**可空**列（kind / sequence / evidence_path / specializations
        以及多值方向三列）并把 metadata 更新为当前版本，使 ``is_compatible()`` 通过、
        新数据可写入。旧行缺失列取 NULL/空，不影响检索（读路径不依赖这些字段）。

        只应在**显式迁移**（隔离副本）中调用，绝不在启动时自动执行；真实索引回退由
        ``rebuild_maintenance`` 的归档目录承担。
        """
        error = self.legacy_upgrade_error()
        if error:
            raise ValueError(error)
        table = self.database.open_table(self.table_name)
        schema = getattr(table, "schema", None)
        if schema is None:
            return False
        # 只补可空列：非空列在已有行上无法添加，缺非空列说明物理口径已变，须整体重建。
        missing = [
            field for field in self._schema()
            if field.name not in schema.names and field.nullable
        ]
        if missing:
            table.add_columns(missing)
        self._write_metadata()
        return bool(missing)

    def inplace_upgrade_error(self) -> str | None:
        """就地修复可行性：向量口径未变，且目标 schema 只比物理表多出可空列。

        与 ``legacy_upgrade_error`` 的分工：后者把「仅 schema 8 / chunk 6」当作离线
        迁移工具的安全闸门；这里服务运行时的自动修复，判据只有物理共存能力——
        embedding 模型或向量长度一变，旧行既读不出也写不回，只能换库；只加字段或
        只改分块方式时，旧行与新行物理同构，可以就地共存。
        """
        if not self._table_exists():
            return None
        try:
            actual = self._read_metadata()
        except (OSError, ValueError):
            return "In-place upgrade unavailable: missing or invalid metadata"
        if actual.get("embedding_model") != self.metadata["embedding_model"]:
            return "In-place upgrade unavailable: embedding model changed"
        if actual.get("vector_dimension") != self.metadata["vector_dimension"]:
            return "In-place upgrade unavailable: vector dimension changed"
        schema = getattr(self.database.open_table(self.table_name), "schema", None)
        if schema is None:
            return None
        if schema.field("vector").type.list_size != self.vector_dimension:
            return "In-place upgrade unavailable: physical vector dimension mismatch"
        for field in self._schema():
            if field.name not in schema.names and not field.nullable:
                return f"In-place upgrade unavailable: required column {field.name} is missing"
        return None

    def upgrade_inplace(self) -> bool:
        """补齐缺失的可空列并刷新 metadata，让旧索引就地变为可写。

        不动任何既有行：旧数据文件缺的列读作 NULL，且检索路径不依赖这些新增字段。
        升级后再对全部实体重新投影一次，新旧数据口径即收敛到当前版本。
        """
        error = self.inplace_upgrade_error()
        if error:
            raise ValueError(error)
        added = False
        if self._table_exists():
            table = self.database.open_table(self.table_name)
            schema = getattr(table, "schema", None)
            if schema is not None:
                missing = [f for f in self._schema() if f.name not in schema.names]
                if missing:
                    table.add_columns(missing)
                    added = True
        self._write_metadata()
        return added

    def upsert(self, chunks: list[SearchChunk]) -> None:
        with self._write_lock:
            self._upsert(chunks)

    def _upsert(self, chunks: list[SearchChunk]) -> None:
        if not chunks:
            return
        self._require_compatible()
        records = [self._record(chunk) for chunk in chunks]
        if not self._table_exists():
            table = self.database.create_table(
                self.table_name,
                data=records,
                schema=self._schema(),
            )
            self._create_fts_index(table)
            self._write_metadata()
        else:
            table = self.database.open_table(self.table_name)
            chunk_ids = ", ".join(self._quote(chunk.id) for chunk in chunks)
            table.delete(f"id IN ({chunk_ids})")
            table.add(records)
            self._dirty_marker.touch(exist_ok=True)
            self._pending_modifications += 1
            if self._pending_modifications >= self.optimize_every:
                table.optimize()
                self._dirty_marker.unlink(missing_ok=True)
                self._pending_modifications = 0

    def delete_candidate(self, candidate_id: str) -> None:
        with self._write_lock:
            if self._table_exists():
                self.database.open_table(self.table_name).delete(f"candidate_id = {self._quote(candidate_id)}")

    def replace_candidate(self, chunks: list[SearchChunk]) -> None:
        """Replace all projected revisions of one candidate; empty deletion uses delete_candidate."""
        if not chunks or len({chunk.candidate_id for chunk in chunks}) != 1:
            raise ValueError("replace_candidate requires chunks for exactly one candidate")
        with self._write_lock:
            self._require_compatible()
            # Validate before removing any existing rows. A failed write remains retryable.
            for chunk in chunks:
                self._record(chunk)
            self.delete_candidate(chunks[0].candidate_id)
            self._upsert(chunks)

    def update_candidate_filters(self, candidate_id: str, **fields: Any) -> None:
        allowed = {"total_years", "highest_degree", "location", "candidate_status", "qs_rank",
                   "preferred_location", "preferred_locations"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported candidate projection fields")
        if "preferred_locations" in fields or "preferred_location" in fields:
            values = list(fields.get("preferred_locations") or ())
            if fields.get("preferred_location"):
                values.insert(0, fields["preferred_location"])
            fields["preferred_locations"] = list(dict.fromkeys(values))
            fields["preferred_location"] = values[0] if values else None
        with self._write_lock:
            self._require_compatible()
            if self._table_exists() and fields:
                self.database.open_table(self.table_name).update(
                    where=f"candidate_id = {self._quote(candidate_id)}", values=fields)

    @staticmethod
    def _ensure_column(table: Any, name: str) -> None:
        """给旧表补充缺失列（schema evolution），避免带新字段写入时报错。"""
        schema = getattr(table, "schema", None)
        if schema is not None and name not in schema.names:
            table.add_columns(pa.field(name, pa.string()))

    @staticmethod
    def _create_fts_index(table: Any) -> None:
        config = FTS(
            base_tokenizer="whitespace",
            lower_case=True,
            stem=False,
            remove_stop_words=False,
        )
        table.create_index("keyword_index_text", config=config, replace=True)
        table.create_index("body_index_text", config=config, replace=True)

    def delete_revision(self, revision_id: str) -> None:
        if not self._table_exists():
            return
        self.database.open_table(self.table_name).delete(
            f"revision_id = {self._quote(revision_id)}"
        )

    def get_revision_chunks(self, revision_id: str) -> list[dict[str, Any]]:
        """Return the raw rows (with vectors) for a revision, in arbitrary order."""
        if not self._table_exists():
            return []
        table = self.database.open_table(self.table_name)
        return table.search(None).where(f"revision_id = {self._quote(revision_id)}").limit(None).to_list()

    def get_candidate_chunk_contents(self, candidate_ids: list[str]) -> dict[str, list[str]]:
        """Return all chunk contents keyed by candidate_id, without pandas round-trip."""
        self._check_deadline()
        if not self._table_exists() or not candidate_ids:
            return {}
        table = self.database.open_table(self.table_name)
        ids = ", ".join(self._quote(c) for c in candidate_ids)
        try:
            rows = table.search(None).where(f"candidate_id IN ({ids})").limit(None).to_list()
        except Exception:
            return {}
        result: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            result[row["candidate_id"]].append(row.get("keyword_text") or "")
        return dict(result)

    def warmup(self) -> int:
        """Open the index table and return its row count to preload it."""
        if not self._table_exists():
            return 0
        return self.database.open_table(self.table_name).count_rows()

    def is_ready(self) -> bool:
        """True when the index table exists and is readable（轻量检查，避免 count_rows 阻塞）。

        旧索引（schema 8 / chunk 6）可读但不可写，is_ready 按「可读」判定，使其能
        继续服务关键词/向量/混合检索；写入路径仍用严格 is_compatible 校验。
        """
        return self._table_exists() and self.is_readable()

    def optimize_pending(self) -> bool:
        """Add the last partial write batch to FTS without blocking readiness."""
        if not self._table_exists() or not self._dirty_marker.exists():
            return False
        self.database.open_table(self.table_name).optimize()
        self._dirty_marker.unlink(missing_ok=True)
        self._pending_modifications = 0
        return True

    def search(self, request: SearchRequest) -> list[SearchHit]:
        fts_rows = self.search_fts(request.query, request.filters, request.limit)
        if not request.query_vector:
            return self.fuse(fts_rows, [], request.limit)
        vector_rows = self.search_vector(request.query_vector, request.filters, request.limit)
        return self.fuse(fts_rows, vector_rows, request.limit)

    def search_fts(self, query: str, filters: CandidateFilters, limit: int,
                   search_body: bool = False) -> list[dict[str, Any]]:
        self._require_readable()
        if not self._table_exists():
            return []
        table = self.database.open_table(self.table_name)
        if table.count_rows() == 0:
            return []
        where = self._where(filters)
        # search_body=false 只检索父文档概况字段；子 chunk 的 keyword_index_text 含经历/项目正文，
        # 必须用 chunk_type='parent' 隔离，否则正文开关语义失效。
        if not search_body:
            where = " AND ".join(c for c in (where, "chunk_type = 'parent'") if c)
        fts_columns = ["keyword_index_text", "body_index_text"] if search_body else "keyword_index_text"
        # 查询侧与文档侧使用同一套词法分词：中文按 jieba 切词、英文 casefold、
        # 技术标记原子保留，否则中文查询会被 whitespace tokenizer 当作一个整体
        # token，无法命中已分词的中文文档 token。
        fts_query = " ".join(tokenize_lexical_text(_sanitize_fts_query(query)))
        if not fts_query:
            return []
        builder = table.search(fts_query, query_type="fts", fts_columns=fts_columns)
        if where:
            builder = builder.where(where)
        return self._candidate_rows(builder, table.count_rows(), limit, filters)

    def search_fts_boolean(self, query: str, concepts, operator: str,
                           filters: CandidateFilters, limit: int,
                           search_body: bool = False) -> list[dict[str, Any]]:
        """Alias-aware Boolean recall over ``keyword_index_text``.

        Retrieves with an OR-of-aliases FTS query, then progressively expands the
        retrieval window until enough concept-matching candidates are found, the
        table is exhausted, or the shared deadline expires.

        ``and`` 在**候选人级**求与：把该候选人全部 chunk 命中的 concept 取并集后再判断是否
        覆盖全部待满足 concept，而不是要求单个 chunk 同时命中——JD 级查询会解析出数十至上千个
        concept（含大量通用词），单 chunk 口径结构性无解。待满足集只取词表已策展的 concept
        （技能/通用同义词），忽略通用词；若查询里没有任何策展 concept，「同时满足」无技能约束
        可施加，退化为「满足任一」。
        """
        self._require_readable()
        if not self._table_exists():
            return []
        table = self.database.open_table(self.table_name)
        row_count = table.count_rows()
        if row_count == 0:
            return []
        where = self._where(filters)
        # search_body=false 只检索父文档概况字段；子 chunk 的 keyword_index_text 含经历/项目正文，
        # 必须用 chunk_type='parent' 隔离，否则正文开关语义失效（与 search_fts 对齐）。
        if not search_body:
            where = " AND ".join(c for c in (where, "chunk_type = 'parent'") if c)
        required = concepts
        if operator == "and":
            curated = tuple(concept for concept in concepts if is_curated_concept(concept.canonical))
            if curated:
                required = curated
            else:
                operator = "or"
        fts_query = " ".join(dict.fromkeys(
            alias.casefold() for concept in required for alias in concept.aliases
        ))
        if not fts_query:
            return []
        builder = table.search(fts_query, query_type="fts", fts_columns="keyword_index_text")
        if where:
            builder = builder.where(where)
        token_sets = _concept_token_sets(required)
        if operator == "and":
            return self._and_candidate_rows(builder, row_count, limit, token_sets)
        retrieval_limit = min(max(limit * 3, 100), row_count)
        matched: list[dict[str, Any]] = []
        seen: set[str] = set()
        while retrieval_limit:
            self._check_deadline()
            rows = builder.limit(retrieval_limit).to_list()
            self._check_deadline()
            for row in rows:
                candidate_id = row["candidate_id"]
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                if _matches_concepts(row.get("keyword_index_text") or "", token_sets, operator):
                    matched.append(row)
            if len(matched) >= limit or len(rows) < retrieval_limit or retrieval_limit >= row_count:
                return matched[:limit]
            retrieval_limit = min(retrieval_limit * 2, row_count)
        return matched[:limit]

    def _and_candidate_rows(self, builder: Any, row_count: int, limit: int,
                            token_sets: tuple[set[str], ...]) -> list[dict[str, Any]]:
        """候选人级 AND：窗口倍增直到凑够 limit 个「覆盖全部 concept」的候选人。

        代表行取该候选人排名最靠前的 chunk，返回顺序与首次出现顺序一致。
        """
        retrieval_limit = min(max(limit * 3, 100), row_count)
        representatives: dict[str, dict[str, Any]] = {}
        progress: dict[str, set[int]] = {}
        while retrieval_limit:
            self._check_deadline()
            rows = builder.limit(retrieval_limit).to_list()
            self._check_deadline()
            for row in rows:
                candidate_id = row["candidate_id"]
                representatives.setdefault(candidate_id, row)
                progress.setdefault(candidate_id, set()).update(
                    _matched_concept_indices(row.get("keyword_index_text") or "", token_sets))
            complete = [cid for cid, hits in progress.items() if len(hits) == len(token_sets)]
            if len(complete) >= limit or len(rows) < retrieval_limit or retrieval_limit >= row_count:
                return [representatives[cid] for cid in complete[:limit]]
            retrieval_limit = min(retrieval_limit * 2, row_count)
        return []

    def search_vector(self, query_vector: tuple[float, ...], filters: CandidateFilters, limit: int) -> list[dict[str, Any]]:
        self._require_readable()
        if not self._table_exists() or not query_vector:
            return []
        table = self.database.open_table(self.table_name)
        if table.count_rows() == 0:
            return []
        where = self._where(filters)
        builder = table.search(list(query_vector), vector_column_name="vector", query_type="vector")
        if where:
            builder = builder.where(where, prefilter=True)
        return self._candidate_rows(builder, table.count_rows(), limit, filters)

    def _candidate_rows(self, builder: Any, row_count: int, limit: int,
                        filters: CandidateFilters) -> list[dict[str, Any]]:
        """Expand ranked recall until enough *eligible candidates*, or all rows, are read."""
        retrieval_limit = min(max(limit, 32), row_count)
        while retrieval_limit:
            self._check_deadline()
            rows = builder.limit(retrieval_limit).to_list()
            self._check_deadline()
            unique: dict[str, dict[str, Any]] = {}
            for row in rows:
                unique.setdefault(row["candidate_id"], row)
            if filters.exclude_skills:
                from kerui_recruit.search.query import has_skill
                evidence = self.get_candidate_chunk_contents(list(unique))
                if any(not evidence.get(cid) for cid in unique):
                    raise ValueError("EXCLUSION_UNVERIFIED")
                unique = {cid: row for cid, row in unique.items() if evidence.get(cid) and not any(
                    has_skill(content, skill) for content in evidence[cid] for skill in filters.exclude_skills)}
                for row in unique.values():
                    row["_verified_exclusions"] = filters.exclude_skills
            if len(unique) >= limit or len(rows) < retrieval_limit or retrieval_limit >= row_count:
                return list(unique.values())[:limit]
            retrieval_limit = min(retrieval_limit * 2, row_count)
        return []

    @staticmethod
    def _check_deadline() -> None:
        deadline = SEARCH_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Search deadline reached")

    def fuse(self, fts_rows: list[dict[str, Any]], vector_rows: list[dict[str, Any]], limit: int) -> list[SearchHit]:
        return self._rrf(fts_rows, vector_rows, limit)

    def filter_search(self, filters: CandidateFilters, limit: int) -> list[SearchHit]:
        """Filter-only retrieval（无文本/语义查询），用于纯过滤条件查询。"""
        self._require_readable()
        if not self._table_exists():
            return []
        table = self.database.open_table(self.table_name)
        if table.count_rows() == 0:
            return []
        where = self._where(filters)
        builder = table.search(None)
        if where:
            builder = builder.where(where)
        # Exclusions are verified by the service within the request deadline.
        rows = self._candidate_rows(builder, table.count_rows(), limit, filters)
        return [self._hit_from_row(row, 0.0, ()) for row in rows]

    def hits_from_rows(self, rows: list[dict[str, Any]], channel: str) -> list[SearchHit]:
        """Convert raw LanceDB rows into ``SearchHit`` for single-channel recall.

        FTS rows carry ``_score`` (BM25, higher is better); vector rows carry
        ``_distance`` (lower is better), converted to similarity for a
        consistently higher-better score.
        """
        hits: list[SearchHit] = []
        for row in rows:
            if channel == "vector":
                distance = row.get("_distance")
                score = (1.0 / (1.0 + distance)) if isinstance(distance, (int, float)) else 0.0
            else:
                raw = row.get("_score")
                score = raw if isinstance(raw, (int, float)) else 0.0
            hits.append(self._hit_from_row(row, score, (channel,)))
        return hits

    @staticmethod
    def _hit_from_row(row: dict[str, Any], score: float, channels: tuple[str, ...]) -> SearchHit:
        return SearchHit(
            chunk_id=row["id"],
            candidate_id=row["candidate_id"],
            revision_id=row["revision_id"],
            content=row.get("keyword_text") or "",
            score=score,
            matched_channels=channels,
            total_years=row.get("total_years"),
            highest_degree=row.get("highest_degree"),
            location=row.get("location"),
            qs_rank=row.get("qs_rank"),
            verified_exclusions=tuple(row.get("_verified_exclusions", ())),
            vector_text=row.get("vector_text") or "",
        )

    def _rrf(
        self,
        bm25_rows: list[dict[str, Any]],
        vector_rows: list[dict[str, Any]],
        limit: int,
    ) -> list[SearchHit]:
        # 候选人级 RRF：每个通道先按候选人取最佳 rank，再融合；通道证据取并集，
        # 避免一个候选人的多个子 chunk 挤占多个名次，也避免父子 chunk 的通道被拆散。
        scores: dict[str, float] = defaultdict(float)
        channels: dict[str, set[str]] = defaultdict(set)
        rows: dict[str, dict[str, Any]] = {}
        for channel, ranked_rows in (("bm25", bm25_rows), ("vector", vector_rows)):
            best_rank: dict[str, int] = {}
            for rank, row in enumerate(ranked_rows, start=1):
                candidate_id = row["candidate_id"]
                if candidate_id not in best_rank:
                    best_rank[candidate_id] = rank
                    rows.setdefault(candidate_id, row)
                channels[candidate_id].add(channel)
            for candidate_id, rank in best_rank.items():
                scores[candidate_id] += 1.0 / (self.rrf_k + rank)
        ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))
        hits: list[SearchHit] = []
        for candidate_id in ordered_ids:
            row = rows[candidate_id]
            hits.append(
                SearchHit(
                    chunk_id=row["id"],
                    candidate_id=candidate_id,
                    revision_id=row["revision_id"],
                    content=row.get("keyword_text") or "",
                    score=scores[candidate_id],
                    matched_channels=tuple(sorted(channels[candidate_id])),
                    total_years=row.get("total_years"),
                    highest_degree=row.get("highest_degree"),
                    location=row.get("location"),
                    qs_rank=row.get("qs_rank"),
                    verified_exclusions=tuple(row.get("_verified_exclusions", ())),
                    vector_text=row.get("vector_text") or "",
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def _record(self, chunk: SearchChunk) -> dict[str, Any]:
        if len(chunk.vector) != self.vector_dimension:
            raise ValueError(
                f"Expected vector dimension {self.vector_dimension}, got {len(chunk.vector)}"
            )
        keyword_text = chunk.effective_keyword_text
        keyword_index_text = chunk.effective_keyword_index_text
        vector_text = chunk.effective_vector_text
        location_terms = list(dict.fromkeys((
            *chunk.location_terms,
            *((chunk.location,) if chunk.location else ()),
        )))
        school_tags = list(dict.fromkeys((
            *chunk.school_tags,
            *((chunk.school_level,) if chunk.school_level else ()),
        )))
        return {
            "id": chunk.id,
            "candidate_id": chunk.candidate_id,
            "revision_id": chunk.revision_id,
            "keyword_text": keyword_text,
            "keyword_index_text": keyword_index_text,
            "vector_text": vector_text,
            "body_index_text": chunk.body_index_text or "",
            "chunk_type": chunk.chunk_type,
            "parent_id": chunk.parent_id,
            "kind": chunk.kind,
            "sequence": chunk.sequence,
            "evidence_path": list(chunk.evidence_path),
            "vector": list(chunk.vector),
            "name_terms": list(chunk.name_terms),
            "school_terms": list(chunk.school_terms),
            "company_terms": list(chunk.company_terms),
            "title_terms": list(chunk.title_terms),
            "location_terms": location_terms,
            "name_text": _join_terms(chunk.name_terms),
            "school_text": _join_terms(chunk.school_terms),
            "company_text": _join_terms(chunk.company_terms),
            "title_text": _join_terms(chunk.title_terms),
            "skills": list(chunk.skills),
            "age": chunk.age,
            "total_years": chunk.total_years,
            "highest_degree": chunk.highest_degree,
            "location": chunk.location,
            "school_tags": school_tags,
            "qs_rank": chunk.qs_rank,
            "candidate_status": chunk.candidate_status,
            "preferred_location": chunk.preferred_location,
            "preferred_locations": list(dict.fromkeys((
                *((chunk.preferred_location,) if chunk.preferred_location else ()), *chunk.preferred_locations))),
            "direction": chunk.direction,
            "school_region": chunk.school_region,
            "specializations": list(chunk.specializations),
            "career_directions": list(chunk.career_directions),
            "career_specializations": list(chunk.career_specializations),
            "business_directions": list(chunk.business_directions),
        }

    def _table_exists(self) -> bool:
        return self.table_name in self.database.list_tables().tables

    def _schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string(), nullable=False),
                pa.field("candidate_id", pa.string(), nullable=False),
                pa.field("revision_id", pa.string(), nullable=False),
                pa.field("keyword_text", pa.string(), nullable=False),
                pa.field("keyword_index_text", pa.string(), nullable=False),
                pa.field("vector_text", pa.string(), nullable=False),
                pa.field("body_index_text", pa.string(), nullable=False),
                pa.field("chunk_type", pa.string()),
                pa.field("parent_id", pa.string()),
                pa.field("kind", pa.string()),
                pa.field("sequence", pa.int64()),
                pa.field("evidence_path", pa.list_(pa.string())),
                pa.field(
                    "vector",
                    pa.list_(pa.float32(), self.vector_dimension),
                    nullable=False,
                ),
                pa.field("name_terms", pa.list_(pa.string())),
                pa.field("school_terms", pa.list_(pa.string())),
                pa.field("company_terms", pa.list_(pa.string())),
                pa.field("title_terms", pa.list_(pa.string())),
                pa.field("location_terms", pa.list_(pa.string())),
                pa.field("name_text", pa.string()),
                pa.field("school_text", pa.string()),
                pa.field("company_text", pa.string()),
                pa.field("title_text", pa.string()),
                pa.field("skills", pa.list_(pa.string())),
                pa.field("age", pa.int64()),
                pa.field("total_years", pa.float64()),
                pa.field("highest_degree", pa.string()),
                pa.field("location", pa.string()),
                pa.field("school_tags", pa.list_(pa.string())),
                pa.field("qs_rank", pa.int64()),
                pa.field("candidate_status", pa.string(), nullable=False),
                pa.field("preferred_location", pa.string()),
                pa.field("preferred_locations", pa.list_(pa.string())),
                pa.field("direction", pa.string()),
                pa.field("school_region", pa.string()),
                pa.field("specializations", pa.list_(pa.string())),
                pa.field("career_directions", pa.list_(pa.string())),
                pa.field("career_specializations", pa.list_(pa.string())),
                pa.field("business_directions", pa.list_(pa.string())),
            ]
        )

    def _where(self, filters: CandidateFilters) -> str:
        clauses: list[str] = []
        # 手机号/性别等无法在索引内表达的字段，先由数据库层解析出候选人集合，
        # 再作为最基础的过滤条件缩小范围，避免受向量召回上限影响。
        if filters.candidate_ids:
            quoted = ", ".join(self._quote(c) for c in filters.candidate_ids)
            clauses.append(f"candidate_id IN ({quoted})")
        if filters.min_years is not None:
            clauses.append(f"total_years >= {float(filters.min_years)}")
        if filters.max_years is not None:
            clauses.append(f"total_years <= {float(filters.max_years)}")
        if filters.min_age is not None:
            clauses.append(f"age >= {int(filters.min_age)}")
        if filters.max_age is not None:
            clauses.append(f"age <= {int(filters.max_age)}")
        degree_values = filters.degree_values()
        if degree_values:
            quoted = ", ".join(self._quote(d) for d in degree_values)
            clauses.append(f"highest_degree IN ({quoted})")
        location_values = filters.location_values()
        if location_values:
            quoted = ", ".join(self._quote(loc) for loc in location_values)
            clauses.append(f"array_has_any(location_terms, [{quoted}])")
        preferred_values = filters.preferred_location_values()
        if preferred_values:
            quoted = ", ".join(self._quote(loc) for loc in preferred_values)
            clauses.append(f"array_has_any(preferred_locations, [{quoted}])")
        if filters.candidate_status:
            clauses.append(
                f"candidate_status = {self._quote(filters.candidate_status)}"
            )
        if filters.max_qs_rank is not None:
            clauses.append(f"qs_rank <= {int(filters.max_qs_rank)}")
        school_values = filters.school_level_values()
        if school_values:
            quoted = ", ".join(self._quote(s) for s in school_values)
            clauses.append(f"array_has_any(school_tags, [{quoted}])")
        # 字段级确定性筛选：直接命中对应字段词项文本，不依赖召回数量上限。
        if filters.name:
            clauses.append(self._like_contains("name_text", filters.name))
        if filters.company:
            clauses.append(self._like_contains("company_text", filters.company))
        if filters.title:
            clauses.append(self._like_contains("title_text", filters.title))
        if filters.school:
            clauses.append(self._like_contains("school_text", filters.school))
        if filters.direction:
            clauses.append(f"direction = {self._quote(filters.direction)}")
        if filters.school_region:
            clauses.append(f"school_region = {self._quote(filters.school_region)}")
        # 细分专长是 schema 9 新增列：旧索引（schema 8）无此列，专长精确过滤不可用，
        # 跳过该子句（放宽召回）而非报错，避免旧索引在只读回退模式下崩溃。
        for column, values in (
            ("specializations", filters.specializations),
            ("career_directions", filters.career_directions),
            ("career_specializations", filters.career_specializations),
            ("business_directions", filters.business_directions),
        ):
            clause = self._array_any_clause(column, values)
            if clause:
                clauses.append(clause)
        return " AND ".join(clauses)

    def _array_any_clause(self, column: str, values: tuple[str, ...]) -> str | None:
        """多值列的「任一命中」子句；列缺失（旧索引只读回退）时返回 None 以放宽召回。"""
        if not values or not self._has_column(column):
            return None
        quoted = ", ".join(self._quote(value) for value in values)
        return f"array_has_any({column}, [{quoted}])"

    @staticmethod
    def _like_contains(column: str, keyword: str) -> str:
        folded = keyword.casefold().replace("'", "''")
        return f"{column} LIKE '%{folded}%'"

    @staticmethod
    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
