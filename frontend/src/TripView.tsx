import { ArrowRight, MapPin, Navigation } from "lucide-react";
import type { Trip } from "./api";
import { TransportLegCard } from "./TransportLegCard";
import { TripStatePanel } from "./TripStatePanel";
import { Markdown } from "./Markdown";

type Props = {
  trips: Trip[];
  onFill: (text: string) => void;
  onStart: () => void;
  onCompare?: (messageId:string) => void;
  onQuote?: (messageId:string,label:string) => void;
};

export function TripView({ trips, onFill, onStart, onCompare, onQuote }: Props) {
  return (trips.length ? (
              trips.map((saved) => { const trip=saved.preview || saved;
                const hotelRows=trip.plan.hotel_recommendations || [];
                const pendingHotels=hotelRows.filter(row=>!["fallback","conflict"].includes(row.status || "") && !["reference_in_budget","not_requested"].includes(row.price_status || "unknown")).map(row=>row.poi_id);
                const alternativeHotels=hotelRows.filter(row=>row.status==="fallback").map(row=>row.poi_id);
                const conflictingHotels=hotelRows.filter(row=>row.status==="conflict").map(row=>row.poi_id);
                const readyHotels=(trip.plan.hotel_ids || []).filter(id=>!pendingHotels.includes(id) && !alternativeHotels.includes(id) && !conflictingHotels.includes(id));
                const edit=(text:string)=>{if(saved.editing_message_id && onQuote)onQuote(saved.editing_message_id,`${trip.spec.destination || "行程"} · 版本 ${trip.version}`);onFill(text);};
                return (
                <section className="trip-card" key={trip.trip_id}>
                  <div className="trip-title">
                    <MapPin size={16} />
                    <h2>{trip.spec.destination}</h2>
                    <span>{trip.spec.day_count} {trip.spec.planning_window ? "个自然日" : "天"}</span>
                  </div>
                  <p className="muted">
                    {trip.is_preview && <strong>草稿预览，尚未保存 · </strong>}
                    {trip.spec.start_date || "日期待定"}{trip.spec.date_is_tentative ? "（暂定参考）" : ""} · 版本 {trip.version}
                  </p>
                  {saved.comparison_message_id && onCompare && <button className="trip-comparison-link" onClick={()=>onCompare(saved.comparison_message_id!)}>查看本轮两版草案</button>}
                  <TripStatePanel facts={trip.facts || []} />
                  {trip.spec.planning_window && <p className="muted">{trip.spec.planning_window.start_text}至{trip.spec.planning_window.end_text}；首末日按可用时段安排。{trip.spec.requested_day_count ? !trip.spec.duration_adjustment && `原天数要求：${trip.spec.requested_day_count}天。` : "天数由明确日期范围推导。"}</p>}
                  {trip.spec.duration_adjustment && <p role="status">{trip.spec.duration_adjustment}</p>}
                  {!trip.spec.planning_window && trip.plan.intercity && trip.plan.intercity.completeness!=="not_required" && <p className="muted">{trip.plan.intercity.day_count_basis==="destination_days" ? "保留目的地游览天数，交通日另列，需确认预览。" : `${trip.plan.intercity.basis_source==="default" ? "未明确口径，先按" : "按已声明口径，"}${trip.spec.day_count} 日内含往返。`}</p>}
                  {trip.plan.intercity && <TransportLegCard journey={trip.plan.intercity} leg={trip.plan.intercity.outbound} direction="outbound" tripId={trip.trip_id} onFill={onFill}
                    onAdjust={saved.editing_message_id && onQuote ? ()=>edit("去程改为：") : undefined}/>}
                  {trip.spec.preferences?.filter(p => !(trip.facts || []).some(f => f.value.includes(p))).length ? (
                    <div className="tags">
                      {trip.spec.preferences.filter(p => !(trip.facts || []).some(f => f.value.includes(p))).map((p) => (
                        <span key={p}>{p}</span>
                      ))}
                    </div>
                  ) : null}
                  <div className="days">
                    {trip.plan.days.map((day) => (
                      <div className="day" key={day.day_index}>
                        <span className="day-marker">
                          {day.day_index.toString().padStart(2, "0")}
                        </span>
                        <div>
                          <h3>
                            第 {day.day_index} 天 <small>{day.date}</small>
                          </h3>
                          {day.kind==="transport" && <p className="muted">交通日，预留给往返，不安排景点。</p>}
                          {!day.activities.length && day.kind!=="transport" && <p className="muted">自由休整，暂不安排固定景点。</p>}
                          {trip.plan.intercity?.availability_windows?.[String(day.day_index)] && <p className="muted">可游览时段参考：{trip.plan.intercity.availability_windows[String(day.day_index)].allowed_periods.join("、") || "无完整游览时段"}；未核验至分钟。</p>}
                          {day.activities.map((a, i) => (
                              <div key={i}>
                                <p>
                              <small>{a.period}</small>
                              {a.name ||
                                trip.sources[a.poi_id || ""]?.name ||
                                "待确认地点"}
                                </p>
                                {a.subactivities?.length ? <p className="muted">园内参观：{a.subactivities.map(child => trip.sources[child.poi_id]?.name || "待确认场馆").join("、")}</p> : null}
                              </div>
                          ))}
                          <button
                            onClick={() =>
                              edit(
                                `修改行程 ${trip.trip_id} 的第${day.day_index}天：`,
                              )
                            }
                          >
                            调整这一天 <ArrowRight size={12} />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                  <section className="trip-stay-summary"><h3>住宿与餐饮</h3>
                    {trip.plan.hotel_review_note && <p role="status">{trip.plan.hotel_review_note}</p>}
                    <p>住宿候选：{readyHotels.length ? readyHotels.map(id=>trip.sources[id]?.name || "地点待核对").join("、") : pendingHotels.length ? "预算尚未核实，暂不能确认具体酒店" : "具体酒店待选，可先围绕主要游览区域安排"}</p>
                    {pendingHotels.length>0 && <p>待核价（未确认符合预算）：{pendingHotels.map(id=>trip.sources[id]?.name || "地点待核对").join("、")}</p>}
                    {alternativeHotels.length>0 && <p>其他品牌备选（未满足品牌偏好）：{alternativeHotels.map(id=>trip.sources[id]?.name || "地点待核对").join("、")}</p>}
                    {conflictingHotels.length>0 && <p>未通过当前筛选的原候选：{conflictingHotels.map(id=>trip.sources[id]?.name || "地点待核对").join("、")}</p>}
                    {trip.plan.hotel_quotes && <div className="hotel-quote-summary">
                      <p className="muted">{trip.plan.hotel_quotes.retrieved_at
                        ? "问道参考报价 · "+new Date(trip.plan.hotel_quotes.retrieved_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})+"（北京时间）；不保证库存或成交价。"
                        : trip.plan.hotel_quotes.request_id ? "报价查询未取得可用结果" : "住宿报价尚未查询"}</p>
                      {trip.plan.hotel_quotes.stay?.check_in && <p className="muted">适用住宿：{trip.plan.hotel_quotes.stay.check_in} 至 {trip.plan.hotel_quotes.stay.check_out} · {trip.plan.hotel_quotes.stay.rooms}间 · {trip.plan.hotel_quotes.stay.adults}名成人</p>}
                      {trip.plan.hotel_quotes.note && <p role="status">{trip.plan.hotel_quotes.note}</p>}
                      {trip.plan.hotel_quotes.coverage && <p className="muted">本批询问{trip.plan.hotel_quotes.coverage.asked}家 · 返回门店名{trip.plan.hotel_quotes.coverage.returned}家 · 已绑定报价{trip.plan.hotel_quotes.coverage.bound}家 · 完整匹配产品{trip.plan.hotel_quotes.coverage.comparable}个</p>}
                      {trip.plan.hotel_quotes.offers.map(offer=><details key={offer.offer_id}>
                        <summary>{offer.hotel_name} · {offer.room_name} · 查看价格详情</summary>
                        <p className="muted">价格类型：{({per_room_night:"逐晚参考价",from:"起价线索，不代表所需产品可得价",average:"平均价，不能验证每一晚",total:"整段住宿总价",unknown:"口径尚未明确"} as Record<string,string>)[offer.price_basis || "unknown"]}</p>
                        <p>{({reference_match:"完整口径参考价在预算范围",reference_available:"参考报价适用，仍需确认库存",excluded:"已列产品不符合预算",historical:"历史参考，需刷新",different_stay:"与当前入住条件不同",pending:"价格与适用条件待核对"} as Record<string,string>)[offer.status || "pending"]}</p>
                        <ul>{offer.nightly_prices.map(n=><li key={n.date}>{n.date}：{n.amount} {offer.currency}/间/晚</li>)}</ul>
                        {!offer.nightly_prices.length && <p>逐晚价格未知，不能用均价替代每晚预算核对。</p>}
                        {offer.reported_total!=null && <p>供应商全程总价：{offer.reported_total} {offer.currency}</p>}
                        {offer.calculated_total!=null && <p>本地逐晚加总：{offer.calculated_total} {offer.currency}</p>}
                        {offer.average_price!=null && <p>供应商均价：{offer.average_price} {offer.currency}，不用于验证每一晚。</p>}
                        {offer.from_price!=null && <p>供应商起价：{offer.from_price} {offer.currency}，不保证所需产品适用。</p>}
                        {offer.budget_difference!=null && Number(offer.budget_difference)>0 && <p>列示金额{offer.numeric_comparison==="over" ? "高于上限" : "低于下限"}{offer.budget_difference}元（{offer.comparison_basis==="stay_total" ? "全程总价" : "每间每晚"}）。</p>}
                        <p>税费：{offer.taxes_and_fees_status==="included" ? "报价注明已含" : offer.taxes_and_fees_status==="excluded" ? "未含，完整应付金额待确认" : "未知"}；早餐：{offer.breakfast || "未知"}；退改：{offer.cancellation || "未知"}</p>
                        {offer.issues?.map(issue=><p className="muted" key={issue}>{issue}</p>)}
                        {offer.source_url && <a href={offer.source_url} target="_blank" rel="noreferrer">查看携程产品与条款</a>}
                      </details>)}
                      {trip.plan.hotel_quotes.reference_text && <Markdown text={trip.plan.hotel_quotes.reference_text}/>}
                      {!trip.plan.hotel_quotes.offers.length && !trip.plan.hotel_quotes.reference_text && trip.plan.hotel_quotes.retrieved_at && <p>{trip.plan.hotel_quotes.result_note || "本批没有可比较报价，保留住宿要求与待核价候选。"}</p>}
                      {trip.plan.hotel_quotes.raw_text && <details><summary>查看已保存的报价原文</summary><pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{trip.plan.hotel_quotes.raw_text}</pre></details>}
                    </div>}
                    {(hotelRows.length>0 || trip.plan.hotel_ids?.length) && <button onClick={()=>edit("请使用问道刷新这份行程酒店候选在已声明入住条件下的参考房价，只查价，不修改或保存行程。")}>刷新报价（联网查询）</button>}
                    <button onClick={()=>edit("调整这份行程的住宿：")}>调整住宿</button>
                    <p>餐饮：{trip.plan.dining_options?.length ? trip.plan.dining_options.map(m=>`第${m.day_index}天${m.meal}`).join("、") : "结合当天路线就近用餐"}</p>
                    <button onClick={()=>edit("调整这份行程的餐饮：")}>调整餐饮</button>
                  </section>
                  {trip.plan.intercity && <>
                    <TransportLegCard journey={trip.plan.intercity} leg={trip.plan.intercity.inbound} direction="inbound" tripId={trip.trip_id} onFill={onFill}
                      onAdjust={saved.editing_message_id && onQuote ? ()=>edit("返程改为：") : undefined}/>
                    {trip.plan.intercity.completeness==="conflict" && <p role="status">交通与活动有冲突，请先调整草稿。</p>}
                    {trip.plan.intercity.completeness==="partial" && <p role="status">交通资料尚不完整，保留可用城市安排。</p>}
                    {trip.plan.intercity.limitations?.length ? <details><summary>交通假设与需确认项</summary><ul>{[...new Set(trip.plan.intercity.limitations)].map(text=><li key={text}>{text}</li>)}</ul></details> : null}
                    {trip.plan.intercity.ticket_cost?.total_cny != null ? <p>单人参考票价合计 {trip.plan.intercity.ticket_cost.total_cny} CNY；非全程预算。</p> : (trip.plan.intercity.outbound || trip.plan.intercity.inbound) && <p>部分票价未知，未按零元计入。</p>}
                  </>}
                </section>
              );})
            ) : (
              <div className="detail-empty">
                <div className="route-art">
                  <MapPin size={26} />
                  <span />
                  <Navigation size={24} />
                </div>
                <h2>让目的地，成为计划</h2>
                <p>
                  和我聊聊你的目的地与期待。
                  <br />
                  保存的行程会出现在这里。
                </p>
                <button onClick={() => onStart()}>
                  开始规划 <ArrowRight size={14} />
                </button>
              </div>
            ));
}
