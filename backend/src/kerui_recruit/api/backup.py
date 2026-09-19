from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel

from kerui_recruit.api.errors import ApiError
from kerui_recruit.api.services import AppServices
from kerui_recruit.backup.portable import (
    PortableRestoreError,
    PortableRestoreReport,
    is_same_volume,
)


router = APIRouter(prefix="/api/backup", tags=["backup"])


class SnapshotItem(BaseModel):
    filename: str
    path: str
    size_bytes: str
    created: str


class CreateSnapshotRequest(BaseModel):
    label: str = ""


class CreateSnapshotResponse(BaseModel):
    filename: str
    path: str


class RestoreResponse(BaseModel):
    restored_from: str
    safety_backup: str
    restart_required: bool = True
    status: str = "pending_restart"


@router.get("/snapshots", response_model=list[SnapshotItem])
def list_snapshots(request: Request) -> list[SnapshotItem]:
    services: AppServices = request.app.state.services
    return [SnapshotItem(**s) for s in services.backup_service.list_snapshots()]


@router.post("/snapshots", response_model=CreateSnapshotResponse)
def create_snapshot(command: CreateSnapshotRequest, request: Request) -> CreateSnapshotResponse:
    services: AppServices = request.app.state.services
    path = services.backup_service.create_snapshot(label=command.label)
    return CreateSnapshotResponse(filename=path.name, path=str(path))


@router.post("/restore/{filename}", response_model=RestoreResponse)
def restore_snapshot(filename: str, request: Request) -> RestoreResponse:
    services: AppServices = request.app.state.services
    try:
        safety = services.backup_service.restore_snapshot(filename)
    except ValueError as error:
        from kerui_recruit.api.errors import ApiError
        raise ApiError(422, "E_BACKUP_INVALID", str(error)) from error
    return RestoreResponse(restored_from=filename, safety_backup=str(safety))


class CreatePortableRequest(BaseModel):
    target_path: str
    passphrase: str


class CreatePortableResponse(BaseModel):
    path: str
    same_volume: bool


class RestorePortableRequest(BaseModel):
    backup_path: str
    target_root: str
    passphrase: str


class RestorePortableResponse(BaseModel):
    target_root: str
    files_restored: int
    files_verified: int
    ok: bool


@router.post("/portable", response_model=CreatePortableResponse)
def create_portable(command: CreatePortableRequest, request: Request) -> CreatePortableResponse:
    services: AppServices = request.app.state.services
    target = Path(command.target_path)
    path = services.portable_backup_service.create(target, command.passphrase)
    same_volume = is_same_volume(services.portable_backup_service.current_root, target)
    return CreatePortableResponse(path=str(path), same_volume=same_volume)


@router.post("/portable/restore", response_model=RestorePortableResponse)
def restore_portable(command: RestorePortableRequest, request: Request) -> RestorePortableResponse:
    services: AppServices = request.app.state.services
    try:
        report: PortableRestoreReport = services.portable_backup_service.restore(
            Path(command.backup_path),
            Path(command.target_root),
            command.passphrase,
        )
    except PortableRestoreError as error:
        raise ApiError(422, error.code, str(error)) from error
    return RestorePortableResponse(
        target_root=report.target_root,
        files_restored=report.files_restored,
        files_verified=report.files_verified,
        ok=report.ok,
    )
