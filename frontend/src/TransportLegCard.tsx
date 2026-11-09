import { ArrowDownLeft, ArrowUpRight, ArrowRight, TrainFront } from "lucide-react";
import type { IntercityLeg, IntercityPlan } from "./api";
import "./TransportLegCard.css";

export function TransportLegCard({ journey, leg, direction, onFill, tripId, onAdjust }: {
  journey: IntercityPlan; leg?: IntercityLeg; direction: "outbound" | "inbound";
  onFill?: (text: string) => void; tripId?: string; onAdjust?:()=>void;
}) {
  if (!leg) return null;
  const title = direction === "outbound" ? "去程" : "返程";
  const Direction = direction === "outbound" ? ArrowUpRight : ArrowDownLeft;
  const train = leg.selected, seat = leg.selected_seat;
  const transfer = journey.station_transfers?.[direction];
  const states: Record<string, string> = {needs_input:"请补充交通信息", failed:"本次查询未完成", unavailable:"铁路数据暂不可用",
    empty:"本次范围无匹配车次", incomplete:"候选不完整，尚不能确认全部可选车次", seat_unavailable:"本次范围内目标席别暂无已确认可售票",
    sold_out:"当前没有已确认可直接购买的席位", stock_unknown:"余票状态尚未确认", blocked_source:"当前来源限制下未采用铁路数据",
    mode_declared:"沿用指定交通方式，未查询铁路"};
  const date = train?.departure_at || leg.request?.travel_date;
  const day = date ? new Date(date.length === 10 ? `${date}T12:00:00+08:00` : date).toLocaleDateString("zh-CN", {month:"long", day:"numeric", weekday:"short", timeZone:"Asia/Shanghai"}) : "日期待确认";
  const overnight = train ? Math.round((Date.parse(`${train.arrival_at.slice(0,10)}T00:00:00Z`) - Date.parse(`${train.departure_at.slice(0,10)}T00:00:00Z`))/86400000) : 0;
  const fallback = seat && leg.request?.seat_class && seat.seat_class !== leg.request.seat_class;
  const issues = journey.issues?.filter(issue => issue.direction === direction);
  const warning = issues?.length ? issues.map(issue => issue.message).join(" ") : states[leg.status] ||
    (!train && leg.status === "completed" ? "已取得候选，尚未选定可衔接行程的车次。" :
      leg.candidate_truncated ? "返回候选不完整，仅在已返回范围内推荐。" : "");
  return <section className={`transport-card transport-${direction}`} aria-label={`${title}交通`}>
    <header className="transport-heading"><span><Direction size={17} aria-hidden="true"/><strong>{title}交通</strong></span><time>{day}</time></header>
    {train ? <>
      <div className="transport-stations">
        <div><strong>{train.departure_at.slice(11,16)}</strong><span>{train.origin_station}</span></div>
        <ArrowRight size={19} aria-hidden="true"/>
        <div><strong>{train.arrival_at.slice(11,16)}{overnight > 0 && <small>+{overnight}天</small>}</strong><span>{train.destination_station}</span></div>
      </div>
      <div className="transport-service"><span><TrainFront size={15} aria-hidden="true"/>{train.train_code}</span><span>{Math.floor(train.duration_minutes/60)}小时{train.duration_minutes%60 ? `${train.duration_minutes%60}分` : ""}</span></div>
      <div className="transport-fare"><span>{seat?.name || (leg.provider === "user_ticket" ? "已有车票" : "席别未确认")}</span>
        <span>{seat?.price_cny != null ? <><strong>{seat.currency && seat.currency !== "CNY" ? seat.currency : "¥"}{seat.price_cny.toLocaleString("zh-CN")}</strong><small> / 人 / 单程 · 参考</small></> : "票价待核实"}</span></div>
      {fallback && <p className="transport-warning">当前为其他席别备选，尚未满足首选席别。</p>}
      {!seat && leg.provider !== "user_ticket" && !warning && <p className="transport-note">历史快照未保存明确选定席别。</p>}
    </> : <p className="transport-warning">{warning || "等待交通安排"}</p>}
    {train && warning && <p className="transport-warning">{warning}</p>}
    {transfer?.status !== "known" && transfer && <p className="transport-warning">站点接驳未核实，游览窗口含规划假设。</p>}
    <details className="transport-details"><summary>数据时间与接驳</summary>
      <p>{leg.queried_at ? `查询时间：${new Date(leg.queried_at).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"})}（北京时间）` : "本段没有实时查票记录。"}</p>
      {transfer?.status === "known" && <p>站点接驳约 {transfer.duration_minutes} 分钟，按查询时路网参考。</p>}
      {leg.request?.mode_source === "default" && <p>未指定城际方式，先按高铁优先安排。</p>}
      {leg.provider === "user_ticket" && <p>按你提供的车票时间安排，未核实出票状态。</p>}
      <p>推荐不代表已购票；余票与价格以购票时为准。</p>
    </details>
    {(onAdjust || onFill && tripId) && <button className="transport-adjust" onClick={() => onAdjust ? onAdjust() : onFill?.(`请把 ${tripId} 行程的${title}改为：`)}>调整{title}</button>}
  </section>;
}
