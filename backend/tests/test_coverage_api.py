"""页段覆盖接口测试：覆盖视图、确认与重新打开。"""
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services.format_parse import FormatParseService


def _doc() -> str:
    return f"API-{uuid.uuid4().hex[:8].upper()}"


def _make_task(doc: str, task_key: str, page_range: str) -> int:
    task, missing = FormatParseService().create_entry({
        "任务编号": task_key, "文档编号": doc, "解析器类型": "PDF",
        "页码范围": page_range,
    })
    assert task is not None, missing
    return int(task["id"])


def test_coverage_view_and_confirm_endpoints():
    client = TestClient(app)
    doc = _doc()
    FormatParseService().execute_parse(_make_task(doc, f"T1-{doc}", "1-8"))

    view = client.get(f"/api/format_parse/coverage/{doc}")
    assert view.status_code == 200
    body = view.json()
    assert body["doc_id"] == doc
    assert len(body["pages"]) == 8
    assert body["pending_group"]["ready_to_confirm"] is True

    confirmed = client.post(
        f"/api/format_parse/coverage/{doc}/confirm",
        json={"confirmed_by": "值班员", "remark": "首轮确认"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"]["version"] == 1
    assert confirmed.json()["chunk_trigger"]["page_group_version"] == 1

    reopen = client.get(f"/api/format_parse/coverage/{doc}/reopen")
    assert reopen.status_code == 200
    assert len(reopen.json()["accepted_pages"]) == 8


def test_confirm_endpoint_rejects_incomplete_group():
    client = TestClient(app)
    doc = _doc()
    FormatParseService().execute_parse(_make_task(doc, f"T1-{doc}", "1-3"))

    response = client.post(
        f"/api/format_parse/coverage/{doc}/confirm", json={"expected_pages": 8})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["ok"] is False
    assert detail["missing_pages"] == [4, 5, 6, 7, 8]


def test_coverage_route_does_not_shadow_module_listing():
    client = TestClient(app)
    response = client.get("/api/format_parse?size=1")
    assert response.status_code == 200
    assert "items" in response.json()
