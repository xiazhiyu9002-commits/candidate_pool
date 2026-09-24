"""画像 / JD 要求解析的边界用例台（猎头视角 + 边界视角）。

覆盖四类客观条件（学历 / 学校档次 / 年限 / 点名公司）与两类**必须不抽**的反例。
每条用例给出期望值，脚本逐条报告「符合 / 不符」，用于改一处跑一遍、避免按下葫芦浮起瓢。

用法：
    python scripts/audit_requirements_parsing_2026_09_20.py          # 只跑规则层（快、确定性）
    python scripts/audit_requirements_parsing_2026_09_20.py --live   # 再打真实接口（模型 ∪ 规则）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from kerui_recruit.jd.profile_constraints import (  # noqa: E402
    parse_exact_constraints,
    parse_years_requirement,
)

LIVE_URL = "http://127.0.0.1:43127/api/jd/parse-constraints"
LIVE_TOKEN = "0" * 64

# (说明, 文本, 期望 kind→alternatives 集合, 期望年限 (stated, years))
CASES: list[tuple[str, str, dict[str, set[str]], tuple[bool, float | None]]] = [
    # ---- 学历 ----
    ("本科及以上", "本科及以上学历", {"degree": {"本科"}}, (False, None)),
    ("硕士及以上", "硕士及以上学历", {"degree": {"硕士"}}, (False, None)),
    ("博士", "博士学历", {"degree": {"博士"}}, (False, None)),
    ("大专以上", "大专以上学历", {"degree": {"大专"}}, (False, None)),
    ("统招本科", "统招本科，计算机相关专业", {"degree": {"本科"}}, (False, None)),
    ("全日制本科", "全日制本科学历", {"degree": {"本科"}}, (False, None)),
    ("研究生学历", "研究生学历", {"degree": {"硕士"}}, (False, None)),
    ("本科或硕士", "本科或硕士学历", {"degree": {"本科", "硕士"}}, (False, None)),
    ("本硕均需211", "本科与硕士均为 211", {"school_level": {"211"}, "degree": {"本科"}}, (False, None)),
    # ---- 学校档次 ----
    ("985", "卡 985", {"school_level": {"985"}}, (False, None)),
    ("985或211", "必须 985 或 211", {"school_level": {"985", "211"}}, (False, None)),
    ("双一流", "双一流高校", {"school_level": {"双一流"}}, (False, None)),
    ("985本科及以上", "985 本科及以上", {"school_level": {"985"}, "degree": {"本科"}}, (False, None)),
    # ---- 年限 ----
    ("5年以上", "5 年以上 Java 经验", {}, (True, 5.0)),
    ("至少3年", "至少 3 年工作经验", {}, (True, 3.0)),
    ("3-5年", "3-5 年经验", {}, (True, 3.0)),
    ("8年左右", "8 年左右", {}, (True, 8.0)),
    ("五年以上", "五年以上后端经验", {}, (True, 5.0)),
    ("十五年以上", "十五年以上经验", {}, (True, 15.0)),
    ("二十五年", "工作年限：25 年", {}, (True, 25.0)),
    ("工作年限:5年", "工作年限：5 年", {}, (True, 5.0)),
    ("3年+", "3 年+ 经验", {}, (True, 3.0)),
    ("10年及以上", "要求 10 年及以上", {}, (True, 10.0)),
    ("经验不限", "熟悉 Java，经验不限", {}, (True, None)),
    ("应届", "应届生亦可", {}, (True, None)),
    # ---- 点名公司 ----
    ("阿里或字节背景", "具备阿里或字节背景", {"company_history": {"阿里", "字节"}}, (False, None)),
    ("阿里背景", "具备阿里背景", {"company_history": {"阿里"}}, (False, None)),
    ("有字节经验", "有字节经验", {"company_history": {"字节"}}, (False, None)),
    ("曾在腾讯任职", "曾在腾讯任职", {"company_history": {"腾讯"}}, (False, None)),
    ("阿里出身", "阿里出身", {"company_history": {"阿里"}}, (False, None)),
    ("来自美团", "来自美团", {"company_history": {"美团"}}, (False, None)),
    ("百度京东背景", "百度、京东背景", {"company_history": {"百度", "京东"}}, (False, None)),
    ("微软背景", "微软背景优先", {"company_history": {"微软"}}, (False, None)),
    ("英文名 ByteDance", "有 ByteDance 经验", {"company_history": {"字节"}}, (False, None)),
    ("英文名 Alibaba", "有 Alibaba 背景", {"company_history": {"阿里"}}, (False, None)),
    ("蚂蚁集团全称", "阿里或蚂蚁集团背景", {"company_history": {"阿里", "蚂蚁"}}, (False, None)),
    # ---- 用户原始长文本 ----
    (
        "用户长文本 A",
        "本科及以上学历，5 年以上 Java 后端开发经验，具备阿里或字节背景。核心主栈为 Java 与 Spring/Spring Boot，"
        "精通 REST API 与微服务设计开发。掌握 SQL 及 PostgreSQL、MariaDB、Oracle 等关系型数据库与 Elasticsearch 等 NoSQL 技术。"
        "具备 Jenkins、Docker、Kubernetes/OpenShift 的 CI/CD 与容器化编排能力。",
        {"degree": {"本科"}, "company_history": {"阿里", "字节"}},
        (True, 5.0),
    ),
    # ---- 第 3 轮：多段 / 表格型 ----
    (
        "制表符表格",
        "学历\t本科及以上\n工作年限\t5年以上\n目标公司\t字节或阿里",
        {"degree": {"本科"}, "company_history": {"字节", "阿里"}},
        (True, 5.0),
    ),
    (
        "冒号表格",
        "学历：本科及以上\n经验：5 年以上\n公司背景：字节或阿里",
        {"degree": {"本科"}, "company_history": {"字节", "阿里"}},
        (True, 5.0),
    ),
    (
        "多段带序号标题",
        "【任职要求】\n1. 本科及以上学历\n2. 5 年以上后端经验\n3. 有字节背景",
        {"degree": {"本科"}, "company_history": {"字节"}},
        (True, 5.0),
    ),
    (
        "全角括号混排",
        "学历：本科（及以上）；院校：985（或 211）",
        {"degree": {"本科"}, "school_level": {"985", "211"}},
        (False, None),
    ),
    (
        "markdown 表格行",
        "| 学历 | 本科及以上 |\n| 年限 | 5 年以上 |\n| 背景 | 阿里 |",
        {"degree": {"本科"}, "company_history": {"阿里"}},
        (True, 5.0),
    ),
    # ---- 第 3 轮：混合中英 ----
    ("英文公司名混合", "有 Alibaba 或 Tencent 背景", {"company_history": {"阿里", "腾讯"}}, (False, None)),
    ("中英混排学历", "Bachelor degree or above（本科及以上）", {"degree": {"本科"}}, (False, None)),
    ("英文年限", "5+ years of backend experience", {}, (True, 5.0)),
    ("英文至少年限", "at least 3 years experience", {}, (True, 3.0)),
    ("英文最高学历", "Master degree or above", {"degree": {"硕士"}}, (False, None)),
    # ---- 第 3 轮：专业缩写 ----
    ("缩写优先", "K8s 优先，CI/CD 必须", {"skill": {"K8s", "CI/CD"}}, (False, None)),
    ("缩写公司", "有 BAT 背景优先", {}, (False, None)),
    ("缩写学历门槛", "统招本科，CET-6 优先", {"degree": {"本科"}}, (False, None)),
    # ---- 第 3 轮：口语化 ----
    ("口语化年限", "起码 3 年经验", {}, (True, 3.0)),
    ("口语化至少", "怎么也得 5 年吧", {}, (True, 5.0)),
    ("口语化应届", "刚毕业的也行", {}, (True, None)),
    ("口语化泛背景", "最好是大厂出来的", {}, (False, None)),
    ("口语化公司背景", "最好在字节待过", {"company_history": {"字节"}}, (False, None)),
    # ---- 第 3 轮：脏数据 ----
    (
        "重复表述",
        "5 年以上经验，5 年以上经验；本科及以上，本科及以上；阿里背景，阿里背景",
        {"degree": {"本科"}, "company_history": {"阿里"}},
        (True, 5.0),
    ),
    (
        "空值与占位",
        "学历：\n经验：\n背景：\n\t\t\n",
        {},
        (False, None),
    ),
    (
        "无空格粘连",
        "本科及以上学历5年以上经验有字节背景",
        {"degree": {"本科"}, "company_history": {"字节"}},
        (True, 5.0),
    ),
]

# 超长脏数据单独测性能与不崩（不作为语义断言）。
HUGE_TEXT = ("负责交易系统开发，熟悉 Java 与 Spring Boot。" * 4000) + "\n本科及以上学历，5 年以上经验，有字节背景"

# 必须**不抽**的反例：(说明, 文本, 禁止出现的 kind)
NEGATIVES: list[tuple[str, str, set[str]]] = [
    ("阿里云是产品不是公司", "有容器化经验，熟悉阿里云 ACK", {"company_history"}),
    ("腾讯云是产品不是公司", "熟悉腾讯云 COS", {"company_history"}),
    ("经验说的是电商不是公司", "有电商经验，了解阿里", {"company_history"}),
    ("仅提技术栈不算公司", "熟悉 Oracle 数据库", {"company_history"}),
    ("AWS 是技术不是公司", "有 AWS 使用经验", {"company_history"}),
    ("泛化背景不卡", "要求互联网或金融背景", {"company_history"}),
    ("泛化大厂背景不卡", "有一线大厂背景", {"company_history"}),
    ("年份区间不是经验", "2020-2023 年负责交易系统", set()),
    ("985优先是加分不是必须", "985 优先", set()),
]

# 「优先」必须保持 PLUS（不能升级成 MUST，否则会误拒）。
PLUS_CASES: list[tuple[str, str, str]] = [
    ("985优先", "985 优先", "school_level"),
    ("本科以上优先", "本科以上优先", "degree"),
    ("阿里背景优先", "阿里背景优先", "company_history"),
]

# 否定句必须判成 EXCLUDE，**绝不能**按默认 MUST 反转成必须项。
EXCLUDE_CASES: list[tuple[str, str, str, set[str]]] = [
    ("不考虑阿里背景", "不考虑阿里背景", "company_history", {"阿里"}),
    ("排除字节背景", "排除字节背景", "company_history", {"字节"}),
]

# 「门槛的否定式」：否定的是**补集**，等价于「必须 X 及以上」，**绝不能**判成 EXCLUDE——
# 判成排除就把要招的人正好全排除，比漏抽危险得多。
THRESHOLD_DEMAND_CASES: list[tuple[str, str, str]] = [
    ("本科以下勿投", "本科以下勿投", "degree"),
    ("非985勿投", "非 985 勿投", "school_level"),
    ("985/211勿投", "985/211 勿投", "school_level"),
    ("非统招本科勿投", "非统招本科勿投", "degree"),
    ("211以下不考虑", "211 以下不考虑", "school_level"),
]


def rule_view(text: str) -> tuple[dict[str, set[str]], dict[str, str], tuple[bool, float | None]]:
    kinds: dict[str, set[str]] = {}
    strengths: dict[str, str] = {}
    for c in parse_exact_constraints(text, source="manual"):
        kinds.setdefault(c.kind, set()).update(c.alternatives)
        strengths.setdefault(c.kind, c.strength)
    return kinds, strengths, parse_years_requirement(text)


def live_view(text: str) -> tuple[dict[str, set[str]], dict[str, str], tuple[bool, float | None]]:
    body = json.dumps({"source_text": text}).encode()
    request = urllib.request.Request(
        LIVE_URL, data=body,
        headers={"X-Kerui-Session": LIVE_TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode())
    kinds: dict[str, set[str]] = {}
    strengths: dict[str, str] = {}
    for c in data.get("constraints") or []:
        kinds.setdefault(str(c.get("kind")), set()).update(c.get("alternatives") or [])
        strengths.setdefault(str(c.get("kind")), str(c.get("strength")))
    return kinds, strengths, (bool(data.get("years_stated")), data.get("min_years"))


def _selected(names: list[str], only: set[str]) -> bool:
    return not only or names[0] in only


def main() -> int:
    live = "--live" in sys.argv
    only: set[str] = set()
    if "--only" in sys.argv:
        only = {n.strip() for n in sys.argv[sys.argv.index("--only") + 1].split(",") if n.strip()}
    view = live_view if live else rule_view
    label = "真实接口（模型 ∪ 规则）" if live else "规则层"
    failures: list[str] = []

    cases = [c for c in CASES if _selected(c, only)]
    negatives = [c for c in NEGATIVES if _selected(c, only)]
    plus_cases = [c for c in PLUS_CASES if _selected(c, only)]
    exclude_cases = [c for c in EXCLUDE_CASES if _selected(c, only)]
    threshold_cases = [c for c in THRESHOLD_DEMAND_CASES if _selected(c, only)]

    print(f"=== 正向用例（{label}）===")
    for name, text, expect_kinds, expect_years in cases:
        kinds, _strengths, years = view(text)
        problems = []
        for kind, expected in expect_kinds.items():
            actual = kinds.get(kind, set())
            if not expected <= actual:
                problems.append(f"{kind} 期望⊇{sorted(expected)} 实际{sorted(actual)}")
        if years != expect_years:
            problems.append(f"年限 期望{expect_years} 实际{years}")
        status = "OK " if not problems else "FAIL"
        if problems:
            failures.append(f"[正向] {name}：{'；'.join(problems)}")
        print(f"  {status} {name:<16}{'' if not problems else ' <- ' + '；'.join(problems)}")

    print(f"\n=== 反例（{label}，抽到即失败）===")
    for name, text, forbidden in negatives:
        kinds, _strengths, years = view(text)
        problems = [f"{k} 命中 {sorted(kinds[k])}" for k in forbidden if kinds.get(k)]
        if "年份区间不是经验" in name and years != (False, None):
            problems.append(f"年限被误抽为 {years}")
        status = "OK " if not problems else "FAIL"
        if problems:
            failures.append(f"[反例] {name}：{'；'.join(problems)}")
        print(f"  {status} {name:<20}{'' if not problems else ' <- ' + '；'.join(problems)}")

    print(f"\n=== 强度（{label}）「优先」不得升级为 MUST ===")
    for name, text, kind in plus_cases:
        _kinds, strengths, _years = view(text)
        actual = strengths.get(kind)
        ok = actual == "PLUS"
        if not ok:
            failures.append(f"[强度] {name}：{kind} 期望 PLUS 实际 {actual}")
        print(f"  {'OK ' if ok else 'FAIL'} {name:<16}{kind}={actual}")

    print(f"\n=== 否定句必须判成 EXCLUDE（{label}）===")
    for name, text, kind, expect_alternatives in exclude_cases:
        _kinds, strengths, _years = view(text)
        actual = strengths.get(kind)
        ok = actual == "EXCLUDE"
        if not ok:
            failures.append(f"[排除] {name}：{kind} 期望 EXCLUDE 实际 {actual}")
        print(f"  {'OK ' if ok else 'FAIL'} {name:<18}{kind}={actual}（应为 EXCLUDE，不能反转成 MUST）")

    print(f"\n=== 门槛否定式不得判成 EXCLUDE（{label}）===")
    for name, text, kind in threshold_cases:
        _kinds, strengths, _years = view(text)
        actual = strengths.get(kind)
        ok = actual in ("MUST", "PLUS")
        if not ok:
            failures.append(f"[门槛] {name}：{kind} 期望 MUST/PLUS 实际 {actual}")
        print(f"  {'OK ' if ok else 'FAIL'} {name:<18}{kind}={actual}（否定的是补集，不是排除）")

    print(f"\n=== 超长脏数据（{label}）===")
    if live:
        print("  SKIP 真实接口对 source_text 有 10k 上限，超长用例只在规则层跑")
    else:
        started = time.perf_counter()
        kinds, _strengths, years = view(HUGE_TEXT)
        elapsed = time.perf_counter() - started
        ok = (elapsed < 5.0 and "degree" in kinds and "company_history" in kinds
              and years == (True, 5.0))
        if not ok:
            failures.append(f"[超长] {len(HUGE_TEXT)} 字符：{elapsed:.2f}s kinds={sorted(kinds)} years={years}")
        print(f"  {'OK ' if ok else 'FAIL'} {len(HUGE_TEXT)} 字符，耗时 {elapsed * 1000:.0f} ms，"
              f"仍抽出 {sorted(kinds)} 年限={years}")

    total = len(cases) + len(negatives) + len(plus_cases) + len(exclude_cases) + len(threshold_cases)
    print(f"\n通过 {total - len(failures)} / {total}")
    if failures:
        print("\n失败明细：")
        for item in failures:
            print("  -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
