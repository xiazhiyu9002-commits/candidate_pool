import httpx, json

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

def post(path, body):
    r = httpx.post(BASE + path, headers=H, json=body, timeout=60)
    return r.status_code, r.json()

cands = {
    "黄伟林": "01a08fb8-f16c-7f07-83a8-518d5bc4425d",
    "麦健荣": "01a08fb8-f140-7233-9b9c-59b158d1a012",
    "邓愉悦": "01a08fb8-f0c4-723a-876c-cd531e885cc0",
    "蔡柱梁": "01a08fb8-f075-7dc4-89e2-ef0b008dcc5b",
}
jds = {
    "Java 后端与大数据开发工程师": "01a08a1d-683f-7179-87eb-a97baf966061",
    "Java 开发工程师": "01a08a1d-683c-79fb-9d39-fb2185b99277",
    "MOT 全栈开发工程师": "01a08a1d-6824-7cd6-b6b1-d18f4287ec03",
    "资深Java开发工程师": "01a08574-5a15-7172-86b4-12e0f9f22976",
}

scenarios = [
    # (名称, 候选人, JD, recommend_time, enter_interview_time)
    ("推荐未反馈", "黄伟林", "Java 后端与大数据开发工程师", "2026-09-12T10:00:00", None),
    ("明天面试", "麦健荣", "Java 开发工程师", "2026-09-12T10:00:00", "2026-09-13T14:00:00"),
    ("今天面试(未来)", "邓愉悦", "MOT 全栈开发工程师", "2026-09-12T10:00:00", "2026-09-12T23:30:00"),
    ("面试未反馈", "蔡柱梁", "资深Java开发工程师", "2026-09-12T09:00:00", "2026-09-12T09:30:00"),
]

for name, cand, jd_title, rec_t, ent_t in scenarios:
    cid = cands[cand]
    jid = jds[jd_title]
    sc, case = post("/api/case", {"candidate_id": cid, "jd_id": jid})
    if sc not in (200, 201):
        print(f"[{name}] create_case 失败 {sc}: {case}")
        continue
    case_id = case["id"]
    # 推荐
    sc, ev = post(f"/api/case/{case_id}/recommend", {"occurred_at": rec_t, "note": f"评测-{name}"})
    if sc not in (200, 201):
        print(f"[{name}] recommend 失败 {sc}: {ev}")
        continue
    if ent_t:
        sc, ev = post(f"/api/case/{case_id}/enter-interview", {"occurred_at": ent_t, "round_name": "初试", "note": f"评测-{name}"})
        if sc not in (200, 201):
            print(f"[{name}] enter-interview 失败 {sc}: {ev}")
            continue
    print(f"[{name}] OK case_id={case_id} {cand} -> {jd_title}")

# 验证今日待办
r = httpx.get(BASE + "/api/daily-followup/today", headers=H, timeout=60)
print("\n=== 今日待办 ===")
print("status:", r.status_code)
d = r.json()
print("追反馈:", len(d.get("followup", [])))
for it in d.get("followup", []):
    print("   ", it.get("name"), "|", it.get("company"), "|", it.get("title"), "|", it.get("date"))
print("待面试:", len(d.get("interview", [])))
for it in d.get("interview", []):
    print("   ", it.get("name"), "|", it.get("company"), "|", it.get("title"), "|", it.get("time"))
