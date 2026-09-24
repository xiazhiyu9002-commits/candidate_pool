"""共享词法模型单元测试（Task 2）。

先写失败测试，验证 LexicalConcept / 分词 / 别名展开 / 动态学校别名共享语义。
"""
from kerui_recruit.search.lexicon import (
    LexicalConcept,
    concepts_from_query,
    expand_document_tokens,
    is_curated_concept,
    tokenize_lexical_text,
)


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


def test_role_families_are_curated_concepts():
    """岗位族进策展集：搜「前端」时它就是硬条件，参与弱命中门槛与「同时满足」待满足集。"""
    assert is_curated_concept("前端")
    assert is_curated_concept("算法工程师")
    assert is_curated_concept("运维工程师")
    assert is_curated_concept("后端")


def test_industry_terms_expand_but_are_not_curated():
    """行业泛词只做别名展开，不进门槛：银行 ≠ 证券，当硬条件会误杀。"""
    assert not is_curated_concept("电子商务")
    assert not is_curated_concept("人工智能")
    assert concepts_from_query("电商")[0].canonical == "电子商务"


def test_document_expansion_covers_role_and_industry_aliases():
    tokens = expand_document_tokens(["数仓", "前端", "电商"])
    assert {"数据仓库", "数仓"} <= set(tokens)
    assert {"前端", "前端开发"} <= set(tokens)
    assert {"电子商务", "电商"} <= set(tokens)


def test_role_alias_is_symmetric_between_document_and_query():
    """「大前端」与「前端开发」写不同，两侧必须都能命中同一个概念。"""
    assert concepts_from_query("大前端")[0].canonical == "前端"
    assert "前端开发" in expand_document_tokens(["大前端"])
