import type { ConversationUsageReport } from "./api";

const count = (value: number) => value.toLocaleString("en-US");
const purposes: Record<string, string> = {
  unspecified: "常规对话", intent_parse: "意图解析", conversation_summary: "会话摘要",
  trip_requirement_extract: "行程需求", trip_plan: "行程规划", trip_plan_repair: "行程修正",
  tool_reasoning: "工具推理", tool_answer: "答复整理", research_plan: "攻略检索规划",
  research_synthesis: "攻略整理", image_extract: "图片分析", document_summary: "文档摘要",
};

function cost(row: { calls: number; cost_unknown: number; cost_breakdown: { currency: string; amount: number }[] }) {
  const known = row.cost_breakdown.map(item => `${item.amount.toFixed(6)} ${item.currency}`).join(" + ");
  if (!row.calls) return "暂无费用";
  return [known, row.cost_unknown ? `${row.cost_unknown} 次费用待核验` : ""].filter(Boolean).join("；") || "费用待核验";
}

export function ConversationUsage({ conversation, usage }: { conversation: string; usage?: ConversationUsageReport }) {
  if (!conversation) return null;
  if (!usage || usage.conversation_id !== conversation)
    return <div className="conversation-usage pending" role="status">正在读取本会话用量…</div>;
  const totals = usage.totals;
  return <details className="conversation-usage" aria-label="本会话模型用量">
    <summary>
      <strong>本会话用量</strong>
      <span>{count(totals.total_tokens)} token · {totals.calls} 次模型调用</span>
      <span className="conversation-usage-cost">{cost(totals)}</span>
      {totals.running_calls > 0 && <span>{totals.running_calls} 次调用进行中</span>}
    </summary>
    <div className="conversation-usage-body">
      <dl className="conversation-usage-metrics">
        <div><dt>输入 token</dt><dd>{count(totals.prompt_tokens)}</dd></div>
        <div><dt>缓存输入（包含在输入中）</dt><dd>{count(totals.cached_input_tokens)}</dd></div>
        <div><dt>输出 token</dt><dd>{count(totals.completion_tokens)}</dd></div>
        <div><dt>推理 token（包含在输出中）</dt><dd>{count(totals.reasoning_tokens)}</dd></div>
      </dl>
      <p>总量 = 输入 + 输出；仅汇总供应商已返回的已知用量。一次消息可能包含解析、规划、摘要等多次调用，失败和重试产生的实际调用也计入。</p>
      {totals.cost_recalculated > 0 && <p>{totals.cost_recalculated} 次历史调用按当前费率回算。</p>}
      {totals.cache_detail_missing > 0 && <p>{totals.cache_detail_missing} 次调用缓存拆分缺失，不能视为缓存未命中。</p>}
      {totals.unknown_usage > 0 && <p>{totals.unknown_usage} 次调用用量尚未返回或缺失，总量可能不完整。</p>}
      {usage.purposes.length > 0 && <div className="conversation-usage-table"><table>
        <caption>本会话调用明细</caption>
        <thead><tr><th>用途 / 模型</th><th>调用</th><th>输入 / 缓存 / 输出</th><th>估算费用</th></tr></thead>
        <tbody>{usage.purposes.map(row => <tr key={`${row.purpose}:${row.model_id}`}>
          <td>{purposes[row.purpose] || row.purpose}<small>{row.model_id}</small></td>
          <td>{row.calls}</td><td>{count(row.prompt_tokens)} / {count(row.cached_input_tokens)} / {count(row.completion_tokens)}</td>
          <td>{cost(row)}</td>
        </tr>)}</tbody>
      </table></div>}
      <p className="muted">{usage.cost_note}</p>
      <p className="muted">{usage.scope_note}</p>
    </div>
  </details>;
}
