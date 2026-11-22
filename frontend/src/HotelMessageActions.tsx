import {useEffect, useRef, useState} from "react";
import {api} from "./api";
import type {HotelAction, HotelOriginal} from "./api";
import {Markdown} from "./Markdown";
import "./HotelMessageActions.css";

const choiceKey=(action:HotelAction)=>[action.task_id,action.task_version,action.query_index,action.candidate_set_id,action.candidate_set_version,action.candidate_id].join(":");

export function HotelMessageActions({conversationId, actions, disabled, onQueued}: {
  conversationId: string; actions: HotelAction[]; disabled: boolean; onQueued: () => void;
}) {
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const [original,setOriginal]=useState<HotelOriginal | null>(null);
  const [selectedChoice,setSelectedChoice]=useState("");
  const [selectionNotice,setSelectionNotice]=useState("");
  const generation=useRef(0);
  const choices=actions.filter(action=>action.kind==="select");
  const choiceScope=choices.map(choiceKey).join("|");
  useEffect(()=>{generation.current++;setOriginal(null);setError("");setBusy(false);setSelectedChoice("");setSelectionNotice("");return ()=>{generation.current++;};},[conversationId,choiceScope]);
  async function read(action: HotelAction,cursor=0) {
    const current=generation.current;setBusy(true);setError("");
    try {
      const page=await api<HotelOriginal>(`/conversations/${conversationId}/hotel-quotes/${encodeURIComponent(action.source_event_id!)}/original?revision=${encodeURIComponent(action.revision!)}&cursor=${cursor}`);
      if(current===generation.current)setOriginal(page);
    } catch(e) {if(current===generation.current)setError(e instanceof Error ? e.message : "资料读取失败。");}
    finally {if(current===generation.current)setBusy(false);}
  }
  async function act(action: HotelAction) {
    if(action.kind==="view_original"){await read(action);return;}
    const current=generation.current;setBusy(true);setError("");
    if(action.kind==="select"){setSelectedChoice(choiceKey(action));setSelectionNotice("正在提交选择："+action.label);}
    const {label,selectable,reference_text,...binding}=action;
    try {
      await api(`/conversations/${conversationId}/hotel-actions`,{method:"POST",body:JSON.stringify({...binding,client_request_id:crypto.randomUUID()})});
      if(current===generation.current){if(action.kind==="select")setSelectionNotice("已提交选择："+action.label);onQueued();}
    } catch(e) {if(current===generation.current){setSelectedChoice("");setSelectionNotice("");setError(e instanceof Error ? e.message : "选择未完成，请使用最新回复。");onQueued();}}
    finally {if(current===generation.current)setBusy(false);}
  }
  return <section aria-label="酒店资料与选择" className="hotel-actions">
    {actions.filter(action=>action.reference_text).map((action,i)=><details key={'reference-'+i}>
      <summary>已保存资料补充说明（本地整理，非新查询）</summary>
      <div style={{maxHeight:"20rem",overflowY:"auto"}}><Markdown text={action.reference_text}/></div>
    </details>)}
    {choices.length>0 && <div className="hotel-choice-panel">
      <div className="hotel-choice-heading"><strong>请选择具体门店</strong>
        <p>{disabled || choices.every(a=>a.selectable===false) ? "当前候选暂不可选，请使用最新回复中的选项。" : "下方卡片可以点击。请核对门店名称和地址，选择后将继续核价。"}</p>
      </div>
      <div className="hotel-choice-list">
        {choices.map(action=>{
          const match=action.label.match(/^(\d+)\.\s*(.*)$/);
          const ordinal=match?.[1];const label=match?.[2] || action.label;
          const separator=label.indexOf(" · ");
          const name=separator<0 ? label : label.slice(0,separator);
          const address=separator<0 ? "" : label.slice(separator+3);
          const selected=selectedChoice===choiceKey(action);
          return <button type="button" key={choiceKey(action)}
            className={"hotel-choice"+(selected ? " hotel-choice-selected" : "")}
            aria-label={action.label+(action.selectable===false ? "（不可选）" : "")}
            aria-busy={busy && selected} disabled={busy || disabled || action.selectable===false} onClick={()=>void act(action)}>
            <span className="hotel-choice-number" aria-hidden="true">{ordinal || "店"}</span>
            <span className="hotel-choice-copy"><strong>{name}</strong>{address && <span>地址：{address}</span>}</span>
            <span className="hotel-choice-cta" aria-hidden="true">{action.selectable===false ? "不可选择" : selected ? (busy ? "提交中…" : "已提交") : ordinal ? `选择第${ordinal}家` : "选择此店"}</span>
          </button>;
        })}
      </div>
      {selectionNotice && <p className="hotel-choice-status" role="status">{selectionNotice}</p>}
    </div>}
    {actions.some(action=>action.kind!=="select") && <div className="hotel-secondary-actions">
      {actions.filter(action=>action.kind!=="select").map((action,i)=><button type="button" key={i}
        disabled={busy || (action.kind!=="view_original" && disabled)} onClick={()=>void act(action)}>{action.label}</button>)}
    </div>}
    {error && <p role="alert">{error}</p>}
    {original && <dialog open aria-label="酒店报价原文" style={{position:"fixed",inset:"10vh auto auto 50%",transform:"translateX(-50%)",width:"min(760px,90vw)",maxHeight:"80vh",zIndex:100,borderRadius:16,padding:24}}>
      <h3>酒店报价原文</h3><p className="muted">已保存的历史资料 · 本地读取，不重新查价</p>
      <pre style={{whiteSpace:"pre-wrap",maxHeight:"55vh",overflowY:"auto"}}>{original.text}</pre>
      <p>第 {Math.floor(original.cursor/4000)+1} 页 · 共 {original.total_chars} 字符</p>
      <div className="dialog-actions">
        <button disabled={busy || original.cursor===0} onClick={()=>void read({kind:"view_original",label:"原文",source_event_id:original.source_event_id,revision:original.revision},Math.max(0,original.cursor-4000))}>上一页</button>
        <button disabled={busy || original.next_cursor===null} onClick={()=>void read({kind:"view_original",label:"原文",source_event_id:original.source_event_id,revision:original.revision},original.next_cursor!)}>下一页</button>
        <button onClick={()=>{generation.current++;setOriginal(null);setBusy(false);}}>关闭</button>
      </div>
    </dialog>}
  </section>;
}
