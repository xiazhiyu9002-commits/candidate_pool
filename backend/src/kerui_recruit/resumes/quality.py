from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

# 有效字符：中日韩汉字 + 拉丁字母 + 数字。中英文简历同等对待，
# 不把「中文占比低」当作质量差。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9]")

# 一个页面至少需要这么多有效字符，才认为直接提取的文本足以代表正文。
MIN_MEANINGFUL_CHARS = 30
# 单一重复片段占全文比例达到该值时，判定为水印主导。
DOMINANT_FRAGMENT_RATIO = 0.5
# 页面图片覆盖率达到该值时，认为页面「以图片为主」。
IMAGE_HEAVY_COVERAGE = 0.3


@dataclass(frozen=True, slots=True)
class TextQuality:
    char_count: int
    valid_char_count: int
    cjk_count: int
    latin_count: int
    repeated_ratio: float
    dominant_ratio: float

    @property
    def meaningful(self) -> bool:
        """文本是否足以代表正文（不含以单一重复水印片段为主的文本）。

        只用 dominant_ratio 判定水印：真实简历（尤其工作经历）常有大段措辞重复，
        若用「任意重复子串占比」会误伤正常正文，故不作为水印判据。
        """
        if self.valid_char_count < MIN_MEANINGFUL_CHARS:
            return False
        if self.dominant_ratio >= DOMINANT_FRAGMENT_RATIO:
            return False
        return True


def analyze_text(text: str) -> TextQuality:
    """分析一段文本的可信度信号，供页面路由、OCR 结果与结构化有效性复用。"""
    stripped = "".join(ch for ch in text if not ch.isspace())
    if not stripped:
        return TextQuality(
            char_count=0,
            valid_char_count=0,
            cjk_count=0,
            latin_count=0,
            repeated_ratio=0.0,
            dominant_ratio=0.0,
        )
    cjk = len(_CJK_RE.findall(stripped))
    latin = len(_LATIN_RE.findall(stripped))
    digit = len(_DIGIT_RE.findall(stripped))
    return TextQuality(
        char_count=len(stripped),
        valid_char_count=cjk + latin + digit,
        cjk_count=cjk,
        latin_count=latin,
        repeated_ratio=_repeated_ratio(stripped),
        dominant_ratio=_dominant_fragment_ratio(_nonempty_lines(text)),
    )


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _dominant_fragment_ratio(lines: list[str]) -> float:
    """最常出现的整行占全文字符的比例（仅在该行出现两次及以上时计）。"""
    if not lines:
        return 0.0
    counts = Counter(lines)
    top_line, top_count = counts.most_common(1)[0]
    if top_count < 2:
        return 0.0
    total = sum(len(line) for line in lines)
    return (len(top_line) * top_count) / total if total else 0.0


def _repeated_ratio(compact: str, *, min_len: int = 6, max_len: int = 16) -> float:
    """返回属于「重复长片段」的字符占比。

    通过滑动窗口统计出现两次及以上的子串，能同时捕获：
    - 逐行重复的水印（同一行长文本多次出现）；
    - 拼接在同一行内的重复水印。
    """
    n = len(compact)
    if n < min_len * 2:
        return 0.0
    covered = bytearray(n)
    upper = min(max_len, n)
    for size in range(upper, min_len - 1, -1):
        seen: dict[str, list[int]] = {}
        for index in range(n - size + 1):
            segment = compact[index : index + size]
            positions = seen.get(segment)
            if positions is None:
                seen[segment] = [index]
            else:
                positions.append(index)
        for positions in seen.values():
            if len(positions) < 2:
                continue
            for start in positions:
                for offset in range(start, start + size):
                    covered[offset] = 1
    return sum(covered) / n if n else 0.0
