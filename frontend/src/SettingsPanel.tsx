import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import { RuntimeStatusPanel } from "./RuntimeStatusPanel";

interface ServiceField { configured: boolean; source: string; editable: boolean; value: string; pending_restart: boolean; overrides_environment?: boolean }
interface ServiceState { version: string; fields: Record<string, ServiceField>; restart_required: boolean }
interface Preferences { version: number; enabled: boolean; suggestions: boolean; values: Record<string, unknown>; scope: string }
interface CostBreakdown { currency: string; amount: number }
interface UsageRow { purpose?: string; model_id?: string; calls: number; prompt_tokens: number; cached_input_tokens: number; completion_tokens: number; estimated_cost: number; cost_unknown: number; cost_recalculated?: number; cache_detail_missing?: number; unknown_usage?: number; unknown_pricing?: number; currency?: string; cost_mixed?: boolean; cost_breakdown?: CostBreakdown[] }
interface ModelUsage { totals: UsageRow; purposes: UsageRow[]; currency: string; pricing_active?: boolean; cost_note: string; budget?: { tier: string; daily_limit: number; monthly_limit: number; day_reserved: number; month_reserved: number; day_inflight?: number; day_actual_tokens?: number; day_unknown_occupied?: number; day_legacy_occupied?: number; day_cutover?: string; month_cutover?: string } }
interface ServiceTestReport { results: { label: string; model_id: string; service: string; status: "ok" | "failed" | "skipped"; detail: string; elapsed_ms: number }[]; summary: { ok: number; failed: number; skipped: number }; restart_required?: boolean }
type FieldControl = { type: "text" | "password" | "url" | "number"; placeholder: string; inputMode?: "text" | "url" | "numeric" | "decimal"; step?: string; min?: string; max?: string; maxLength?: number };
const labels: Record<string, string> = {
  SEARCH_API_KEY: "Tavily 搜索 Key", SEARCH_BASE_URL: "搜索地址", LLM_API_KEY: "模型 Key",
  LLM_BASE_URL: "模型地址", LLM_MODEL_ID: "默认模型 ID", AMAP_API_KEY: "高德 Key",
  LLM_INTENT_MODEL_ID: "意图解析模型", LLM_SUMMARY_MODEL_ID: "会话摘要模型",
  LLM_RESEARCH_MODEL_ID: "研究模型", LLM_VISION_MODEL_ID: "视觉模型",
  LLM_PROVIDER_ALIAS: "供应商别名", LLM_CONFIG_VERSION: "费率/配置版本",
  AI_BUDGET_TIER: "AI 预算档位", AI_DAILY_TOKEN_LIMIT: "每日 token 上限",
  AI_MONTHLY_TOKEN_LIMIT: "每月 token 上限", AI_SUMMARY_ENABLED: "按需会话摘要",
  AI_VECTOR_ENABLED: "本地混合检索", AI_EMBEDDING_MODEL_DIR: "本地语义模型目录",
  AI_EMBEDDING_MIN_SIMILARITY: "语义检索相似度门槛", LLM_INPUT_PRICE_PER_M: "输入费率/百万 token",
  LLM_CACHED_INPUT_PRICE_PER_M: "缓存输入费率/百万 token",
  LLM_OUTPUT_PRICE_PER_M: "输出费率/百万 token", LLM_PRICE_CURRENCY: "费率币种",
  LLM_MODEL_PRICES: "用途模型费率映射（可选）",
  AI_DAILY_COST_LIMIT: "每日金额上限（可选）", AI_MONTHLY_COST_LIMIT: "每月金额上限（可选）", AI_COST_CURRENCY: "金额限额币种",
};
const fieldHelp: Record<string, { description: string; example?: string }> = {
  AI_DAILY_COST_LIMIT: { description: "0表示不限。启用后全部调用模型需要完整费率与匹配币种；费用未知时停止新调用。", example: "1" },
  AI_MONTHLY_COST_LIMIT: { description: "应用级尽力金额控制，供应商账单仍需自行设限；0表示不限。", example: "20" },
  AI_COST_CURRENCY: { description: "金额上限统一使用的币种，不自动换汇。", example: "USD" },
  LLM_MODEL_PRICES: { description: "不同模型使用不同价格；未配置的用途模型显示费用未知。provider须匹配供应商别名。", example: '[{"provider":"main","model":"small-model","input":0.1,"cached":0,"output":0.2,"currency":"USD"}]' },
  SEARCH_API_KEY: { description: "用于联网搜索旅行攻略。请填写 Tavily 控制台创建的 API Key。", example: "tvly-xxxxxxxx" },
  SEARCH_BASE_URL: { description: "搜索服务地址，当前固定使用 Tavily 官方 HTTPS 地址。", example: "https://api.tavily.com" },
  LLM_API_KEY: { description: "模型服务凭据。已保存的 Key 不会回显，留空表示不替换。", example: "sk-xxxxxxxx" },
  LLM_BASE_URL: { description: "兼容 OpenAI Chat Completions 的 HTTPS /v1 地址，不能包含 Key 或查询参数。", example: "https://api.example.com/v1" },
  LLM_MODEL_ID: { description: "默认文本模型名称，需要支持 JSON 输出和工具调用；图片功能还要求视觉输入。", example: "gpt-5.5" },
  AMAP_API_KEY: { description: "高德开放平台的 Web 服务 Key，用于地点、天气和路线查询。", example: "32 位高德 Web 服务 Key" },
  LLM_INTENT_MODEL_ID: { description: "可选的轻量意图解析模型；留空继续使用默认模型，失败会回退默认模型。", example: "your-small-model" },
  LLM_SUMMARY_MODEL_ID: { description: "可选的长会话摘要模型，仅在启用按需摘要后使用。", example: "your-summary-model" },
  LLM_RESEARCH_MODEL_ID: { description: "可选的攻略选源和研究整理模型。", example: "your-research-model" },
  LLM_VISION_MODEL_ID: { description: "可选的图片识别模型，必须支持 image_url 输入。", example: "your-vision-model" },
  LLM_PROVIDER_ALIAS: { description: "仅用于本地用量报表区分供应商，不参与请求。", example: "main-provider" },
  LLM_CONFIG_VERSION: { description: "费率或模型配置版本标签，便于比较不同阶段成本。", example: "2026-09-v1" },
  AI_BUDGET_TIER: { description: "经济减少上下文和输出，均衡为默认，高质量提供更充分的输出预算。" },
  AI_DAILY_TOKEN_LIMIT: { description: "应用每天可保守预留的 token 上限；0 表示不限。", example: "1000000" },
  AI_MONTHLY_TOKEN_LIMIT: { description: "应用每月可保守预留的 token 上限；0 表示不限。", example: "20000000" },
  AI_SUMMARY_ENABLED: { description: "长会话达到阈值时生成带来源引用的摘要；默认关闭。" },
  AI_VECTOR_ENABLED: { description: "使用本地BGE中文模型补充关键词检索。小样本已有改善，仍待扩大验收；默认关闭。需先安装可选依赖并准备模型。" },
  AI_EMBEDDING_MODEL_DIR: { description: "已准备好的BGE模型文件夹；相对路径以项目目录为准。服务不会自动下载模型。", example: ".models/bge-small-zh-v1.5-onnx" },
  AI_EMBEDDING_MIN_SIMILARITY: { description: "0到1，越高越严格。0.5来自开发集试验，不能保证所有资料都适用。", example: "0.5" },
  LLM_INPUT_PRICE_PER_M: { description: "供应商每百万普通输入 token 的真实价格。不了解时留空。", example: "1.25" },
  LLM_CACHED_INPUT_PRICE_PER_M: { description: "供应商每百万缓存输入 token 的真实价格。", example: "0.125" },
  LLM_OUTPUT_PRICE_PER_M: { description: "供应商每百万输出 token 的真实价格。", example: "10" },
  LLM_PRICE_CURRENCY: { description: "上述费率使用的三位币种代码，默认 USD。", example: "USD" },
};
const serviceGroups = [
  { title: "联网搜索", description: "用于搜索公开旅行攻略和来源。", defaultOpen: true, keys: ["SEARCH_API_KEY", "SEARCH_BASE_URL"] },
  { title: "模型与地图基础配置", description: "配置模型三项和高德 Key 后即可使用主要 Agent 能力。", defaultOpen: true, keys: ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "AMAP_API_KEY"] },
  { title: "用途模型（可选）", description: "留空时全部沿用默认模型，不需要为每项单独配置。", defaultOpen: false, keys: ["LLM_INTENT_MODEL_ID", "LLM_SUMMARY_MODEL_ID", "LLM_RESEARCH_MODEL_ID", "LLM_VISION_MODEL_ID", "LLM_PROVIDER_ALIAS", "LLM_CONFIG_VERSION"] },
  { title: "预算与记忆", description: "默认值保持兼容；摘要和混合检索不会自动开启。", defaultOpen: false, keys: ["AI_BUDGET_TIER", "AI_DAILY_TOKEN_LIMIT", "AI_MONTHLY_TOKEN_LIMIT", "AI_DAILY_COST_LIMIT", "AI_MONTHLY_COST_LIMIT", "AI_COST_CURRENCY", "AI_SUMMARY_ENABLED", "AI_VECTOR_ENABLED", "AI_EMBEDDING_MODEL_DIR", "AI_EMBEDDING_MIN_SIMILARITY"] },
  { title: "费用估算（可选）", description: "必须填写供应商真实费率；留空时显示费用未知。", defaultOpen: false, keys: ["LLM_INPUT_PRICE_PER_M", "LLM_CACHED_INPUT_PRICE_PER_M", "LLM_OUTPUT_PRICE_PER_M", "LLM_PRICE_CURRENCY", "LLM_MODEL_PRICES"] },
] as const;
const fieldControls: Record<string, FieldControl> = {
  AI_EMBEDDING_MIN_SIMILARITY: { type: "number", inputMode: "decimal", min: "0", max: "1", step: "0.01", placeholder: "0.5" },
  AI_DAILY_COST_LIMIT: { type: "number", inputMode: "decimal", placeholder: "0表示不限；例如1", min: "0", step: "0.01" },
  AI_MONTHLY_COST_LIMIT: { type: "number", inputMode: "decimal", placeholder: "0表示不限；例如20", min: "0", step: "0.01" },
  AI_COST_CURRENCY: { type: "text", placeholder: "USD、CNY或EUR", maxLength: 3 },
  LLM_MODEL_PRICES: { type: "text", placeholder: "JSON费率数组；见上方示例", maxLength: 4096 },
  SEARCH_API_KEY: { type: "password", placeholder: "例如 tvly-xxxxxxxx；已保存时留空表示保留", maxLength: 4096 },
  SEARCH_BASE_URL: { type: "url", inputMode: "url", placeholder: "https://api.tavily.com", maxLength: 4096 },
  LLM_API_KEY: { type: "password", placeholder: "例如 sk-xxxxxxxx；已保存时留空表示保留", maxLength: 4096 },
  LLM_BASE_URL: { type: "url", inputMode: "url", placeholder: "例如 https://api.example.com/v1", maxLength: 4096 },
  LLM_MODEL_ID: { type: "text", placeholder: "例如 gpt-5.5", maxLength: 200 },
  AMAP_API_KEY: { type: "password", placeholder: "填写高德 Web 服务 Key；已保存时留空表示保留", maxLength: 4096 },
  LLM_INTENT_MODEL_ID: { type: "text", placeholder: "留空沿用默认模型；例如 your-small-model", maxLength: 200 },
  LLM_SUMMARY_MODEL_ID: { type: "text", placeholder: "留空沿用默认模型；例如 your-summary-model", maxLength: 200 },
  LLM_RESEARCH_MODEL_ID: { type: "text", placeholder: "留空沿用默认模型；例如 your-research-model", maxLength: 200 },
  LLM_VISION_MODEL_ID: { type: "text", placeholder: "留空沿用默认模型；例如 your-vision-model", maxLength: 200 },
  LLM_PROVIDER_ALIAS: { type: "text", placeholder: "例如 main-provider", maxLength: 80 },
  LLM_CONFIG_VERSION: { type: "text", placeholder: "例如 2026-09-v1", maxLength: 80 },
  AI_DAILY_TOKEN_LIMIT: { type: "number", inputMode: "numeric", placeholder: "0 表示不限；例如 1000000", step: "1000", min: "0" },
  AI_MONTHLY_TOKEN_LIMIT: { type: "number", inputMode: "numeric", placeholder: "0 表示不限；例如 20000000", step: "1000", min: "0" },
  LLM_INPUT_PRICE_PER_M: { type: "number", inputMode: "decimal", placeholder: "例如 1.25；不了解请留空", step: "0.001", min: "0" },
  LLM_CACHED_INPUT_PRICE_PER_M: { type: "number", inputMode: "decimal", placeholder: "例如 0.125；不了解请留空", step: "0.001", min: "0" },
  LLM_OUTPUT_PRICE_PER_M: { type: "number", inputMode: "decimal", placeholder: "例如 10；不了解请留空", step: "0.001", min: "0" },
  LLM_PRICE_CURRENCY: { type: "text", inputMode: "text", placeholder: "默认 USD；也可填写 CNY 或 EUR", maxLength: 3 },
};
const choices: Record<string, string[]> = { pace: ["轻松", "适中", "紧凑"], transport: ["公共交通", "步行", "驾车"], night: ["不安排夜游", "可以夜游"], reply_style: ["简洁", "详细", "表格"] };
const preferenceLabels: Record<string, string> = { pace: "行程节奏", transport: "交通偏好", night: "夜间活动", reply_style: "回复风格", interests: "兴趣类别", departure_city: "常用出发城市", budget: "默认预算" };

function SettingsGroup({ title, description, initiallyOpen, children }: { title: string; description: string; initiallyOpen: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(initiallyOpen);
  return <details className="settings-group" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span><strong>{title}</strong><small>{description}</small></span></summary>
    <div className="settings-group-fields">{children}</div>
  </details>;
}

function formatUsageCost(row: UsageRow, fallbackCurrency: string) {
  const known = Math.max(0, row.calls - (row.cost_unknown || 0));
  let amount = "";
  if (row.cost_mixed && row.cost_breakdown?.length)
    amount = row.cost_breakdown.map(item => `${item.amount.toFixed(6)} ${item.currency}`).join(" + ");
  else if (known > 0)
    amount = `${(row.estimated_cost || 0).toFixed(6)} ${row.currency || fallbackCurrency || "USD"}`;
  const parts = [];
  if (row.cost_recalculated) parts.push(`含 ${row.cost_recalculated} 次按当前费率回算`);
  if (row.cache_detail_missing) parts.push(`${row.cache_detail_missing} 次缓存拆分缺失`);
  if (row.cost_unknown && known > 0) parts.push(`${row.cost_unknown} 次无法估算`);
  if (!amount) amount = row.cost_unknown ? `${row.cost_unknown} 次无法估算` : "暂无费用";
  return amount + (parts.length ? `（${parts.join("；")}）` : "");
}

export function SettingsPanel({ section = "all" }: { section?: "services" | "preferences" | "all" }) {
  const [services, setServices] = useState<ServiceState | null>(null);
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [usage, setUsage] = useState<ModelUsage | null>(null);
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [preferenceChanges, setPreferenceChanges] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [testVisible, setTestVisible] = useState(false);
  const [testingAll, setTestingAll] = useState(false);
  const [testReport, setTestReport] = useState<ServiceTestReport | null>(null);
  const [testError, setTestError] = useState("");
  const [testHovered, setTestHovered] = useState(false);
  const [testFocused, setTestFocused] = useState(false);
  const [budget, setBudget] = useState({ amount: "", currency: "CNY", basis: "每人", period: "每天", category: "住宿" });
  useEffect(() => {
    let active = true;
    if (section !== "preferences") {
      void api<ServiceState>("/settings/services").then(v => { if (active) setServices(v); }).catch(() => { if (active) setMessage("服务配置读取失败，请检查本地文件权限和格式。"); });
      void api<ModelUsage>("/settings/model-usage").then(v => { if (active) setUsage(v); }).catch(() => { if (active) setUsage(null); });
    }
    if (section !== "services")
      void api<Preferences>("/preferences").then(v => { if (active) setPrefs(v); }).catch(() => { if (active) setMessage("偏好读取失败。"); });
    return () => { active = false; };
  }, [section]);

  useEffect(() => {
    if (!testVisible || testingAll || testHovered || testFocused) return;
    const timer = window.setTimeout(() => setTestVisible(false), 20_000);
    return () => window.clearTimeout(timer);
  }, [testVisible, testingAll, testHovered, testFocused, testReport, testError]);

  async function act(work: () => Promise<void>) {
    setBusy(true); setMessage("");
    try { await work(); } catch (error) { setMessage(error instanceof Error ? error.message : "操作未完成，请重试。"); }
    finally { setBusy(false); }
  }
  async function savePreferences(extra: Record<string, unknown> = {}) {
    if (!prefs) return;
    const values = { ...preferenceChanges };
    if (budget.amount) values.budget = { ...budget, amount: Number(budget.amount) };
    const result = await api<Preferences>("/preferences", { method: "PATCH", body: JSON.stringify({ version: prefs.version, values, ...extra }) });
    setPrefs(result); setPreferenceChanges({}); setBudget({ ...budget, amount: "" }); setMessage("偏好已更新；不改变已有行程。");
  }
  return <section className="service-settings">
    {section !== "preferences" && <RuntimeStatusPanel />}
    {section !== "preferences" && <>
    {testVisible && <aside className="service-test-notice" role="region" aria-label="服务测试结果"
      aria-busy={testingAll} onMouseEnter={() => setTestHovered(true)} onMouseLeave={() => setTestHovered(false)}
      onFocusCapture={() => setTestFocused(true)} onBlurCapture={event => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setTestFocused(false);
      }}>
      <div className="service-test-heading"><h3>服务连接测试</h3>
        <button type="button" aria-label="关闭服务测试结果" onClick={() => setTestVisible(false)}>×</button></div>
      <div aria-live="polite">
        {testingAll && <p>正在测试已保存的模型、Tavily 和高德，请稍候…</p>}
        {testError && <p className="service-test-error">{testError}</p>}
        {testReport && <>
          <p className="service-test-summary">通过 {testReport.summary.ok} 项 · 失败 {testReport.summary.failed} 项 · 跳过 {testReport.summary.skipped} 项</p>
          <ul className="service-test-results">{testReport.results.map((row, index) => <li key={`${row.service}:${row.model_id}:${index}`}>
            <div><strong>{row.label}</strong><span className={`service-test-badge ${row.status}`}>
              {row.status === "ok" ? "通过" : row.status === "failed" ? "失败" : "跳过"}</span></div>
            {row.model_id && <small>{row.model_id}</small>}
            <p>{row.detail}{row.elapsed_ms > 0 && <span> · {(row.elapsed_ms / 1000).toFixed(1)} 秒</span>}</p>
          </li>)}</ul>
          {testReport.restart_required && <p>本次测试的是已保存的新配置；正在运行的 Agent 仍需重启后采用新配置。</p>}
        </>}
      </div>
      {!testingAll && <small>20 秒后自动关闭；悬停或聚焦可暂停，也可点击右上角关闭。</small>}
    </aside>}
    <p>保存在本机项目 .env，重启后生效。修改启动环境提供的字段时，Web 会记住该字段改用项目配置。密钥留空保留原值，已存密钥不回显。</p>
    {services && serviceGroups.map(group => <SettingsGroup key={group.title} title={group.title} description={group.description} initiallyOpen={group.defaultOpen}>
      {group.keys.filter(key => services.fields[key]).map(key => { const field = services.fields[key]; const help = fieldHelp[key]; const control = fieldControls[key]; return <div className="settings-field" key={key}>
      <label htmlFor={`setting-${key}`}>{labels[key]} <small>{field.configured ? "已配置" : "未配置"} · {field.source === "environment" ? "启动环境控制" : field.source === "parent" ? "父目录回退" : field.source === "default" ? "默认值" : "项目配置"}</small></label>
      {help && <small id={`setting-help-${key}`} className="settings-help">{help.description}{help.example && <> 示例：<code>{help.example}</code></>}</small>}
      {field.source === "environment" && field.editable && <small>当前由启动环境提供；保存替换值后，Web 重启时将优先使用本项目配置。</small>}
      {field.overrides_environment && <small>Web 已指定此字段优先使用项目配置。{field.pending_restart ? "等待重启生效。" : ""}</small>}
      {!field.editable && key !== "SEARCH_BASE_URL" && <small>当前后端将此字段设为只读；请更新并重启后端，再刷新页面。</small>}
      <div className="settings-input-row">
        {key === "AI_BUDGET_TIER" ? <select id={`setting-${key}`} aria-describedby={`setting-help-${key}`} disabled={busy || !field.editable}
          value={(changes[key] ?? field.value) || "balanced"} onChange={e => setChanges({ ...changes, [key]: e.target.value })}>
          <option value="economy">经济</option><option value="balanced">均衡</option><option value="quality">高质量</option>
        </select> : ["AI_SUMMARY_ENABLED", "AI_VECTOR_ENABLED"].includes(key) ? <select id={`setting-${key}`} aria-describedby={`setting-help-${key}`} disabled={busy || !field.editable}
          value={(changes[key] ?? field.value) || "false"} onChange={e => setChanges({ ...changes, [key]: e.target.value })}>
          <option value="false">关闭</option><option value="true">启用</option>
        </select> : <input id={`setting-${key}`} aria-describedby={`setting-help-${key}`} type={control?.type || "text"} inputMode={control?.inputMode} step={control?.step} min={control?.min} max={control?.max}
          autoComplete="off" spellCheck={false} disabled={busy || !field.editable || key === "SEARCH_BASE_URL"} maxLength={control?.maxLength}
          placeholder={control?.placeholder || "留空表示不设置"}
          value={changes[key] === null ? "" : changes[key] ?? field.value}
          onChange={e => setChanges({ ...changes, [key]: ["LLM_PRICE_CURRENCY","AI_COST_CURRENCY"].includes(key) ? e.target.value.toUpperCase().slice(0, 3) : e.target.value })} />}
        {key.endsWith("KEY") && <button disabled={busy || !field.editable} onClick={() => setChanges({ ...changes, [key]: changes[key] === null ? "" : null })}>
          {changes[key] === null ? "撤销清除" : "清除"}</button>}
      </div>
      {changes[key] === null && <small>保存后清空该项目凭据，并抑制父目录回退。</small>}
    </div>})}</SettingsGroup>)}
    <p>搜索当前使用 Tavily 官方地址。更换模型地址时须重新输入匹配该地址的 Key。URL 不能包含密钥或查询参数。</p>
    <div className="settings-actions">
      <button disabled={busy || !services || Object.keys(changes).length === 0} onClick={() => void act(async () => {
        const modelUrl = changes.LLM_BASE_URL;
        if (typeof modelUrl === "string" && modelUrl !== "" && modelUrl.trim().replace(/\/+$/, "") !== services!.fields.LLM_BASE_URL?.value.replace(/\/+$/, "")
          && services!.fields.LLM_API_KEY?.configured && !changes.LLM_API_KEY?.trim())
          throw new Error("更换模型地址时，请同时输入用于新地址的模型 Key，再保存。");
        const result = await api<ServiceState>("/settings/services", { method: "PATCH", body: JSON.stringify({ version: services!.version, changes }) });
        setServices(result); setChanges({}); setMessage("已保存本次修改的字段，其他配置保持不变。请重启后端使新配置生效。");
      })}>保存服务配置</button>
      <button disabled={busy || !services || Object.keys(changes).length > 0} onClick={() => void act(async () => {
        setTestVisible(true); setTestHovered(false); setTestFocused(false); setTestingAll(true); setTestReport(null); setTestError("");
        try {
          const result = await api<ServiceTestReport>("/settings/services/test", { method: "POST", body: JSON.stringify({ service: "all" }) });
          setTestReport(result);
          void api<ModelUsage>("/settings/model-usage").then(setUsage).catch(() => {});
        } catch (error) {
          setTestError(error instanceof Error ? error.message : "服务测试未完成，请稍后重试。");
        } finally { setTestingAll(false); }
      })}>{testingAll ? "正在测试全部服务…" : "一键测试全部服务"}</button>
      <button disabled={busy || !services || Object.keys(changes).length > 0} onClick={() => void act(async () => {
        await api("/settings/services/test", { method: "POST", body: JSON.stringify({ service: "search" }) }); setMessage("Tavily 搜索测试成功。");
      })}>测试已保存搜索配置</button>
      <button disabled={busy || !services || Object.keys(changes).length > 0} onClick={() => void act(async () => {
        await api("/settings/services/test", { method: "POST", body: JSON.stringify({ service: "llm" }) }); setMessage("模型列表接口测试成功；实际推理能力需另测。");
      })}>测试已保存模型地址</button>
    </div>
    <small>请先保存修改再测试。一键测试会向已配置的不同模型发送最小推理请求、向视觉模型发送固定测试图片，并调用 Tavily 和高德，可能消耗少量费用或额度。单独的模型地址测试仅请求 /models；所有测试均不发送聊天资料。</small>
    {services?.restart_required && <p role="status">配置已保存，当前运行服务仍使用启动时配置，待重启。</p>}

    <h3>模型用量</h3>
    {usage ? <>
      <p>调用 {usage.totals.calls ?? 0} 次；已知输入 {usage.totals.prompt_tokens ?? 0}；已知缓存输入 {usage.totals.cached_input_tokens ?? 0}；已知输出 {usage.totals.completion_tokens ?? 0}。</p>
      {(usage.totals.cache_detail_missing ?? 0)>0 && <small>{usage.totals.cache_detail_missing} 次调用缓存拆分未知；已知缓存输入为0不代表这些调用未命中。</small>}
      {(usage.totals.unknown_usage ?? 0)>0 && <small>{usage.totals.unknown_usage} 次调用用量缺失或无效；上述合计仅包含已知值。</small>}
      {usage.totals.calls > 0 && <p>估算费用合计：{formatUsageCost(usage.totals, usage.currency)}</p>}
      {services?.restart_required && <p role="status">费率配置已经保存，但当前运行服务尚未重启；报表仍按当前进程中的旧费率计算。</p>}
      {usage.budget && <><p>档位：{usage.budget.tier}；今日预算占用 {usage.budget.day_reserved}/{usage.budget.daily_limit || "不限"}；本月占用 {usage.budget.month_reserved}/{usage.budget.monthly_limit || "不限"}。</p>
        <small>今日供应商实际token {usage.budget.day_actual_tokens ?? 0}；在途保守预留 {usage.budget.day_inflight ?? 0}；未知用量占用 {usage.budget.day_unknown_occupied ?? 0}；历史保守占用 {usage.budget.day_legacy_occupied ?? 0}。预留采用估算，不代表账单实际token。已有周期保留原统计，上海时区切换日期：日 {usage.budget.day_cutover || "未提供"} / 月 {usage.budget.month_cutover || "未提供"}。</small></>}
      {usage.purposes.length > 0 && <div className="settings-table-scroll"><table><thead><tr><th>用途</th><th>模型</th><th>调用</th><th>输入/缓存/输出</th><th>估算费用</th></tr></thead><tbody>
        {usage.purposes.map(row => <tr key={`${row.purpose}:${row.model_id}`}><td>{row.purpose}</td><td>{row.model_id}</td><td>{row.calls}</td>
          <td>{row.prompt_tokens}/{row.cached_input_tokens}/{row.completion_tokens}</td>
          <td>{formatUsageCost(row, usage.currency)}</td></tr>)}
      </tbody></table></div>}
      <small>{usage.cost_note}</small>
    </> : <p>暂时无法读取模型用量。</p>}
    </>}

    {section !== "services" && <>
    <p>同一本地使用者跨网页会话共享。文件、图片、聊天和行程不会跨会话读取；QQ 偏好不互通。本次要求优先。</p>
    {prefs && <>
      <label><input type="checkbox" checked={prefs.enabled} disabled={busy} onChange={e => void act(() => savePreferences({ enabled: e.target.checked, values: {} }))} /> 应用已保存偏好</label>
      <label><input type="checkbox" checked={prefs.suggestions} disabled={busy} onChange={e => void act(() => savePreferences({ suggestions: e.target.checked, values: {} }))} /> 提供偏好保存建议</label>
      {Object.entries(choices).map(([key, options]) => <label className="settings-field" key={key}>{preferenceLabels[key]}
        <select disabled={busy} value={String(preferenceChanges[key] === null ? "" : preferenceChanges[key] ?? prefs.values[key] ?? "")} onChange={e => setPreferenceChanges({ ...preferenceChanges, [key]: e.target.value || null })}>
          <option value="">不设置</option>{options.map(value => <option key={value}>{value}</option>)}
        </select></label>)}
      <fieldset disabled={busy}><legend>兴趣类别</legend>{["博物馆", "历史街区", "自然景观", "美食", "公园", "艺术"].map(value => {
        const selected = (preferenceChanges.interests === null ? [] : preferenceChanges.interests ?? prefs.values.interests ?? []) as string[];
        return <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={e => {
          const next = e.target.checked ? [...selected, value] : selected.filter(v => v !== value);
          setPreferenceChanges({ ...preferenceChanges, interests: next.length ? next : null });
        }} />{value}</label>;
      })}</fieldset>
      <label className="settings-field">常用出发城市<input disabled={busy} maxLength={30} value={String(preferenceChanges.departure_city === null ? "" : preferenceChanges.departure_city ?? prefs.values.departure_city ?? "")}
        onChange={e => setPreferenceChanges({ ...preferenceChanges, departure_city: e.target.value || null })} /></label>
      <fieldset disabled={busy}><legend>默认预算（留空不修改）</legend>
        <input aria-label="预算金额" type="number" min="1" max="1000000" value={budget.amount} onChange={e => setBudget({ ...budget, amount: e.target.value })} />
        {([['currency', ['CNY', 'USD', 'EUR']], ['basis', ['每人', '总额']], ['period', ['每天', '全程']], ['category', ['住宿', '餐饮', '交通', '旅行总预算']]] as const).map(([key, options]) =>
          <select aria-label={key} key={key} value={budget[key]} onChange={e => setBudget({ ...budget, [key]: e.target.value })}>{options.map(v => <option key={v}>{v}</option>)}</select>)}
        {prefs.values.budget != null && <p>已保存：{(() => { const value = prefs.values.budget as Record<string, unknown>; return `${value.currency || ""} ${value.amount || ""} · ${value.basis || ""} · ${value.period || ""} · ${value.category || ""}`; })()} <button onClick={() => { setPreferenceChanges({ ...preferenceChanges, budget: null }); setBudget({ ...budget, amount: "" }); }}>标记清除预算</button></p>}
      </fieldset>
      <div className="settings-actions"><button disabled={busy} onClick={() => void act(() => savePreferences())}>保存偏好</button>
        <button disabled={busy} onClick={() => void act(() => savePreferences({ clear: true, values: {} }))}>忘记所有偏好</button></div>
      <small>忘记偏好不删除既有聊天回执、行程或备份。也可在聊天中说“记住，以后优先公共交通”。</small>
    </>}
    </>}
    <p role="status" aria-live="polite">{busy ? "正在处理…" : message}</p>
  </section>;
}
