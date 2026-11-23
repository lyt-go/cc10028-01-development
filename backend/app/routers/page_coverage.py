"""页段覆盖接口：覆盖视图、确认页组与重新打开确认记录。

覆盖清单是“格式解析”模块的页级延伸，路由挂在 ``/api/format_parse/...``
固定路径上，必须注册在通用 ``/api/{module}/{entry_id}`` 路由之前。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.format_parse import FormatParseService

router = APIRouter(prefix="/api/format_parse", tags=["页段覆盖"])


class ConfirmPayload(BaseModel):
    expected_pages: int | None = Field(default=None, ge=1)
    decision: str = "确认分块"
    remark: str | None = None
    confirmed_by: str | None = None
    # 页号 → 该页采用的解析任务 ID；不传默认取最新解析产物
    page_sources: dict[int, int] | None = None


class ExecutePayload(BaseModel):
    # 本次重跑修正了哪些页，用于覆盖视图标记内容变化
    corrected_pages: list[int] = Field(default_factory=list)


def _service() -> FormatParseService:
    return FormatParseService()


@router.post("/tasks/{task_id}/execute")
def execute_task(task_id: int, payload: ExecutePayload | None = None) -> dict:
    """执行解析任务，按页码范围产出页产物并登记覆盖清单。"""
    corrected = payload.corrected_pages if payload else []
    result = _service().execute_parse(task_id, corrected_pages=corrected)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/documents/{doc_id}/chunks")
def split_document(doc_id: str) -> dict:
    """按确认页组重新切分文档，用于手动补算或核对页归属。"""
    from app.services.chunk_split import ChunkSplitService

    result = ChunkSplitService().split_from_document(f"{doc_id}_parsed")
    if "error" in result:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/coverage/{doc_id}")
def get_coverage(doc_id: str) -> dict:
    """覆盖比对视图：每页进入哪次分块、来源任务与待补齐页。"""
    return _service().coverage_view(doc_id)


@router.post("/coverage/{doc_id}/confirm")
def confirm_coverage(doc_id: str, payload: ConfirmPayload) -> dict:
    """把当前已有页冻结为一次确认页组；页组不完整时拒绝确认。"""
    service = _service()
    result = service.confirm_pages(
        doc_id,
        expected_pages=payload.expected_pages,
        decision=payload.decision,
        remark=payload.remark or "",
        confirmed_by=payload.confirmed_by or "",
        page_sources=payload.page_sources,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/coverage/{doc_id}/reopen")
def reopen_coverage(doc_id: str) -> dict:
    """重新打开最近一次确认：采纳页与未被采纳页的来源、待处理原因。"""
    result = _service().reopen_confirmation(doc_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result
