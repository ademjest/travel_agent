import { useEffect, useRef, useState } from "react";
import { ArrowRight, RefreshCw } from "lucide-react";
import { api, dateText } from "./api";
import type { RailResultPage, RailResultReference, RailTrain } from "./api";
import "./RailResultCard.css";

const preferences: Record<string,string> = { high_speed_only: "仅高铁", high_speed: "高铁优先", d_train: "仅动车", conventional: "普通列车", all: "不限车种" };
const availability: Record<string,string> = { available: "有票", sold_out: "无票", waitlist: "候补", unknown: "余票未知" };
const clock = (hour: number) => `${Math.floor(hour).toString().padStart(2,"0")}:${Math.round((hour % 1)*60).toString().padStart(2,"0")}`;
const time = (value: string, day: string) => value.slice(0,10) === day ? value.slice(11,16) : value.slice(5,16).replace("T"," ");

export function RailResultCard({ conversationId, result, disabled = false, onQueryQueued }: {
  conversationId: string; result: RailResultReference; disabled?: boolean; onQueryQueued: () => Promise<unknown>;
}) {
  const [selection, setSelection] = useState({ page: 1, period: "current" });
  const [data, setData] = useState<RailResultPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [queryError, setQueryError] = useState("");
  const [querying, setQuerying] = useState(false);
  const [retry, setRetry] = useState(0);
  const request = useRef<{ period: string; id: string } | null>(null);
  const path = `/conversations/${conversationId}/rail-results/${encodeURIComponent(result.id)}`;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    api<RailResultPage>(`${path}?page=${selection.page}&period=${selection.period}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, selection.page, selection.period, retry]);

  async function query(period: string) {
    if (disabled || querying) return;
    if (request.current?.period !== period) request.current = { period, id: crypto.randomUUID() };
    setQuerying(true); setQueryError("");
    try {
      await api(`${path}/query`, { method: "POST", body: JSON.stringify({ period, client_request_id: request.current.id }) });
      await onQueryQueued();
      request.current = null;
    } catch (reason) { setQueryError((reason as Error).message); }
    finally { setQuerying(false); }
  }

  const window = data?.request.departure_window;
  const scope = window?.[0] === 0 && window?.[1] === 24 ? "全天" : (window?.[0] === 0 || window?.[0] === 9) && window?.[1] === 12 ? "上午" : window?.[0] === 6 && window?.[1] === 10 ? "早上" : window?.[0] === 12 && window?.[1] === 18 ? "下午" : window?.[0] === 18 && window?.[1] === 24 ? "晚上" : "指定时段";
  const first = data && data.matched_count ? (data.page-1)*data.page_size+1 : 0;
  return <section className="rail-card" aria-label={`${result.origin}到${result.destination}车次结果`} aria-busy={loading}>
    <header className="rail-card-header">
      <div><span className="rail-card-kicker">铁路车次</span><h3>{result.origin}<ArrowRight size={16} />{result.destination}</h3></div>
      {data && <span className="rail-preference">{preferences[data.request.train_preference]}</span>}
    </header>
    {data && <>
      <p className="rail-scope">{data.request.travel_date} · 查询范围：{scope} {clock(window![0])}–{clock(window![1])}</p>
      <p className="rail-result-count">本次取得 <strong>{data.total_count}</strong> 组车次{data.time_span.length > 0 && <span> · 出发 {data.time_span.join("–")}</span>}</p>
      {data.filters.length > 1 && <div className="rail-filters" role="tablist" aria-label="已查时段筛选">
        {data.filters.map(filter => <button type="button" key={filter.id} role="tab" aria-selected={selection.period === filter.id}
          disabled={loading} onClick={() => setSelection({ page: 1, period: filter.id })}>{filter.label}</button>)}
      </div>}
      {data.incomplete && <p className="rail-coverage" role="note">{data.legacy_partial ? "旧记录仅保留部分结果，可重新查询完整列表。" : `供应商候选达到${data.candidate_limit ? data.candidate_limit+"条" : ""}上限，本次结果可能未完整覆盖查询范围；可缩小时段补查。`}</p>}
    </>}
    {error && <div className="rail-error" role="alert">{error}<button type="button" onClick={() => setRetry(value => value+1)}>重试读取</button></div>}
    {!data && loading && <p className="rail-empty">正在读取车次结果…</p>}
    {data && <>
      <nav className="rail-pagination" aria-label="车次分页">
        <button type="button" disabled={loading || data.page <= 1} onClick={() => setSelection(value => ({ ...value, page: data.page-1 }))}>上一页</button>
        <span>第{data.page}/{data.page_count}页<small>已展示 {first}–{first ? first+data.items.length-1 : 0} / {data.matched_count} 组</small></span>
        <button type="button" disabled={loading || data.page >= data.page_count} onClick={() => setSelection(value => ({ ...value, page: data.page+1 }))}>下一页</button>
      </nav>
      {data.items.length > 0 ? <ul className={`rail-train-list ${loading ? "rail-loading" : ""}`}>
        {data.items.map(group => {
          const train = group.options[0];
          const seat = "quoted_seat" in train ? train.quoted_seat : train.seats.find(value => value.name === "二等座") || train.seats[0];
          return <li key={`${group.departure_date}:${group.train_code}`} className="rail-train">
            <div className="rail-train-row">
              <div className="rail-train-code"><strong>{group.train_code}</strong>{data.request.train_preference === "high_speed" && group.train_code.startsWith("D") && <small>动车备选</small>}</div>
              <div className="rail-train-route"><span>{train.origin_station} → {train.destination_station}</span><small>{time(train.departure_at,group.departure_date)} → {time(train.arrival_at,group.departure_date)} · {Math.floor(train.duration_minutes/60)}小时{train.duration_minutes%60}分</small></div>
              <div className="rail-train-price"><strong>{seat?.price_cny != null ? `¥${seat.price_cny}` : "票价未知"}</strong><small data-availability={seat?.availability}>{seat?.name || "席别未提供"} · {availability[seat?.availability || "unknown"]}</small></div>
            </div>
            <details className="rail-options"><summary>{group.options.length > 1 ? `${group.options.length}个乘车区间与席别` : "查看席别与详情"}</summary>
              {group.options.map((option: RailTrain, index: number) => <div className="rail-option" key={index}>
                <p>{option.origin_station} {time(option.departure_at,group.departure_date)} → {option.destination_station} {time(option.arrival_at,group.departure_date)}</p>
                <ul>{option.seats.length ? option.seats.map((value, i) => <li key={i}><span>{value.name || "席别未提供"}</span><strong>{value.price_cny != null ? `¥${value.price_cny}` : "票价未知"}</strong><span data-availability={value.availability}>{availability[value.availability]}</span></li>) : <li>票价和余票未提供。</li>}</ul>
              </div>)}
            </details>
          </li>;
        })}
      </ul> : <p className="rail-empty">{data.status === "not_on_sale" ? "供应商返回尚未起售。" : data.incomplete ? "本次快照未取得该时段结果，不能据此认定无车。" : "本次未返回匹配车次。"}</p>}
      <footer className="rail-card-footer"><span>数据时间：{dateText(data.queried_at)}<small>余票和价格会变化，购票前请在12306复核。</small></span>
        <button type="button" title="重新查询当前范围的车票" disabled={disabled || querying} onClick={() => query("current")}><RefreshCw size={13} />{querying ? "提交中…" : "刷新余票"}</button>
      </footer>
      {data.query_actions.length > 0 && <div className="rail-query-actions"><small>以下操作会重新查询</small><div>{data.query_actions.map(action => <button type="button" key={action.period} disabled={disabled || querying} onClick={() => query(action.period)}>{action.label}</button>)}</div></div>}
      {queryError && <p className="rail-error" role="alert">{queryError}</p>}
    </>}
  </section>;
}
