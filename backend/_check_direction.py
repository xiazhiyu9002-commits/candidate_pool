import sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, "src")
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from kerui_recruit.db.session import create_engine_for
from kerui_recruit.db import models

e = create_engine_for(Path(r"C:\Users\Nl\Desktop\安装包+源码\candidate_pool\.dev-data\db\recruit.sqlite3"))
F = sessionmaker(e, expire_on_commit=False)
s = F()
rows = s.execute(
    select(models.Jd, models.JdRevision)
    .join(models.JdRevision, models.JdRevision.jd_id == models.Jd.id)
    .where(models.Jd.status == "OPEN", models.Jd.deleted_at.is_(None),
           models.JdRevision.is_current.is_(True), models.JdRevision.status == "READY")
).all()
c = Counter((r.parsed_data or {}).get("direction") for _, r in rows)
print("direction 分布:", dict(c))
print()
for j, r in rows:
    print(f"  {j.title[:26]!r:30} -> {(r.parsed_data or {}).get('direction')!r}")
