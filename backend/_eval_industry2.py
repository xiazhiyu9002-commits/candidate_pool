import sys
from pathlib import Path
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db import models
from kerui_recruit.search.industry import normalize_industries

e = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
F = sessionmaker(e, expire_on_commit=False)
s = F()

# OPEN JD
jd_rows = s.execute(
    select(models.Jd, models.JdRevision)
    .join(models.JdRevision, models.JdRevision.jd_id == models.Jd.id)
    .where(models.Jd.status == "OPEN", models.Jd.deleted_at.is_(None),
           models.JdRevision.is_current.is_(True), models.JdRevision.status == "READY")
).all()

# 候选人行业桶集合
cands = s.scalars(select(models.Candidate)).all()
cand_buckets = {}
for c in cands:
    rev = s.scalars(select(models.ResumeRevision)
                    .where(models.ResumeRevision.document_id.in_([d.id for d in c.documents]))
                    .where(models.ResumeRevision.is_current.is_(True))).first()
    if rev is None:
        continue
    pd = rev.parsed_data or {}
    cand_buckets[c.id] = normalize_industries([
        pd.get("industry"), pd.get("current_industry"), pd.get("longest_industry"),
    ])

print("=== 行业归一化效果（新 _industry_match 口径）===\n")
total_jd_with_ind = 0
total_jd_matched_any = 0
for jd, rev in jd_rows:
    jind = (rev.parsed_data or {}).get("industry")
    if not jind:
        continue
    total_jd_with_ind += 1
    jd_buckets = normalize_industries([jind])
    n_hit = sum(1 for cb in cand_buckets.values() if jd_buckets & cb)
    if n_hit > 0:
        total_jd_matched_any += 1
    print(f"  {jd.title[:22]!r:26} industry={jind!r:30} -> 桶={sorted(jd_buckets)} 命中候选人={n_hit}")
print(f"\n有行业的 JD {total_jd_with_ind} 个，其中至少命中 1 名候选人行业 {total_jd_matched_any} 个")
