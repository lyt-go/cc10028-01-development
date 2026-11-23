"""页段覆盖清单与分块取数集成测试。

覆盖两条任务核对场景：
1. 初次完成第 1-8 页，确认后分块；第二次只重跑第 4-6 页且更正第 5 页。
2. 补页尚未代表一整份文档时，覆盖视图说明第 5 页会进入哪次分块、还缺哪些页；
   确认后分块只取一次完整确认的页组，不把半份补页结果替掉历史数据。
"""
import json
import threading
import uuid

from app.domain.page_coverage import PageCoverageManifest
from app.services.base import DATA_ROOT
from app.services.chunk_split import ChunkSplitService
from app.services.format_parse import FormatParseService


def _doc() -> str:
    return f"COV-{uuid.uuid4().hex[:8].upper()}"


def _task(doc_id: str, task_key: str, page_range: str) -> dict:
    task, missing = FormatParseService().create_entry({
        "任务编号": task_key,
        "文档编号": doc_id,
        "解析器类型": "PDF",
        "页码范围": page_range,
        "解析模式": "full",
        "输出格式": "jsonl",
        "超时秒数": "300",
        "解析器版本": "v2.1.0",
    })
    assert task is not None, missing
    return task


def _chunks(doc_id: str) -> list[dict]:
    path = DATA_ROOT / "chunks" / f"{doc_id}_parsed_chunks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestCoverageFirstPass:
    def test_first_pass_fills_one_to_eight(self):
        doc = _doc()
        svc = FormatParseService()
        task = _task(doc, f"T1-{doc}", "1-8")
        result = svc.execute_parse(task["id"])
        assert "error" not in result, result
        assert result["page_range"] == "1-8"
        assert result["pages"] == 8

        view = svc.coverage_view(doc)
        assert view["expected_pages"] == 8
        assert view["confirmed"] is None
        assert [row["page_num"] for row in view["pages"]] == list(range(1, 9))
        # 首轮就是整份覆盖：待确认页指向下一次确认页组
        page5 = next(row for row in view["pages"] if row["page_num"] == 5)
        assert page5["task_id"] == task["id"]
        assert page5["coverage"] == "待确认"
        assert "第1次确认页组" in page5["next_chunk_target"]

    def test_partial_first_pass_reports_missing_pages(self):
        doc = _doc()
        svc = FormatParseService()
        svc.execute_parse(_task(doc, f"T1-{doc}", "1-3")["id"])
        view = svc.coverage_view(doc)
        assert view["missing_pages"] == [4, 5, 6, 7, 8]
        assert view["pending_group"]["ready_to_confirm"] is False
        assert "4、5、6、7、8" in view["pending_group"]["summary"]

        rejected = svc.confirm_pages(doc, expected_pages=8)
        assert rejected["ok"] is False
        assert rejected["missing_pages"] == [4, 5, 6, 7, 8]
        assert not (DATA_ROOT / "chunks" / f"{doc}_parsed_chunks.jsonl").exists()


class TestCoverageRerunFourthToSixth:
    def _prepare_confirmed(self, doc: str):
        svc = FormatParseService()
        first = _task(doc, f"T1-{doc}", "1-8")
        svc.execute_parse(first["id"])
        confirmed = svc.confirm_pages(doc, confirmed_by="值班员")
        assert confirmed["ok"] is True
        return svc, first, confirmed["confirmed"]

    def test_rerun_only_replaces_confirmed_page_group_after_confirm(self):
        doc = _doc()
        svc, first, snapshot = self._prepare_confirmed(doc)
        assert snapshot["version"] == 1
        chunks_v1 = _chunks(doc)
        assert {c["page"] for c in chunks_v1} == set(range(1, 9))
        assert {c["page_group_version"] for c in chunks_v1} == {1}
        page5_hash_v1 = next(c["content_hash"] for c in chunks_v1 if c["page"] == 5)

        second = _task(doc, f"T2-{doc}", "4-6")
        rerun = svc.execute_parse(second["id"], corrected_pages=[5])
        assert rerun["pages"] == 3
        # 补页未确认：分块仍是第 1 次确认页组，历史数据未被替换
        trigger = rerun["triggers"]["chunk_split"]
        assert trigger["coverage_mode"] == "确认页组 v1"
        assert trigger["page_group_version"] == 1
        chunks_after = _chunks(doc)
        assert chunks_after == chunks_v1
        assert next(c["content_hash"] for c in chunks_after if c["page"] == 5) == page5_hash_v1

        view = svc.coverage_view(doc)
        page5 = next(row for row in view["pages"] if row["page_num"] == 5)
        assert page5["coverage"] == "已确认"
        assert page5["task_id"] == first["id"]
        assert page5["has_pending_update"] is True
        assert page5["latest_task_key"] == f"T2-{doc}"
        assert "第2次确认页组" in page5["next_chunk_target"]
        assert "T2-" in page5["next_chunk_target"]
        # 第 5 页会进入第 2 次确认页组（来自补页任务）
        assert any(
            source["page_num"] == 5 and source["task_id"] == second["id"]
            and source["corrected"]
            for source in view["pending_group"]["sources"]
        )

    def test_after_confirm_chunks_carry_rerun_attribution(self):
        doc = _doc()
        svc, first, _ = self._prepare_confirmed(doc)
        second = _task(doc, f"T2-{doc}", "4-6")
        svc.execute_parse(second["id"], corrected_pages=[5])

        confirmed = svc.confirm_pages(doc, confirmed_by="值班员")
        assert confirmed["ok"] is True
        assert confirmed["confirmed"]["version"] == 2
        chunks = _chunks(doc)
        assert {c["page_group_version"] for c in chunks} == {2}
        for page in (4, 5, 6):
            assert {c["task_id"] for c in chunks if c["page"] == page} == {second["id"]}
        for page in (1, 2, 3, 7, 8):
            assert {c["task_id"] for c in chunks if c["page"] == page} == {first["id"]}

        # 分块页归属能回查原解析记录：hash / 文件 / 任务与实际解析产物一致
        parsed_file = DATA_ROOT / "parsed" / f"{doc}_run_{second['id']}.jsonl"
        parsed = {row["page_num"]: row for row in map(json.loads, parsed_file.read_text().splitlines())}
        page5_chunk = next(c for c in chunks if c["page"] == 5)
        assert page5_chunk["parsed_file"] == parsed_file.name
        assert page5_chunk["content_hash"] == parsed[5]["content_hash"]
        assert page5_chunk["text"].startswith(parsed[5]["text"][:20])

        view = svc.coverage_view(doc)
        assert view["confirmed"]["version"] == 2
        assert view["confirmed"]["pages"] == list(range(1, 9))
        assert view["has_unconfirmed_updates"] is False

    def test_reopen_shows_unadopted_page_sources(self):
        doc = _doc()
        svc, first, _ = self._prepare_confirmed(doc)
        second = _task(doc, f"T2-{doc}", "4-6")
        svc.execute_parse(second["id"], corrected_pages=[5])
        svc.confirm_pages(doc)

        reopen = svc.reopen_confirmation(doc)
        assert reopen["ok"] is True
        adopted = {row["page_num"]: row for row in reopen["accepted_pages"]}
        assert adopted[5]["task_id"] == second["id"]
        unadopted = [row for row in reopen["unadopted_pages"] if row["page_num"] in (4, 5, 6)]
        assert unadopted, "首轮第 4-6 页产物应作为未采纳项保留"
        assert {row["source_task_key"] for row in unadopted} == {f"T1-{doc}"}
        assert all(row["status"] == "未采纳" for row in unadopted)
        assert all(row["reason"] for row in unadopted)


class TestCoveragePendingOnly:
    def test_pending_view_explains_page5_target_and_missing_pages(self):
        doc = _doc()
        svc = FormatParseService()
        svc.execute_parse(_task(doc, f"T1-{doc}", "1-3")["id"])
        svc.execute_parse(_task(doc, f"T2-{doc}", "4-6")["id"])
        view = svc.coverage_view(doc)
        assert view["confirmed"] is None
        assert view["missing_pages"] == [7, 8]
        page5 = next(row for row in view["pages"] if row["page_num"] == 5)
        assert page5["task_key"] == f"T2-{doc}"
        assert "第1次确认页组" in page5["next_chunk_target"]
        assert "7、8" in view["pending_group"]["summary"]
        assert view["pending_group"]["ready_to_confirm"] is False

    def test_partial_rerun_does_not_chunk_unconfirmed_mix(self):
        doc = _doc()
        svc = FormatParseService()
        # 首轮只做 1-3，补页 4-6：两次都不完整，任何一次都不得写出分块
        first = svc.execute_parse(_task(doc, f"T1-{doc}", "1-3")["id"])
        second = svc.execute_parse(_task(doc, f"T2-{doc}", "4-6")["id"])
        assert "error" in first["triggers"]["chunk_split"]
        assert "error" in second["triggers"]["chunk_split"]
        assert second["triggers"]["chunk_split"].get("pending") is True
        assert not (DATA_ROOT / "chunks" / f"{doc}_parsed_chunks.jsonl").exists()


class TestCoverageConcurrency:
    def test_concurrent_rerun_never_lands_half_group(self):
        doc = _doc()
        svc = FormatParseService()
        svc.execute_parse(_task(doc, f"T1-{doc}", "1-8")["id"])
        svc.confirm_pages(doc)
        baseline = _chunks(doc)

        results: list[dict] = []
        errors: list[BaseException] = []

        def rerun(key: str, pages: str):
            try:
                local = FormatParseService()
                task = _task(doc, key, pages)
                results.append(local.execute_parse(task["id"], corrected_pages=[5]))
            except BaseException as exc:  # pragma: no cover - 失败时给出上下文
                errors.append(exc)

        threads = [
            threading.Thread(target=rerun, args=(f"T{i}-{doc}", "4-6"))
            for i in range(2, 5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        assert len(results) == 3
        # 三次补页都未确认：分块仍是第 1 次确认页组
        assert _chunks(doc) == baseline

        manifest = PageCoverageManifest(svc).load(doc)
        assert manifest["confirmed"]["version"] == 1
        assert len(manifest["runs"]) == 4
        for run in manifest["runs"].values():
            if run["status"] == "已完成":
                assert run["pages"]
        view = svc.coverage_view(doc)
        assert view["confirmed"]["version"] == 1
        assert view["has_unconfirmed_updates"] is True

        # 并发登记后再次确认仍取一次完整页组，且不丢已确认页
        confirmed = svc.confirm_pages(doc)
        assert confirmed["ok"] is True
        assert confirmed["confirmed"]["page_count"] == 8
        chunks = _chunks(doc)
        assert {c["page"] for c in chunks} == set(range(1, 9))
        assert {c["page_group_version"] for c in chunks} == {2}


class TestCoverageChunkReadsManifest:
    def test_chunk_service_refuses_mixed_pages_without_confirmation(self):
        doc = _doc()
        svc = FormatParseService()
        svc.execute_parse(_task(doc, f"T1-{doc}", "1-8")["id"])
        svc.confirm_pages(doc)
        # 再补一次 4-6，然后抹掉确认：页组来自两次任务，不得直接切分
        svc.execute_parse(_task(doc, f"T2-{doc}", "4-6")["id"])
        baseline = _chunks(doc)
        manifest = PageCoverageManifest(svc)
        data = manifest.load(doc)
        data["confirmed"] = None
        manifest.save(doc, data)

        result = ChunkSplitService().split_from_document(f"{doc}_parsed")
        assert "error" in result
        assert result["pending"] is True
        assert "未写入分块" in result["error"]
        assert _chunks(doc) == baseline, "拒绝切分时不得改写既有分块产物"

    def test_chunk_page_sources_reference_parse_runs(self):
        doc = _doc()
        svc = FormatParseService()
        task = _task(doc, f"T1-{doc}", "1-8")
        svc.execute_parse(task["id"])
        result = ChunkSplitService().split_from_document(f"{doc}_parsed")
        assert "error" not in result, result
        assert result["pages"] == list(range(1, 9))
        assert result["coverage_mode"] == "未确认（单次完整解析）"
        for source in result["page_sources"]:
            expected = DATA_ROOT / "parsed" / f"{doc}_run_{task['id']}.jsonl"
            assert (DATA_ROOT / "parsed" / source["parsed_file"]) == expected
            assert source["task_id"] == task["id"]
            assert source["content_hash"]

    def test_chunk_refuses_when_confirmed_artifact_missing(self):
        doc = _doc()
        svc = FormatParseService()
        task = _task(doc, f"T1-{doc}", "1-8")
        svc.execute_parse(task["id"])
        svc.confirm_pages(doc)
        # 原解析产物被清理：不能编造无来源文本，必须报告缺失页
        (DATA_ROOT / "parsed" / f"{doc}_run_{task['id']}.jsonl").unlink()

        result = ChunkSplitService().split_from_document(f"{doc}_parsed")
        assert "error" in result
        assert result["missing_artifacts"] == list(range(1, 9))
        assert "回查原解析记录" in result["error"]
