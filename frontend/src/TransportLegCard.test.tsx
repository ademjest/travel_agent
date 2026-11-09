// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,within} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import {TransportLegCard} from "./TransportLegCard";
import {TripMessage} from "./TripMessage";
import type {IntercityLeg,TripPresentation} from "./api";
afterEach(cleanup);
const leg:IntercityLeg={status:"completed",selected:{train_code:"G1",origin_station:"甲城南",destination_station:"乙城南",
  departure_at:"2026-11-01T23:00+08:00",arrival_at:"2026-11-02T01:00+08:00",duration_minutes:120,
  seats:[{name:"商务座",price_cny:788,availability:"available"},{name:"二等座",price_cny:203,availability:"available"}]},
  selected_seat:{name:"二等座",seat_class:"second_class",price_cny:203,availability:"available"},queried_at:"2026-10-30T01:00:00Z"};
it("shows a return-day issue only on the affected leg",()=>{
  const journey={completeness:"conflict",issues:[{code:"transport_conflict",direction:"inbound",day_index:3,message:"第3天：返程前的活动窗口不足。"}]};
  render(<><TransportLegCard journey={journey} leg={leg} direction="outbound"/>
    <TransportLegCard journey={journey} leg={{status:"completed"}} direction="inbound"/></>);
  expect(within(screen.getByRole("region",{name:"去程交通"})).queryByText(/窗口不足/)).toBeNull();
  expect(within(screen.getByRole("region",{name:"返程交通"})).getByText(/第3天/)).toBeTruthy();
});
it("distinguishes a completed query from a selected service for old drafts",()=>{
  render(<TransportLegCard journey={{completeness:"conflict"}} leg={{status:"completed"}} direction="outbound"/>);
  expect(screen.getByText("已取得候选，尚未选定可衔接行程的车次。")).toBeTruthy();
  expect(screen.queryByText("交通与活动窗口存在冲突，请先调整草稿。")).toBeNull();
});
it("uses the server-selected seat, shows overnight arrival and only expands local details",()=>{
  const fill=vi.fn();render(<TransportLegCard journey={{completeness:"partial"}} leg={leg} direction="outbound" tripId="TR-1" onFill={fill}/>);
  expect(screen.getByText("二等座")).toBeTruthy();expect(screen.getByText("¥203")).toBeTruthy();
  expect(screen.queryByText(/788/)).toBeNull();expect(screen.getByText("+1天")).toBeTruthy();
  fireEvent.click(screen.getByText("数据时间与接驳"));expect(fill).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"调整去程"}));expect(fill).toHaveBeenCalledWith("请把 TR-1 行程的去程改为：");
});
it("does not infer a chosen seat or price for old snapshots",()=>{
  render(<TransportLegCard journey={{completeness:"partial"}} leg={{...leg,selected_seat:undefined}} direction="inbound"/>);
  expect(screen.getByText("历史快照未保存明确选定席别。")).toBeTruthy();expect(screen.queryByText(/¥203/)).toBeNull();
});

it("binds a message reference before filling the transport edit",()=>{
  const fill=vi.fn(),quote=vi.fn();
  const p:TripPresentation={trip:{trip_id:"TR-1",version:1,spec:{},sources:{},plan:{days:[],intercity:{completeness:"partial",outbound:leg}}},
    prefix:"",suffix:"",is_preview:true,sections:[{kind:"transport",direction:"outbound"}]};
  render(<TripMessage presentation={p} onFill={fill} onQuote={quote}/>);
  fireEvent.click(screen.getByRole("button",{name:"调整去程"}));
  expect(quote).toHaveBeenCalledOnce();expect(fill).toHaveBeenCalledWith("去程改为：");
});
it("keeps culture, days, confirmation and two transport cards in the original message order",()=>{
  const p:TripPresentation={trip:{trip_id:"TR-1",version:1,spec:{},sources:{},plan:{days:[],intercity:{completeness:"partial",outbound:leg,inbound:leg}}},
    prefix:"修改说明",suffix:"确认后才保存",is_preview:true,sections:[{kind:"text",text:"行程概览"},{kind:"transport",direction:"outbound"},
      {kind:"text",text:"第1天：城市历史与文化介绍"},{kind:"transport",direction:"inbound"}]};
  const {container}=render(<TripMessage presentation={p}/>);const text=container.textContent || "";
  expect(text.indexOf("去程交通")).toBeLessThan(text.indexOf("城市历史"));expect(text.indexOf("城市历史")).toBeLessThan(text.indexOf("返程交通"));
  expect(text).toContain("确认后才保存");expect(screen.queryByRole("button")).toBeNull();
});
