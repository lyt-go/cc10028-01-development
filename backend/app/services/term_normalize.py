"""术语归一服务：抽取术语到标准术语的映射、同义词归并与消歧规则生效版本。

从实体识别结果读取术语并进行同义词归并和标准化映射，持久化术语映射表。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TermNormalizeService(BaseModuleService):
    MODULE = "term_normalize"
    ENTITY = "术语映射"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["映射编号", "源术语", "标准术语", "术语类别", "消歧规则", "同义词组", "生效版本", "映射来源"]
    REQUIRED_FIELDS = ["映射编号", "源术语", "标准术语"]
    STATUS_ORDER = ["草稿", "待映射", "已生效", "已废弃"]
    ACTION_RULES = {"提交映射": "待映射", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    TERM_DICTIONARY = {
        "AI": ["人工智能", "机器学习", "深度学习"],
        "知识抽取": ["信息抽取", "实体抽取", "知识获取"],
        "文档解析": ["文件解析", "文本解析", "版式识别"],
        "知识图谱": ["语义网络", "本体库", "图数据库"],
        "OCR": ["光学字符识别", "文字识别", "图像识别"],
    }

    def normalize_from_entities(self, entity_set_id: str) -> dict[str, Any]:
        """从实体识别结果进行术语归一化，将识别到的实体映射到标准术语并持久化。"""
        entity_file = self._data_dir("entities") / f"{entity_set_id}_entities.jsonl"
        entities = self._read_jsonl(entity_file)
        if not entities:
            return {"error": f"实体产物 {entity_set_id} 不存在，请先执行实体识别"}

        mappings = []
# 数据操作：标准化术语数据映射到标准术语数据集
        map_counter = 0
        seen_terms = set()

        for ent in entities:
            term_text = ent.get("entity_text", "")
            if term_text in seen_terms:
                continue
            seen_terms.add(term_text)

            # 在术语字典中查找同义词/标准术语
            for standard, synonyms in self.TERM_DICTIONARY.items():
                for syn in synonyms:
                    if syn in term_text or term_text in syn:
                        map_counter += 1
                        mappings.append({
                            "mapping_id": f"MAP-{map_counter:06d}",
                            "source_term": term_text,
                            "standard_term": standard,
                            "category": ent.get("entity_type", "TECH"),
                            "disambiguation": f"基于同义词组「{standard}」消歧匹配",
# 数据产物：术语映射数据集写入terms目录持久化文件
                            "synonym_group": synonyms,
                            "version": "v1.0",
                            "source": f"entity_{entity_set_id}",
                            "mapped_at": time.time(),
                        })
                        break

        # 对未匹配术语创建待确认映射
        for ent in entities:
            term_text = ent.get("entity_text", "")
            if not any(m["source_term"] == term_text for m in mappings):
                map_counter += 1
                mappings.append({
                    "mapping_id": f"MAP-{map_counter:06d}",
                    "source_term": term_text,
                    "standard_term": f"<待归一: {term_text[:8]}>",
                    "category": ent.get("entity_type", "OTHER"),
                    "disambiguation": "待人工确认标准术语映射",
                    "synonym_group": [],
                    "version": "v1.0-draft",
# 数据文件导入：外部字典文件批量更新术语映射数据集
                    "source": f"entity_{entity_set_id}",
                    "mapped_at": time.time(),
                })

        output_path = self._data_dir("terms") / f"{entity_set_id}_term_map.jsonl"
        written = self._write_jsonl(output_path, mappings)

        self._write_checkpoint(f"term_{entity_set_id}", {
            "entity_set_id": entity_set_id,
            "total_terms": len(seen_terms),
            "mapped": sum(1 for m in mappings if "<待归一" not in m["standard_term"]),
            "unmapped": sum(1 for m in mappings if "<待归一" in m["standard_term"]),
        })

        return {
            "entity_set_id": entity_set_id,
            "total_terms": len(seen_terms),
            "mappings": len(mappings),
            "records_written": written,
            "output": str(output_path),
            "mapped_count": sum(1 for m in mappings if "<待归一" not in m["standard_term"]),
        }

    def batch_apply_dictionary(self, terms: list[str]) -> list[dict[str, Any]]:
        """批量对术语列表应用字典匹配，返回映射清单。"""
        results = []
        for term in terms:
            mapped = None
            for standard, synonyms in self.TERM_DICTIONARY.items():
                if any(syn in term for syn in synonyms):
                    mapped = {"source": term, "standard": standard, "status": "已匹配"}
                    break
            if not mapped:
                mapped = {"source": term, "standard": None, "status": "待映射"}
            results.append(mapped)
        return results