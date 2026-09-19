"""共享词法模型单元测试（Task 2）。

先写失败测试，验证 LexicalConcept / 分词 / 别名展开 / 动态学校别名共享语义。
"""
from kerui_recruit.search.lexicon import (
    LexicalConcept,
    concepts_from_query,
    expand_document_tokens,
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
