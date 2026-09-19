import httpx, json

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

def get(path):
    return httpx.get(BASE + path, headers=H, timeout=60)

def put(path, body):
    return httpx.put(BASE + path, headers=H, json=body, timeout=60)

# ---- 候选人批量接口 ----
cid = "01a08fb8-f16c-7f07-83a8-518d5bc4425d"  # 黄伟林
r = get("/api/resumes/candidates/page?page=1&page_size=5")
cand = next((c for c in r.json()["items"] if c["candidate_id"] == cid), None)
orig_salary = (cand["parsed_data"] or {}).get("salary") if cand else None
print("候选人原始 salary:", repr(orig_salary))

r = put(f"/api/resumes/candidate/{cid}/parsed", {"parsed_data": {"salary": "测试薪资-勿扰"}})
print("\n[候选人批量保存] status:", r.status_code, r.text[:200])

# 还原
r = put(f"/api/resumes/candidate/{cid}/parsed", {"parsed_data": {"salary": orig_salary}})
print("[候选人还原] status:", r.status_code, r.text[:120])

# 空 parsed_data 校验
r = put(f"/api/resumes/candidate/{cid}/parsed", {"parsed_data": {}})
print("[候选人空数据] status:", r.status_code, r.text[:120])

# ---- JD 批量接口 ----
jid = "01a08a1d-683c-79fb-9d39-fb2185b99277"  # Java 开发工程师
r = get("/api/jd/page?page=1&page_size=100")
jd = next((j for j in r.json()["items"] if j["jd_id"] == jid), None)
orig_summary = (jd["parsed_data"] or {}).get("summary") if jd else None
print("\nJD 原始 summary:", repr(orig_summary)[:80])

r = put(f"/api/jd/{jid}/parsed", {"parsed_data": {"summary": "测试摘要-勿扰", "direction": "BACKEND"}})
print("[JD批量保存] status:", r.status_code, r.text[:200])

r = put(f"/api/jd/{jid}/parsed", {"parsed_data": {"summary": orig_summary}})
print("[JD还原] status:", r.status_code, r.text[:120])
