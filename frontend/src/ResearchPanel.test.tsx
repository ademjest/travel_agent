// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ResearchPanel, forgetResearchView } from "./ResearchPanel";
import { Markdown } from "./Markdown";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("shows pending cultural material without claiming the source is missing", async () => {
  const row = structured("pending-culture");
  const note = "已收到参考资料，景点介绍尚待整理或核对，可查看原文。";
  const value = { ...row, presentation: { ...row.presentation,
    entries: [{ ...row.presentation.entries[0], intro: {}, cultural_note: note }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="pending-culture" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(screen.getByText(note)).toBeTruthy();
  fireEvent.click(screen.getByText("查看这一景点的原文"));
  expect(document.querySelector("mark")?.textContent).toBeTruthy();
});

it("restores the saved reading position after closing and reopening the wide report",async()=>{
  vi.stubGlobal("fetch",vi.fn(async()=>({ok:true,json:async()=>[structured("reopen-scroll")]})));
  render(<ResearchPanel conversation="reopen-scroll" onPlan={()=>{}}/>);
  fireEvent.click(await screen.findByText("查看完整报告"));
  const report=screen.getByLabelText("研究报告内容");
  report.scrollTop=450;fireEvent.scroll(report);
  fireEvent.click(screen.getByRole("button",{name:"关闭报告"}));
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByText("查看完整报告"));
  expect(screen.getByLabelText("研究报告内容").scrollTop).toBe(450);
});

it("summarizes a repeated place once while keeping its distinct visit dates in the report",async()=>{
  const row=structured("multi-day-directory");
  const entry=row.presentation.entries[0];
  const value={...row,presentation:{...row.presentation,entries:[
    {...entry,visit_date:"2030-10-01"},{...entry,target_id:"target-2",visit_date:"2030-10-02"}]}};
  vi.stubGlobal("fetch",vi.fn(async()=>({ok:true,json:async()=>[value]})));
  render(<ResearchPanel conversation="multi-day-directory" onPlan={()=>{}}/>);
  expect(await screen.findByText("1 个景点 · 1 份已保存来源")).toBeTruthy();
  fireEvent.click(screen.getByText("查看完整报告"));
  expect(screen.getByText("游览日：2030-10-01")).toBeTruthy();
  expect(screen.getByText("游览日：2030-10-02")).toBeTruthy();
});

it("forgets a deleted conversation's saved report view", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [structured()] })));
  const first = render(<ResearchPanel conversation="deleted-view" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(document.querySelector(".research-details")?.hasAttribute("open")).toBe(true);
  first.unmount();
  forgetResearchView("deleted-view");
  render(<ResearchPanel conversation="deleted-view" onPlan={() => {}} />);
  await screen.findByText("查看完整报告");
  expect(document.querySelector(".research-details")?.hasAttribute("open")).toBe(false);
});

function structured(id = "saved-first", price = "27元", stage = "partial") {
  return { research_id: id, content_version: id+price+stage, stage, updated_at: "2026-10-21T09:00:00Z", report: "兼容报告", source_count: 1,
    presentation: { schema_version: "travel-answer-v2", source_retrieved_at: "2026-10-21T09:00:00Z",
      dates: { visit_start_date: "2026-10-22", date_is_tentative: true }, diagnostics: { physical_calls: 1 },
      sources: [{ id: 1, provider: "ctrip_wendao", title: "供应商原文", text: "原文完整回答<script>alert(1)</script>" }],
      entries: [{ target_id: "target-1", name: "测试博物馆", intro: { text: "青瓷与城墙遗址讲述城市的文化故事。" },
        tips: [{ text: "不妨放慢脚步细看器物。" }], extras: [], variants: [], materials: [{ source_ids: [1], start: 0, end: 6 }],
        practical: [{ topic: "ticket_price", text: `成人票${price}（未持优待证件）`, source_ids: [1] }] }] } };
}

it("omits empty navigation records from original sources and follows a readable material", async () => {
  const row = structured("readable-material");
  const value = { ...row, presentation: { ...row.presentation,
    sources: [{ id: 2, title: "Untitled", text: "" }, { id: 3, title: "西安世博园官方网站", text: " \n " },
      ...row.presentation.sources],
    entries: [{ ...row.presentation.entries[0], materials: [{ source_ids: [2, 1], start: 0, end: 6 }] }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="readable-material" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  fireEvent.click(screen.getByText("查看这一景点的原文"));
  const select = screen.getByRole("combobox", { name: "选择来源" }) as HTMLSelectElement;
  expect(Array.from(select.options).map(option => option.text)).toEqual(["供应商原文"]);
  expect(select.value).toBe("1");
  expect(document.querySelector("mark")?.textContent).toBe("原文完整回答");
});

it("preserves real website text and nonempty snapshots regardless of the source title", async () => {
  const row = structured("source-content");
  const value = { ...row, presentation: { ...row.presentation, sources: [
    ...row.presentation.sources, { id: 2, title: "Untitled", text: "已保存的网页正文" },
    { id: 3, title: "原始快照", text: " \n ", snapshot: { answer: "已保存的结构化原文" } },
    { id: 4, title: "空对象", snapshot: {} }, { id: 5, title: "空数组", snapshot: [] },
    { id: 6, title: "空字符串", snapshot: "  " }, { id: 7, title: "空快照", snapshot: null }
  ] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="source-content" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  fireEvent.click(screen.getByText("查看原文"));
  const select = screen.getByRole("combobox", { name: "选择来源" }) as HTMLSelectElement;
  expect(Array.from(select.options).map(option => option.text)).toEqual(["供应商原文", "Untitled", "原始快照"]);
  fireEvent.change(select, { target: { value: "2" } });
  expect(screen.getByText("已保存的网页正文")).toBeTruthy();
  fireEvent.change(select, { target: { value: "3" } });
  expect(screen.getByText(/已保存的结构化原文/)).toBeTruthy();
});

it("falls back to readable original text when a previously selected source becomes empty", async () => {
  const row = structured("source-refresh");
  let extra = { id: 2, title: "网页原文", text: "网页原文内容" };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [{ ...row,
    content_version: extra.text, presentation: { ...row.presentation, sources: [...row.presentation.sources, extra] } }] })));
  const { rerender } = render(<ResearchPanel conversation="source-refresh" refreshKey="before" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  fireEvent.click(screen.getByText("查看原文"));
  fireEvent.change(screen.getByRole("combobox", { name: "选择来源" }), { target: { value: "2" } });
  expect(screen.getByText("网页原文内容")).toBeTruthy();
  extra = { ...extra, text: "" };
  rerender(<ResearchPanel conversation="source-refresh" refreshKey="after" onPlan={() => {}} />);
  await waitFor(() => expect((screen.getByRole("combobox", { name: "选择来源" }) as HTMLSelectElement).value).toBe("1"));
  expect(screen.getByText(/原文完整回答/)).toBeTruthy();
  expect(screen.queryByRole("option", { name: "网页原文" })).toBeNull();
});

it("does not offer an empty source selector when no original content was saved", async () => {
  const row = structured("empty-sources");
  const value = { ...row, presentation: { ...row.presentation, sources: [{ id: 1, title: "空记录", text: " \n " }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="empty-sources" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  fireEvent.click(screen.getByText("查看这一景点的原文"));
  expect(screen.queryByRole("combobox", { name: "选择来源" })).toBeNull();
  expect(screen.getByText("本记录没有单独保存来源原文。")).toBeTruthy();
  expect(document.querySelector("mark")).toBeNull();
});

it("renders the verified official link inside reservation rules with safe external attributes", async () => {
  const row = structured("official-navigation");
  const value = { ...row, presentation: { ...row.presentation, entries: [{ ...row.presentation.entries[0],
    official_link: { url: "https://museum.example/visitor", label: "测试博物馆官方网站", status: "verified_navigation" },
    practical: [{ topic: "reservation_rules", text: "需实名预约。" }] }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation="official-link" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  const link = screen.getByRole("link", { name: "测试博物馆官方网站" });
  expect(link.getAttribute("href")).toBe("https://museum.example/visitor");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toContain("noopener");
  expect(link.closest("p")?.textContent).toContain("预约规则：需实名预约。");
  expect(screen.getByText("资料已部分整理")).toBeTruthy();
});

it.each(["javascript:alert(1)", "http://museum.example", "https://museum.example/?token=secret"])("omits an invalid navigation record without a placeholder: %s", async url => {
  const row = structured("unsafe-navigation");
  const value = { ...row, presentation: { ...row.presentation, entries: [{ ...row.presentation.entries[0],
    official_link: { url, label: "不应显示的官网", status: "verified_navigation" } }] } };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [value] })));
  render(<ResearchPanel conversation={url} onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(screen.queryByRole("link", { name: "不应显示的官网" })).toBeNull();
  expect(screen.queryByText(/详细信息见景点官网/)).toBeNull();
  expect(screen.queryByText("预约规则：")).toBeNull();
});

it.each([["completed", "资料已整理"], ["failed", "资料整理失败"], ["cancelled", "资料整理已取消"], ["blocked", "资料整理受阻"]])("uses the correct terminal text for %s", async (stage, label) => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [structured(stage, "27元", stage)] })));
  render(<ResearchPanel conversation={"terminal-" + stage} onPlan={() => {}} />);
  expect(await screen.findByText(label)).toBeTruthy();
});

it("keeps one chapter per target and switches source mode without duplicating prose", async () => {
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => [structured()] }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ResearchPanel conversation="one-chapter" onPlan={() => {}} />);
  fireEvent.click(await screen.findByText("查看完整报告"));
  expect(screen.getAllByText("成人票27元（未持优待证件）", { exact: false })).toHaveLength(1);
  expect(screen.getAllByRole("heading", { name: "测试博物馆" })).toHaveLength(1);
  expect(screen.getByText(/日期暂定/)).toBeTruthy();
  expect(screen.getByText("介绍：").tagName).toBe("STRONG");
  expect(screen.getByText("票价：").tagName).toBe("STRONG");
  expect(screen.queryByText("不妨放慢脚步细看器物。")).toBeNull();
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
