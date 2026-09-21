import { useEffect, useState } from "react";
import { api } from "./api";

interface ServiceField { configured: boolean; source: string; editable: boolean; value: string; pending_restart: boolean }
interface ServiceState { version: string; fields: Record<string, ServiceField>; restart_required: boolean }
interface Preferences { version: number; enabled: boolean; suggestions: boolean; values: Record<string, unknown>; scope: string }
const labels: Record<string, string> = { SEARCH_API_KEY: "Tavily 搜索 Key", SEARCH_BASE_URL: "搜索地址", LLM_API_KEY: "模型 Key", LLM_BASE_URL: "模型地址", LLM_MODEL_ID: "模型 ID", AMAP_API_KEY: "高德 Key" };
const choices: Record<string, string[]> = { pace: ["轻松", "适中", "紧凑"], transport: ["公共交通", "步行", "驾车"], night: ["不安排夜游", "可以夜游"], reply_style: ["简洁", "详细", "表格"] };
const preferenceLabels: Record<string, string> = { pace: "行程节奏", transport: "交通偏好", night: "夜间活动", reply_style: "回复风格", interests: "兴趣类别", departure_city: "常用出发城市", budget: "默认预算" };

export function SettingsPanel() {
  const [services, setServices] = useState<ServiceState | null>(null);
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [changes, setChanges] = useState<Record<string, string | null>>({});
  const [preferenceChanges, setPreferenceChanges] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [budget, setBudget] = useState({ amount: "", currency: "CNY", basis: "每人", period: "每天", category: "住宿" });
  useEffect(() => {
    let active = true;
    void api<ServiceState>("/settings/services").then(v => { if (active) setServices(v); }).catch(() => { if (active) setMessage("服务配置读取失败，请检查本地文件权限和格式。"); });
    void api<Preferences>("/preferences").then(v => { if (active) setPrefs(v); }).catch(() => { if (active) setMessage("偏好读取失败。"); });
    return () => { active = false; };
  }, []);

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
    <h3>服务配置</h3>
    <p>保存在本机项目 .env，重启后生效；模型和高德配置也可能影响 QQ 下次启动。密钥留空保留原值，已存密钥不回显。</p>
    {services && Object.entries(services.fields).map(([key, field]) => <div className="settings-field" key={key}>
      <label htmlFor={`setting-${key}`}>{labels[key]} <small>{field.configured ? "已配置" : "未配置"} · {field.source === "environment" ? "启动环境控制" : field.source === "parent" ? "父目录回退" : "项目配置"}</small></label>
      <div className="settings-input-row">
        <input id={`setting-${key}`} type={key.endsWith("KEY") ? "password" : "text"} autoComplete="off" spellCheck={false}
          disabled={busy || !field.editable || key === "SEARCH_BASE_URL"} maxLength={4096}
          placeholder={key.endsWith("KEY") ? "留空保留；输入新值才替换" : "服务地址或模型名称"}
          value={changes[key] === null ? "" : changes[key] ?? field.value}
          onChange={e => setChanges({ ...changes, [key]: e.target.value })} />
        {key.endsWith("KEY") && <button disabled={busy || !field.editable} onClick={() => setChanges({ ...changes, [key]: changes[key] === null ? "" : null })}>
          {changes[key] === null ? "撤销清除" : "清除"}</button>}
      </div>
      {changes[key] === null && <small>保存后清空该项目凭据，并抑制父目录回退。</small>}
    </div>)}
    <p>搜索当前使用 Tavily 官方地址。更换模型地址时须重新输入匹配该地址的 Key。URL 不能包含密钥或查询参数。</p>
    <div className="settings-actions">
      <button disabled={busy || !services} onClick={() => void act(async () => {
        const result = await api<ServiceState>("/settings/services", { method: "PATCH", body: JSON.stringify({ version: services!.version, changes }) });
        setServices(result); setChanges({}); setMessage("已保存本次修改的字段，其他配置保持不变。请重启后端使新配置生效。");
      })}>保存服务配置</button>
      <button disabled={busy || !services || Object.keys(changes).length > 0} onClick={() => void act(async () => {
        await api("/settings/services/test", { method: "POST", body: JSON.stringify({ service: "search" }) }); setMessage("Tavily 搜索测试成功。");
      })}>测试已保存搜索配置</button>
      <button disabled={busy || !services || Object.keys(changes).length > 0} onClick={() => void act(async () => {
        await api("/settings/services/test", { method: "POST", body: JSON.stringify({ service: "llm" }) }); setMessage("模型列表接口测试成功；实际推理能力需另测。");
      })}>测试已保存模型地址</button>
    </div>
    <small>搜索测试会向 api.tavily.com 发起一次固定查询，可能消耗服务额度；模型测试请求已保存地址的 /models，不发送聊天资料。</small>
    {services?.restart_required && <p role="status">配置已保存，当前运行服务仍使用启动时配置，待重启。</p>}

    <h3>我的偏好</h3>
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
        {prefs.values.budget != null && <p>已保存：{JSON.stringify(prefs.values.budget)} <button onClick={() => { setPreferenceChanges({ ...preferenceChanges, budget: null }); setBudget({ ...budget, amount: "" }); }}>标记清除预算</button></p>}
      </fieldset>
      <div className="settings-actions"><button disabled={busy} onClick={() => void act(() => savePreferences())}>保存偏好</button>
        <button disabled={busy} onClick={() => void act(() => savePreferences({ clear: true, values: {} }))}>忘记所有偏好</button></div>
      <small>忘记偏好不删除既有聊天回执、行程或备份。也可在聊天中说“记住，以后优先公共交通”。</small>
    </>}
    <p role="status" aria-live="polite">{busy ? "正在处理…" : message}</p>
  </section>;
}
