import {useEffect,useLayoutEffect,useRef,useState} from "react";
import {api,dateText} from "./api";
import type {PlanComparison,Trip} from "./api";
import {Markdown} from "./Markdown";
import {TripMessage} from "./TripMessage";

function planText(plan?:Trip["plan"],places:Trip["sources"]={}) {
  return plan?.days.map(day=>`### 第 ${day.day_index} 天${day.date ? ` · ${day.date}` : ""}\n`+
    (day.activities.length ? day.activities.map(a=>`- ${a.period || ""}：${a.name || places[a.poi_id || ""]?.name || "地点待确认"}${a.reason ? `；${a.reason}` : ""}`+
      (a.subactivities?.length ? `\n  园内参观：${a.subactivities.map(c=>places[c.poi_id]?.name || "地点待确认").join("、")}` : "")+
      (a.description?.intro?.text ? `\n  ${a.description.intro.text}` : "")).join("\n") : "自由休整或交通日。" )).join("\n\n") || "本分支未保存可展示的行程。";
}

export function PlanComparisonPanel({conversationId,messageId}:{conversationId:string;messageId:string}) {
  const [selectedMessage,setSelectedMessage]=useState(messageId);
  const [report,setReport]=useState<PlanComparison | null>(null);
  const [error,setError]=useState("");
  const [tab,setTab]=useState("amap");
  const [excerpt,setExcerpt]=useState(false);
  const [notice,setNotice]=useState("");
  const body=useRef<HTMLDivElement>(null);
  const scrolls=useRef<Record<string,number>>({});
  useEffect(()=>{
    const controller=new AbortController();setReport(null);setError("");setNotice("");
    api<PlanComparison>(`/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(selectedMessage)}/plan-comparison`,{signal:controller.signal})
      .then(value=>{if(!controller.signal.aborted)setReport(value);})
      .catch(reason=>{if(!controller.signal.aborted)setError(reason instanceof Error ? reason.message : "读取草案失败。");});
    return ()=>controller.abort();
  },[conversationId,selectedMessage]);
  useLayoutEffect(()=>{if(body.current)body.current.scrollTop=scrolls.current[tab] || 0;},[tab,report]);
  const tabs=[{id:"amap",label:"高德数据草案"},{id:"wendao",label:"问道原文"},{id:"compare",label:"选择与调整"},{id:"final",label:"最终行程"}];
  const branch=report?.branches[tab];
  const wendao=report?.branches.wendao;
  const raw=wendao?.data?.report || wendao?.data?.sources?.filter(s=>s.provider==="ctrip_wendao").map(s=>s.text || "").join("\n") || "未取得问道完整原文。";
  const choices:Record<string,string>={amap:"采用高德数据草案",wendao:"采用问道参考方案",merge:"融合两稿"};
  const attempt=report?.branches.compare;
  const selection=report?.decision.choice ? report.decision : attempt;
  const effectivePlan=report?.branches["compare-repair"]?.plan || attempt?.plan || (selection?.choice==="amap" ? report?.branches.amap?.plan : undefined);
  const text=tab==="amap" ? planText(branch?.plan,branch?.places) : tab==="wendao" ? excerpt ? wendao?.schedule_text || "未保存日程摘录。" : raw : tab==="compare" ?
    `${choices[selection?.choice || ""] || "未形成新的比较决定"}\n\n${selection?.reason || "未记录选择理由。"}\n\n${planText(effectivePlan,report?.final_trip?.sources || report?.branches.amap?.places)}` :
    report?.final_text || report?.final_note || "未保存最终行程。";
  async function copy() {try {await navigator.clipboard.writeText(text);setNotice("已复制当前内容");}catch{setNotice("复制失败，请手动选择正文。");}}
  return <section className="plan-comparison" aria-label="本轮草案对比">
    <p className="muted">读取已保存资料，不重新查询或调用模型。高德草案由项目模型基于地点数据生成；问道原文保留原始表达。</p>
    <nav className="comparison-tabs" aria-label="草案内容">{tabs.map(item=><button key={item.id} aria-pressed={tab===item.id} onClick={()=>{
      scrolls.current[tab]=body.current?.scrollTop || 0;setTab(item.id);setNotice("");
    }}>{item.label}</button>)}</nav>
    {error && <p role="alert">{error}</p>}
    {!report && !error && <p role="status">正在读取本条消息的草案…</p>}
    {report && <>
      <div className="comparison-actions"><button onClick={copy}>复制当前内容</button><button onClick={()=>{
        const url=URL.createObjectURL(new Blob([`# ${tabs.find(item=>item.id===tab)?.label}\n\n消息：${selectedMessage}\n\n${text}`],{type:"text/markdown;charset=utf-8"}));
        const link=document.createElement("a");link.href=url;link.download=`plan-${selectedMessage.replace(":","-")}-${tab}.md`;link.click();URL.revokeObjectURL(url);
      }}>导出当前内容</button><span role="status">{notice}</span></div>
      <div className="comparison-body" ref={body}>
        {report.decision.status==="skipped" && <p className="comparison-note">{report.decision.reason}</p>}
        {report.previous_message_id && <button onClick={()=>{scrolls.current={};setSelectedMessage(report.previous_message_id!);setTab("amap");}}>查看所引用的上一轮草案</button>}
        {branch?.created_at && <p className="muted">保存时间：{dateText(branch.created_at)} · {branch.status==="completed" ? "已完成本分支" : "本分支未完成"}</p>}
        {tab==="wendao" && <><label className="comparison-excerpt"><input type="checkbox" checked={excerpt} onChange={e=>setExcerpt(e.target.checked)}/>仅看供比较的日程摘录</label>
          {wendao && <p className="muted">{wendao.days_grounded ? "日程覆盖全部要求天数。" : "未验证覆盖全部要求天数，不能视为完整逐日方案。"}原文事实尚未独立核实。</p>}</>}
        {tab==="compare" && <><p className="muted">比较尝试与最终采用结果分别展示。选择理由是模型解释，不能据此认定融合优于单稿。</p>
          {attempt?.status==="failed" && <p className="comparison-note">比较尝试未通过：{attempt.error_summary || `历史记录仅保留 ${attempt.error_type || "失败类型"}，具体原因未保存。`}
            {attempt.error_code && <>（{attempt.error_code}{attempt.validation_path ? ` · ${attempt.validation_path}` : ""}）</>}
            {report.decision.repaired && <> 已通过一次受限修复。</>}</p>}</>}
        {tab==="final" ? <>{report.final_note && <p className="comparison-note">{report.final_note}</p>}{report.final_presentation ? <TripMessage presentation={report.final_presentation}/> : <Markdown text={text}/>}</> : <Markdown text={text}/>}
        {tab==="compare" && <details><summary>比较结果原始结构</summary><pre>{JSON.stringify(report.branches.compare || report.decision,null,2)}</pre></details>}
        {tab==="amap" && branch?.plan && <details><summary>高德草案原始结构</summary><pre>{JSON.stringify(branch.plan,null,2)}</pre></details>}
      </div>
    </>}
  </section>;
}
