from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from kerui_recruit.api.services import AppServices


router = APIRouter(prefix="/api/mapping", tags=["mapping"])


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None


class CreateSnapshotRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)


class SnapshotResponse(BaseModel):
    id: str
    label: str
    is_current: bool


class BuildTreeRequest(BaseModel):
    text: str = Field(min_length=1)
    label: str = ""


class TreeNodeResponse(BaseModel):
    id: str
    name: str
    sort_order: int
    children: list[TreeNodeResponse]


@router.post("/projects", response_model=ProjectResponse)
def create_project(command: CreateProjectRequest, request: Request) -> ProjectResponse:
    services: AppServices = request.app.state.services
    project = services.mapping_service.create_project(
        name=command.name,
        description=command.description,
    )
    return ProjectResponse(id=project.id, name=project.name, description=project.description)


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(request: Request) -> list[ProjectResponse]:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        from sqlalchemy import select
        from kerui_recruit.db.models import MappingProject
        projects = session.scalars(
            select(MappingProject).where(MappingProject.deleted_at == None).order_by(MappingProject.created_at.desc())
        ).all()
    return [ProjectResponse(id=p.id, name=p.name, description=p.description) for p in projects]


@router.post("/projects/{project_id}/snapshots", response_model=SnapshotResponse)
def create_snapshot(project_id: str, command: CreateSnapshotRequest, request: Request) -> SnapshotResponse:
    services: AppServices = request.app.state.services
    snap = services.mapping_service.create_snapshot(project_id, command.label)
    return SnapshotResponse(id=snap.id, label=snap.label, is_current=snap.is_current)


@router.get("/projects/{project_id}/snapshots", response_model=list[SnapshotResponse])
def list_snapshots(project_id: str, request: Request) -> list[SnapshotResponse]:
    services: AppServices = request.app.state.services
    with services.session_factory() as session:
        from sqlalchemy import select
        from kerui_recruit.db.models import MappingSnapshot
        snaps = session.scalars(
            select(MappingSnapshot)
            .where(MappingSnapshot.project_id == project_id)
            .order_by(MappingSnapshot.created_at.desc())
        ).all()
    return [SnapshotResponse(id=s.id, label=s.label, is_current=s.is_current) for s in snaps]


@router.get("/snapshots/{snapshot_id}/tree", response_model=list[TreeNodeResponse])
def get_tree(snapshot_id: str, request: Request) -> list[TreeNodeResponse]:
    services: AppServices = request.app.state.services
    roots = services.mapping_service.get_tree(snapshot_id)
    return [_tree_node_to_response(n) for n in roots]


@router.get("/snapshots/{snapshot_id}/export")
def export_tree(snapshot_id: str, request: Request) -> Response:
    services: AppServices = request.app.state.services
    xlsx_bytes = services.export_service.export_mapping_tree(snapshot_id)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="mapping_{snapshot_id}.xlsx"'},
    )


@router.get("/snapshots/{snapshot_id}/export-pdf")
def export_tree_pdf(snapshot_id: str, request: Request) -> Response:
    services: AppServices = request.app.state.services
    pdf_bytes = services.export_service.export_mapping_tree_pdf(snapshot_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="mapping_{snapshot_id}.pdf"'},
    )


@router.post("/projects/{project_id}/build-from-text", response_model=SnapshotResponse)
def build_tree_from_text(
    project_id: str, command: BuildTreeRequest, request: Request
) -> SnapshotResponse:
    services: AppServices = request.app.state.services
    snap = services.mapping_service.build_tree_from_text(
        project_id=project_id,
        text=command.text,
        label=command.label,
    )
    return SnapshotResponse(id=snap.id, label=snap.label, is_current=snap.is_current)


def _tree_node_to_response(node) -> TreeNodeResponse:
    return TreeNodeResponse(
        id=node.id,
        name=node.name,
        sort_order=node.sort_order,
        children=[_tree_node_to_response(c) for c in node.children],
    )