import { labels, fieldHelp, fieldControls, serviceGroups, fieldChoices } from "./settingsFields";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import type { RailMCPStatus } from "./api";
import { RuntimeStatusPanel } from "./RuntimeStatusPanel";
import { ModelSettings } from "./ModelSettings";
import { ModelUsagePanel } from "./ModelUsagePanel";
import type { ModelUsage } from "./ModelUsagePanel";
import type { ModelConfigInput, ModelConfigStatus } from "./ModelSettings";

interface ServiceField { configured: boolean; source: string; editable: boolean; value: string; pending_restart: boolean; overrides_environment?: boolean }
interface ServiceState { version: string; fields: Record<string, ServiceField>; restart_required: boolean; rail_mcp?: RailMCPStatus; model_config?:ModelConfigStatus }
interface PreferenceFields { labels: Record<string,string>; choices: Record<string,string[]>; interests: string[]; budget: Record<string,string[]> }
interface Preferences { fields: PreferenceFields; version: number; enabled: boolean; suggestions: boolean; values: Record<string, unknown>; scope: string }
interface ServiceTestReport { results: { label: string; model_id: string; service: string; status: "ok" | "failed" | "skipped"; detail: string; elapsed_ms: number }[]; summary: { ok: number; failed: number; skipped: number }; restart_required?: boolean }
function SettingsGroup({ title, description, initiallyOpen, children }: { title: string; description: string; initiallyOpen: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(initiallyOpen);
  return <details className="settings-group" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span><strong>{title}</strong><small>{description}</small></span></summary>
    <div className="settings-group-fields">{children}</div>
  </details>;
}

export function SettingsPanel({ section = "all" }: { section?: "services" | "preferences" | "all" }) {
  const [services, setServices] = useState<ServiceState | null>(null);
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [usage, setUsage] = useState<ModelUsage | null>(null);
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [modelDraft,setModelDraft] = useState<ModelConfigInput | null>(null);
  const [preferenceChanges, setPreferenceChanges] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [testVisible, setTestVisible] = useState(false);
  const [testingAll, setTestingAll] = useState(false);
  const [testReport, setTestReport] = useState<ServiceTestReport | null>(null);
  const [testError, setTestError] = useState("");
  const [testHovered, setTestHovered] = useState(false);
  const [testFocused, setTestFocused] = useState(false);
  const [budget, setBudget] = useState<Record<string,string>>({ amount: "" });
  useEffect(() => {
    let active = true;
    if (section !== "preferences") {
      void api<ServiceState>("/settings/services").then(v => { if (active) setServices(v); }).catch(() => { if (active) setMessage("服务配置读取失败，请检查本地文件权限和格式。"); });
      void api<ModelUsage>("/settings/model-usage").then(v => { if (active) setUsage(v); }).catch(() => { if (active) setUsage(null); });
    }
    if (section !== "services")
      void api<Preferences>("/preferences").then(v => { if (active) { setPrefs(v); setBudget({ amount: "", ...Object.fromEntries(Object.entries(v.fields.budget).map(([key,options]) => [key,options[0]])) }); } }).catch(() => { if (active) setMessage("偏好读取失败。"); });
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
  const dirty=Object.keys(changes).length>0 || modelDraft!==null;
  async function testServices(target:Record<string,string>) {
    await act(async () => {
      setTestVisible(true); setTestHovered(false); setTestFocused(false); setTestingAll(true); setTestReport(null); setTestError("");
      try {
        setTestReport(await api<ServiceTestReport>("/settings/services/test", { method:"POST",body:JSON.stringify(target) }));
        void api<ModelUsage>("/settings/model-usage").then(setUsage).catch(() => {});
      } catch (error) { setTestError(error instanceof Error ? error.message : "服务测试未完成，请稍后重试。"); }
      finally { setTestingAll(false); }
    });
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
        {testingAll && <p>正在测试已保存的服务，请稍候…</p>}
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
    {services?.model_config && <ModelSettings value={services.model_config} revision={services.version} disabled={busy} unsaved={dirty}
      onChange={setModelDraft} onProbe={(profile_id,capability) => void testServices({ service:"model",profile_id,capability })}/>}
    {services && serviceGroups.map(group => ({ ...group,
      title:services.model_config && group.keys.some(key => key==="AMAP_API_KEY") ? "地图服务" : group.title,
      description:services.model_config && group.keys.some(key => key==="AMAP_API_KEY") ? "用于地点、天气和路线查询。" : group.description,
      keys:group.keys.filter(key => services.fields[key] &&
      !(services.model_config && (key.startsWith("LLM_") || ["AI_DAILY_COST_LIMIT","AI_MONTHLY_COST_LIMIT","AI_COST_CURRENCY"].includes(key)))) }))
      .filter(group => group.keys.length).map(group => <SettingsGroup key={group.title} title={group.title} description={group.description} initiallyOpen={group.defaultOpen}>
      {group.keys.map(key => { const field = services.fields[key]; const help = fieldHelp[key]; const control = fieldControls[key]; return <div className="settings-field" key={key}>
      <label htmlFor={`setting-${key}`}>{labels[key]} <small>{field.configured ? "已配置" : "未配置"} · {field.source === "environment" ? "启动环境控制" : field.source === "parent" ? "父目录回退" : field.source === "default" ? "默认值" : "项目配置"}</small></label>
      {help && <small id={`setting-help-${key}`} className="settings-help">{help.description}{help.example && <> 示例：<code>{help.example}</code></>}</small>}
      {field.source === "environment" && field.editable && <small>当前由启动环境提供；保存替换值后，Web 重启时将优先使用本项目配置。</small>}
      {field.overrides_environment && <small>Web 已指定此字段优先使用项目配置。{field.pending_restart ? "等待重启生效。" : ""}</small>}
      {!field.editable && key !== "SEARCH_BASE_URL" && <small>当前后端将此字段设为只读；请更新并重启后端，再刷新页面。</small>}
      <div className="settings-input-row">
        {fieldChoices[key] ? <select id={`setting-${key}`} aria-describedby={`setting-help-${key}`} disabled={busy || !field.editable}
          value={(changes[key] ?? field.value) || fieldChoices[key].fallback} onChange={e => setChanges({ ...changes, [key]: e.target.value })}>
          {fieldChoices[key].options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select> : <input id={`setting-${key}`} aria-describedby={`setting-help-${key}`} type={control?.type || "text"} inputMode={control?.inputMode} step={control?.step} min={control?.min} max={control?.max}
          autoComplete="off" spellCheck={false} disabled={busy || !field.editable || key === "SEARCH_BASE_URL"} maxLength={control?.maxLength}
          placeholder={control?.placeholder || "留空表示不设置"}
          value={changes[key] === null ? "" : changes[key] ?? field.value}
          onChange={e => setChanges({ ...changes, [key]: ["LLM_PRICE_CURRENCY","AI_COST_CURRENCY"].includes(key) ? e.target.value.toUpperCase().slice(0, 3) : e.target.value })} />}
        {(key.endsWith("KEY") || key === "RAIL_MCP_CONFIG") && <button disabled={busy || !field.editable} onClick={() => setChanges({ ...changes, [key]: changes[key] === null ? "" : null })}>
          {changes[key] === null ? "撤销清除" : "清除"}</button>}
      </div>
      {changes[key] === null && <small>保存后清空该项目凭据，并抑制父目录回退。</small>}
    </div>})}</SettingsGroup>)}
    <p>搜索当前使用 Tavily 官方地址。更换模型地址时须重新输入匹配该地址的 Key。URL 不能包含密钥或查询参数。</p>
    <div className="settings-actions">
      <button disabled={busy || !services || !dirty} onClick={() => void act(async () => {
        const modelUrl = changes.LLM_BASE_URL;
        if (typeof modelUrl === "string" && modelUrl !== "" && modelUrl.trim().replace(/\/+$/, "") !== services!.fields.LLM_BASE_URL?.value.replace(/\/+$/, "")
          && services!.fields.LLM_API_KEY?.configured && !changes.LLM_API_KEY?.trim())
          throw new Error("更换模型地址时，请同时输入用于新地址的模型 Key，再保存。");
        const result = await api<ServiceState>("/settings/services", { method: "PATCH", body: JSON.stringify({ version: services!.version, changes,...(modelDraft ? { model_config:modelDraft } : {}) }) });
        setServices(result); setChanges({}); setModelDraft(null); setMessage("已保存本次修改的字段，其他配置保持不变。请重启后端使新配置生效。");
      })}>保存服务配置</button>
      <button disabled={busy || !services || dirty} onClick={() => void testServices({ service:"all" })}>{testingAll ? "正在测试全部服务…" : "一键测试全部服务"}</button>
    </div>
    <details className="service-test-help"><summary>服务测试说明</summary><p>测试已保存的模型、Tavily、高德、12306 和携程问道。未启用或未配置的服务会跳过；单项失败不影响其他结果。</p><p>最多 12 次模型请求；12306 仅初始化与发现工具，不查车票；问道提交 1 次固定短问句。可能消耗费用或额度，不发送聊天资料，问道费用与用量未知。</p></details>
    {services?.rail_mcp && <small>12306 · {services.rail_mcp.state === "invalid_config" ? "配置无效" : services.rail_mcp.expired ? "配置已过期" : services.rail_mcp.enabled ? "已启用" : "未启用"} · 运行连接{services.rail_mcp.runtime?.state === "ready" ? "就绪" : services.rail_mcp.runtime?.state === "connecting" ? "连接中" : services.rail_mcp.runtime ? "暂不可用" : "未启动"}</small>}
    {services?.restart_required && <p role="status">配置已保存，当前运行服务仍使用启动时配置，待重启。</p>}

    <ModelUsagePanel usage={usage}/>
    </>}

    {section !== "services" && <>
    <p>同一本地使用者跨网页会话共享。文件、图片、聊天和行程不会跨会话读取；QQ 偏好不互通。本次要求优先。</p>
    {prefs && <>
      <label><input type="checkbox" checked={prefs.enabled} disabled={busy} onChange={e => void act(() => savePreferences({ enabled: e.target.checked, values: {} }))} /> 应用已保存偏好</label>
      <label><input type="checkbox" checked={prefs.suggestions} disabled={busy} onChange={e => void act(() => savePreferences({ suggestions: e.target.checked, values: {} }))} /> 提供偏好保存建议</label>
      {Object.entries(prefs.fields.choices).map(([key, options]) => <label className="settings-field" key={key}>{prefs.fields.labels[key]}
        <select disabled={busy} value={String(preferenceChanges[key] === null ? "" : preferenceChanges[key] ?? prefs.values[key] ?? "")} onChange={e => setPreferenceChanges({ ...preferenceChanges, [key]: e.target.value || null })}>
          <option value="">不设置</option>{options.map(value => <option key={value}>{value}</option>)}
        </select></label>)}
      <fieldset disabled={busy}><legend>兴趣类别</legend>{prefs.fields.interests.map(value => {
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
        {Object.entries(prefs.fields.budget).map(([key, options]) =>
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
