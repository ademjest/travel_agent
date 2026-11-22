// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,waitFor} from "@testing-library/react";
import {afterEach,expect,it,vi} from "vitest";
import {PlanComparisonPanel} from "./PlanComparisonPanel";
import {api} from "./api";
vi.mock("./api",()=>({api:vi.fn(),dateText:(value:string)=>value}));
afterEach(()=>{cleanup();vi.clearAllMocks();});
const report={message_id:"d:1",branches:{
  amap:{status:"completed",created_at:"2026-10-30",places:{p:{name:"历史馆"}},plan:{days:[{day_index:1,activities:[{poi_id:"p",period:"上午"}]}]}},
  wendao:{status:"completed",created_at:"2026-10-30",days_grounded:false,schedule_text:"仅供比较的日程",data:{report:"问道完整文化背景介绍"}},
  compare:{status:"completed",choice:"merge",reason:"选择理由"}},
  final_trip:null,final_presentation:null,final_text:"当时的最终回复",final_note:"此条历史预览未保存独立快照",decision:{},previous_message_id:null};
it("reads one saved report, distinguishes original/excerpt and restores each tab's scroll position",async()=>{
  vi.mocked(api).mockResolvedValue(report);
  const {container}=render(<PlanComparisonPanel conversationId="c" messageId="d:1"/>);
  await screen.findByText(/历史馆/);
  const body=container.querySelector('.comparison-body') as HTMLDivElement;body.scrollTop=220;
  fireEvent.click(screen.getByRole('button',{name:'问道原文'}));
  expect(screen.getByText('问道完整文化背景介绍')).toBeTruthy();
  expect(screen.getByText(/未验证覆盖全部要求天数/)).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox'));expect(screen.getByText('仅供比较的日程')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'高德数据草案'}));expect(body.scrollTop).toBe(220);
  fireEvent.click(screen.getByRole('button',{name:'最终行程'}));expect(screen.getByText('当时的最终回复')).toBeTruthy();
  expect(api).toHaveBeenCalledTimes(1);
});
it("aborts an unfinished read when leaving the conversation",async()=>{
  vi.mocked(api).mockImplementation(()=>new Promise(()=>{}));
  const {unmount}=render(<PlanComparisonPanel conversationId="c" messageId="d:1"/>);
  await waitFor(()=>expect(api).toHaveBeenCalledOnce());
  const signal=vi.mocked(api).mock.calls[0][1]?.signal;unmount();expect(signal?.aborted).toBe(true);
});

it("shows the actual fallback and validation cause even when the comparison receipt failed",async()=>{
  vi.mocked(api).mockResolvedValue({...report,decision:{status:"failed",choice:"amap",reason:"比较未通过，采用高德草案。"},
    branches:{...report.branches,compare:{status:"failed",created_at:"2026-10-30",error_type:"TripPlanError",
      error_code:"transport_conflict",error_summary:"活动与交通窗口冲突。",validation_path:"days[0].activities"}}});
  render(<PlanComparisonPanel conversationId="c" messageId="d:1"/>);
  await screen.findByRole("button",{name:"复制当前内容"});fireEvent.click(screen.getByRole("button",{name:"选择与调整"}));
  expect(screen.getByText("比较未通过，采用高德草案。")).toBeTruthy();
  const note=screen.getByText(/比较尝试未通过/);
  expect(note.textContent).toContain("活动与交通窗口冲突");expect(note.textContent).toContain("transport_conflict");
  expect(api).toHaveBeenCalledOnce();
});
