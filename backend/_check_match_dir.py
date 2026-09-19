import sys
from pathlib import Path
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db import models

e = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
F = sessionmaker(e, expire_on_commit=False)
s = F()

# 被测试 JD
rev = s.get(models.JdRevision, "01a08a1d-6840-761b-94f1-3f0c9465250b")
print("JD parsed_data.direction =", repr((rev.parsed_data or {}).get("direction")))
print("JD parsed_data.industry  =", repr((rev.parsed_data or {}).get("industry")))

# 取几个 top 候选人，看其 direction
ids = ["01a08557-71ef-74ba-95d3-559114271846",
       "01a08fb8-c20b-73d5-af53-751a1578ecc8",
       "01a08fb8-c2ed-7d1b-9929-aec669302ce8"]
for cid in ids:
    r = s.scalars(select(models.ResumeRevision).join(models.ResumeDocument)
                  .where(models.ResumeDocument.candidate_id == cid,
                         models.ResumeRevision.is_current.is_(True),
                         models.ResumeRevision.status == "READY")).first()
    pd = (r.parsed_data or {}) if r else {}
    print(f"candidate {cid[:8]} direction={pd.get('direction')!r} industry={pd.get('industry')!r}")
