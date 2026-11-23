"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import page_coverage, resource

# 固定路径（如 /api/format_parse/coverage/...）必须排在通用 /api/{module} 之前。
ROUTERS = [page_coverage, resource]
