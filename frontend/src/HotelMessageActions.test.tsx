// @vitest-environment jsdom
import {act,cleanup,fireEvent,render,screen} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import {HotelMessageActions} from "./HotelMessageActions";
import type {HotelAction} from "./api";

afterEach(()=>{cleanup();vi.unstubAllGlobals();});
const choose:HotelAction={kind:"select",label:"1. 酒店A",task_id:"task",task_version:3,candidate_set_id:"list",candidate_set_version:2,candidate_id:"poi",selectable:true};
const original:HotelAction={kind:"view_original",label:"查看原文",source_event_id:"web:source",revision:"digest"};
const page={text:"原文第一页",cursor:0,next_cursor:4000,total_chars:4003,source_event_id:"web:source",revision:"digest",historical:true,queried_at:"2026-11-06"};

it.each([1,2])("keeps visible choice %i bound to its exact hotel id",async(number)=>{
  const choices:HotelAction[]=[{...choose,label:"1. 汉庭甲店 · 甲路1号",candidate_id:"hotel-a"},
    {...choose,label:"2. 汉庭乙店 · 乙路2号",candidate_id:"hotel-b"}];
  const fetch=vi.fn(async(_url:string,_options?:RequestInit)=>({ok:true,json:async()=>({job_id:2})}));vi.stubGlobal("fetch",fetch);
  render(<HotelMessageActions conversationId="scope" actions={choices} disabled={false} onQueued={vi.fn()}/>);
  expect(screen.getByText("请选择具体门店")).toBeTruthy();
  expect(screen.getByText(`选择第${number}家`)).toBeTruthy();
  expect(screen.getByText(number===1 ? "地址：甲路1号" : "地址：乙路2号")).toBeTruthy();
  fireEvent.click(screen.getByRole("button",{name:choices[number-1].label}));
  await screen.findByText("已提交选择："+choices[number-1].label);
  const body=JSON.parse(fetch.mock.calls[0][1]!.body as string);
  expect(body).toMatchObject({candidate_id:number===1 ? "hotel-a" : "hotel-b",task_id:"task",task_version:3,candidate_set_id:"list",candidate_set_version:2});
  expect(body).not.toHaveProperty("label");
});

it("does not show a selection from an outdated candidate-list response",async()=>{
  let settle!:(value:unknown)=>void;
  const fetch=vi.fn(()=>new Promise(resolve=>{settle=resolve;}));vi.stubGlobal("fetch",fetch);
  const queued=vi.fn();const props={conversationId:"scope",disabled:false,onQueued:queued};
  const view=render(<HotelMessageActions {...props} actions={[choose]}/>);
  fireEvent.click(screen.getByRole("button",{name:choose.label}));
  expect(screen.getByText("正在提交选择："+choose.label)).toBeTruthy();
  view.rerender(<HotelMessageActions {...props} actions={[{...choose,candidate_set_version:3}]}/>);
  await act(async()=>settle({ok:true,json:async()=>({job_id:2})}));
  expect(screen.queryByRole("status")).toBeNull();expect(queued).not.toHaveBeenCalled();
});

it("does not highlight the same hotel in a different query's list",async()=>{
  const fetch=vi.fn(async(_url:string,_options?:RequestInit)=>({ok:true,json:async()=>({job_id:2})}));vi.stubGlobal("fetch",fetch);
  const other={...choose,label:"1. 酒店A另一个查询",query_index:1,candidate_set_id:"other-list"};
  render(<HotelMessageActions conversationId="scope" actions={[choose,other]} disabled={false} onQueued={vi.fn()}/>);
  fireEvent.click(screen.getByRole("button",{name:choose.label}));
  await screen.findByText("已提交选择："+choose.label);
  expect(screen.getByRole("button",{name:other.label}).classList.contains("hotel-choice-selected")).toBe(false);
});

it("submits a bound choice once and keeps identity and versions",async()=>{
  let settle!:(value:unknown)=>void;
  const fetch=vi.fn((_url:string,_options?:RequestInit)=>new Promise(resolve=>{settle=resolve;}));vi.stubGlobal("fetch",fetch);
  const queued=vi.fn();render(<HotelMessageActions conversationId="scope" actions={[choose]} disabled={false} onQueued={queued}/>);
  fireEvent.click(screen.getByRole("button",{name:"1. 酒店A"}));fireEvent.click(screen.getByRole("button",{name:"1. 酒店A"}));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toMatchObject({task_id:"task",task_version:3,candidate_set_version:2,candidate_id:"poi"});
  await act(async()=>settle({ok:true,json:async()=>({job_id:1})}));expect(queued).toHaveBeenCalledTimes(1);
});

it("reads all original pages using GET even while queries are disabled",async()=>{
  const fetch=vi.fn(async(url:string,_options?:RequestInit)=>({ok:true,json:async()=>url.includes("cursor=4000") ? {...page,text:"末页",cursor:4000,next_cursor:null} : page}));vi.stubGlobal("fetch",fetch);
  render(<HotelMessageActions conversationId="scope" actions={[original,choose]} disabled={true} onQueued={vi.fn()}/>);
  expect((screen.getByRole("button",{name:"1. 酒店A"}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button",{name:"查看原文"}));await screen.findByText("原文第一页");
  fireEvent.click(screen.getByRole("button",{name:"下一页"}));await screen.findByText("末页");
  expect(fetch).toHaveBeenCalledTimes(2);expect(fetch.mock.calls.every(call=>call.length===1 || !call[1]?.method)).toBe(true);
  expect((screen.getByRole("button",{name:"下一页"}) as HTMLButtonElement).disabled).toBe(true);
});

it("shows a stale-list failure and refreshes without choosing another option",async()=>{
  const fetch=vi.fn(async()=>({ok:false,status:409,json:async()=>({detail:"候选列表已变化"})}));vi.stubGlobal("fetch",fetch);
  const refresh=vi.fn();render(<HotelMessageActions conversationId="scope" actions={[choose]} disabled={false} onQueued={refresh}/>);
  fireEvent.click(screen.getByRole("button",{name:"1. 酒店A"}));expect((await screen.findByRole("alert")).textContent).toContain("候选列表已变化");
  expect(fetch).toHaveBeenCalledTimes(1);expect(refresh).toHaveBeenCalledTimes(1);
});

it("does not show another conversation's late original response",async()=>{
  let settle!:(value:unknown)=>void;vi.stubGlobal("fetch",vi.fn(()=>new Promise(resolve=>{settle=resolve;})));
  const props={actions:[original],disabled:false,onQueued:vi.fn()};const view=render(<HotelMessageActions conversationId="first" {...props}/>);
  fireEvent.click(screen.getByRole("button",{name:"查看原文"}));view.rerender(<HotelMessageActions conversationId="second" {...props}/>);
  await act(async()=>settle({ok:true,json:async()=>page}));expect(screen.queryByRole("dialog")).toBeNull();
});

it("makes a legacy non-lodging option visibly unavailable",()=>{
  const fetch=vi.fn();vi.stubGlobal("fetch",fetch);
  render(<HotelMessageActions conversationId="scope" actions={[{...choose,selectable:false}]} disabled={false} onQueued={vi.fn()}/>);
  const button=screen.getByRole("button",{name:"1. 酒店A（不可选）"});fireEvent.click(button);expect(fetch).not.toHaveBeenCalled();
});

it("does not reopen a closed original when a page request finishes late",async()=>{
  let settle!:(value:unknown)=>void;
  vi.stubGlobal("fetch",vi.fn().mockResolvedValueOnce({ok:true,json:async()=>page}).mockImplementationOnce(()=>new Promise(resolve=>{settle=resolve;})));
  render(<HotelMessageActions conversationId="scope" actions={[original]} disabled={false} onQueued={vi.fn()}/>);
  fireEvent.click(screen.getByRole("button",{name:"查看原文"}));await screen.findByText("原文第一页");
  fireEvent.click(screen.getByRole("button",{name:"下一页"}));fireEvent.click(screen.getByRole("button",{name:"关闭"}));
  await act(async()=>settle({ok:true,json:async()=>({...page,text:"末页",next_cursor:null})}));expect(screen.queryByRole("dialog")).toBeNull();
});

it("shows legacy source reference text without making a query",()=>{
  const fetch=vi.fn();vi.stubGlobal("fetch",fetch);
  render(<HotelMessageActions conversationId="scope" actions={[{...original,reference_text:"未核验的原文片段\n\n> 2026-11-09：210 RMB；税费未知"}]} disabled={true} onQueued={vi.fn()}/>);
  expect(screen.getByText(/210 RMB/)).toBeTruthy();expect(screen.getByText(/非新查询/)).toBeTruthy();expect(fetch).not.toHaveBeenCalled();
});
