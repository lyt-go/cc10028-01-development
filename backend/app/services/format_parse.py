"""格式解析服务：PDF/Word/HTML等格式文档的解析器选择、页码范围切分与输出格式控制。

每次解析任务按页码范围只产出本任务负责的页，并写入不可变的运行产物：
``data/parsed/<doc>_run_<task_id>.jsonl``。该产物随后登记进页段覆盖清单，
供值班人员确认页组与分块切分读取。解析完成后自动触发分块切分和版式还原，
但分块只消费“已确认页组”，不会因为一次补页把历史数据换掉。
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.domain.page_coverage import PageCoverageManifest
from app.services.base import BaseModuleService, _ensure_dir
from app.store import store


def parse_page_range(spec: str, fallback_total: int) -> list[int]:
    """把“1-8”“4-6”“2,5,7-9”解析为页号列表；空值表示整份文档。"""
    text = (spec or "").strip()
    if not text:
        return list(range(1, fallback_total + 1))
    pages: set[int] = set()
    for chunk in re.split(r"[,，;；\s]+", text):
        if not chunk:
            continue
        if "-" in chunk:
            start_s, _, end_s = chunk.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            pages.update(range(max(start, 1), max(end, 0) + 1))
        else:
            try:
                pages.add(int(chunk))
            except ValueError:
                continue
    return sorted(p for p in pages if p >= 1)


def format_page_range(pages: list[int]) -> str:
    """把页号列表压缩成连续段描述，例如 [1,2,3,5] → '1-3,5'。"""
    if not pages:
        return ""
    ordered = sorted(set(pages))
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = page
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


class FormatParseService(BaseModuleService):
    MODULE = "format_parse"
    ENTITY = "解析任务"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["任务编号", "文档编号", "解析器类型", "页码范围", "修订页", "解析模式", "输出格式", "超时秒数", "解析器版本"]
    REQUIRED_FIELDS = ["任务编号", "文档编号", "解析器类型"]
    STATUS_ORDER = ["草稿", "待解析", "解析中", "已完成", "解析失败"]
    ACTION_RULES = {"启动解析": "解析中", "标记完成": "已完成", "标记失败": "解析失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # 支持的解析器类型与对应正则
    PARSER_REGISTRY = {
        "PDF": {"description": "PDF文档解析器，提取文本段落与页面布局"},
        "DOCX": {"description": "Word文档解析器，提取段落样式与层级结构"},
        "HTML": {"description": "HTML文档解析器，提取标签语义与DOM树"},
# 数据产物：从接入批次中批量创建解析任务数据集
        "TXT": {"description": "纯文本文档解析器，按字符集编码读取"},
        "MARKDOWN": {"description": "Markdown解析器，提取标题层级与代码块"},
    }

    # 未指定页码范围时按整份文档估算总页数，供覆盖视图判断还缺哪些页
    DEFAULT_TOTAL_PAGES = 8

    def __init__(self) -> None:
        self.coverage = PageCoverageManifest(self)

    def create_from_batch(self, batch_key: str) -> dict[str, Any]:
        """从接入批次创建解析任务，读取批次中的文档清单逐个生成解析任务。"""
        # 读取已校验批次文件
        batch_file = self._data_dir("batches") / "validated.jsonl"
        batches = self._read_jsonl(batch_file)
        matched = [b for b in batches if b.get("batch_key") == batch_key]
        if not matched:
            return {"error": f"批次 {batch_key} 未找到校验记录，可能未通过校验"}
        batch = matched[0]

        batch_rows = [r for r in store.rows("file_intake") if r.get("批次编号") == batch_key]
        if not batch_rows:
            return {"error": f"批次 {batch_key} 未在file_intake登记"}

        batch_row = batch_rows[0]
        file_list = batch_row.get("文件清单", "").split(",")
        file_type = batch_row.get("文件类型", "PDF")
        parser_type = self.PARSER_REGISTRY.get(file_type, self.PARSER_REGISTRY["PDF"])

        created = []
        for i, fname in enumerate(file_list):
            fname = fname.strip()
            if not fname:
                continue
            task_values = {
                "任务编号": f"PARSE-{batch_key}-{i+1:03d}",
                "文档编号": fname,
                "解析器类型": file_type,
                "页码范围": "",
                "解析模式": "full",
                "输出格式": "jsonl",
                "超时秒数": "300",
                "解析器版本": "v2.1.0",
            }
            result, missing = self.create_entry(task_values)
            if result:
                created.append(result["任务编号"])
        return {"batch_key": batch_key, "tasks_created": len(created), "task_ids": created}

    def execute_parse(self, task_id: int,
                      corrected_pages: list[int] | None = None) -> dict[str, Any]:
        """执行文档解析任务，按页码范围写出本任务页产物并登记页段覆盖清单。

        运行产物按任务分开落盘（``<doc>_run_<task_id>.jsonl``），不会追加到
        其他任务的产物里；合并视图由页段覆盖清单的确认快照负责。
        ``corrected_pages`` 声明本次重跑修正了哪些页，用于覆盖视图标记内容变化。
        """
        task = store.find(self.MODULE, task_id)
        if task is None:
# 数据操作：解析文档生成已解析页面产物与区域检测
            return {"error": f"解析任务 {task_id} 不存在"}
        if task.get("status") not in ("草稿", "待解析", "解析中"):
            return {"error": f"解析任务 {task_id} 状态不允许执行解析，当前状态: {task.get('status')}"}

        task["status"] = "解析中"
        start_time = time.time()
        doc_id = task["文档编号"]
        parser = task["解析器类型"]
        task_key = task.get("任务编号") or f"TASK-{task_id}"
        requested_pages = parse_page_range(task.get("页码范围", ""), self.DEFAULT_TOTAL_PAGES)
        page_range_text = format_page_range(requested_pages)
        corrections = set(corrected_pages or parse_page_range(str(task.get("修订页") or ""), 0))
        corrections &= set(requested_pages)

        try:
            # 模拟文档解析：先生成整份文档，再只保留本任务负责的页
            content = self._simulate_doc_content(doc_id, paragraphs=8)
            expected_total = self._expected_pages(requested_pages)
            all_pages = self._simulate_pages(
                content, expected_total, doc_id, corrected_pages=corrections)
            page_map = {int(p["page"]): p for p in all_pages}
            pages = [page_map[num] for num in requested_pages if num in page_map]
            table_regions = self._detect_table_regions(pages)
            image_regions = self._detect_image_regions(pages)

            # 写入本任务的不可变运行产物
            output_path = self._data_dir("parsed") / f"{doc_id}_run_{task_id}.jsonl"
            parsed_records = []
            for p in pages:
                parsed_records.append({
                    "doc_id": doc_id,
# 数据产物：解析page产物数据写入jsonl文件
                    "page_num": p["page"],
                    "paragraphs": len(p["paragraphs"]),
                    "text_length": p["text_length"],
                    "has_tables": p["has_tables"],
                    "has_images": p["has_images"],
                    "parser": parser,
                    "content_hash": self._compute_checksum("\n".join(p["paragraphs"])),
                    "text": "\n".join(p["paragraphs"]),
                    "task_id": task_id,
                    "task_key": task_key,
                    "parsed_file": output_path.name,
                })
            written = self._overwrite_jsonl(output_path, parsed_records)

            # 运行产物同时保留一份“最新页文本”索引，供旧版按文档读取的消费方兼容
            self._write_page_index(doc_id, parsed_records)
            # 聚合文件只是最新页快照，方便旧消费方按整篇读取；分块不读它。
            self._write_document_snapshot(doc_id)

            # 写入表格区域到共享产物，供table_extract读取
            if table_regions:
                table_path = self._data_dir("layout") / f"{doc_id}_tables.jsonl"
                self._overwrite_jsonl(table_path, table_regions)

            # 写入图片区域到共享产物，供ocr_engine读取
            if image_regions:
                image_path = self._data_dir("layout") / f"{doc_id}_images.jsonl"
                self._overwrite_jsonl(image_path, image_regions)

            elapsed = time.time() - start_time
# 数据操作：解析完毕写入检查点，存储数据集至持久化文件
            task["status"] = "已完成"
            task["pending"] = False
            task["页码范围"] = page_range_text
            if elapsed > float(task.get("超时秒数") or 300):
                task["abnormal"] = True

            # 登记页段覆盖清单：本次任务负责的页与内容摘要
            manifest = self.coverage.register_run(
                doc_id,
                task_id=task_id,
                task_key=task_key,
                page_range=page_range_text,
                pages=parsed_records,
                parsed_file=output_path.name,
                output_format=task.get("输出格式", "jsonl"),
                expected_pages=self._expected_pages(requested_pages),
                corrected_pages=sorted(corrections),
            )

            # 写入检查点
            self._write_checkpoint(f"parse_{doc_id}", {
                "task_id": task_id,
                "doc_id": doc_id,
                "pages": len(pages),
                "page_range": page_range_text,
                "records_written": written,
                "elapsed": elapsed,
                "table_count": len(table_regions),
                "image_count": len(image_regions),
            })

            # 跨模块自动触发：分块只读取已确认页组，无确认时不产出半份结果
            triggers = {}
            trig_chunk = self._trigger_chunk_split(doc_id)
            triggers["chunk_split"] = trig_chunk
            trig_layout = self._trigger_layout_restore(doc_id)
            triggers["layout_restore"] = trig_layout
            trig_quality = self._trigger_quality_check(doc_id)
            triggers["extract_quality"] = trig_quality

            return {
                "task_id": task_id,
                "doc_id": doc_id,
                "page_range": page_range_text,
                "pages": len(pages),
                "records_written": written,
# 【数据工程】解析操作完成后触发下游分块数据集消费
                "elapsed": round(elapsed, 2),
                "output": str(output_path),
                "coverage_confirmed": bool((manifest.get("confirmed") or {}).get("version")),
                "triggers": triggers,
            }
        except Exception as e:
            task["status"] = "解析失败"
            task["abnormal"] = True
            self.coverage.mark_run_failed(
                doc_id, task_id=task_id, task_key=task_key, reason=str(e))
            return {"task_id": task_id, "error": str(e), "doc_id": doc_id}

    def _expected_pages(self, requested_pages: list[int]) -> int:
        """整份文档的应有页数：页面范围上界与默认完整页数取大。

        显式页码上界代表值班人员声明的完整范围（如 1-8）；没有声明时按默认
        完整页数估算，保证覆盖视图能列出所有缺口。
        """
        declared = max(requested_pages, default=0)
        return max(declared, self.DEFAULT_TOTAL_PAGES)

    def _write_page_index(self, doc_id: str, records: list[dict[str, Any]]) -> None:
        """维护按页最新的文本索引，供版式还原等按整篇文档读取的模块使用。

        该索引只做“每页取最新”，确认与分块仍以覆盖清单的确认快照为准。
        """
        index_path = self._data_dir("parsed") / f"{doc_id}_parsed_index.json"
        index = self._read_json(index_path, {}) or {}
        pages = index.get("pages", {}) if isinstance(index, dict) else {}
        for rec in records:
            pages[str(rec["page_num"])] = rec
        self._write_json(index_path, {
            "doc_id": doc_id,
            "pages": pages,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _write_document_snapshot(self, doc_id: str) -> None:
        """把按页最新文本落成 ``<doc>_parsed.jsonl``，供版式还原等旧消费方读取。

        这是“每页取最新”的展示快照，不参与确认；分块一律走确认页组。
        """
        snapshot = self._pages_for_document(doc_id)
        self._overwrite_jsonl(self._data_dir("parsed") / f"{doc_id}_parsed.jsonl", snapshot)

    def _pages_for_document(self, doc_id: str) -> list[dict[str, Any]]:
        """按页返回文档最新解析文本，供下游按页构造产物（不改变页归属）。"""
        index = self._read_json(self._data_dir("parsed") / f"{doc_id}_parsed_index.json", {})
        if isinstance(index, dict) and index.get("pages"):
            records = list(index["pages"].values())
            return sorted(records, key=lambda r: int(r.get("page_num", 0)))
        # 兼容旧产物：按文档聚合文件
        legacy = self._read_jsonl(self._data_dir("parsed") / f"{doc_id}_parsed.jsonl")
        latest: dict[int, dict[str, Any]] = {}
        for rec in legacy:
            latest[int(rec.get("page_num", 0))] = rec
        return [latest[k] for k in sorted(latest)]

    # -- 页段覆盖清单对外入口 --

    def coverage_view(self, doc_id: str) -> dict[str, Any]:
        """覆盖视图：说明每页会进入哪次分块、还缺哪些页。"""
        return self.coverage.view(doc_id)

    def confirm_pages(self, doc_id: str, expected_pages: int | None = None,
                      decision: str = "确认分块", remark: str = "",
                      confirmed_by: str = "",
                      page_sources: dict[int, int] | None = None) -> dict[str, Any]:
        """确认当前页组用于分块；页组不完整时拒绝确认。

        ``page_sources`` 可指定某页采用哪个解析任务（默认取最新），用于补页更正后
        逐页挑选来源。
        """
        view = self.coverage.view(doc_id)
        manifest = self.coverage.load(doc_id)
        expected = expected_pages
        if expected is None:
            expected = int(
                (manifest.get("confirmed") or {}).get("expected_pages")
                or manifest.get("expected_pages")
                or view.get("expected_pages")
                or self.DEFAULT_TOTAL_PAGES
            )
        result = self.coverage.confirm(
            doc_id, expected_pages=int(expected), decision=decision,
            remark=remark, confirmed_by=confirmed_by, page_sources=page_sources)
        if result.get("ok"):
            result["chunk_trigger"] = self._trigger_chunk_split(doc_id)
        return result

    def reopen_confirmation(self, doc_id: str) -> dict[str, Any]:
        """重新打开最近一次确认，回查未采纳页的来源与待处理原因。"""
        return self.coverage.reopen(doc_id)

    def _simulate_pages(self, content: list[str], total_pages: int | None = None,
                        doc_id: str = "", corrected_pages: set[int] | None = None) -> list[dict[str, Any]]:
        """模拟将文本内容切分为多页文档，每页含段落、表格和图片区域标记。

        页文本按“文档+页号”确定性生成，跨任务重跑同一页内容稳定；
        ``corrected_pages`` 里的页会附加一处修订标记，用于核对覆盖视图里
        的内容摘要变化。页数取 ``total_pages``，默认沿用 3 页演示规模。
        """
        page_total = int(total_pages or 3)
        corrected = set(corrected_pages or ())
        pages = []
        for page_num in range(1, page_total + 1):
            base = self._simulate_doc_content(f"{doc_id or 'DOC'}-P{page_num}", paragraphs=3)
            if page_num in corrected:
                # 补页任务对该页的修正：同一页在不同任务下摘要不同，可核对采纳来源
                base = base + [f"【修订】第{page_num}页内容已由补页任务更正。"]
            pages.append({
                "page": page_num,
                "paragraphs": base,
                "text_length": sum(len(line) for line in base),
                "has_tables": page_num % 2 == 1,
                "has_images": page_num % 3 == 0,
            })
        return pages

    def _detect_table_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的表格区域边界并生成结构化输出标记。"""
        regions = []
        for p in pages:
            if p["has_tables"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*10}, {p['page']*8}, {p['page']*10+200}, {p['page']*8+80})",
                    "rows": 4,
                    "cols": 5,
                    "header_row": 1,
                    "merged_cells": 2,
                })
        return regions

    def _detect_image_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的图片区域边界并标记识别语言和精度要求。"""
        regions = []
        for p in pages:
            if p["has_images"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*5}, {p['page']*4+1}, {p['page']*5+150}, {p['page']*4+151})",
                    "language": "chinese",
                    "precision": "high",
                })
        return regions