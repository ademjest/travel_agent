import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Fact { source_id: number; topic?: string; value_raw: string; context_raw?: string }
interface Practical { topic: string; text: string; notes?: string[]; source_ids?: number[] }
interface Entry { target_id: string; name: string; intro?: { text?: string }; tips?: { text: string }[]; practical?: Practical[]; slots?: Practical[]; extras: Fact[]; variants: Fact[]; materials?: { start?: number; end?: number; source_ids: number[] }[] }
interface Source { id: number; title?: string; provider?: string; text?: string; snapshot?: unknown }
interface Presentation { schema_version: string; entries: Entry[]; sources: Source[]; dates: { date_is_tentative?: boolean; visit_start_date?: string; start_date?: string }; source_retrieved_at?: string; diagnostics?: unknown; replayed_saved_answer?: boolean }
interface Research { research_id?: string; content_version?: string; stage: string; updated_at: string; report: string; source_count: number; presentation?: Presentation }
type Mode = "reading" | "source" | "diagnostics";
type Position = { reading: number; source: number; diagnostics: number };
interface SavedView { open: boolean; selected: string; mode: Mode; source: number; positions: Record<string, Position>; expanded?: Record<string, boolean> }
const savedViews = new Map<string, SavedView>();
const topics: Record<string, string> = { opening_hours: "开放时间", ticket_price: "门票", reservation_rules: "预约方式", schedule_note: "行程提示" };
const terminal = new Set(["completed", "partial", "failed", "cancelled", "blocked"]);
const identity = (row: Research) => row.research_id || row.presentation?.source_retrieved_at || row.updated_at || "legacy";
const version = (row: Research) => row.content_version || JSON.stringify(row);
const timeLabel = (value?: string) => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未记录";

function Prose({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{ img: () => null }}>{text}</ReactMarkdown>;
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
  const marker = useRef<HTMLElement>(null);
  const positions = useRef<Record<string, Position>>({});
  const explicitPosition = useRef(false);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [reload, setReload] = useState(0);
  const openRef = useRef(false);
  const selectedRef = useRef("");
  const session = useRef(conversation);

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
    if (session.current === conversation) savedViews.set(conversation, { open, selected, mode, source: sourceId, positions: positions.current, expanded });
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
    const prior = positions.current[positionKey] || { reading: 0, source: 0, diagnostics: 0 };
    positions.current[positionKey] = { ...prior, [mode]: container.current?.scrollTop || 0 };
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
  const source = view?.sources.find(s => s.id === sourceId) || view?.sources[0];
  const newResult = open && rows[0] && identity(rows[0]) !== currentId;
  const showSource = (entry: Entry) => {
    remember();
    const material = entry.materials?.find(m => m.start !== undefined);
    setSourceId(material?.source_ids[0] || view?.sources[0]?.id || 0);
    setRange(material?.start !== undefined && material.end !== undefined ? { start: material.start, end: material.end } : null);
    explicitPosition.current = material?.start !== undefined; setMode("source");
  };
  return <section className="research-panel" aria-label="当前会话研究">
    <h3>旅行研究</h3>
    <small role="status">{terminal.has(row.stage) ? "资料已整理" : "正在整理旅行资料…"}</small>
    {error && <p role="status">{error}</p>}
    <details className="research-details" open={open}>
      <summary onClick={event => {
        event.preventDefault(); remember(); setOpen(!open); openRef.current = !open;
        if (!open) setReload(value => value + 1);
      }}>查看完整报告</summary>
      {open && <>
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
            {newResult && <p>正在阅读之前保存的版本。</p>}
            {view?.dates && <p>{view.dates.date_is_tentative ? "日期暂定：" : "参考日期："}{view.dates.visit_start_date || view.dates.start_date || "待定"}</p>}
            {view ? view.entries.map(entry => <section key={entry.target_id} data-target={entry.target_id}>
              <h4>{entry.name}</h4>
              {entry.intro?.text && <Prose text={entry.intro.text} />}
              {(entry.practical || entry.slots || []).map(slot => <p key={slot.topic}><strong>{topics[slot.topic] || slot.topic}：</strong>{slot.text}{slot.notes?.length ? " " + slot.notes.join(" ") : ""}</p>)}
              {entry.tips?.map((tip, index) => <Prose key={index} text={tip.text} />)}
              {entry.extras.map((fact, index) => <p key={index}>{fact.topic === "duration" ? "建议游玩时长" : "咨询电话"}：{fact.value_raw}</p>)}
              {entry.variants.length > 0 && <details open={!!expanded[`${currentId}:${entry.target_id}`]}><summary onClick={event => {
                event.preventDefault(); const key = `${currentId}:${entry.target_id}`; setExpanded(old => ({ ...old, [key]: !old[key] }));
              }}>其他票种与适用条件</summary>{entry.variants.map((fact, index) => <div key={index}>{fact.context_raw && fact.context_raw !== entry.variants[index - 1]?.context_raw && <small>{fact.context_raw}</small>}<p>{fact.value_raw}</p></div>)}</details>}
              <button onClick={() => showSource(entry)}>查看这一景点的原文</button>
            </section>) : <Prose text={row.report} />}
          </> : mode === "source" ? <>
            {view?.sources.length ? <label>选择来源<select aria-label="选择来源" value={source?.id || 0} onChange={event => { remember(); setSourceId(Number(event.target.value)); setRange(null); }}>
              {view.sources.map(s => <option key={s.id} value={s.id}>{s.title || `来源${s.id}`}</option>)}
            </select></label> : null}
            {source?.text ? range ? <pre className="research-source">{source.text.slice(0, range.start)}<mark ref={marker}>{source.text.slice(range.start, range.end)}</mark>{source.text.slice(range.end)}</pre> : <Prose text={source.text} />
              : source?.snapshot ? <pre>{JSON.stringify(source.snapshot, null, 2)}</pre> : <p>本记录没有单独保存来源原文。</p>}
          </> : <pre>{JSON.stringify(view?.diagnostics || { stage: row.stage }, null, 2)}</pre>}
        </div>
        <button onClick={onPlan}>根据资料规划</button>
      </>}
    </details>
  </section>;
}
