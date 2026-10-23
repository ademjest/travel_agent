// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ResearchPanel } from "./ResearchPanel";
import { Markdown } from "./App";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

function structured(id = "saved-first", price = "27元", stage = "partial") {
  return { research_id: id, content_version: id+price+stage, stage, updated_at: "2026-10-21T09:00:00Z", report: "兼容报告", source_count: 1,
    presentation: { schema_version: "travel-answer-v2", source_retrieved_at: "2026-10-21T09:00:00Z",
      dates: { visit_start_date: "2026-10-22", date_is_tentative: true }, diagnostics: { physical_calls: 1 },
      sources: [{ id: 1, provider: "ctrip_wendao", title: "供应商原文", text: "原文完整回答<script>alert(1)</script>" }],
      entries: [{ target_id: "target-1", name: "测试博物馆", intro: { text: "青瓷与城墙遗址讲述城市的文化故事。" },
        tips: [{ text: "不妨放慢脚步细看器物。" }], extras: [], variants: [], materials: [{ source_ids: [1], start: 0, end: 6 }],
        practical: [{ topic: "ticket_price", text: `成人票${price}（未持优待证件）`, source_ids: [1] }] }] } };
}

it("keeps one chapter per target and switches source mode without duplicating prose", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => [structured()] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ResearchPanel conversation="one-chapter" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(screen.getAllByText("成人票27元（未持优待证件）", { exact: false })).toHaveLength(1);
  expect(screen.getAllByRole("heading", { name: "测试博物馆" })).toHaveLength(1);
  expect(screen.getByText(/日期暂定/)).toBeTruthy();
  expect(screen.queryByText(/原文完整回答/)).toBeNull();
  fireEvent.click(screen.getByText("查看原文"));
  expect(screen.getByText(/原文完整回答/)).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "测试博物馆" })).toBeNull();
  expect(document.querySelector("script")).toBeNull();
  fireEvent.click(screen.getByText("阅读报告"));
  expect(screen.getByRole("heading", { name: "测试博物馆" })).toBeTruthy();
});

it("hides historical relative and same-origin evidence links while preserving external links", () => {
  const origin = window.location.origin;
  render(<Markdown text={`[旧来源](#source-bbbbbbbbbbbb-1-target-1-ticket_price) [绝对来源](${origin}/#source-bbbbbbbbbbbb-1-target-1-ticket_price) [外部链接](https://example.test/#source-bbbbbbbbbbbb-1-target-1-ticket_price)`} />);
  expect(screen.queryByText("旧来源")).toBeNull();
  expect(screen.queryByText("绝对来源")).toBeNull();
  expect(screen.getByRole("link", { name: "外部链接" }).getAttribute("target")).toBe("_blank");
});

it("stops terminal polling, preserves manual scroll over 12 seconds, and restores reading position", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => [structured()] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ResearchPanel conversation="scroll-terminal" onPlan={() => {}} />);
  await act(async () => {});
  fireEvent.click(screen.getByText("查看完整报告"));
  await act(async () => {});
  const report = screen.getByLabelText("研究报告内容");
  report.scrollTop = 333; fireEvent.scroll(report);
  const count = fetchMock.mock.calls.length;
  await act(async () => { vi.advanceTimersByTime(12000); });
  expect(report.scrollTop).toBe(333);
  expect(fetchMock).toHaveBeenCalledTimes(count);
  fireEvent.click(screen.getByText("查看原文"));
  fireEvent.click(screen.getByText("阅读报告"));
  expect(report.scrollTop).toBe(333);
});

it("background polling never relocates the user after an explicit source selection", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => [structured("running", "27元", "reading")] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ResearchPanel conversation="scroll-running" onPlan={() => {}} />);
  await act(async () => {});
  fireEvent.click(screen.getByText("查看完整报告")); await act(async () => {});
  fireEvent.click(screen.getByText("查看这一景点的原文"));
  const report = screen.getByLabelText("研究报告内容");
  report.scrollTop = 200; fireEvent.scroll(report);
  await act(async () => { vi.advanceTimersByTime(4000); });
  await act(async () => { vi.advanceTimersByTime(4000); });
  await act(async () => { vi.advanceTimersByTime(4000); });
  expect(fetchMock.mock.calls.length).toBeGreaterThan(3);
  expect(report.scrollTop).toBe(200);
});

it("offers a new report without replacing the old one and keeps planning user initiated", async () => {
  const onPlan = vi.fn();
  let rows = [structured("old", "27元")];
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => rows })));
  const { rerender } = render(<ResearchPanel conversation="versions" refreshKey="first" onPlan={onPlan} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  await waitFor(() => expect(screen.getByText(/成人票27元/)).toBeTruthy());
  rows = [structured("new", "37元"), structured("old", "27元")];
  rerender(<ResearchPanel conversation="versions" refreshKey="second" onPlan={onPlan} />);
  await screen.findByText("有新结果，点击查看");
  expect(screen.getByText(/成人票27元/)).toBeTruthy();
  expect(screen.queryByText(/成人票37元/)).toBeNull();
  fireEvent.click(screen.getByText("有新结果，点击查看"));
  expect(screen.getByText(/成人票37元/)).toBeTruthy();
  expect(onPlan).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("根据资料规划"));
  expect(onPlan).toHaveBeenCalledTimes(1);
});

it("ignores a delayed response after changing conversations", async () => {
  let release!: (value: unknown) => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("first-delayed")
    ? new Promise(resolve => { release = resolve; }) : { ok: true, json: async () => [] }));
  const { rerender } = render(<ResearchPanel conversation="first-delayed" onPlan={() => {}} />);
  rerender(<ResearchPanel conversation="second-empty" onPlan={() => {}} />);
  await act(async () => { release({ ok: true, json: async () => [structured()] }); });
  expect(screen.queryByText("查看完整报告")).toBeNull();
});

it("renders a legacy long report completely without a chat size cap", async () => {
  const report = "## 旧报告\n### 故宫\n" + "完整条款。".repeat(2000) + "\n末尾条件不得省略。";
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [{ stage: "completed", report, source_count: 1 }] })));
  render(<ResearchPanel conversation="legacy-long" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(screen.getByText(/末尾条件不得省略/)).toBeTruthy();
});

it("preserves expanded ticket conditions when returning from source mode", async () => {
  const row = structured();
  const variant = { source_id: 1, value_raw: "学生票需出示学生证。" };
  const value = { ...row, presentation: { ...row.presentation, entries: [{ ...row.presentation.entries[0], variants: [variant] }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="expanded-variant" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  fireEvent.click(screen.getByText("其他票种与适用条件"));
  fireEvent.click(screen.getByText("查看原文"));
  fireEvent.click(screen.getByText("阅读报告"));
  expect(screen.getByText("学生票需出示学生证。").closest("details")?.open).toBe(true);
});
