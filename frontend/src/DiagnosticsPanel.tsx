import { useEffect, useState } from "react";
import { api, dateText } from "./api";
import type { ExecutionReport } from "./api";
import { RetrievalStatusView } from "./RuntimeStatusPanel";

export function DiagnosticsPanel({ conversationId, jobId }: { conversationId: string; jobId: number }) {
  const [report, setReport] = useState<ExecutionReport | null>(null);
  const [error, setError] = useState("");
  const [copyState, setCopyState] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setReport(null);
    setError("");
    setCopyState("");
    api<ExecutionReport>(`/conversations/${encodeURIComponent(conversationId)}/jobs/${jobId}/diagnostics`)
      .then((value) => { if (!cancelled) setReport(value); })
      .catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "读取过程失败。"); });
    return () => { cancelled = true; };
  }, [conversationId, jobId, refresh]);
  return <section className="diagnostics-panel" aria-label="本次消息处理过程">
    <p className="muted">直接查看这条提问的处理记录，无需查找编号或运行命令。读取不会重新执行请求或产生模型费用。</p>
    <button onClick={() => setRefresh((value) => value + 1)}>刷新记录</button>
    {error && <p role="alert">{error}</p>}
    {!report && !error && <p role="status">正在读取处理记录…</p>}
    {report && <>
      <h3>{report.state}</h3>
      <p>{report.note}</p>
      {report.retrieval_status && <RetrievalStatusView status={report.retrieval_status} />}
      <p className="muted">请求 #{report.job_id} · 已登记模型调用 {report.model_calls} 次</p>
      <button onClick={async () => {
        try {
          await navigator.clipboard.writeText(report.report_text);
          setCopyState("诊断报告已复制");
        } catch { setCopyState("复制失败，请展开下方报告文本手动复制。"); }
      }}>复制诊断报告</button>
      <span role="status">{copyState}</span>
      <ol className="diagnostics-timeline">
        {report.timeline.map((step) => <li key={step.id} className={step.warning ? "diagnostic-warning" : ""}>
          <strong>{step.title}</strong><time>{dateText(step.created_at)}</time>
          <p>{step.description}{step.recovered ? "（此项先前异常已恢复）" : ""}</p>
          <details><summary>技术详情</summary><pre>{JSON.stringify(step.details, null, 2)}</pre></details>
        </li>)}
      </ol>
      <details><summary>报告文本（可手动复制）</summary>
        <textarea aria-label="诊断报告文本" readOnly value={report.report_text} rows={8} />
      </details>
    </>}
  </section>;
}
