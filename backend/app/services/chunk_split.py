"""分块切分服务：文档文本按语义边界的分块切分、重叠窗口保留与检索标签标注。

分块只从“页段覆盖清单”的确认页组读取页数据：每个确认页归到具体解析任务，
切出的分块记录页号、任务编号与内容摘要，可回查原解析记录。补页任务尚未确认时
不会用半份页组覆盖历史分块。
"""
from __future__ import annotations

import time
from typing import Any

from app.domain.page_coverage import PageCoverageManifest
from app.services.base import BaseModuleService
from app.store import store


class ChunkSplitService(BaseModuleService):
    MODULE = "chunk_split"
    ENTITY = "文本分块"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["分块编号", "文档编号", "分块长度", "重叠窗口", "切分边界", "分块序号", "检索标签", "切分策略"]
    REQUIRED_FIELDS = ["分块编号", "文档编号", "分块长度"]
    STATUS_ORDER = ["草稿", "待切分", "已切分", "切分异常"]
    ACTION_RULES = {"启动切分": "待切分", "确认切分": "已切分", "标记异常": "切分异常"}
    NEGATIVE_ACTIONS = ["标记异常"]

    STRATEGY_PRESETS = {
        "semantic": {"description": "按语义边界切分，保留完整句子和段落结构"},
        "fixed": {"description": "按固定长度切分，超出部分截断"},
        "sliding": {"description": "滑动窗口切分，保留重叠区以确保上下文连贯"},
    }

    def __init__(self) -> None:
        self.coverage = PageCoverageManifest(self)

    @staticmethod
    def _doc_id_of(parsed_doc_id: str) -> str:
        """把 ``CHUNK-..._parsed`` 形式的集合键还原为文档编号。"""
        key = (parsed_doc_id or "").strip()
        return key[:-len("_parsed")] if key.endswith("_parsed") else key

    def split_from_document(self, parsed_doc_id: str, *, allow_unconfirmed: bool = True) -> dict[str, Any]:
        """从确认页组读取选中页并切分为文本分块，记录页归属后触发实体识别和向量嵌入。

        页数据来源优先级：

        1. 覆盖清单存在确认页组 → 只读确认页组，一个任务一次确认，不混入新补页；
        2. 尚无确认页组 → 只接受“某一次完整解析”覆盖整份文档的产物；
           补页任务只跑部分页时拒绝切分，不拿半份结果替掉历史分块。
        """
        doc_id = self._doc_id_of(parsed_doc_id)
        chunk_set_id = f"{doc_id}_parsed"
        manifest = self.coverage.load(doc_id)
        confirmed = manifest.get("confirmed") or {}
        confirmed_group = self.coverage.confirmed_pages(manifest)

        if confirmed_group:
            page_records, page_group_version, missing_artifacts = self._records_from_confirmed(
                doc_id, confirmed_group)
            coverage_mode = f"确认页组 v{confirmed.get('version')}"
            if missing_artifacts:
                return {
                    "error": f"确认页组中第 {'、'.join(map(str, missing_artifacts))} 页的原解析产物缺失，"
                             f"无法回查原解析记录；请重新执行解析或重新确认",
                    "missing_artifacts": missing_artifacts,
                }
            if not page_records:
                return {"error": f"文档 {doc_id} 的确认页组无可用页产物，请重新确认或补页"}
        else:
            page_records, run = self._records_from_complete_run(doc_id)
            page_group_version = None
            coverage_mode = "未确认（单次完整解析）"
            if not page_records:
                present = sorted(int(p) for p in manifest.get("pages", {}))
                missing = self._missing_pages(manifest, [{"page_num": p} for p in present])
                if not present:
                    return {"error": f"解析产物 {parsed_doc_id} 不存在，请先执行格式解析"}
                # 只有部分页被重跑，或缺页：不能写出半份分块
                return {
                    "error": f"文档 {doc_id} 尚无整份确认页组，且现有页来自多次解析"
                             f"（已覆盖第 {'、'.join(map(str, present))} 页，"
                             f"尚缺第 {'、'.join(map(str, missing)) or '无'} 页），"
                             f"未写入分块以免覆盖历史数据",
                    "missing_pages": missing,
                    "present_pages": present,
                    "pending": True,
                }
            if not allow_unconfirmed:
                return {"error": f"文档 {doc_id} 尚未确认页组，不允许切分"}

        strategy = "semantic"
        chunk_size = 512
        overlap = 50

        chunks = []
        chunk_seq = 0
        for rec in page_records:
            page_text = rec.get("text") or ""
            if not page_text:
                return {"error": f"第 {rec.get('page_num')} 页解析产物无文本内容，无法切分"}
            # 按句子边界切分
            sentences = page_text.replace("。", "。\n").split("\n")
            current_chunk = ""
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if len(current_chunk) + len(sent) > chunk_size:
                    if current_chunk:
                        chunk_seq += 1
                        chunks.append(self._build_chunk(
                            rec, chunk_seq, current_chunk, overlap, strategy,
                            page_group_version))
                    # 保留重叠窗口
                    overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else ""
                    current_chunk = overlap_text + sent
# 数据产物：分块数据集写入chunks目录持久化文件，同时触发下游实体数据集与向量索引
                else:
                    current_chunk += sent if not current_chunk else " " + sent
            # 处理最后一段
            if current_chunk:
                chunk_seq += 1
                chunks.append(self._build_chunk(
                    rec, chunk_seq, current_chunk, overlap, strategy,
                    page_group_version))

        output_path = self._data_dir("chunks") / f"{chunk_set_id}_chunks.jsonl"
        written = self._overwrite_jsonl(output_path, chunks)

        # 写入检查点
        self._write_checkpoint(f"chunk_{chunk_set_id}", {
            "parsed_doc_id": chunk_set_id,
            "doc_id": doc_id,
            "chunks_count": len(chunks),
            "pages": [r["page_num"] for r in page_records],
            "page_group_version": page_group_version,
            "coverage_mode": coverage_mode,
            "strategy": strategy,
            "total_length": sum(c["length"] for c in chunks),
            "decision_boundary": "按语义边界切分是保持上下文完整性的最佳实践",
        })

# 数据产物：标签检索分块数据集按标签过滤定位分块条目
        # 跨模块自动触发：实体识别和向量嵌入
        triggers = {}
        if chunk_set_id:
            trig_entity = self._trigger_entity_recognize(chunk_set_id)
            triggers["entity_recognize"] = trig_entity
            trig_vector = self._trigger_vector_embed(chunk_set_id)
            triggers["vector_embed"] = trig_vector

        return {
            "parsed_doc_id": chunk_set_id,
            "doc_id": doc_id,
            "chunks": len(chunks),
            "pages": [r["page_num"] for r in page_records],
            "coverage_mode": coverage_mode,
            "page_group_version": page_group_version,
            "records_written": written,
            "strategy": strategy,
            "output": str(output_path),
            "page_sources": [
                {
                    "page_num": r["page_num"],
                    "task_id": r.get("task_id"),
                    "task_key": r.get("task_key"),
                    "parsed_file": r.get("parsed_file"),
                    "content_hash": r.get("content_hash"),
                }
                for r in page_records
            ],
            "triggers": triggers,
        }

    def _build_chunk(self, rec: dict[str, Any], seq: int, text: str, overlap: int,
                     strategy: str, page_group_version: int | None) -> dict[str, Any]:
        """构造分块记录，附页号与来源解析任务，便于回查原解析记录。"""
        doc_id = rec.get("doc_id", "")
        return {
            "chunk_id": f"CHUNK-{doc_id}-{seq:04d}",
            "doc_id": doc_id,
            "page": rec.get("page_num", 1),
            "seq": seq,
            "length": len(text),
            "overlap": overlap,
            "boundary": "sentence" if strategy == "semantic" else "fixed",
            "tags": self._generate_tags(text),
            "text": text,
            "strategy": strategy,
            # 页归属：回查原解析记录
            "task_id": rec.get("task_id"),
            "task_key": rec.get("task_key", ""),
            "run_id": str(rec.get("task_id")) if rec.get("task_id") is not None else "",
            "parsed_file": rec.get("parsed_file", ""),
            "content_hash": rec.get("content_hash", ""),
            "page_group_version": page_group_version,
            "split_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _records_from_confirmed(self, doc_id: str,
                                confirmed_group: dict[int, dict[str, Any]]
                                ) -> tuple[list[dict[str, Any]], int, list[int]]:
        """按确认页组逐页读取原解析产物，返回该次确认冻结的页记录。

        页归属必须能回查原解析记录：原产物缺失时记录缺失页并拒绝切分，
        不退化成无来源的模拟文本。
        """
        records: list[dict[str, Any]] = []
        missing: list[int] = []
        version = 0
        manifest = self.coverage.load(doc_id)
        version = int((manifest.get("confirmed") or {}).get("version", 0))
        for page_num in sorted(confirmed_group):
            page = confirmed_group[page_num]
            parsed_file = page.get("parsed_file") or ""
            path = self._data_dir("parsed") / parsed_file if parsed_file else None
            record = None
            if path is not None and path.exists():
                for candidate in self._read_jsonl(path):
                    if int(candidate.get("page_num", 0)) == page_num:
                        record = candidate
                        break
            if record is None:
                missing.append(page_num)
                continue
            record = dict(record)
            record.setdefault("task_id", page.get("task_id"))
            record.setdefault("task_key", page.get("task_key", ""))
            record.setdefault("parsed_file", parsed_file)
            record.setdefault("content_hash", page.get("content_hash", ""))
            # 确认快照里的摘要才是该次分块依据的版本，覆盖产物里的最新值
            record["content_hash"] = page.get("content_hash", record.get("content_hash", ""))
            records.append(record)
        return records, version, missing

    def _records_from_complete_run(self, doc_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """尚无确认页组时，仅接受“单次完整解析”覆盖整份文档的运行。

        多次解析并存（例如首轮 1-8 页 + 补页 4-6 页）说明页组尚未确认，
        此时返回空，由调用方拒绝切分，避免混任务或半份结果覆盖历史分块。
        """
        manifest = self.coverage.load(doc_id)
        completed = [
            run for run in manifest.get("runs", {}).values()
            if run.get("status") == "已完成" and run.get("pages")
        ]
        expected = max(
            int(manifest.get("expected_pages") or 0),
            self._expected_total(manifest),
        )
        if len(completed) != 1 or not expected:
            return [], None
        run = completed[0]
        if sorted(int(p) for p in run["pages"]) != list(range(1, expected + 1)):
            return [], None
        path = self._data_dir("parsed") / (run.get("parsed_file") or "")
        if not path.exists():
            return [], None
        records = sorted(self._read_jsonl(path), key=lambda r: int(r.get("page_num", 0)))
        for rec in records:
            rec.setdefault("task_id", run.get("task_id"))
            rec.setdefault("task_key", run.get("task_key", ""))
            rec.setdefault("parsed_file", run.get("parsed_file", ""))
        return records, run

    def _missing_pages(self, manifest: dict[str, Any],
                       records: list[dict[str, Any]]) -> list[int]:
        """计算尚缺页号：以清单登记的整份页数为准，缺的页都要补齐。"""
        expected = max(
            int((manifest.get("confirmed") or {}).get("expected_pages") or 0),
            int(manifest.get("expected_pages") or 0),
            self._expected_total(manifest),
        )
        present = {int(r.get("page_num", 0)) for r in records}
        return [p for p in range(1, expected + 1) if p not in present]

    @staticmethod
    def _expected_total(manifest: dict[str, Any]) -> int:
        """清单里登记过的运行页码最大值，用于判断还缺哪些页。"""
        pages = {int(p) for p in manifest.get("pages", {})}
        return max(pages, default=0)


    def _generate_tags(self, text: str) -> list[str]:
        """基于文本内容生成检索标签，识别文本中的关键实体类别。"""
        tags = []
# 数据边界：切分操作处理空分块与缺失产物异常边界数据
        keywords = {
            "企业": ["公司", "集团", "企业", "有限公司"],
            "人员": ["张", "博士", "科学家", "工程师", "经理"],
            "时间": ["2024年", "季度", "月", "日"],
            "地点": ["Beijing", "Shanghai", "总部", "分部"],
            "金额": ["亿元", "万元", "投资"],
            "技术": ["知识图谱", "人工智能", "技术", "系统"],
        }
        for tag, words in keywords.items():
            if any(w in text for w in words):
                tags.append(tag)
        return tags if tags else ["通用"]