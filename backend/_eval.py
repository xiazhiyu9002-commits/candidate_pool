import httpx, json

BASE = "http://127.0.0.1:43127"
H = {"X-Kerui-Session": "0" * 64}

def search(query, mode, filters=None):
    r = httpx.post(BASE + "/api/search/candidates", headers=H,
                   json={"query": query, "mode": mode, "filters": filters or {}, "limit": 100}, timeout=120)
    return r.json()

print("=== 1. 技能归一化（keyword 搜索，验证复合词/别名）===")
for q in ["Spring AI", "K8s", "微服务", "Spring Cloud Alibaba", "Nacos", "Avaloq"]:
    d = search(q, "keyword")
    items = d.get("items", [])
    names = [i.get("name") for i in items[:4]]
    print(f"  '{q}' -> {len(items)} 条 | top={names}")

print("\n=== 2. 方向 + 国内外高校筛选 ===")
for code, label in [("BACKEND","后端"),("FRONTEND","前端"),("ALGORITHM","算法"),("DATA","数据"),("OPS","运维")]:
    d = search("", "hybrid", {"direction": code})
    print(f"  方向 {label} = {len(d.get('items', []))}")
for region, label in [("overseas","国外"),("domestic","国内")]:
    d = search("", "hybrid", {"school_region": region})
    print(f"  {label}高校 = {len(d.get('items', []))}")

print("\n=== 3. 向量模式分数分布（相对阈值 top1×0.8）===")
for q in ["Java 后端 微服务", "算法 机器学习", "金融 风控"]:
    d = search(q, "vector")
    items = d.get("items", [])
    if items:
        scores = [i.get("score") for i in items]
        print(f"  '{q}' -> {len(scores)} 条 | 分数 [{min(scores):.3f} ~ {max(scores):.3f}]")
    else:
        print(f"  '{q}' -> 0 条 | degraded={d.get('degraded_reasons')}")
