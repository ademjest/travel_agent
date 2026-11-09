import type { ConversationUsageReport } from "./api";
import { ModelIdentity, purposeName } from "./ModelUsagePanel";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const count = (value: number) => value.toLocaleString("en-US");

function cost(row: { calls: number; cost_unknown: number; cost_breakdown: { currency: string; amount: number }[] }) {
  const known = row.cost_breakdown.map(item => `${item.amount.toFixed(6)} ${item.currency}`).join("；");
  if (!row.calls) return "暂无费用";
  return [known, row.cost_unknown ? `${row.cost_unknown} 次无法估算` : ""].filter(Boolean).join("；") || "无法估算";
}

export function ConversationUsage({ conversation, usage }: { conversation: string; usage?: ConversationUsageReport }) {
  const [opened,setOpened]=useState(false);
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>setOpened(false),[conversation]);
  useEffect(()=>{if(opened && dialog.current) {
    if(dialog.current.showModal)dialog.current.showModal();else dialog.current.setAttribute("open","");
  }},[opened]);
  if (!conversation) return null;
  if (!usage || usage.conversation_id !== conversation)
    return <div className="conversation-usage pending" role="status">正在读取本会话用量…</div>;
  const totals = usage.totals;
  return <div className="conversation-usage" aria-label="本会话模型用量">
    <button className="conversation-usage-toggle" onClick={()=>setOpened(true)} aria-haspopup="dialog">
      <strong>本会话用量</strong>
      <span>{count(totals.total_tokens)} token · {totals.calls} 次模型调用</span>
      <span className="conversation-usage-cost">{cost(totals)}</span>
      {totals.running_calls > 0 && <span>{totals.running_calls} 次调用进行中</span>}
    </button>
    {opened && createPortal(<dialog className="modal usage-modal" ref={dialog} aria-label="本会话调用明细" onCancel={()=>setOpened(false)}>
    <div className="modal-inner"><header><h2>本会话用量</h2><button onClick={()=>setOpened(false)} aria-label="关闭用量详情">关闭</button></header>
    <div className="conversation-usage-body">
      <dl className="conversation-usage-metrics">
        <div><dt>输入 token</dt><dd>{count(totals.prompt_tokens)}</dd></div>
        <div><dt>缓存输入（包含在输入中）</dt><dd>{count(totals.cached_input_tokens)}</dd></div>
        <div><dt>输出 token</dt><dd>{count(totals.completion_tokens)}</dd></div>
        <div><dt>推理 token（包含在输出中）</dt><dd>{count(totals.reasoning_tokens)}</dd></div>
      </dl>
      <p>总量 = 输入 + 输出；仅汇总供应商已返回的已知用量。一次消息可能包含解析、规划、摘要等多次调用，失败和重试产生的实际调用也计入。</p>
      {totals.cost_recalculated > 0 && <p>{totals.cost_recalculated} 次历史调用按已保存费率快照回算。</p>}
      {!!totals.cost_supplemented && <p>{totals.cost_supplemented} 次历史缺价按当前费率补估。读取不会改写账本，费率变化时补估金额可能变化。</p>}
      {!!totals.unknown_pricing && <p>{totals.unknown_pricing} 次缺费率，无法估算。</p>}
      {!!totals.unknown_identity && <p>{totals.unknown_identity} 次身份无法匹配，未猜填费用。</p>}
      {totals.cache_detail_missing > 0 && <p>{totals.cache_detail_missing} 次调用缓存拆分缺失，不能视为缓存未命中。</p>}
      {totals.unknown_usage > 0 && <p>{totals.unknown_usage} 次调用用量尚未返回或缺失，总量可能不完整。</p>}
      {usage.purposes.length > 0 && <div className="conversation-usage-table"><table>
        <caption>本会话调用明细</caption>
        <thead><tr><th>用途 / 模型</th><th>调用</th><th>输入 / 缓存 / 输出</th><th>估算费用</th></tr></thead>
        <tbody>{usage.purposes.map(row => <tr key={`${row.connection_id}:${row.profile_id}:${row.purpose}:${row.model_id}`}>
          <td>{purposeName(row.purpose)}<ModelIdentity row={row}/></td>
          <td>{row.calls}</td><td>{count(row.prompt_tokens)} / {count(row.cached_input_tokens)} / {count(row.completion_tokens)}</td>
          <td>{cost(row)}</td>
        </tr>)}</tbody>
      </table></div>}
      {!!usage.rounds?.length && <section className="usage-rounds" aria-label="逐轮用量">
        <h3>逐轮调用与计费依据</h3>{usage.rounds.map((round,index)=><details key={round.event_key}>
          <summary>第{index+1}轮 · {round.totals.calls}次 · {count(round.totals.prompt_tokens+round.totals.completion_tokens)} Token · {cost(round.totals)}</summary>
          {round.user_message && <p>{round.user_message}</p>}
          {!round.calls.length && <p>本轮未调用模型。</p>}
          {round.calls.map(call=><p key={call.call_id}><strong>{purposeName(call.purpose)}</strong> · {call.model_id} · 输入 {call.prompt_tokens ?? "未知"} / 输出 {call.completion_tokens ?? "未知"} · {call.elapsed_seconds==null ? "耗时未知" : `${call.elapsed_seconds.toFixed(1)}秒`}<br/>
            {call.cost==null ? ({usage:"缺用量",pricing:"缺费率",identity:"身份无法匹配"}[call.unknown] || "无法估算") : `${call.origin==="current_pricing" ? "按当前费率补估" : "调用时估算"} ${call.cost.toFixed(6)} ${call.currency}`}
            {call.origin==="current_pricing" && <small title={call.pricing_version}> · 费率版本 {call.pricing_version?.slice(0,8)}</small>}
          </p>)}
        </details>)}
      </section>}
      <p className="muted">{usage.cost_note}</p>
      {usage.supplier_usage && <p className="muted">供应商工具：问道已记录提交 {usage.supplier_usage.wendao_submissions ?? "未知"} 次；费用与用量未知。{usage.supplier_usage.note}</p>}
      <p className="muted">{usage.scope_note}</p>
    </div>
    </div></dialog>,document.body)}
  </div>;
}
