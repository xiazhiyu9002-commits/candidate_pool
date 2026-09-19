import httpx, json

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

def get(path, **kw):
    return httpx.get(BASE + path, headers=H, timeout=120, **kw)

def post(path, **kw):
    return httpx.post(BASE + path, headers=H, timeout=120, **kw)

# 1. 候选人数量
r = get("/api/resumes/candidates/page", params={"page": 1, "page_size": 1})
print("候选人 total:", r.json().get("total"))

# 2. JD 列表（探测 OPEN JD）
r = get("/api/jd/page", params={"page": 1, "page_size": 100})
if r.status_code == 200:
    jds = r.json().get("items", [])
    print("JD 总数:", r.json().get("total"))
    open_jds = [j for j in jds if j.get("jd_status") == "OPEN"]
    print("OPEN JD 数量(本页):", len(open_jds))
    for j in jds[:5]:
        print("  JD:", j.get("title"), "| status=", j.get("jd_status"), "| revision=", j.get("revision_id"))
else:
    print("JD page 接口失败:", r.status_code, r.text[:200])

# 3. 索引状态
r = get("/api/search/index-status")
print("\n索引状态:", r.text[:300])
