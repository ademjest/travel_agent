// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ConversationUsage } from "./ConversationUsage";
import type { ConversationUsageReport } from "./api";

afterEach(cleanup);

function usageFixture(identity = "a"): ConversationUsageReport {
  const totals = { calls: 1, prompt_tokens: 100, cached_input_tokens: 40, completion_tokens: 20,
    reasoning_tokens: 5, total_tokens: 120, running_calls: 0, estimated_cost: .000328,
    cost_unknown: 0, cost_recalculated: 0, cache_detail_missing: 0, unknown_usage: 0,
    cost_breakdown: [{ currency: "USD", amount: .000328 }] };
  return { conversation_id: identity, totals, currency: "USD", cost_note: "费用是本地估算。",
    scope_note: "仅统计当前会话。", purposes: [{ ...totals, purpose: "intent_parse", model_id: "test-model" }] };
}

it("shows input plus output without double counting cached or reasoning tokens", () => {
  render(<ConversationUsage conversation="a" usage={usageFixture()} />);
  expect(screen.getByText("120 token · 1 次模型调用")).toBeTruthy();
  expect(screen.getByText("缓存输入（包含在输入中）")).toBeTruthy();
  expect(screen.getByText("推理 token（包含在输出中）")).toBeTruthy();
  expect(screen.getAllByText("0.000328 USD").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByText("本会话用量"));
  expect(screen.getByRole("table", { name: "本会话调用明细" })).toBeTruthy();
  expect(screen.getByText("test-model")).toBeTruthy();
});

it("does not display another conversation's usage while the next conversation loads", () => {
  render(<ConversationUsage conversation="b" usage={usageFixture("a")} />);
  expect(screen.getByText("正在读取本会话用量…")).toBeTruthy();
  expect(screen.queryByText("120 token · 1 次模型调用")).toBeNull();
});

it("keeps known fees visible when some calls are pending or unknown", () => {
  const usage = usageFixture();
  usage.totals = { ...usage.totals, calls: 2, running_calls: 1, cost_unknown: 1, unknown_usage: 1,
    cache_detail_missing: 1, cost_recalculated: 1 };
  render(<ConversationUsage conversation="a" usage={usage} />);
  expect(screen.getByText("0.000328 USD；1 次费用待核验")).toBeTruthy();
  expect(screen.getByText("1 次调用进行中")).toBeTruthy();
  expect(screen.getByText(/1 次调用用量尚未返回或缺失/)).toBeTruthy();
});

it("refreshes totals and keeps currencies separate", () => {
  const usage = usageFixture();
  const view = render(<ConversationUsage conversation="a" usage={usage} />);
  const updated = { ...usage, totals: { ...usage.totals, calls: 2, total_tokens: 240,
    cost_breakdown: [{ currency: "USD", amount: .000328 }, { currency: "CNY", amount: .002 }] } };
  view.rerender(<ConversationUsage conversation="a" usage={updated} />);
  expect(screen.getByText("240 token · 2 次模型调用")).toBeTruthy();
  expect(screen.getByText("0.000328 USD + 0.002000 CNY")).toBeTruthy();
});

it("does not show conversation statistics on the uncreated welcome screen", () => {
  const { container } = render(<ConversationUsage conversation="" />);
  expect(container.textContent).toBe("");
});
