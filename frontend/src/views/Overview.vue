<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "文件接入", "created": 0, "pending": 0, "abnormal": 0}, {"name": "格式解析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "版式还原", "created": 0, "pending": 0, "abnormal": 0}, {"name": "表格抽取", "created": 0, "pending": 0, "abnormal": 0}, {"name": "OCR识别", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分块切分", "created": 0, "pending": 0, "abnormal": 0}, {"name": "实体识别", "created": 0, "pending": 0, "abnormal": 0}, {"name": "关系抽取", "created": 0, "pending": 0, "abnormal": 0}, {"name": "术语归一", "created": 0, "pending": 0, "abnormal": 0}, {"name": "敏感词过滤", "created": 0, "pending": 0, "abnormal": 0}, {"name": "向量嵌入", "created": 0, "pending": 0, "abnormal": 0}, {"name": "知识图谱", "created": 0, "pending": 0, "abnormal": 0}, {"name": "抽取质量", "created": 0, "pending": 0, "abnormal": 0}, {"name": "人工复核", "created": 0, "pending": 0, "abnormal": 0}, {"name": "文档版本", "created": 0, "pending": 0, "abnormal": 0}, {"name": "抽取血缘", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果交付", "created": 0, "pending": 0, "abnormal": 0}, {"name": "失败重采", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
