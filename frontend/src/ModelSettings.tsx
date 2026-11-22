import { useEffect, useState } from "react";
import "./ModelSettings.css";
import { purposeName } from "./ModelUsagePanel";

export interface ModelConnection {
  id: string; name: string; base_url: string; protocol: "openai_chat_completions";
  key_configured?: boolean; api_key?: string | null;
}
export interface ModelProfile {
  id: string; name: string; connection_id: string; model_id: string; kind: "chat" | "embedding";
  capabilities: string[]; token_parameter: "max_tokens" | "max_completion_tokens"; temperature: "allow" | "omit";
  pricing: { input?: number | string | null; cached?: number | string | null; output?: number | string | null; currency: string; version?: string };
}
export interface ModelConfigInput {
  schema_version: 1; connections: ModelConnection[]; profiles: ModelProfile[];
  roles: Record<string,string>; purpose_overrides: Record<string,string>;
  cost_limits: Record<string,{ daily?: number | string | null; monthly?: number | string | null }>;
}
export interface ModelConfigStatus extends ModelConfigInput {
  valid: boolean; legacy: boolean; configured: boolean; error?: string; source: string;
  pending_restart: boolean; config_version?: string;
  runtime_config_version?:string;
  runtime_pricing?:{profile_id:string;connection_id:string;model_id:string;pricing:ModelProfile['pricing'];complete:boolean;purposes:string[]}[];
}
const roles: Record<string,string> = { regular:"常规模型", basic:"基础任务", complex:"复杂任务", vision:"图片理解" };
const caps: Record<string,string> = { text:"文本", json:"JSON", tools:"工具调用", vision:"图片输入", embedding:"向量" };
const identifier = (kind: string) => `${kind}-${crypto.randomUUID().slice(0,12)}`;
const priceText=(price?:ModelProfile['pricing'])=>price ? `输入 ${price.input ?? "未填"} / 缓存 ${price.cached ?? price.input ?? "未填"} / 输出 ${price.output ?? "未填"} ${price.currency}（每百万 Token）` : "尚未配置";
function editable(value: ModelConfigStatus): ModelConfigInput {
  return { schema_version:1, connections:value.connections.map(c => ({ ...c, api_key:"" })),
    profiles:structuredClone(value.profiles), roles:{ ...value.roles },
    purpose_overrides:{ ...value.purpose_overrides }, cost_limits:structuredClone(value.cost_limits) };
}

export function ModelSettings({ value, revision, disabled, unsaved, onChange, onProbe }: {
  value:ModelConfigStatus; revision:string; disabled:boolean; unsaved:boolean;
  onChange:(value:ModelConfigInput | null)=>void; onProbe:(profile:string,capability:string)=>void;
}) {
  const [draft,setDraft]=useState(() => editable(value));
  useEffect(() => { setDraft(editable(value)); onChange(null); },[value,revision,onChange]);
  function change(next:ModelConfigInput) { setDraft(next); onChange(next); }
  function connection(id:string,patch:Partial<ModelConnection>) {
    change({ ...draft,connections:draft.connections.map(c => c.id===id ? { ...c,...patch } : c) });
  }
  function profile(id:string,patch:Partial<ModelProfile>) {
    change({ ...draft,profiles:draft.profiles.map(p => p.id===id ? { ...p,...patch,
      ...((patch.model_id!==undefined && patch.model_id!==p.model_id || patch.connection_id!==undefined && patch.connection_id!==p.connection_id)
        ? { pricing:{ currency:p.pricing.currency } } : {}) } : p) });
  }
  const currencies=[...new Set([...draft.profiles.map(p => p.pricing.currency),...Object.keys(draft.cost_limits)])].filter(Boolean);
  return <section className="model-settings" aria-label="模型连接与分工">
    <h3>模型连接与分工</h3>
    {!value.valid && <p role="alert">{value.error} 请修正配置后保存，当前不会自动采用旧供应商。</p>}
    {value.legacy && <p>当前沿用原模型配置，尚未迁移。下方已保留原意图、摘要、研究和视觉的独立分配；保存后使用新的连接与模型档案。</p>}
    <p>连接只保存一次地址与 Key，模型档案可共用连接。基础和复杂任务留空时沿用常规模型；输出预算档位单独设置。</p>
    <fieldset disabled={disabled} className="model-config-section"><legend>供应商连接</legend>
      {draft.connections.map(c => <fieldset className="model-connection" key={c.id} aria-label={`连接 ${c.name}`}>
        <legend>{c.name || "新连接"}</legend>
        <label>连接名称<input value={c.name} maxLength={80} onChange={e => connection(c.id,{ name:e.target.value })}/></label>
        <label>API 地址<input type="url" value={c.base_url} placeholder="https://api.example.com/v1" maxLength={2048} onChange={e => connection(c.id,{ base_url:e.target.value })}/></label>
        <label>连接 Key <small>{c.key_configured ? "已保存，留空保留" : "未配置"}</small>
          <input type="password" autoComplete="off" value={c.api_key ?? ""} maxLength={4096} onChange={e => connection(c.id,{ api_key:e.target.value })}/></label>
        <div className="model-config-actions"><button type="button" onClick={() => connection(c.id,{ api_key:c.api_key===null ? "" : null })}>{c.api_key===null ? "撤销清除 Key" : "清除连接 Key"}</button>
          <button type="button" disabled={draft.profiles.some(p => p.connection_id===c.id)} onClick={() => change({ ...draft,connections:draft.connections.filter(row => row.id!==c.id) })}>删除连接</button></div>
        {c.api_key===null && <small>保存后清空此连接的 Key；其他连接保持原值。</small>}
        <small>OpenAI-compatible Chat Completions；更换地址须重新输入新地址的 Key。删除前须先移走所引用的模型。</small>
      </fieldset>)}
      <button type="button" disabled={draft.connections.length>=12} onClick={() => change({ ...draft,connections:[...draft.connections,
        { id:identifier("connection"),name:"新连接",base_url:"",api_key:"",protocol:"openai_chat_completions" }] })}>添加连接</button>
    </fieldset>
    <fieldset disabled={disabled} className="model-config-section"><legend>模型档案</legend>
      {draft.profiles.map(p => <fieldset className="model-profile" key={p.id} aria-label={`模型 ${p.name}`}>
        <legend>{p.name || "新模型"}</legend>
        <div className="model-config-grid"><label>档案名称<input value={p.name} maxLength={80} onChange={e => profile(p.id,{ name:e.target.value })}/></label>
          <label>所属连接<select value={p.connection_id} onChange={e => profile(p.id,{ connection_id:e.target.value })}>{draft.connections.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
          <label>实际模型 ID<input value={p.model_id} maxLength={200} onChange={e => profile(p.id,{ model_id:e.target.value })}/></label>
          <label>模型类型<select value={p.kind} onChange={e => profile(p.id,{ kind:e.target.value as ModelProfile["kind"],capabilities:e.target.value==="embedding" ? ["embedding"] : ["text","json","tools"] })}><option value="chat">聊天模型</option><option value="embedding">云向量实验</option></select></label></div>
        <div className="model-capabilities" aria-label="声明能力">{Object.entries(caps).filter(([key]) => p.kind==="embedding" ? key==="embedding" : key!=="embedding").map(([key,label]) =>
          <label key={key}><input type="checkbox" checked={p.capabilities.includes(key)} disabled={key==="text" || key==="embedding"} onChange={e => profile(p.id,{ capabilities:e.target.checked ? [...p.capabilities,key] : p.capabilities.filter(c => c!==key) })}/>{label}</label>)}</div>
        <details><summary>参数与价格</summary><div className="model-config-grid">
          <label>输出上限参数<select value={p.token_parameter} onChange={e => profile(p.id,{ token_parameter:e.target.value as ModelProfile["token_parameter"] })}><option value="max_completion_tokens">max_completion_tokens</option><option value="max_tokens">max_tokens</option></select></label>
          <label>温度参数<select value={p.temperature} onChange={e => profile(p.id,{ temperature:e.target.value as ModelProfile["temperature"] })}><option value="allow">保留调用方设置</option><option value="omit">不发送温度参数</option></select></label>
          <label>价格币种<input value={p.pricing.currency} maxLength={3} onChange={e => profile(p.id,{ pricing:{ ...p.pricing,currency:e.target.value.toUpperCase() } })}/></label>
          {([['input','普通输入 / 百万 tokens'],['cached','缓存输入 / 百万 tokens'],['output','输出 / 百万 tokens']] as const).map(([key,label]) => <label key={key}>{label}<input type="number" min="0" step="any" value={p.pricing[key] ?? ""} placeholder={key==="cached" ? "留空按普通输入费率" : "留空表示费用未知"} onChange={e => profile(p.id,{ pricing:{ ...p.pricing,[key]:e.target.value==="" ? null : Number(e.target.value) } })}/></label>)}
        </div></details>
        <details><summary>费率生效状态 · {unsaved ? "有未保存修改" : value.pending_restart ? "已保存，待重启" : "与运行配置一致"}</summary>
          <p>服务器已保存：{priceText(value.profiles.find(row=>row.id===p.id)?.pricing)}</p>
          {value.runtime_pricing?.filter(row=>row.profile_id===p.id).map(row=><div key={row.profile_id}>
            <p>当前运行（{row.model_id}）：{priceText(row.pricing)} · {row.complete ? "费率完整" : "缺少费率"}</p>
            <p>实际用途：{row.purposes.map(purposeName).join("、") || "未分配"}</p>
          </div>)}
          <small>保存版本 {value.config_version?.slice(0,8) || "未提供"} · 运行版本 {value.runtime_config_version?.slice(0,8) || "未提供"}。新调用冻结当时费率；历史缺价仅在身份匹配时按当前费率补估。</small>
        </details>
        <div className="model-config-actions">{p.capabilities.map(cap => <button type="button" key={cap} disabled={unsaved} onClick={() => onProbe(p.id,cap)}>测试{caps[cap]}</button>)}
          <button type="button" disabled={[...Object.values(draft.roles),...Object.values(draft.purpose_overrides)].includes(p.id)} onClick={() => change({ ...draft,profiles:draft.profiles.filter(row => row.id!==p.id) })}>删除模型</button></div>
        <small>能力为配置声明；显式测试使用固定资料，会产生模型调用。文本通过不代表 JSON、工具或图片通过。删除前须先调整分工。</small>
        {(p.pricing.input==null || p.pricing.input==="" || p.pricing.output==null || p.pricing.output==="") && <small>单价未完整，费用保持未知；更换模型或连接后需重新填写对应费率。</small>}
      </fieldset>)}
      <button type="button" disabled={!draft.connections.length || draft.profiles.length>=36} onClick={() => {
        const id=identifier("profile");change({ ...draft,profiles:[...draft.profiles,{ id,name:"新模型",connection_id:draft.connections[0].id,model_id:"",kind:"chat",capabilities:["text","json","tools"],token_parameter:"max_completion_tokens",temperature:"allow",pricing:{ currency:"USD" } }],roles:draft.roles.regular ? draft.roles : { ...draft.roles,regular:id } });
      }}>添加模型档案</button>
    </fieldset>
    <fieldset disabled={disabled} className="model-config-section"><legend>模型分工</legend>
      <div className="model-config-grid">{Object.entries(roles).map(([key,label]) => <label key={key}>{label}<select value={draft.roles[key] || ""} onChange={e => { const next={ ...draft.roles }; if (e.target.value) next[key]=e.target.value; else delete next[key];change({ ...draft,roles:next }); }}>
        <option value="">{key==="regular" ? "请选择（必选）" : key==="vision" ? "不启用图片理解" : "沿用常规模型"}</option>
        {draft.profiles.filter(p => p.kind==="chat" && (key!=="vision" || p.capabilities.includes("vision"))).map(p => <option key={p.id} value={p.id}>{p.name} / {draft.connections.find(c => c.id===p.connection_id)?.name}</option>)}</select></label>)}</div>
      {Object.keys(draft.purpose_overrides).length>0 && <details><summary>高级：原用途覆盖（优先于三项分工）</summary>{Object.entries(draft.purpose_overrides).map(([purpose,id]) => <label key={purpose}>{purpose}<select value={id} onChange={e => { const next={ ...draft.purpose_overrides }; if (e.target.value) next[purpose]=e.target.value; else delete next[purpose];change({ ...draft,purpose_overrides:next }); }}>
        <option value="">取消覆盖，使用分工</option>{draft.profiles.filter(p => p.kind==="chat" && (purpose!=="image_extract" || p.capabilities.includes("vision"))).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>)}</details>}
    </fieldset>
    <fieldset disabled={disabled} className="model-config-section"><legend>按币种金额上限</legend>
      {currencies.map(currency => <div className="model-config-grid" key={currency}>{([['daily','日上限'],['monthly','月上限']] as const).map(([key,label]) => <label key={key}>{currency} {label}<input type="number" min="0" step="any" placeholder="0 表示该币种不限金额" value={draft.cost_limits[currency]?.[key] ?? ""} onChange={e => change({ ...draft,cost_limits:{ ...draft.cost_limits,[currency]:{ ...draft.cost_limits[currency],[key]:e.target.value==="" ? 0 : Number(e.target.value) } } })}/></label>)}</div>)}
      <small>币种分别计费，不作汇率换算。未设置金额上限的币种仍受 Token、次数和并发上限控制；费用未知时不能承诺金额上限达标。</small>
    </fieldset>
    {value.pending_restart && <p role="status">模型配置已保存，重启后生效；当前运行分工见上方版本信息。</p>}
  </section>;
}
