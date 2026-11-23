import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  CoverageReopen,
  CoverageView,
  EntryRow,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 页段覆盖视图：每页会进入哪次分块、来源解析任务与待补齐页。 */
export async function loadCoverage(docId: string): Promise<CoverageView> {
  const response = await request(`/api/format_parse/coverage/${encodeURIComponent(docId)}`)
  if (!response.ok) throw new Error('页段覆盖清单读取失败')
  return await response.json()
}

/** 执行解析任务：按页码范围产出页产物并登记覆盖清单。 */
export async function executeParseTask(
  taskId: number,
  correctedPages: number[] = [],
): Promise<{ ok: boolean; message: string; data?: unknown }> {
  const response = await request(`/api/format_parse/tasks/${taskId}/execute`, {
    method: 'POST',
    body: JSON.stringify({ corrected_pages: correctedPages }),
  })
  const body = await response.json()
  if (!response.ok) {
    return { ok: false, message: body?.detail?.error ?? '解析执行失败', data: body?.detail }
  }
  return { ok: true, message: `解析完成：第 ${body.page_range} 页`, data: body }
}

/** 确认页组用于分块；页组不完整时后端返回 409 与缺口说明。 */
export async function confirmCoverage(
  docId: string,
  payload: { expected_pages?: number; remark?: string; confirmed_by?: string } = {},
): Promise<{ ok: boolean; message: string; data?: unknown }> {
  const response = await request(
    `/api/format_parse/coverage/${encodeURIComponent(docId)}/confirm`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
  const body = await response.json()
  if (!response.ok) {
    const detail = body?.detail ?? {}
    const missing = Array.isArray(detail.missing_pages) ? detail.missing_pages.join('、') : ''
    return {
      ok: false,
      message: missing ? `确认未生效：尚缺第 ${missing} 页` : (detail.reason ?? '确认未生效'),
      data: detail,
    }
  }
  return { ok: true, message: '页组已确认，分块已按确认页组重算', data: body }
}

/** 重新打开最近一次确认：回查采纳页与未被采纳页的来源、待处理原因。 */
export async function reopenCoverage(docId: string): Promise<CoverageReopen> {
  const response = await request(`/api/format_parse/coverage/${encodeURIComponent(docId)}/reopen`)
  if (!response.ok) throw new Error('该文档尚无确认记录')
  return await response.json()
}
