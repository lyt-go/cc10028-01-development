# 文档解析与知识抽取平台

面向多格式文档批量接入、版式解析、分块切分、实体关系抽取、向量化嵌入与抽取质量校验入库的非结构化知识抽取平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 文件接入 | `file_intake` | 接入批次 | 批次编号、来源系统、文件清单 |
| 格式解析 | `format_parse` | 解析任务 | 任务编号、文档编号、解析器类型、页码范围 |
| 版式还原 | `layout_restore` | 版式结构 | 文档编号、段落结构、标题层级 |
| 表格抽取 | `table_extract` | 表格区域 | 表格编号、文档编号、页码位置 |
| OCR识别 | `ocr_engine` | 识别任务 | 任务编号、图片区域、识别语言 |
| 分块切分 | `chunk_split` | 文本分块 | 分块编号、文档编号、分块长度 |
| 实体识别 | `entity_recognize` | 命名实体 | 实体编号、分块编号、实体类型 |
| 关系抽取 | `relation_extract` | 实体关系 | 关系编号、头实体、尾实体 |
| 术语归一 | `term_normalize` | 术语映射 | 映射编号、源术语、标准术语 |
| 敏感词过滤 | `sensitive_filter` | 过滤规则 | 规则编号、匹配模式、命中词条 |
| 向量嵌入 | `vector_embed` | 向量化任务 | 任务编号、分块编号、嵌入模型 |
| 知识图谱 | `kg_build` | 图谱三元组 | 三元组编号、头实体、谓词关系 |
| 抽取质量 | `extract_quality` | 质检报告 | 报告编号、批次编号、抽样比例 |
| 人工复核 | `annot_review` | 复核任务 | 复核编号、分块编号、复核对象 |
| 文档版本 | `doc_version` | 版本快照 | 快照编号、文档编号、版本号 |
| 抽取血缘 | `extract_lineage` | 血缘链路 | 链路编号、源文件、解析节点 |
| 结果交付 | `result_deliver` | 交付任务 | 交付编号、快照编号、交付格式 |
| 失败重采 | `parse_retry` | 重采任务 | 重采编号、失败环节、原始批次 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 页段覆盖清单（格式解析）

同一文档可能有多次解析任务：初次完成第 1-8 页，之后只重跑第 4-6 页并更正第 5 页。
`format_parse` 通过页段覆盖清单记录“哪一页来自哪个解析任务”，并保证分块只消费
**一次完整确认的页组**。

产物目录：

| 文件 | 说明 |
| --- | --- |
| `data/parsed/<doc>_run_<task_id>.jsonl` | 单个解析任务的不可变页产物 |
| `data/parsed/<doc>_parsed_index.json` | 每页最新文本索引（供版式还原等旧消费方） |
| `data/coverage/<doc>_pages.json` | 页段覆盖清单：运行记录、页归属、确认快照 |
| `data/chunks/<doc>_parsed_chunks.jsonl` | 分块结果，每块带 `page/task_id/content_hash` |

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/format_parse/tasks/{task_id}/execute` | 执行解析；请求体 `{ corrected_pages: [5] }` 标记本次更正页 |
| GET | `/api/format_parse/coverage/{doc_id}` | 覆盖视图：每页的当前分块归属、下次确认归属、待补齐页 |
| POST | `/api/format_parse/coverage/{doc_id}/confirm` | 确认整份页组用于分块；页组不完整返回 409 与缺口 |
| GET | `/api/format_parse/coverage/{doc_id}/reopen` | 重新打开最近一次确认，回查未采纳页来源与原因 |
| POST | `/api/format_parse/documents/{doc_id}/chunks` | 按确认页组手动重算分块 |

行为要点：

- **覆盖视图**：每页给出 `chunk_target`（当前进入哪次确认页组）与
  `next_chunk_target`（下次确认后归属），并说明还缺哪些页。补页任务只跑第 4-6 页时，
  第 5 页会显示“第 2 次确认页组（候选，任务 T2-...）”。
- **确认是原子的**：只有整份页数齐全才写 `confirmed` 快照；不完整时拒绝确认，
  不覆盖历史确认。分块按页读取该快照指向的 `<doc>_run_<task_id>.jsonl`，
  每块记录 `task_id/task_key/parsed_file/content_hash`，可回查原解析记录。
- **并发安全**：同一文档的清单读改写有进程内锁；补页任务解析完成时若尚无新的确认，
  分块仍用旧确认页组，不会被半份结果替换。
- **未采纳页可回查**：重新打开确认时，`unadopted_pages` 列出未被采纳的同页产物，
  含来源任务、内容摘要与待处理原因。
- 测试使用 `APP_DATA_DIR` 指向临时目录，避免运行产物与重试检查点跨会话污染。


领域：数据工程
子方向：数据采集、爬虫与RPA
