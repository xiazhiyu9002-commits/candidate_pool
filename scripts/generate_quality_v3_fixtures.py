"""从冻结快照生成 v3 评测夹具（只读快照 -> backend/tests/fixtures/*.json）。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / ".semantic-audit-snapshot"
FIX = ROOT / "backend" / "tests" / "fixtures"
FIX.mkdir(parents=True, exist_ok=True)


def load(name: str):
    return json.loads((SNAP / name).read_text(encoding="utf-8"))


def build_search() -> list[dict]:
    retrieval = load("retrieval.json")["intents"]
    labels = load("blind_labels.json")
    cases = []
    for qid, item in retrieval.items():
        if qid > "Q24":
            continue
        pool = set().union(*(set(m["ids"]) for m in item["modes"].values()))
        hits = []
        for cid in sorted(pool):
            label = labels.get(qid, {}).get(cid, {})
            hits.append({
                "alias": cid,
                "model_grade": int(label.get("grade", -1)) if label else None,
                "headhunter_grade": None,
                "adjudicated_grade": None,
                "model_evidence": label.get("evidence", "") if label else "",
                "model_gap": label.get("gap", "") if label else "",
            })
        cases.append({
            "case_id": qid,
            "query_type": "candidate_search",
            "query": item["intent"],
            "hits": hits,
        })
    return cases


def build_match() -> list[dict]:
    results = load("match_results.json")
    labels = load("match_blind_labels.json")["labels"]
    cases = []
    # 正向：JD -> 候选人
    for jd_id, modes in results.get("forward", {}).items():
        ids = modes.get("hybrid", {}).get("ids", [])
        hits = []
        for cid in ids:
            label = labels.get(f"{cid}:{jd_id}", {})
            hits.append({
                "alias": cid,
                "model_verdict": label.get("verdict"),
                "model_grade": int(label.get("grade", -1)) if "grade" in label else None,
                "headhunter_verdict": None,
                "adjudicated_verdict": None,
            })
        cases.append({"case_id": jd_id, "query_type": "jd_to_candidates", "hits": hits})
    # 反向：候选人 -> JD
    for cid, modes in results.get("reverse", {}).items():
        jds = modes.get("hybrid", {}).get("jds", [])
        hits = []
        for jd_id in jds:
            label = labels.get(f"{cid}:{jd_id}", {})
            hits.append({
                "alias": jd_id,
                "model_verdict": label.get("verdict"),
                "model_grade": int(label.get("grade", -1)) if "grade" in label else None,
                "headhunter_verdict": None,
                "adjudicated_verdict": None,
            })
        cases.append({"case_id": cid, "query_type": "candidate_to_jds", "hits": hits})
    return cases


search_cases = build_search()
match_cases = build_match()
(FIX / "search_quality_v3.json").write_text(json.dumps(search_cases, ensure_ascii=False, indent=2), encoding="utf-8")
(FIX / "match_quality_v3.json").write_text(json.dumps(match_cases, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"search cases: {len(search_cases)}, match cases: {len(match_cases)}")
print(f"written: {FIX / 'search_quality_v3.json'}")
print(f"written: {FIX / 'match_quality_v3.json'}")
