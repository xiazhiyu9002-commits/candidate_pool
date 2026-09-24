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

LEXICON_VERSION = "3"


@dataclass(frozen=True, slots=True)
class LexicalConcept:
    canonical: str
    aliases: tuple[str, ...]


# 概念表分三组，同一份数据服务三处：文档侧词展开（expand_document_tokens）、
# 查询侧概念解析（concepts_from_query）、以及「已策展概念」标记（is_curated_concept）。
# 策展口径（2026-09-21 定）：
# - 技能 + 常用缩写：策展。K8s→Kubernetes、数仓→数据仓库 这类写法差异必须互相命中。
# - 岗位族：策展。猎头搜「前端」时它是硬条件，值得参与混合模式的弱命中门槛。
# - 行业/领域泛词：**仅展开、不策展**。「金融/电商」在简历里无处不在，当门槛既不精准
#   又会误杀转行的人（银行 ≠ 证券），所以只用于别名对齐，不进门槛与「同时满足」待满足集。

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
    # 数据栈常用缩写：简历写「数仓」与查询写「数据仓库」必须互相命中。
    "数据仓库": ("数据仓库", "数仓"),
    "HTML5": ("HTML5", "H5"),
    "Elasticsearch": ("Elasticsearch", "ES"),
    "消息队列": ("消息队列", "MQ"),
    "持续集成": ("持续集成", "CI/CD", "CICD"),
    "自然语言处理": ("自然语言处理", "NLP"),
    "计算机视觉": ("计算机视觉", "Computer Vision"),
    "推荐系统": ("推荐系统", "推荐算法"),
    "Spark": ("Spark",),
    "Flink": ("Flink",),
    "Hadoop": ("Hadoop",),
    "Hive": ("Hive",),
    "Linux": ("Linux",),
    "Android": ("Android",),
    "iOS": ("iOS",),
    "小程序": ("小程序",),
    "微前端": ("微前端",),
}

# 岗位族概念（策展）：猎头搜「前端/算法/运维」时，它就是硬条件而非模糊关键词。
# canonical 取最常见的写法，避免改动历史证据文本里的展示名。
_ROLE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "前端": ("前端", "前端开发", "Web前端", "web前端", "大前端", "前端工程师"),
    "后端": ("后端", "服务端", "后端开发", "服务端开发", "后端工程师"),
    "算法工程师": ("算法工程师", "算法", "算法开发"),
    "测试工程师": ("测试工程师", "测试开发", "测开", "QA", "软件测试"),
    "运维工程师": ("运维工程师", "运维", "SRE"),
    "数据开发": ("数据开发", "数仓开发", "数据仓库开发", "ETL", "大数据开发"),
    "数据分析": ("数据分析", "数据分析师"),
    "数据挖掘": ("数据挖掘",),
    "全栈": ("全栈", "全栈工程师", "FullStack"),
    "架构师": ("架构师", "系统架构师", "技术架构师"),
    "产品经理": ("产品经理",),
}

# 行业/领域概念（仅展开）：只做两边别名对齐，不参与策展门槛。
_INDUSTRY_CONCEPTS: dict[str, tuple[str, ...]] = {
    "电子商务": ("电子商务", "电商"),
    "物联网": ("物联网", "IoT"),
    "云计算": ("云计算", "Cloud Computing"),
    "大数据": ("大数据", "Big Data"),
    "区块链": ("区块链", "Blockchain"),
    "人工智能": ("人工智能", "AI"),
    "金融科技": ("金融科技", "FinTech"),
    "医疗健康": ("医疗健康", "Healthcare"),
    "在线教育": ("在线教育",),
    "智能制造": ("智能制造",),
}

# 已策展概念（技能 + 岗位族）：is_curated_concept 的唯一判据。
_CURATED_CONCEPTS: dict[str, tuple[str, ...]] = {**_SKILL_CONCEPTS, **_ROLE_CONCEPTS}
_CURATED_CANONICALS = frozenset(_CURATED_CONCEPTS)

# 全部概念（含行业泛词）：文档侧展开、查询侧概念解析共用。
_ALL_CONCEPTS: dict[str, tuple[str, ...]] = {**_CURATED_CONCEPTS, **_INDUSTRY_CONCEPTS}
_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = _ALL_CONCEPTS

# casefold -> canonical（技能）：沿用原语义，只服务 normalize_skill（JD 匹配侧也依赖它）。
_SKILL_NORMALIZE: dict[str, str] = {}
for _canonical, _aliases in _SKILL_CONCEPTS.items():
    for _alias in _aliases:
        _SKILL_NORMALIZE[_alias.casefold()] = _canonical

# casefold -> canonical（技能 + 岗位族 + 行业）：供概念解析与文档侧词展开。
_CONCEPT_NORMALIZE: dict[str, str] = {}
for _canonical, _aliases in _ALL_CONCEPTS.items():
    for _alias in _aliases:
        _CONCEPT_NORMALIZE.setdefault(_alias.casefold(), _canonical)

# canonical -> casefold 别名（文档侧展开用，覆盖全部三组）。
_CONCEPT_FOLDED: dict[str, tuple[str, ...]] = {
    canonical: tuple(alias.casefold() for alias in aliases)
    for canonical, aliases in _ALL_CONCEPTS.items()
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
    r"C\+\+|C#|\.NET|Node\.js|Spring\s+Boot|Spring\s+Cloud\s+Alibaba|Spring\s+Cloud|Spring\s+AI|CI/CD|CICD",
    re.IGNORECASE,
)

# jieba 自定义词典（中文领域术语 + 英文技术词 + 岗位族），保证不被切碎。
for _word in (
    "JavaScript", "TypeScript", "Kubernetes", "Spring Boot", "Node.js",
    "Spring Cloud", "Spring Cloud Alibaba", "Spring AI", "Nacos", "Seata",
    "Sentinel", "Dubbo", "MyBatis", "Avaloq", "RAG",
    "云原生", "机器学习", "深度学习", "大模型", "微服务", "后端", "服务端",
    "数据仓库", "数仓", "消息队列", "持续集成", "自然语言处理", "计算机视觉",
    "推荐系统", "电子商务", "物联网", "云计算", "大数据", "区块链", "人工智能",
    "金融科技", "医疗健康", "在线教育", "智能制造", "微前端",
    "前端", "前端开发", "大前端", "后端开发", "服务端开发", "算法工程师", "算法开发",
    "测试工程师", "测试开发", "测开", "软件测试", "运维工程师", "数据开发", "数仓开发",
    "数据分析", "数据分析师", "数据挖掘", "全栈", "全栈工程师", "架构师", "系统架构师",
    "产品经理",
):
    jieba.add_word(_word)

# 预热 jieba：在导入期完成前缀词典构建，避免首次检索线程内触发约 0.4s 的加载延迟
# （搜索有独立超时预算，线程内首次分词导致的冷启动会使 FTS 误判超时）。
tuple(jieba.cut("预热"))


def normalize_skill(value: str) -> str:
    """把技能 token 规范化为唯一 canonical 名。"""
    return _SKILL_NORMALIZE.get(value.casefold(), value)


def is_curated_concept(canonical: str) -> bool:
    """该 canonical 是否为词表已策展的概念（技能/岗位族），而非查询里的通用词。

    用于布尔 AND 只约束「技能类」概念：JD 级查询会解析出大量通用词（负责/熟悉/经验），
    要求全部命中无解。行业/领域泛词只做别名展开，不算策展概念。
    """
    return canonical in _CURATED_CANONICALS


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
    """文档侧展开：命中的概念统一规范为 canonical 并展开其全部别名（casefold、去重）。

    覆盖技能、岗位族与行业泛词三组 —— 文档侧与查询侧用同一份表，写法差异才能互相命中
    （例如简历写「数仓」、查询写「数据仓库」）。
    """
    tokens: list[str] = []
    for value in values:
        if not value:
            continue
        for token in tokenize_lexical_text(value):
            canonical = _CONCEPT_NORMALIZE.get(token, token)
            tokens.extend(_CONCEPT_FOLDED.get(canonical, (token,)))
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
