<template>
  <section class="coverage-panel" data-testid="coverage-panel">
    <header class="coverage-head">
      <div>
        <h3>页段覆盖清单</h3>
        <p class="coverage-desc">
          同一文档多次解析按页比对：每页标明会进入哪次分块、来源解析任务，以及还缺哪些页。
        </p>
      </div>
      <div class="coverage-actions">
        <label class="coverage-doc">
          <span>文档编号</span>
          <input v-model="docId" data-testid="coverage-doc" placeholder="输入文档编号后加载" />
        </label>
        <button class="btn" data-testid="coverage-load" type="button" @click="load">读取覆盖视图</button>
        <button
          class="btn primary"
          data-testid="coverage-confirm"
          type="button"
          :disabled="!view || busy || !view.pending_group.ready_to_confirm"
          @click="confirm"
        >
          确认页组用于分块
        </button>
        <button class="btn ghost" data-testid="coverage-reopen" type="button" :disabled="!docId || busy" @click="reopen">
          重新打开本次确认
        </button>
      </div>
    </header>

    <p v-if="message" class="coverage-message" data-testid="coverage-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="coverage-error">{{ errorMessage }}</p>

    <template v-if="view">
      <div class="coverage-summary">
        <article class="stat-card">
          <span class="stat-label">整份页数</span>
          <strong class="stat-value">{{ view.expected_pages }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前确认</span>
          <strong class="stat-value">
            {{ view.confirmed ? `第${view.confirmed.version}次 · ${view.confirmed.page_count}页` : '未确认' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待补齐</span>
          <strong class="stat-value" data-testid="coverage-missing">
            {{ view.pending_group.pages_missing.length ? view.pending_group.pages_missing.join('、') : '无' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待下次确认</span>
          <strong class="stat-value">{{ view.has_unconfirmed_updates ? '有更新页' : '无' }}</strong>
        </article>
      </div>

      <p class="coverage-hint" data-testid="coverage-pending-summary">
        {{ view.pending_group.summary }}
        <span v-if="view.confirmed">
          · 当前分块使用第{{ view.confirmed.version }}次确认页组
          <template v-if="view.confirmed.confirmed_by">（{{ view.confirmed.confirmed_by }}）</template>
        </span>
      </p>

      <table class="data-table coverage-table">
        <thead>
          <tr>
            <th>页号</th>
            <th>覆盖状态</th>
            <th>来源解析任务</th>
            <th>内容摘要</th>
            <th>当前进入分块</th>
            <th>下次确认后归属</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in view.pages"
            :key="row.page_num"
            :data-testid="`coverage-page-${row.page_num}`"
            :data-coverage="row.coverage"
          >
            <td>{{ row.page_num }}</td>
            <td>
              <span class="coverage-badge" :class="badgeClass(row.coverage)">{{ row.coverage }}</span>
            </td>
            <td>
              <template v-if="row.task_key">{{ row.task_key }}</template>
              <template v-else>—</template>
              <span v-if="row.has_pending_update" class="coverage-tag">有更新待确认</span>
            </td>
            <td class="mono">{{ row.content_hash || '—' }}</td>
            <td>{{ row.chunk_target }}</td>
            <td>{{ row.next_chunk_target }}</td>
            <td class="coverage-reason">{{ row.reason || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <details v-if="reopenData" class="coverage-reopen" data-testid="coverage-reopen-panel" open>
        <summary>
          本次确认回查：采纳 {{ reopenData.accepted_pages.length }} 页，未被采纳
          {{ reopenData.unadopted_pages.length }} 条
        </summary>
        <table class="data-table">
          <thead>
            <tr><th>页号</th><th>结果</th><th>来源解析任务</th><th>待处理原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in reopenData.accepted_pages" :key="`a-${row.page_num}`">
              <td>{{ row.page_num }}</td>
              <td>已采纳</td>
              <td>{{ row.task_key }}</td>
              <td class="mono">{{ row.content_hash }}</td>
            </tr>
            <tr v-for="row in reopenData.unadopted_pages" :key="`u-${row.page_num}-${row.source_task_id}`">
              <td>{{ row.page_num }}</td>
              <td>{{ row.status }}</td>
              <td>{{ row.source_task_key || '—' }}</td>
              <td class="coverage-reason">{{ row.reason }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import { confirmCoverage, loadCoverage, reopenCoverage } from '@/api/backend-service'
import type { CoverageReopen, CoverageView } from '@/data/types'

const docId = ref('')
const view = ref<CoverageView | null>(null)
const reopenData = ref<CoverageReopen | null>(null)
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)

function badgeClass(coverage: string): string {
  if (coverage === '已确认') return 'ok'
  if (coverage === '待确认') return 'warn'
  if (coverage === '待补齐') return 'danger'
  return ''
}

async function load() {
  if (!docId.value.trim()) {
    errorMessage.value = '请先填写文档编号'
    return
  }
  await loadDoc(docId.value.trim())
}

/** 供父级行操作直接按文档编号加载覆盖视图。 */
async function loadDoc(id: string) {
  docId.value = id
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  reopenData.value = null
  try {
    view.value = await loadCoverage(id)
    if (!view.value.expected_pages) message.value = '该文档还没有解析任务，覆盖清单为空'
  } catch (error) {
    view.value = null
    errorMessage.value = error instanceof Error ? error.message : '覆盖清单读取失败'
  } finally {
    busy.value = false
  }
}

defineExpose({ loadDoc })

async function confirm() {
  if (!view.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await confirmCoverage(view.value.doc_id)
    if (!result.ok) {
      errorMessage.value = result.message
      return
    }
    message.value = result.message
    view.value = await loadCoverage(view.value.doc_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  } finally {
    busy.value = false
  }
}

async function reopen() {
  busy.value = true
  errorMessage.value = ''
  try {
    reopenData.value = await reopenCoverage(docId.value.trim())
  } catch (error) {
    reopenData.value = null
    errorMessage.value = error instanceof Error ? error.message : '重新打开确认失败'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.coverage-panel {
  margin-top: 18px;
  padding: 14px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.coverage-head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.coverage-head h3 { margin: 0; font-size: 15px; }
.coverage-desc { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.coverage-actions { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.coverage-doc { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.coverage-doc input { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.coverage-summary { display: flex; gap: 12px; margin: 12px 0; }
.coverage-hint { font-size: 12px; color: var(--muted); margin: 0 0 10px; }
.coverage-message { font-size: 12px; color: #027a48; margin: 8px 0; }
.coverage-table td { vertical-align: top; }
.coverage-reason { color: var(--muted); font-size: 12px; }
.mono { font-family: monospace; font-size: 12px; }
.coverage-badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; background: #eef2f6; }
.coverage-badge.ok { background: #dcfae6; color: #027a48; }
.coverage-badge.warn { background: #fef0c7; color: #93370d; }
.coverage-badge.danger { background: #fee4e2; color: #b42318; }
.coverage-tag { margin-left: 6px; font-size: 11px; color: #93370d; }
.coverage-reopen { margin-top: 12px; font-size: 13px; }
.coverage-reopen summary { cursor: pointer; margin-bottom: 8px; color: var(--brand); }
</style>
