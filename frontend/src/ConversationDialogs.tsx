export type Modal = "notifications" | "space" | "services" | "preferences" | "delete" | "diagnostics" | "comparison" | null;
import { Check, Info, LoaderCircle, X } from "lucide-react";
import type { Confirmation, Conversation, DeletionJob, DeletionSummary,DraftAction } from "./api";
import { Markdown } from "./Markdown";
import { ErrorBoundary } from "./ErrorBoundary";
import { dateText } from "./api";
import { TripChangeDiff } from "./TripStatePanel";

export function ConfirmationCards({ confirmations, active, sending, busy, fill, confirm, renderedPreviews=[], onQuote,onDraftAction,draftSending={} }: {
  confirmations: Confirmation[]; active: string; sending: boolean; busy: boolean;
  fill: (text: string) => void; confirm: (id: string, version: number) => Promise<void>;
  renderedPreviews?: string[]; onQuote?: (id:string,label?:string)=>void;
  onDraftAction?:(id:string,version:number,action:DraftAction)=>Promise<void>;
  draftSending?:Record<string,boolean>;
}) {
  return <>{confirmations.map(c=>{
    const itinerary=c.command==="确认行程修改";
    const operation=c.draft_state?.operation;
    const activeOperation=operation && ["pending","running","retry"].includes(operation.status);
    const revalidating=!!draftSending[`${active}:${c.id}:${c.version}:revalidate`] || !!(activeOperation && operation.action==="revalidate");
    const discarding=!!draftSending[`${active}:${c.id}:${c.version}:discard`] || !!(activeOperation && operation.action==="discard");
    const renew=()=>{if(itinerary)void onDraftAction?.(c.id,c.version,"revalidate");else fill("重新查询预约规则");};
    return <ErrorBoundary key={`${active}-${c.id}-${c.version}`} fallback={<section className="confirmation" role="alert">
      <p>这份预览暂时无法显示，原内容仍保留。</p><button disabled={revalidating || discarding || itinerary && !onDraftAction} onClick={renew}>重新核对并生成预览</button>
    </section>}>
      <section className="confirmation" id={`draft-${c.id}`} data-draft-id={c.id}>
        <div className="confirmation-heading"><Info size={18}/><strong>{c.state==="expired" ? "确认已过期，草案仍保留" : "有一项变更需要你确认"}</strong></div>
        {c.draft_state && <small>草案修订 {c.version} · {c.draft_state.base_version ? `基于已保存版本 ${c.draft_state.base_version}` : "尚未创建正式行程"}</small>}
        {c.diff && <TripChangeDiff diff={c.diff}/>}
        {typeof c.preview==="string" && c.preview.trim() ? (renderedPreviews.includes(c.preview) ? <p>上方保留完整预览。可以稍后继续，确认到期后需重新核对。</p> : c.diff ? <details><summary>展开完整预览</summary><Markdown text={c.preview}/></details> : <Markdown text={c.preview}/>) : <p role="alert">完整预览暂时不可用，原行程未修改。</p>}
        {c.can_confirm===false && c.unavailable_reason && <p role="alert">{c.unavailable_reason}</p>}
        {operation?.message && <p role="alert">{operation.message}</p>}
        {(revalidating || discarding) && <p role="status">{discarding ? "正在放弃这份草案…" : "正在重新核对这份草案…"}</p>}
        <div className="confirmation-actions">
          <button className="primary-button" disabled={sending || busy || revalidating || discarding || c.can_confirm===false || typeof c.preview!=="string" || !c.preview.trim()} onClick={()=>confirm(c.id,c.version)}>
            {c.state==="expired" ? "确认已过期" : itinerary ? "保存这版行程" : "确认这次变更"}
          </button>
          {itinerary ? <>
            <button disabled={discarding || sending || c.draft_state?.can_discard===false || !onDraftAction} onClick={()=>{void onDraftAction?.(c.id,c.version,"discard");}}>放弃这份草案</button>
            <button disabled={revalidating || discarding || sending || c.draft_state?.can_revalidate===false || !onDraftAction} onClick={renew}>重新核对并生成预览</button>
          </> : <button onClick={()=>{if(c.reference_message_id)onQuote?.(c.reference_message_id,"提醒设置");fill("取消当前任务");}}>引用后结束这项设置</button>}
          {c.reference_message_id && onQuote && <button onClick={()=>onQuote(c.reference_message_id!,"行程草案")}>引用草案继续修改</button>}
          <small>{c.state==="expired" ? "旧确认不能保存；内容可继续核对或放弃" : `确认有效至 ${dateText(c.expires_at)}`}</small>
        </div>
      </section>
    </ErrorBoundary>;
  })}</>;
}

export function DeletionDialog({ target, summary, error, sending, onCancel, onConfirm }: {
  target: Conversation | null; summary: DeletionSummary | null; error: string; sending: boolean;
  onCancel: () => void; onConfirm: () => Promise<void>;
}) {
  return <div className="delete-dialog">
              <p>
                将永久删除 <strong>“{target?.title}”</strong>{" "}
                及其关联数据。
              </p>
              <p>
                聊天记忆、附件、行程和通知会被清理；进行中的任务、未到期提醒、定时查询和规则监测将停止。
              </p>
              {summary ? (
                <dl className="delete-summary">
                  {(
                    [
                      ["消息", summary.messages],
                      ["附件", summary.attachments],
                      ["行程", summary.trips],
                      ["方案草稿", summary.planning_artifacts],
                      ["会话摘要", summary.conversation_summaries],
                      ["大型工具结果", summary.tool_artifacts],
                      ["进行中任务", summary.active_tasks],
                      ["待处理提醒", summary.reminders],
                      ["定时查询", summary.scheduled_queries],
                      ["规则监测", summary.policy_watches],
                    ] as [string, number][]
                  ).map(([name, count]) => (
                    <div key={name}>
                      <dt>{name}</dt>
                      <dd>{count}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                !error && (
                  <p className="delete-loading">
                    <LoaderCircle className="spin" size={15} />
                    正在检查关联数据…
                  </p>
                )
              )}
              <p className="delete-warning">
                此操作不可恢复。其他会话和 QQ
                数据不会被删除；其他会话仍在使用的共享文件会保留。
              </p>
              {error && (
                <p className="delete-error" role="alert">
                  {error}
                </p>
              )}
              <div className="delete-actions">
                <button
                  autoFocus
                  disabled={sending}
                  onClick={onCancel}
                >
                  取消
                </button>
                <button
                  className="danger-button"
                  disabled={!summary || sending}
                  onClick={onConfirm}
                >
                  {sending ? (
                    <>
                      <LoaderCircle size={15} className="spin" />
                      正在提交…
                    </>
                  ) : (
                    "删除并停止关联任务"
                  )}
                </button>
              </div>
            </div>;
}

export function DeletionNotices({ jobs, retry, dismiss }: {
  jobs: DeletionJob[]; retry: (id: string) => Promise<void>; dismiss: (id: string) => void;
}) {
  return <>{jobs.length > 0 && (
            <div className="deletion-notices" aria-live="polite">
              {jobs.map((job) => (
                <div
                  className={`deletion-notice ${job.state}`}
                  key={job.conversation_id}
                >
                  {job.state === "completed" ? (
                    <Check size={16} />
                  ) : job.state === "failed" ? (
                    <Info size={16} />
                  ) : (
                    <LoaderCircle size={16} className="spin" />
                  )}
                  <span>
                    {job.title && <small>{job.title}</small>}
                    <strong>
                      {job.state === "completed"
                        ? "会话及关联数据已删除"
                        : job.state === "failed"
                          ? "会话已停用，清理尚未完成"
                          : "会话已停用，正在清理关联数据"}
                    </strong>
                    <small>
                      {job.error ||
                        (job.state === "completed"
                          ? "不会再执行该会话的任务与提醒。"
                          : "删除已受理，关闭网页不会中断清理。")}
                    </small>
                  </span>
                  {job.state === "failed" && (
                    <button onClick={() => retry(job.conversation_id)}>
                      重试清理
                    </button>
                  )}
                  {job.state === "completed" && (
                    <button
                      className="icon-button tiny"
                      aria-label="关闭删除完成提示"
                      onClick={() => dismiss(job.conversation_id)}
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}</>;
}
