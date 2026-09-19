"""共享词法模型：技能/学历/学校别名的统一来源，供文档侧与查询侧共用。

- 中文用固定版本 jieba 分词，加载项目内自定义技术/领域词典。
- 英文 casefold，但保留 C++ / C# / .NET / Node.js 等技术标记边界。
- 技能静态映射在此定义，文档展开与查询概念解析共用同一份数据。
- 学校标准名/别名由 ``SchoolReference.alias_groups()`` 动态注入（``extra_alias_groups``）。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable

import jieba

# PyInstaller 打包（frozen）后，jieba 依赖 pkg_resources.resource_stream 定位 dict.txt，
# 这条路径在打包环境下会失效，导致中文词典没加载、中文分词失败（中文检索搜不到）。
# 这里显式用 sys._MEIPASS 定位打包内的词典并重新加载，恢复中文分词能力。
if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
    _JIEBA_DICT = os.path.join(sys._MEIPASS, "jieba", "dict.txt")
    if os.path.isfile(_JIEBA_DICT):
        jieba.dt.set_dictionary(_JIEBA_DICT)
        jieba.dt.initialize()

from kerui_recruit.search.degrees import DEGREE_ALIASES, normalize_degree

LEXICON_VERSION = "2"


@dataclass(frozen=True, slots=True)
class LexicalConcept:
    canonical: str
    aliases: tuple[str, ...]


# 技能概念：canonical -> (canonical, 别名...)。展示形式，匹配时 casefold。
_SKILL_CONCEPTS: dict[str, tuple[str, ...]] = {
    "JavaScript": ("JavaScript", "JS"),
    "TypeScript": ("TypeScript", "TS"),
    "Go": ("Go", "Golang"),
    "Rust": ("Rust",),
    "C++": ("C++", "CPP", "Cplusplus"),
    "C#": ("C#", "CSharp"),
    "React": ("React", "ReactJS"),
    "Vue": ("Vue", "VueJS"),
    "SQL": ("SQL",),
    "Node.js": ("Node.js", "Node", "NodeJS"),
    "PostgreSQL": ("PostgreSQL", "Postgres"),
    "Kubernetes": ("Kubernetes", "K8s"),
    "Python": ("Python",),
    "Java": ("Java",),
    "Spring Boot": ("Spring Boot", "SpringBoot"),
    "Spring Cloud": ("Spring Cloud", "SpringCloud"),
    "Spring AI": ("Spring AI", "SpringAI"),
    "Avaloq": ("Avaloq",),
    "Redis": ("Redis",),
    "MySQL": ("MySQL",),
    "Kafka": ("Kafka",),
    "Docker": ("Docker",),
    "微服务": ("微服务", "Microservices", "微服务架构"),
    "云原生": ("云原生", "Cloud Native", "云原生架构"),
    "大模型": ("大模型", "LLM", "大语言模型"),
    "RAG": ("RAG", "检索增强生成"),
    "机器学习": ("机器学习", "Machine Learning", "ML"),
    "深度学习": ("深度学习", "Deep Learning", "DL"),
}

# 通用领域同义词（同一概念组）。
_GENERAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "后端": ("后端", "服务端"),
}

# casefold -> canonical（仅技能）。
_SKILL_NORMALIZE: dict[str, str] = {}
for _canonical, _aliases in _SKILL_CONCEPTS.items():
    for _alias in _aliases:
        _SKILL_NORMALIZE[_alias.casefold()] = _canonical

# casefold -> canonical（技能 + 通用同义词），供概念解析。
_CONCEPT_NORMALIZE = dict(_SKILL_NORMALIZE)
for _canonical, _aliases in _GENERAL_SYNONYMS.items():
    for _alias in _aliases:
        _CONCEPT_NORMALIZE[_alias.casefold()] = _canonical

# canonical -> 展示别名（技能 + 通用同义词）。
_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {**_SKILL_CONCEPTS, **_GENERAL_SYNONYMS}

# canonical -> casefold 别名（用于 FTS 文档展开）。
_SKILL_FOLDED: dict[str, tuple[str, ...]] = {
    canonical: tuple(alias.casefold() for alias in aliases)
    for canonical, aliases in _SKILL_CONCEPTS.items()
}

# 学历：canonical -> casefold 别名（与过滤词表共用同一来源）。
_DEGREE_FOLDED: dict[str, tuple[str, ...]] = {}
for _alias, _canonical in DEGREE_ALIASES.items():
    _DEGREE_FOLDED.setdefault(_canonical, [])
    _key = _alias.casefold()
    if _key not in _DEGREE_FOLDED[_canonical]:
        _DEGREE_FOLDED[_canonical].append(_key)
for _canonical in list(_DEGREE_FOLDED):
    _folded = _canonical.casefold()
    if _folded not in _DEGREE_FOLDED[_canonical]:
        _DEGREE_FOLDED[_canonical].insert(0, _folded)
_DEGREE_FOLDED = {c: tuple(a) for c, a in _DEGREE_FOLDED.items()}

# 带标点/多词的技术标记：必须原子保留，不能被子串或标点切分破坏。
_TECH_TOKEN_RE = re.compile(
    r"C\+\+|C#|\.NET|Node\.js|Spring\s+Boot|Spring\s+Cloud\s+Alibaba|Spring\s+Cloud|Spring\s+AI",
    re.IGNORECASE,
)

# jieba 自定义词典（中文领域术语 + 英文技术词），保证不被切碎。
for _word in (
    "JavaScript", "TypeScript", "Kubernetes", "Spring Boot", "Node.js",
    "Spring Cloud", "Spring Cloud Alibaba", "Spring AI", "Nacos", "Seata",
    "Sentinel", "Dubbo", "MyBatis", "Avaloq", "RAG",
    "云原生", "机器学习", "深度学习", "大模型", "微服务", "后端", "服务端",
):
    jieba.add_word(_word)

# 预热 jieba：在导入期完成前缀词典构建，避免首次检索线程内触发约 0.4s 的加载延迟
# （搜索有独立超时预算，线程内首次分词导致的冷启动会使 FTS 误判超时）。
tuple(jieba.cut("预热"))


def normalize_skill(value: str) -> str:
    """把技能 token 规范化为唯一 canonical 名。"""
    return _SKILL_NORMALIZE.get(value.casefold(), value)


def is_curated_concept(canonical: str) -> bool:
    """该 canonical 是否为词表已策展的概念（技能/通用同义词），而非查询里的通用词。

    用于布尔 AND 只约束「技能类」概念：JD 级查询会解析出大量通用词（负责/熟悉/经验），
    要求全部命中无解。
    """
    return canonical in _CONCEPT_ALIASES


def tokenize_lexical_text(text: str) -> tuple[str, ...]:
    """词法分词：保留技术标记边界 + jieba 中文分词 + 英文 casefold。"""
    if not text:
        return ()
    protected: list[str] = []

    def _collect(match) -> str:
        protected.append(match.group(0).casefold())
        return " "

    remainder = _TECH_TOKEN_RE.sub(_collect, text)
    tokens = list(protected)
    for segment in jieba.cut(remainder):
        token = segment.strip().casefold()
        if token:
            tokens.append(token)
    return tuple(tokens)


def _dedupe(tokens: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return tuple(result)


def expand_document_tokens(values: Iterable[str]) -> tuple[str, ...]:
    """文档侧展开：每个技能规范为 canonical 并展开全部别名（casefold、去重）。"""
    tokens: list[str] = []
    for value in values:
        if not value:
            continue
        for token in tokenize_lexical_text(value):
            canonical = _SKILL_NORMALIZE.get(token, token)
            tokens.extend(_SKILL_FOLDED.get(canonical, (token,)))
    return _dedupe(tokens)


def expand_degree_tokens(value: str) -> tuple[str, ...]:
    """学历文档侧展开：规范为 canonical 后展开全部别名（casefold、去重）。"""
    if not value:
        return ()
    canonical = normalize_degree(value)
    if canonical is None:
        return tokenize_lexical_text(value)
    return _DEGREE_FOLDED.get(canonical, (canonical.casefold(),))


def expand_school_tokens(
    school: str,
    alias_groups: dict[str, tuple[str, ...]] | None,
) -> tuple[str, ...]:
    """学校文档侧展开：标准名命中或别名命中时展开全部别名（casefold、去重）。"""
    tokens = list(tokenize_lexical_text(school))
    if not alias_groups:
        return _dedupe(tokens)
    folded = school.casefold()
    if school in alias_groups:
        tokens.extend(alias.casefold() for alias in alias_groups[school])
    else:
        for canonical, aliases in alias_groups.items():
            if folded == canonical.casefold() or any(alias.casefold() == folded for alias in aliases):
                tokens.append(canonical.casefold())
                tokens.extend(alias.casefold() for alias in aliases)
                break
    return _dedupe(tokens)


def concepts_from_query(
    text: str,
    extra_alias_groups: dict[str, tuple[str, ...]] | None = None,
) -> tuple[LexicalConcept, ...]:
    """把查询文本解析为概念组；动态学校别名按最长归一化 span 优先匹配。"""
    groups = extra_alias_groups or {}
    alias_map: dict[str, str] = {}
    for canonical, aliases in groups.items():
        for alias in (canonical, *aliases):
            key = alias.casefold()
            if key not in alias_map:
                alias_map[key] = canonical

    folded = text.casefold()

    # 动态别名匹配（最长优先），得到非重叠区间。
    matches: list[tuple[int, int, str]] = []
    for alias in sorted(alias_map, key=len, reverse=True):
        start = 0
        while True:
            idx = folded.find(alias, start)
            if idx == -1:
                break
            matches.append((idx, idx + len(alias), alias_map[alias]))
            start = idx + len(alias)
    matches.sort(key=lambda item: item[0])
    non_overlap: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, canonical in matches:
        if start >= last_end:
            non_overlap.append((start, end, canonical))
            last_end = end

    concepts: list[LexicalConcept] = []
    cursor = 0
    for start, end, canonical in non_overlap:
        _append_token_concepts(concepts, folded[cursor:start])
        concepts.append(
            LexicalConcept(canonical=canonical, aliases=(canonical, *groups.get(canonical, ())))
        )
        cursor = end
    _append_token_concepts(concepts, folded[cursor:])
    return tuple(concepts)


def _append_token_concepts(concepts: list[LexicalConcept], text: str) -> None:
    for token in tokenize_lexical_text(text):
        canonical = _CONCEPT_NORMALIZE.get(token, token)
        aliases = _CONCEPT_ALIASES.get(canonical, (canonical,))
        concepts.append(LexicalConcept(canonical=canonical, aliases=aliases))
