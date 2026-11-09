import "./ModelUsagePanel.css";

interface CostBreakdown { currency:string; amount:number }
export interface UsageRow {
  purpose?:string; model_id?:string; connection_id?:string; profile_id?:string; connection_name?:string; profile_name?:string;
  calls:number; prompt_tokens:number; cached_input_tokens:number; completion_tokens:number;
  estimated_cost:number; cost_unknown:number; cost_recalculated?:number; cache_detail_missing?:number;
  unknown_usage?:number; unknown_pricing?:number; currency?:string; cost_mixed?:boolean; cost_breakdown?:CostBreakdown[];
  cost_supplemented?:number; unknown_identity?:number;
  cost_bases?:{basis:string;currency:string;pricing_version:string;amount:number;calls:number}[];
}
export interface ModelUsage {
  totals:UsageRow; purposes:UsageRow[]; currency:string; pricing_active?:boolean; cost_note:string;
  budget?:{ tier:string; daily_limit:number; monthly_limit:number; day_reserved:number; month_reserved:number;
    day_inflight?:number; day_actual_tokens?:number; day_unknown_occupied?:number; day_legacy_occupied?:number;
    day_cutover?:string; month_cutover?:string; cost_limits?:Record<string,{daily?:number|string;monthly?:number|string}>;
    unlimited_currencies?:string[] };
}
const number=(value:number | undefined) => (value ?? 0).toLocaleString("zh-CN");
const purposeNames:Record<string,string>={intent_parse:"意图解析",conversation_summary:"会话摘要",tool_reasoning:"工具推理",tool_answer:"答复整理",
  trip_answer_write:"行程答复",hotel_quote_extract:"酒店报价提取",retrieval_embedding:"检索向量",
  trip_requirement_extract:"行程需求",trip_plan:"行程规划",trip_plan_repair:"行程修正",trip_plan_compare:"草案比较",
  research_plan:"研究规划",research_targets:"研究对象",research_fact_synthesis:"事实整理",research_synthesis:"研究汇总",
  travel_answer_write:"旅行回答",image_extract:"图片理解",document_summary:"文档摘要",service_probe:"服务测试",reminder_extract:"提醒抽取",unspecified:"常规调用"};
export const purposeName=(purpose:string)=>purposeNames[purpose] || purpose;
const tiers:Record<string,string>={economy:"经济",balanced:"均衡",quality:"质量"};

export function ModelIdentity({row}:{row:Pick<UsageRow,"model_id"|"profile_id"|"connection_id"|"connection_name"|"profile_name">}) {
  const model=row.model_id || "未记录模型";
  const connection=row.connection_id && row.connection_id!=="legacy_unknown" ? row.connection_name || row.connection_id : "";
  const labels=[...new Set([connection,row.profile_name!==model ? row.profile_name : ""].filter(value=>value && value!==model))];
  return <span className="model-identity"><span className="model-identity-id">{model}</span>{labels.length>0 && <small>{labels.join(" · ")}</small>}</span>;
}
function cost(row:UsageRow,currency:string) {
  if (row.cost_breakdown?.length) return row.cost_breakdown.map(v=>`${v.amount.toFixed(6)}\u00a0${v.currency}`).join("；");
  if (row.calls>(row.cost_unknown || 0)) return `${(row.estimated_cost || 0).toFixed(6)}\u00a0${row.currency || currency || "USD"}`;
  return row.cost_unknown ? "无法估算" : "暂无费用";
}

export function ModelUsagePanel({usage}:{usage:ModelUsage | null}) {
  if (!usage) return <section className="model-usage-panel"><h3>模型用量</h3><p>暂时无法读取模型用量。</p></section>;
  const {totals,budget}=usage;
  const legacy=usage.purposes.some(row=>row.connection_id==="legacy_unknown");
  return <section className="model-usage-panel" aria-label="模型用量">
    <header className="model-usage-heading"><div><h3>模型用量</h3><p>保留的调用记录 · 费用为本地估算</p></div>{budget && <span className="model-usage-tier">{tiers[budget.tier] || budget.tier}输出档位</span>}</header>
    <div className="model-usage-summary">
      <div><span>模型调用</span><strong>{number(totals.calls)}<small>次</small></strong><small>含已记录的失败与重试</small></div>
      <div><span>已知 Token</span><strong>{number((totals.prompt_tokens || 0)+(totals.completion_tokens || 0))}</strong><small>输入 {number(totals.prompt_tokens)} · 输出 {number(totals.completion_tokens)}</small></div>
      <div><span>已知估算费用</span><strong className="model-usage-amount">{cost(totals,usage.currency)}</strong><small>{totals.cost_unknown ? `${number(totals.cost_unknown)} 次无法估算` : "以供应商账单为准"}</small></div>
    </div>
    {!!totals.unknown_usage && <p className="model-usage-note">{totals.unknown_usage} 次调用用量缺失或无效；合计仅包含已知值。</p>}
    {budget && <div className="model-usage-budget" aria-label="Token 预算占用">
      <div><span>今日占用</span><strong>{number(budget.day_reserved)}</strong><small>{budget.daily_limit ? `上限 ${number(budget.daily_limit)}` : "未设 Token 上限"}</small></div>
      <div><span>本月占用</span><strong>{number(budget.month_reserved)}</strong><small>{budget.monthly_limit ? `上限 ${number(budget.monthly_limit)}` : "未设 Token 上限"}</small></div>
    </div>}
    <details className="model-usage-details"><summary>预算与统计说明</summary>
      <p>Token 为输入与输出之和；缓存输入包含在输入中，推理包含在输出中，不重复计入。预算占用含保守预留，不等同于账单实际用量。</p>
      {!!totals.cache_detail_missing && <p>{totals.cache_detail_missing} 次调用缓存拆分未知，不能视为缓存未命中。</p>}
      {!!totals.cost_recalculated && <p>含 {totals.cost_recalculated} 次按已保存费率快照回算。</p>}
      {!!totals.cost_supplemented && <p>{totals.cost_supplemented} 次按当前费率补估，不改写历史账本。费率改变后补估金额会变化。</p>}
      {!!totals.unknown_pricing && <p>{totals.unknown_pricing} 次缺费率。</p>}
      {!!totals.unknown_identity && <p>{totals.unknown_identity} 次身份无法匹配，不能补估。</p>}
      {legacy && <p>部分历史记录未保存连接或档案身份，按原模型 ID 与原费用展示；不会按当前配置补填归属。</p>}
      {budget && <>
        <p>今日实际 Token {number(budget.day_actual_tokens)} · 在途预留 {number(budget.day_inflight)} · 未知用量占用 {number(budget.day_unknown_occupied)} · 历史占用 {number(budget.day_legacy_occupied)}。</p>
        {Object.entries(budget.cost_limits || {}).map(([currency,limit])=><p key={currency}>{currency} 金额上限：日 {limit.daily || "不限"} / 月 {limit.monthly || "不限"}。</p>)}
        {!!budget.unlimited_currencies?.length && <p>{budget.unlimited_currencies.join("、")} 未设置金额上限，仍受 Token、次数与并发约束。</p>}
        {(budget.day_cutover || budget.month_cutover) && <p>既有统计时区切换边界：日 {budget.day_cutover || "未提供"}；月 {budget.month_cutover || "未提供"}。</p>}
      </>}
      <p>{usage.cost_note}</p>
    </details>
    {usage.purposes.length>0 && <div className="model-usage-table-wrap"><table className="model-usage-table">
      <caption>按用途与模型统计</caption><thead><tr><th>用途</th><th>模型</th><th>调用次数</th><th>已知用量</th><th>估算费用</th></tr></thead>
      <tbody>{usage.purposes.map(row=><tr key={`${row.connection_id}:${row.profile_id}:${row.purpose}:${row.model_id}`}>
        <td><span title={row.purpose}>{purposeName(row.purpose || "")}</span></td><td><ModelIdentity row={row}/></td>
        <td className="model-usage-number">{number(row.calls)}</td>
        <td className="model-usage-tokens"><span>输入 {number(row.prompt_tokens)}</span><span>输出 {number(row.completion_tokens)}</span><small>缓存 {number(row.cached_input_tokens)}{row.cache_detail_missing ? ` · ${row.cache_detail_missing}次未提供` : ""}</small></td>
        <td className="model-usage-cost"><span>{cost(row,usage.currency)}</span>{!!row.cost_unknown && <small>{row.cost_unknown} 次无法估算</small>}
          {row.cost_bases?.map(item=><small key={`${item.basis}:${item.currency}:${item.pricing_version}`} title={item.pricing_version}>
            {item.basis==="current_pricing" ? "按当前费率补估" : "调用时估算"} {item.amount.toFixed(6)} {item.currency}{item.basis==="current_pricing" && ` · 版本 ${item.pricing_version.slice(0,8)}`}
          </small>)}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>;
}
