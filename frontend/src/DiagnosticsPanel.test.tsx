// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DiagnosticsPanel } from "./DiagnosticsPanel";

const sample = {
  job_id: 7, job_status: "completed", state: "处理已完成，有降级或异常记录", note: "请核对旧版回退。",
  model_calls: 2, report_text: "不含原文的诊断报告", replayed: false,
  timeline: [{ id: 1, stage: "summary", title: "会话摘要", created_at: "2026-10-16T08:00:00Z",
    description: "摘要更新未成功；已沿用有效旧版。", warning: true, details: { status: "validation_failed" } }],
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("loads the selected message, explains fallback, copies without POST or model calls", async () => {
  const fetch = vi.fn(async (_url: string, _options: RequestInit = {}) => ({ ok: true, json: async () => sample }));
  vi.stubGlobal("fetch", fetch);
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<DiagnosticsPanel conversationId="a" jobId={7} />);
  await screen.findByText(sample.state);
  expect(screen.getByText(sample.timeline[0].description)).toBeTruthy();
  fireEvent.click(screen.getByText("复制诊断报告"));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(sample.report_text));
  expect(fetch.mock.calls[0][0]).toBe("/api/conversations/a/jobs/7/diagnostics");
  expect(fetch.mock.calls).toHaveLength(1);
});

it("does not display a late response from a previously selected request", async () => {
  let resolve: (value: unknown) => void = () => {};
  vi.stubGlobal("fetch", vi.fn()
    .mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ...sample, state: "第二条请求" }) }));
  const view = render(<DiagnosticsPanel conversationId="a" jobId={7} />);
  view.rerender(<DiagnosticsPanel conversationId="b" jobId={8} />);
  await screen.findByText("第二条请求");
  await act(async () => resolve({ ok: true, json: async () => sample }));
  expect(screen.queryByText(sample.state)).toBeNull();
});

it("offers retry and manual copy if browser clipboard is unavailable", async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: "读取失败" }) })
    .mockResolvedValue({ ok: true, json: async () => sample });
  vi.stubGlobal("fetch", fetch);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  render(<DiagnosticsPanel conversationId="a" jobId={7} />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByText("刷新记录"));
  await screen.findByText(sample.state);
  fireEvent.click(screen.getByText("复制诊断报告"));
  await screen.findByText(/复制失败/);
  expect((screen.getByLabelText("诊断报告文本") as HTMLTextAreaElement).value).toBe(sample.report_text);
});
