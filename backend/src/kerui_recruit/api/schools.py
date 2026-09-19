from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from kerui_recruit.api.services import AppServices
from kerui_recruit.db.models import School
from kerui_recruit.schools.importing import CSV_TEMPLATE, import_schools, parse_csv
from kerui_recruit.schools.reference import SchoolReference

router = APIRouter(prefix="/api/schools", tags=["schools"])


class SchoolItem(BaseModel):
    id: str
    canonical_name: str
    aliases: list[str]
    tags: list[str]
    qs_year: int | None = None
    qs_rank_start: int | None = None
    qs_rank_end: int | None = None
    rank_display: str | None = None
    source: str


class ResolveRequest(BaseModel):
    name: str = Field(min_length=1)


class SchoolImportRequest(BaseModel):
    records: list[dict] = Field(default_factory=list)
    csv: str | None = None


@router.get("", response_model=list[SchoolItem])
def list_schools(request: Request) -> list[SchoolItem]:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        rows = session.scalars(select(School).order_by(School.canonical_name)).all()
    return [
        SchoolItem(
            id=s.id, canonical_name=s.canonical_name, aliases=s.aliases or [],
            tags=s.tags or [], qs_year=s.qs_year, qs_rank_start=s.qs_rank_start,
            qs_rank_end=s.qs_rank_end, rank_display=s.rank_display, source=s.source,
        )
        for s in rows
    ]


@router.get("/import-template")
def import_template() -> dict:
    return {"format": "csv", "template": CSV_TEMPLATE}


@router.post("/import")
def import_schools_endpoint(command: SchoolImportRequest, request: Request) -> dict:
    services: AppServices = request.app.state.services
    if command.csv is not None:
        records = parse_csv(command.csv)
    else:
        records = command.records
    if not records:
        return {"imported": 0, "updated": 0, "failed": 0, "errors": []}
    with services.session_factory() as session, session.begin():
        result = import_schools(session, records)
    return result


@router.post("/resolve")
def resolve_school(command: ResolveRequest, request: Request) -> dict:
    services: AppServices = request.app.state.services
    return SchoolReference(services.session_factory).resolve(command.name)
