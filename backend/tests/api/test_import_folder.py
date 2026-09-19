from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kerui_recruit.core.settings import Settings
from kerui_recruit.runtime import create_runtime_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_root=tmp_path / "data",
        session_token=SecretStr("launch-token"),
    )
    app = create_runtime_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {"X-Kerui-Session": "launch-token"}


def _pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "张三 本科 6年 Java 后端开发 金融风控 上海")
    payload = document.tobytes()
    document.close()
    return payload


def test_import_folder_scans_and_ingests(tmp_path: Path, client: TestClient) -> None:
    folder = tmp_path / "resumes"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(_pdf_bytes())
    (folder / "notes.txt").write_text("ignore me")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "b.pdf").write_bytes(_pdf_bytes())

    response = client.post(
        "/api/resumes/import-folder",
        json={"directory": str(folder)},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["imported"]) == 2
    assert body["skipped"] == ["notes.txt"]
    assert body["errors"] == []
