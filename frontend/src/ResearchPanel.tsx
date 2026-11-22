import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "./api";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Fact { source_id: number; topic?: string; value_raw: string; context_raw?: string }
interface Practical { topic: string; text: string; notes?: string[]; source_ids?: number[] }
interface NavigationLink { url: string; label: string; status: string }
interface Entry { target_id: string; parent_target_id?: string | null; name: string; visit_date?:string|null; official_link?: NavigationLink | null; intro?: { text?: string }; cultural_note?: string; tips?: { text: string }[]; practical?: Practical[]; slots?: Practical[]; extras: Fact[]; variants: Fact[]; materials?: { start?: number; end?: number; source_ids: number[] }[] }
interface Source { id: number; title?: string; provider?: string; text?: string; snapshot?: unknown }
interface Presentation { schema_version: string; entries: Entry[]; sources: Source[]; dates: { date_is_tentative?: boolean; visit_start_date?: string; start_date?: string }; source_retrieved_at?: string; diagnostics?: unknown; replayed_saved_answer?: boolean; parser_revision?:string;cache_reprocessed?:boolean }
interface Research { research_id?: string; content_version?: string; stage: string; updated_at: string; report: string; source_count: number; presentation?: Presentation }
type Mode = "reading" | "source" | "diagnostics";
type Position = { reading: number; source: number; diagnostics: number };
interface SavedView { open: boolean; selected: string; mode: Mode; source: number; positions: Record<string, Position>; expanded?: Record<string, boolean> }
const savedViews = new Map<string, SavedView>();
export function forgetResearchView(conversation: string) {
  savedViews.delete(conversation);
}
function saveView(conversation: string, view: SavedView) {
  if (!conversation) return;
  savedViews.delete(conversation);
  savedViews.set(conversation, view);
  while (savedViews.size > 32) savedViews.delete(savedViews.keys().next().value!);
}
const topics: Record<string, string> = { opening_hours: "开放时间", ticket_price: "票价", reservation_rules: "预约规则", schedule_note: "行程提示" };
const terminal = new Set(["completed", "partial", "failed", "cancelled", "blocked"]);
const stageLabels: Record<string, string> = { completed: "资料已整理", partial: "资料已部分整理", failed: "资料整理失败", cancelled: "资料整理已取消", blocked: "资料整理受阻" };
const identity = (row: Research) => row.research_id || row.presentation?.source_retrieved_at || row.updated_at || "legacy";
const version = (row: Research) => row.content_version || JSON.stringify(row);
const timeLabel = (value?: string) => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未记录";

function Prose({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{ img: () => null,
    a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{text}</ReactMarkdown>;
}

function OfficialLink({ link, standalone = false }: { link?: NavigationLink | null; standalone?: boolean }) {
  if (!link || !["verified_navigation", "candidate_navigation"].includes(link.status)) return null;
  try {
    const url = new URL(link.url);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return null;
  } catch { return null; }
  const content = <> 详细信息见景点官网：<a href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a></>;
  return standalone ? <p><strong>预约规则：</strong>{content}</p> : content;
}

export function ResearchPanel({ conversation, onPlan, refreshKey = "", busy = false }: { conversation: string; onPlan: () => void; refreshKey?: string; busy?: boolean }) {
  const [rows, setRows] = useState<Research[]>([]);
  const [selected, setSelected] = useState("");
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("reading");
  const [sourceId, setSourceId] = useState(0);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const container = useRef<HTMLDivElement>(null);
  const dialog=useRef<HTMLDialogElement>(null);
  const marker = useRef<HTMLElement>(null);
  const positions = useRef<Record<string, Position>>({});
  const explicitPosition = useRef(false);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [reload, setReload] = useState(0);
  const openRef = useRef(false);
  const selectedRef = useRef("");
  const session = useRef(conversation);
  useEffect(()=>{if(open && dialog.current && !dialog.current.open) {
    if(dialog.current.showModal)dialog.current.showModal();else dialog.current.setAttribute("open","");
  }},[open,rows.length]);

  useEffect(() => {
    session.current = conversation;
    const saved = savedViews.get(conversation);
    setRows([]); setSelected(saved?.selected || ""); selectedRef.current = saved?.selected || "";
    setOpen(saved?.open || false); openRef.current = saved?.open || false;
    setMode(saved?.mode || "reading"); setSourceId(saved?.source || 0);
    setExpanded(saved?.expanded || {});
    positions.current = saved?.positions || {}; setRange(null); setError("");
  }, [conversation]);

  useEffect(() => {
    if (session.current === conversation) saveView(conversation, { open, selected, mode, source: sourceId, positions: positions.current, expanded });
  }, [conversation, open, selected, mode, sourceId, expanded]);

  useEffect(() => {
    if (!conversation) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const update = async () => {
      try {
        const incoming = await api<Research[]>(`/conversations/${conversation}/research`);
        if (!active || session.current !== conversation) return;
        setError("");
        setRows(previous => {
          const pinned = openRef.current ? previous.find(r => identity(r) === selectedRef.current) : undefined;
          const next = pinned && !incoming.some(r => identity(r) === identity(pinned)) ? [...incoming, pinned] : incoming;
          return JSON.stringify(previous.map(version)) === JSON.stringify(next.map(version)) ? previous : next;
        });
        if (!openRef.current || !selectedRef.current) {
          selectedRef.current = incoming[0] ? identity(incoming[0]) : "";
          setSelected(selectedRef.current);
        }
        if (busy || incoming.some(r => !terminal.has(r.stage))) timer = setTimeout(() => { void update(); }, 4000);
      } catch {
        if (active) setError("资料暂时读取失败，可重新打开报告重试。");
      }
    };
    void update();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [conversation, refreshKey, busy, reload]);

  const row = rows.find(r => identity(r) === selected) || rows[0];
  const currentId = row ? identity(row) : "";
  const positionKey = `${conversation}:${currentId}`;
  const remember = () => {
    if (!container.current) return;
    const prior = positions.current[positionKey] || { reading: 0, source: 0, diagnostics: 0 };
    positions.current[positionKey] = { ...prior, [mode]: container.current.scrollTop };
  };
  const switchMode = (next: Mode) => { remember(); explicitPosition.current = false; setMode(next); setRange(null); };
  useEffect(() => {
    const element = container.current;
    if (!element || !open) return;
    if (explicitPosition.current && marker.current) {
      element.scrollTop += marker.current.getBoundingClientRect().top - element.getBoundingClientRect().top;
      explicitPosition.current = false;
    } else element.scrollTop = positions.current[positionKey]?.[mode] || 0;
  }, [positionKey, mode, open, range]);

  if (!rows.length) return error ? <p role="status">{error}</p> : null;
  const view = row.presentation;
  const sources = (view?.sources || []).filter(s => s.text?.trim() || (s.snapshot &&
    (typeof s.snapshot === "object" ? Object.keys(s.snapshot).length : String(s.snapshot).trim())));
  const source = sources.find(s => s.id === sourceId) || sources[0];
  const newResult = open && rows[0] && identity(rows[0]) !== currentId;
  const showSource = (entry: Entry) => {
    remember();
    const material = entry.materials?.find(m => m.source_ids.some(id => sources.some(s => s.id === id)));
    const entrySource = sources.find(s => material?.source_ids.includes(s.id)) || sources[0];
    setSourceId(entrySource?.id || 0);
    const location = entrySource?.text?.trim() && material?.start !== undefined && material.end !== undefined
      ? { start: material.start, end: material.end } : null;
    setRange(location);
    explicitPosition.current = !!location; setMode("source");
  };
  return <section className="research-panel" aria-label="当前会话研究">
    <h3>旅行研究</h3>
    <small role="status">{stageLabels[row.stage] || "正在整理旅行资料…"}</small>
    <p className="muted">{view ? `${new Set(view.entries.map(entry=>entry.name)).size} 个景点 · ` : ""}{row.source_count} 份已保存来源</p>
    {!!view?.entries.length && <details><summary>景点目录</summary><p>{[...new Set(view.entries.map(entry=>entry.name))].join("、")}</p></details>}
    {error && <p role="status">{error}</p>}
    <details className="research-details" open={open}>
      <summary onClick={event => {
        event.preventDefault(); remember(); setOpen(!open); openRef.current = !open;
        if (!open) setReload(value => value + 1);
      }}>查看完整报告</summary>
      {open && createPortal(<dialog ref={dialog} className="modal research-modal" aria-label="旅行研究完整报告"
        onCancel={()=>{remember();setOpen(false);openRef.current=false;}}><div className="modal-inner">
        <header><h2>旅行研究完整报告</h2><button onClick={()=>{remember();setOpen(false);openRef.current=false;}}>关闭报告</button></header>
        {newResult && <button onClick={() => { remember(); setSelected(identity(rows[0])); selectedRef.current = identity(rows[0]); setMode("reading"); setRange(null); }}>有新结果，点击查看</button>}
        {rows.length > 1 && <label>研究版本<select aria-label="研究版本" value={currentId} onChange={event => {
          remember(); setSelected(event.target.value); selectedRef.current = event.target.value; setMode("reading"); setRange(null);
        }}>{rows.map((r, index) => <option key={identity(r)} value={identity(r)}>{index === 0 ? "最新 · " : ""}{timeLabel(r.updated_at)}</option>)}</select></label>}
        <div className="research-toolbar" aria-label="报告模式">
          <button aria-pressed={mode === "reading"} onClick={() => switchMode("reading")}>阅读报告</button>
          <button aria-pressed={mode === "source"} onClick={() => switchMode("source")}>查看原文</button>
          <button aria-pressed={mode === "diagnostics"} onClick={() => switchMode("diagnostics")}>诊断</button>
        </div>
        <div ref={container} className="research-report markdown" onScroll={remember} tabIndex={0} aria-label="研究报告内容">
          {mode === "reading" ? <>
            <p>资料取得时间：{timeLabel(view?.source_retrieved_at || row.updated_at)}</p>
            {view?.parser_revision && <p className="muted">解析版本 {view.parser_revision} · {view.cache_reprocessed ? "已按当前规则重建展示，原始回执保留" : "原始回执保留"}；查看不重新查询。</p>}
            {newResult && <p>正在阅读之前保存的版本。</p>}
            {view?.dates && <p><strong>{view.dates.date_is_tentative ? "日期暂定：" : "参考日期："}</strong>{view.dates.visit_start_date || view.dates.start_date || "待定"}</p>}
            {view ? view.entries.map(entry => <section key={entry.target_id} data-target={entry.target_id}>
              {entry.parent_target_id ? <h5>园内场馆：{entry.name}</h5> : <h4>{entry.name}</h4>}
              {entry.visit_date && view.entries.some(other=>other.target_id!==entry.target_id && other.name===entry.name) && <small>游览日：{entry.visit_date}</small>}
              {!entry.parent_target_id && entry.intro?.text && <Prose text={"**介绍：** " + entry.intro.text.replace(/^(?:介绍|简介|景点介绍)[：:]\s*/, "")} />}
              {!entry.parent_target_id && !entry.intro?.text && entry.cultural_note && <p role="status">{entry.cultural_note}</p>}
              {(entry.practical || entry.slots || []).map(slot => <p key={slot.topic}><strong>{topics[slot.topic] || slot.topic}：</strong>{slot.text}{slot.notes?.length ? " " + slot.notes.join(" ") : ""}{slot.topic === "reservation_rules" && <OfficialLink link={entry.official_link} />}</p>)}
              {!(entry.practical || entry.slots || []).some(slot => slot.topic === "reservation_rules") && <OfficialLink link={entry.official_link} standalone />}
              {entry.extras.map((fact, index) => <p key={index}><strong>{fact.topic === "duration" ? "建议游玩时长" : "咨询电话"}：</strong>{fact.value_raw}</p>)}
              {entry.variants.length > 0 && <details open={!!expanded[`${currentId}:${entry.target_id}`]}><summary onClick={event => {
                event.preventDefault(); const key = `${currentId}:${entry.target_id}`; setExpanded(old => ({ ...old, [key]: !old[key] }));
              }}>其他票种与适用条件</summary>{entry.variants.map((fact, index) => <div key={index}>{fact.context_raw && fact.context_raw !== entry.variants[index - 1]?.context_raw && <small>{fact.context_raw}</small>}<p>{fact.value_raw}</p></div>)}</details>}
              <button onClick={() => showSource(entry)}>查看这一景点的原文</button>
            </section>) : <Prose text={row.report} />}
          </> : mode === "source" ? <>
            {sources.length ? <label>选择来源<select aria-label="选择来源" value={source?.id || 0} onChange={event => { remember(); setSourceId(Number(event.target.value)); setRange(null); }}>
              {sources.map(s => <option key={s.id} value={s.id}>{s.title || `来源${s.id}`}</option>)}
            </select></label> : null}
            {source?.text?.trim() ? range ? <pre className="research-source">{source.text.slice(0, range.start)}<mark ref={marker}>{source.text.slice(range.start, range.end)}</mark>{source.text.slice(range.end)}</pre> : <Prose text={source.text} />
              : source?.snapshot ? <pre>{JSON.stringify(source.snapshot, null, 2)}</pre> : <p>本记录没有单独保存来源原文。</p>}
          </> : <pre>{JSON.stringify(view?.diagnostics || { stage: row.stage }, null, 2)}</pre>}
        </div>
        <button onClick={onPlan}>根据资料规划</button>
      </div></dialog>,document.body)}
    </details>
  </section>;
}
