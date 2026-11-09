// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RailResultCard } from "./RailResultCard";
import type { RailResultPage } from "./api";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function page(number = 1, morning = false): RailResultPage {
  return { id: "result", request: { origin: "武汉", destination: "西安", travel_date: "2026-11-01", departure_window: morning ? [0,12] : [0,24], train_preference: "high_speed_only", origin_scope: "city", destination_scope: "city" },
    status: "ok", queried_at: "2026-10-28T15:27:00+00:00", total_count: 8, matched_count: 8,
    time_span: morning ? ["07:18","11:55"] : ["07:18","19:21"], page: number, page_count: 2, page_size: 6, period: "current",
    items: Array.from({ length: number === 1 ? 6 : 2 }, (_, i) => ({ train_code: `G${100+(number-1)*6+i}`, departure_date: "2026-11-01", options: [{ train_code: `G${100+i}`, origin_station: "汉口", destination_station: "西安北", departure_at: `2026-11-01T${number === 1 ? "07:18" : "19:21"}+08:00`, arrival_at: "2026-11-01T22:30+08:00", duration_minutes: 220, seats: [{ name: "二等座", price_cny: 233, availability: "available" }] }] })),
    filters: morning ? [{ id: "current", label: "已查时段" }] : [{ id: "current", label: "全天" }, { id: "evening", label: "晚上" }],
    query_actions: morning ? [{ period: "all_day", label: "扩展查询全天" }] : [], incomplete: false, legacy_partial: false };
}

it("reads cached pages and filters with GET while keeping the original retrieval time", async () => {
  const fetch = vi.fn(async (url: string) => ({ ok: true, json: async () => url.includes("period=evening") ? { ...page(2), page: 1, page_count: 1, period: "evening", matched_count: 2 } : page(url.includes("page=2") ? 2 : 1) }));
  vi.stubGlobal("fetch", fetch);
  render(<RailResultCard conversationId="first" result={{ id: "result", origin: "武汉", destination: "西安" }} onQueryQueued={vi.fn()} />);
  await screen.findByText("G100");
  const timestamp = screen.getByText(/数据时间/).textContent;
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByText("G106");
  expect(screen.queryByText("G100")).toBeNull();
  expect(screen.getByText(/数据时间/).textContent).toBe(timestamp);
  fireEvent.click(screen.getByRole("tab", { name: "晚上" }));
  await waitFor(() => expect(screen.getByText("第1/1页")).toBeTruthy());
  expect(fetch.mock.calls.map(call => call[0]).at(-1)).toContain("page=1&period=evening");
  expect(fetch.mock.calls.every(call => !(call as unknown as [string,RequestInit])[1]?.method)).toBe(true);
});

it("only shows the queried morning range and expands by an idempotent typed action", async () => {
  let fail = true;
  const submitted = vi.fn();
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => ({ ok: options?.method === "POST" ? !fail : true,
    status: fail ? 503 : 202, json: async () => options?.method === "POST" ? { detail: "连接暂不可用" } : page(1,true) }));
  vi.stubGlobal("fetch", fetch);
  render(<RailResultCard conversationId="first" result={{ id: "result", origin: "武汉", destination: "西安" }} onQueryQueued={submitted} />);
  await screen.findByText(/查询范围：上午/);
  expect(screen.queryByRole("tablist")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "扩展查询全天" }));
  await screen.findByRole("alert");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "扩展查询全天" }));
  await waitFor(() => expect(submitted).toHaveBeenCalledOnce());
  const requests = fetch.mock.calls.filter(call => call[1]?.method === "POST").map(call => JSON.parse(String(call[1]?.body)));
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[0].period).toBe("all_day");
  expect(Object.keys(requests[0]).sort()).toEqual(["client_request_id","period"]);
});

it("does not publish a stale page after the conversation card unmounts", async () => {
  let resolve!: (value: unknown) => void;
  const pending = new Promise(done => { resolve = done; });
  const fetch = vi.fn(async () => ({ ok: true, json: () => pending }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<RailResultCard conversationId="old" result={{ id: "result", origin: "武汉", destination: "西安" }} onQueryQueued={vi.fn()} />);
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  view.unmount();
  await act(async () => resolve(page()));
  expect(screen.queryByText("G100")).toBeNull();
});

it("shows the requested first-class quote and the current morning window",async()=>{
  const data=page(1,true);data.request.departure_window=[9,12];data.request.seat_class="first_class";
  data.items=data.items.slice(0,1);data.items[0].options[0].quoted_seat={name:"一等座",seat_class:"first_class",price_cny:399,availability:"available"};
  vi.stubGlobal("fetch",vi.fn(async()=>({ok:true,json:async()=>data})));
  render(<RailResultCard conversationId="first" result={{id:"result",origin:"武汉",destination:"西安"}} onQueryQueued={vi.fn()}/>);
  await screen.findByText(/查询范围：上午 09:00–12:00/);
  expect(screen.getByText("¥399")).toBeTruthy();expect(screen.getByText(/一等座 · 有票/)).toBeTruthy();
  expect(screen.getByText("¥399").closest(".rail-train-price")?.textContent).not.toContain("¥233");
});

it("retains the accepted query identity when refreshing the conversation fails", async () => {
  const submitted = vi.fn().mockRejectedValueOnce(new Error("列表同步失败")).mockResolvedValue(undefined);
  const fetch = vi.fn(async (_url: string, options?: RequestInit) => ({ ok: true,
    json: async () => options?.method === "POST" ? { job_id: 1 } : page(1,true) }));
  vi.stubGlobal("fetch", fetch);
  render(<RailResultCard conversationId="first" result={{ id: "result", origin: "武汉", destination: "西安" }} onQueryQueued={submitted} />);
  await screen.findByText(/查询范围：上午/);
  fireEvent.click(screen.getByRole("button", { name: "扩展查询全天" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "扩展查询全天" }));
  await waitFor(() => expect(submitted).toHaveBeenCalledTimes(2));
  const requests = fetch.mock.calls.filter(call => call[1]?.method === "POST").map(call => JSON.parse(String(call[1]?.body)));
  expect(requests).toHaveLength(2);
  expect(requests[0].client_request_id).toBe(requests[1].client_request_id);
});
