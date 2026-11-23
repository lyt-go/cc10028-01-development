"""页段覆盖清单：同一文档多次解析任务的页级归属、确认与分块取数依据。

设计要点：

* 每次解析任务单独登记一条 ``run``，记录该次覆盖的页段、产物文件、页内容摘要。
* 页状态按 **最新一次确认的页组** 快照冻结。补页任务与历史任务并发时，
  只有一次“完整确认”才会把整组页写入 ``confirmed``，分块只读这份快照，
  不会因为另一个任务正在解析就拿到半份结果，也不会覆盖历史已确认数据。
* 覆盖视图还会给出“下一次确认的候选页组”：补页任务只覆盖第 4 至 6 页时，
  视图说明第 5 页将来会进入哪次分块，以及还需要从其他任务补齐哪些页。
* 未被采纳的页（例如同一页存在其他任务的产物、或还没有任何解析产物的页）
  保留来源任务与待处理原因，重新打开确认时仍可回查。
* 同一文档的清单读写用进程内锁串行化，避免并发登记互相覆盖。
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅用于类型标注，避免与 app.services 形成循环导入
    from app.services.base import BaseModuleService

MANIFEST_VERSION = 1

# 页在确认快照里的归属状态
PAGE_ACCEPTED = "已采纳"
PAGE_PENDING = "待补齐"
PAGE_EXCLUDED = "未采纳"

# 单个解析任务在覆盖清单里的运行状态
RUN_COMPLETED = "已完成"
RUN_FAILED = "解析失败"

# 同一文档的清单读改写必须串行；服务按请求实例化，所以锁放在进程级。
_DOC_LOCKS: dict[str, threading.RLock] = {}
_DOC_LOCKS_GUARD = threading.Lock()


def _lock_for(doc_id: str) -> threading.RLock:
    with _DOC_LOCKS_GUARD:
        lock = _DOC_LOCKS.get(doc_id)
        if lock is None:
            lock = threading.RLock()
            _DOC_LOCKS[doc_id] = lock
        return lock


class PageCoverageManifest:
    """页段覆盖清单的持久化读写与覆盖视图拼装。"""

    SUB_DIR = "coverage"

    def __init__(self, io: "BaseModuleService") -> None:
        # 借用基础服务的文件IO（_data_dir/_read_json/_write_json），
        # 避免清单模块重复实现目录约定。
        self._io = io

    # -- 路径与读写 --

    def _path(self, doc_id: str):
        return self._io._data_dir(self.SUB_DIR) / f"{doc_id}_pages.json"

    def load(self, doc_id: str) -> dict[str, Any]:
        """读取文档的页段覆盖清单；不存在时返回空骨架。"""
        with _lock_for(doc_id):
            return self._load_unlocked(doc_id)

    def _load_unlocked(self, doc_id: str) -> dict[str, Any]:
        payload = self._io._read_json(self._path(doc_id))
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("doc_id", doc_id)
        payload.setdefault("manifest_version", MANIFEST_VERSION)
        payload.setdefault("runs", {})
        payload.setdefault("pages", {})
        payload.setdefault("confirmed", None)
        payload.setdefault("expected_pages", 0)
        payload.setdefault("updated_at", None)
        return payload

    def save(self, doc_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """整体落盘清单；写入带临时文件替换，避免读到半份 JSON。"""
        with _lock_for(doc_id):
            return self._save_unlocked(doc_id, manifest)

    def _save_unlocked(self, doc_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest["doc_id"] = doc_id
        manifest["manifest_version"] = MANIFEST_VERSION
        manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._io._write_json(self._path(doc_id), manifest)
        return manifest

    # -- 解析任务登记 --

    def register_run(
        self,
        doc_id: str,
        *,
        task_id: int,
        task_key: str,
        page_range: str,
        pages: list[dict[str, Any]],
        parsed_file: str,
        status: str = RUN_COMPLETED,
        output_format: str = "jsonl",
        expected_pages: int | None = None,
        corrected_pages: list[int] | None = None,
    ) -> dict[str, Any]:
        """登记一次解析任务的页产物。

        ``pages`` 为本次实际写出的页记录，必须含 ``page_num`` 与 ``content_hash``。
        同一文档多次登记按任务维度追加，不覆盖历史运行。``expected_pages``
        表示整份文档应有页数，用于覆盖视图提示还缺哪些页；只会增大不会缩小。
        """
        with _lock_for(doc_id):
            manifest = self._load_unlocked(doc_id)
            run_key = str(task_id)
            corrected = set(int(p) for p in (corrected_pages or []))
            # 同一任务重跑：先清掉该任务之前的页登记，避免同页重复计数
            for page_key, entries in list(manifest.get("pages", {}).items()):
                kept = [e for e in entries if int(e.get("task_id", 0)) != task_id]
                if kept:
                    manifest["pages"][page_key] = kept
                else:
                    manifest["pages"].pop(page_key, None)
            page_entries: list[dict[str, Any]] = []
            for page in sorted(pages, key=lambda p: int(p.get("page_num", 0))):
                page_num = int(page["page_num"])
                entry = {
                    "page_num": page_num,
                    "task_id": task_id,
                    "task_key": task_key,
                    "run_id": run_key,
                    "parsed_file": parsed_file,
                    "content_hash": page.get("content_hash", ""),
                    "text_length": page.get("text_length", 0),
                    "parser": page.get("parser", ""),
                    "status": status,
                    "corrected": page_num in corrected,
                }
                page_entries.append(entry)
                manifest["pages"].setdefault(str(page_num), []).append(entry)

            manifest["runs"][run_key] = {
                "run_id": run_key,
                "task_id": task_id,
                "task_key": task_key,
                "page_range": page_range,
                "pages": [entry["page_num"] for entry in page_entries],
                "parsed_file": parsed_file,
                "status": status,
                "output_format": output_format,
                "corrected_pages": sorted(corrected),
                "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if expected_pages:
                manifest["expected_pages"] = max(
                    int(manifest.get("expected_pages") or 0),
                    int(expected_pages),
                    max((entry["page_num"] for entry in page_entries), default=0),
                )
            return self._save_unlocked(doc_id, manifest)

    def mark_run_failed(self, doc_id: str, *, task_id: int, task_key: str, reason: str) -> dict[str, Any]:
        """登记一次失败的解析任务：只留来源与原因，不产生可采纳页。"""
        with _lock_for(doc_id):
            manifest = self._load_unlocked(doc_id)
            manifest["runs"][str(task_id)] = {
                "run_id": str(task_id),
                "task_id": task_id,
                "task_key": task_key,
                "page_range": "",
                "pages": [],
                "parsed_file": "",
                "status": RUN_FAILED,
                "output_format": "",
                "reason": reason,
                "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            return self._save_unlocked(doc_id, manifest)

    # -- 页归属解析 --

    @staticmethod
    def latest_page_entry(manifest: dict[str, Any], page_num: int) -> dict[str, Any] | None:
        """返回某页最新一次解析登记（用于说明该页当前会进入哪次分块）。"""
        entries = manifest.get("pages", {}).get(str(page_num), [])
        if not entries:
            return None
        return max(entries, key=lambda e: int(e.get("task_id", 0)))

    def confirmed_pages(self, manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """把确认快照展开为 {页号: 页归属}，无确认时返回空字典。"""
        confirmed = manifest.get("confirmed")
        if not isinstance(confirmed, dict):
            return {}
        result: dict[int, dict[str, Any]] = {}
        for page in confirmed.get("pages", []):
            if page.get("status") == PAGE_ACCEPTED:
                result[int(page["page_num"])] = page
        return result

    # -- 确认 --

    def confirm(
        self,
        doc_id: str,
        *,
        expected_pages: int,
        decision: str = "确认分块",
        remark: str = "",
        confirmed_by: str = "",
        page_sources: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        """把当前已有页一次性冻结为“确认页组”。

        只有 ``expected_pages`` 范围内的页都有解析产物时才会覆盖历史确认；
        否则拒绝确认并返回缺口，避免半份结果替掉历史数据。
        ``page_sources`` 允许值班人员指定某页采用哪个解析任务的结果，默认取最新。
        """
        with _lock_for(doc_id):
            manifest = self._load_unlocked(doc_id)
            expected = max(int(expected_pages), 0)
            wanted = {int(k): int(v) for k, v in (page_sources or {}).items()}
            accepted: list[dict[str, Any]] = []
            missing: list[int] = []

            for page_num in range(1, expected + 1):
                entry = self._page_entry_for(
                    manifest, page_num, wanted.get(page_num))
                if entry is None:
                    missing.append(page_num)
                    continue
                accepted.append({
                    "page_num": page_num,
                    "status": PAGE_ACCEPTED,
                    "task_id": entry["task_id"],
                    "task_key": entry["task_key"],
                    "run_id": entry["run_id"],
                    "parsed_file": entry["parsed_file"],
                    "content_hash": entry["content_hash"],
                    "text_length": entry["text_length"],
                    "corrected": bool(entry.get("corrected")),
                })

            if missing:
                return {
                    "ok": False,
                    "reason": "页组不完整，不能确认分块，避免半份结果替掉历史数据",
                    "missing_pages": missing,
                    "expected_pages": expected,
                    "accepted_pages": [p["page_num"] for p in accepted],
                }

            previous = manifest.get("confirmed")
            version = int((previous or {}).get("version", 0)) + 1
            adopted = {(int(p["page_num"]), int(p["task_id"])) for p in accepted}
            excluded = self._build_excluded(manifest, adopted)
            snapshot = {
                "version": version,
                "doc_id": doc_id,
                "expected_pages": expected,
                "pages": accepted,
                "excluded": excluded,
                "decision": decision,
                "remark": remark,
                "confirmed_by": confirmed_by,
                "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "page_count": len(accepted),
                "superseded_version": (previous or {}).get("version"),
            }
            manifest["confirmed"] = snapshot
            manifest.setdefault("confirm_history", []).append({
                "version": version,
                "expected_pages": expected,
                "page_count": len(accepted),
                "decision": decision,
                "remark": remark,
                "confirmed_by": confirmed_by,
                "confirmed_at": snapshot["confirmed_at"],
                "pages": [
                    {
                        "page_num": p["page_num"],
                        "task_id": p["task_id"],
                        "task_key": p["task_key"],
                        "content_hash": p["content_hash"],
                        "corrected": p["corrected"],
                    }
                    for p in accepted
                ],
            })
            self._save_unlocked(doc_id, manifest)
            return {"ok": True, "confirmed": snapshot}

    @staticmethod
    def _page_entry_for(manifest: dict[str, Any], page_num: int,
                        task_id: int | None) -> dict[str, Any] | None:
        """取指定页的解析登记：默认最新，指定任务时只认该任务的产物。"""
        entries = manifest.get("pages", {}).get(str(page_num), [])
        if not entries:
            return None
        if task_id is not None:
            matched = [e for e in entries if int(e.get("task_id", 0)) == int(task_id)]
            if not matched:
                return None
            entries = matched
        return max(entries, key=lambda e: int(e.get("task_id", 0)))

    def _build_excluded(self, manifest: dict[str, Any],
                        adopted: set[tuple[int, int]]) -> list[dict[str, Any]]:
        """确认后未被采纳的页记录：来源任务与待处理原因，供重新打开回查。"""
        adopted_by_page = {page for page, _ in adopted}
        excluded: list[dict[str, Any]] = []
        for page_key, entries in sorted(manifest.get("pages", {}).items(), key=lambda kv: int(kv[0])):
            page_num = int(page_key)
            for entry in entries:
                if (page_num, int(entry.get("task_id", 0))) in adopted:
                    continue
                reason = (
                    "该页的其他解析任务产物未被本次确认采纳，等待下一次确认"
                    if page_num in adopted_by_page else
                    "该页不在本次确认的页段范围或尚无可用产物，等待补页"
                )
                excluded.append({
                    "page_num": page_num,
                    "status": PAGE_EXCLUDED,
                    "reason": reason,
                    "source_task_id": entry.get("task_id"),
                    "source_task_key": entry.get("task_key", ""),
                    "source_run_id": entry.get("run_id"),
                    "source_content_hash": entry.get("content_hash", ""),
                })
        return sorted(excluded, key=lambda p: (int(p["page_num"]), int(p.get("source_task_id") or 0)))

    # -- 覆盖视图 --

    def view(self, doc_id: str) -> dict[str, Any]:
        """生成覆盖比对视图，供值班人员确认进入分块的页。"""
        with _lock_for(doc_id):
            manifest = self._load_unlocked(doc_id)
        return self._view_unlocked(manifest)

    def _view_unlocked(self, manifest: dict[str, Any]) -> dict[str, Any]:
        doc_id = manifest["doc_id"]
        confirmed = manifest.get("confirmed") or {}
        accepted = self.confirmed_pages(manifest)
        runs = sorted(manifest.get("runs", {}).values(), key=lambda r: int(r.get("task_id", 0)))
        all_pages = sorted({int(p) for p in manifest.get("pages", {})})
        expected = int(
            confirmed.get("expected_pages")
            or manifest.get("expected_pages")
            or (max(all_pages) if all_pages else 0)
        )

        # 下一次确认的候选页组：每页取最新解析产物，再整体确认一次。
        # 候选页组不要求来自同一个任务——补页任务的页可以与首轮任务的页合并，
        # 但必须“整份齐全”后才允许确认，避免半份页组进入分块。
        latest_by_page = {
            page_num: self.latest_page_entry(manifest, page_num)
            for page_num in range(1, expected + 1)
        }
        candidate_pages = [p for p in range(1, expected + 1) if latest_by_page.get(p)]
        pages_missing = [p for p in range(1, expected + 1) if latest_by_page.get(p) is None]
        candidate_sources: list[dict[str, Any]] = []
        for page_num in candidate_pages:
            entry = latest_by_page[page_num]
            candidate_sources.append({
                "page_num": page_num,
                "task_id": entry.get("task_id"),
                "task_key": entry.get("task_key", ""),
                "content_hash": entry.get("content_hash", ""),
                "corrected": bool(entry.get("corrected")),
            })
        next_version = int(confirmed.get("version", 0)) + 1
        ready_to_confirm = bool(expected) and not pages_missing

        rows: list[dict[str, Any]] = []
        for page_num in range(1, expected + 1):
            accepted_page = accepted.get(page_num)
            latest = latest_by_page.get(page_num)
            # 若下一版确认仍会采用同一任务产物，说明某页在下一版里的归属已确定
            next_owner = latest if latest is not None else None
            next_owner_label = (
                f"第{next_version}次确认页组（候选，任务 {next_owner.get('task_key')}）"
                if next_owner is not None else
                f"暂无（待补齐第 {'、'.join(map(str, pages_missing))} 页后确认）"
            )
            if accepted_page is not None:
                updated = (
                    latest is not None
                    and int(latest.get("task_id", 0)) != int(accepted_page.get("task_id", 0))
                )
                rows.append({
                    "page_num": page_num,
                    "coverage": "已确认",
                    "run_id": accepted_page.get("run_id"),
                    "task_id": accepted_page.get("task_id"),
                    "task_key": accepted_page.get("task_key"),
                    "content_hash": accepted_page.get("content_hash"),
                    "parsed_file": accepted_page.get("parsed_file", ""),
                    "latest_task_id": latest.get("task_id") if latest else None,
                    "latest_task_key": latest.get("task_key", "") if latest else "",
                    "latest_content_hash": latest.get("content_hash", "") if latest else "",
                    "has_pending_update": updated,
                    "chunk_target": f"第{confirmed.get('version')}次确认页组（当前分块）",
                    "next_chunk_target": next_owner_label if updated or ready_to_confirm else
                                         f"第{confirmed.get('version')}次确认页组（保持）",
                    "reason": (
                        "该页已有更新解析产物，但尚未通过确认，当前分块仍用已确认版本"
                        if updated else ""
                    ),
                })
            elif latest is not None:
                rows.append({
                    "page_num": page_num,
                    "coverage": "待确认",
                    "run_id": latest.get("run_id"),
                    "task_id": latest.get("task_id"),
                    "task_key": latest.get("task_key"),
                    "content_hash": latest.get("content_hash"),
                    "parsed_file": latest.get("parsed_file", ""),
                    "latest_task_id": latest.get("task_id"),
                    "latest_task_key": latest.get("task_key", ""),
                    "latest_content_hash": latest.get("content_hash", ""),
                    "has_pending_update": False,
                    "chunk_target": (
                        f"第{confirmed.get('version')}次确认页组（未含此页）"
                        if confirmed else "暂无（整份页组尚未确认）"
                    ),
                    "next_chunk_target": next_owner_label,
                    "reason": "已有解析产物，需整份页组齐全并确认后才会进入分块",
                })
            else:
                rows.append({
                    "page_num": page_num,
                    "coverage": "待补齐",
                    "run_id": None,
                    "task_id": None,
                    "task_key": "",
                    "content_hash": "",
                    "parsed_file": "",
                    "latest_task_id": None,
                    "latest_task_key": "",
                    "latest_content_hash": "",
                    "has_pending_update": False,
                    "chunk_target": "暂无（尚无解析产物）",
                    "next_chunk_target": f"暂无（待补齐第 {'、'.join(map(str, pages_missing))} 页后确认）",
                    "reason": "尚无任何解析任务覆盖该页",
                })

        return {
            "doc_id": doc_id,
            "expected_pages": expected,
            "confirmed": {
                "version": confirmed.get("version"),
                "page_count": len(accepted),
                "pages": sorted(accepted),
                "decision": confirmed.get("decision", ""),
                "remark": confirmed.get("remark", ""),
                "confirmed_by": confirmed.get("confirmed_by", ""),
                "confirmed_at": confirmed.get("confirmed_at"),
            } if confirmed else None,
            "pending_group": {
                "next_version": next_version if confirmed else 1,
                "candidate_pages": candidate_pages,
                "pages_missing": pages_missing,
                "sources": candidate_sources,
                "ready_to_confirm": ready_to_confirm,
                "summary": (
                    "整份页组已齐全，可确认进入分块"
                    if ready_to_confirm else
                    f"尚缺第 {'、'.join(map(str, pages_missing))} 页，补齐并确认后才可进入分块"
                    if pages_missing else
                    "该文档尚无解析产物，请先执行解析任务"
                ),
            },
            "missing_pages": pages_missing,
            "confirmed_missing_pages": [
                p for p in range(1, expected + 1) if p not in accepted
            ],
            "complete": bool(confirmed) and len(accepted) >= expected,
            "has_unconfirmed_updates": any(row["has_pending_update"] for row in rows),
            "pages": rows,
            "runs": runs,
        }

    # -- 重新打开确认 --

    def reopen(self, doc_id: str) -> dict[str, Any]:
        """重新打开最近一次确认：返回被采纳与未被采纳的页及各自来源。"""
        with _lock_for(doc_id):
            manifest = self._load_unlocked(doc_id)
        confirmed = manifest.get("confirmed")
        if not isinstance(confirmed, dict):
            return {"ok": False, "reason": "该文档尚无确认记录"}

        accepted = confirmed.get("pages", [])
        adopted = {(int(p["page_num"]), int(p["task_id"])) for p in accepted}
        adopted_by_page = {page for page, _ in adopted}
        unadopted: list[dict[str, Any]] = []
        for page_key, entries in sorted(manifest.get("pages", {}).items(), key=lambda kv: int(kv[0])):
            for entry in entries:
                if (int(page_key), int(entry.get("task_id", 0))) in adopted:
                    continue
                unadopted.append({
                    "page_num": int(page_key),
                    "status": PAGE_EXCLUDED,
                    "reason": (
                        "该页的其他解析任务产物未被本次确认采纳，等待下一次确认"
                        if int(page_key) in adopted_by_page else
                        "该页不在本次确认的页段范围或尚无可用产物，等待补页"
                    ),
                    "source_task_id": entry.get("task_id"),
                    "source_task_key": entry.get("task_key", ""),
                    "source_run_id": entry.get("run_id"),
                    "source_content_hash": entry.get("content_hash", ""),
                })
        for page_num in range(1, int(confirmed.get("expected_pages") or 0) + 1):
            if page_num in {int(p["page_num"]) for p in accepted}:
                continue
            if str(page_num) in manifest.get("pages", {}):
                continue
            unadopted.append({
                "page_num": page_num,
                "status": PAGE_PENDING,
                "reason": "尚无任何解析产物，等待补页",
                "source_task_id": None,
                "source_task_key": "",
                "source_run_id": None,
                "source_content_hash": "",
            })

        return {
            "ok": True,
            "doc_id": doc_id,
            "confirmed": {
                "version": confirmed.get("version"),
                "expected_pages": confirmed.get("expected_pages"),
                "page_count": confirmed.get("page_count"),
                "decision": confirmed.get("decision", ""),
                "remark": confirmed.get("remark", ""),
                "confirmed_by": confirmed.get("confirmed_by", ""),
                "confirmed_at": confirmed.get("confirmed_at"),
            },
            "accepted_pages": accepted,
            "excluded_pages": sorted(confirmed.get("excluded", []), key=lambda p: int(p.get("page_num", 0))),
            "unadopted_pages": sorted(unadopted, key=lambda p: (int(p["page_num"]), int(p.get("source_task_id") or 0))),
            "runs": sorted(manifest.get("runs", {}).values(), key=lambda r: int(r.get("task_id", 0))),
        }
