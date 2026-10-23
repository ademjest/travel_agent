// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsPanel } from "./SettingsPanel";

let sent: { url: string; body?: Record<string, unknown> }[];
let usageResponse: Record<string, unknown>;
let serviceTestResponse: Record<string, unknown>;
const service = { version: "v1", restart_required: false, fields: {
  SEARCH_API_KEY: { configured: false, source: "project", editable: true, value: "", pending_restart: false },
  SEARCH_BASE_URL: { configured: true, source: "default", editable: true, value: "https://api.tavily.com", pending_restart: false },
  LLM_API_KEY: { configured: true, source: "project", editable: true, value: "", pending_restart: false },
  LLM_BASE_URL: { configured: true, source: "project", editable: true, value: "https://model.example/v1", pending_restart: false },
  LLM_MODEL_ID: { configured: true, source: "project", editable: true, value: "old-model", pending_restart: false },
  AMAP_API_KEY: { configured: true, source: "project", editable: true, value: "", pending_restart: false },
  AI_BUDGET_TIER: { configured: true, source: "default", editable: true, value: "balanced", pending_restart: false },
  AI_SUMMARY_ENABLED: { configured: true, source: "default", editable: true, value: "false", pending_restart: false },
  AI_VECTOR_ENABLED: { configured: true, source: "default", editable: true, value: "false", pending_restart: false },
  LLM_INTENT_MODEL_ID: { configured: false, source: "default", editable: true, value: "", pending_restart: false },
  LLM_INPUT_PRICE_PER_M: { configured: false, source: "default", editable: true, value: "", pending_restart: false },
  LLM_PRICE_CURRENCY: { configured: true, source: "default", editable: true, value: "USD", pending_restart: false },
  LLM_MODEL_PRICES: { configured: false, source: "default", editable: true, value: "", pending_restart: false },
  AI_DAILY_COST_LIMIT: { configured: true, source: "default", editable: true, value: "0", pending_restart: false },
  AI_COST_CURRENCY: { configured: true, source: "default", editable: true, value: "USD", pending_restart: false },
} };
let serviceResponse: typeof service;
beforeEach(() => {
  sent = []; localStorage.clear(); sessionStorage.clear();
  serviceResponse = structuredClone(service);
  usageResponse = { totals: { calls: 0, prompt_tokens: 0, cached_input_tokens: 0, completion_tokens: 0 }, purposes: [], currency: "USD", cost_note: "unknown" };
  serviceTestResponse = { results: [
    { label: "默认模型", model_id: "test-model", service: "model", status: "ok", detail: "文本推理请求通过。", elapsed_ms: 120 },
    { label: "Tavily 搜索", model_id: "", service: "search", status: "failed", detail: "认证失败，请检查已保存的 Key。", elapsed_ms: 50 },
    { label: "高德地图 API", model_id: "", service: "amap", status: "skipped", detail: "未配置完整，已跳过。", elapsed_ms: 0 },
  ], summary: { ok: 1, failed: 1, skipped: 1 }, restart_required: true };
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: RequestInit = {}) => {
    sent.push({ url, body: options.body ? JSON.parse(String(options.body)) : undefined });
    const body = url.endsWith("/preferences") ? { version: 0, enabled: true, suggestions: true, values: {}, scope: "local" }
      : url.endsWith("/settings/model-usage") ? usageResponse
      : url.endsWith("/settings/services/test") ? serviceTestResponse
      : serviceResponse;
    return { ok: true, json: async () => body };
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("keeps the optional Wendao token write only and requires an explicit enable setting", async () => {
  Object.assign(serviceResponse.fields, {
    WENDAO_API_KEY: { configured: true, source: "project", editable: true, value: "", pending_restart: false },
    WENDAO_ENABLED: { configured: true, source: "default", editable: true, value: "false", pending_restart: false },
  });
  render(<SettingsPanel section="services" />);
  const token = await screen.findByLabelText(/携程问道 Token/);
  expect((token as HTMLInputElement).type).toBe("password");
  expect((token as HTMLInputElement).value).toBe("");
  fireEvent.change(token, { target: { value: "wendao-fixture-secret" } });
  fireEvent.change(screen.getByLabelText(/启用问道参考来源/), { target: { value: "true" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({ WENDAO_API_KEY: "wendao-fixture-secret", WENDAO_ENABLED: "true" }));
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});

it("only sends touched search key, never stores secret in browser persistence", async () => {
  render(<SettingsPanel />);
  const search = await screen.findByLabelText(/Tavily 搜索 Key/);
  expect(screen.getByText("联网搜索")).toBeTruthy();
  expect(screen.getByText(/tvly-xxxxxxxx/)).toBeTruthy();
  expect((screen.getByLabelText(/模型 Key/) as HTMLInputElement).value).toBe("");
  fireEvent.change(search, { target: { value: "new-search-fixture" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.some(s => s.body?.changes)).toBe(true));
  expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({ SEARCH_API_KEY: "new-search-fixture" });
  await waitFor(() => expect((search as HTMLInputElement).value).toBe(""));
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
});

it("explicit clear has distinct null semantics and can be undone", async () => {
  render(<SettingsPanel />);
  await screen.findByLabelText(/Tavily 搜索 Key/);
  fireEvent.click(screen.getAllByText("清除")[1]);
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({ LLM_API_KEY: null }));
});

it("clears unsaved credentials when the panel is closed", async () => {
  const view = render(<SettingsPanel />);
  fireEvent.change(await screen.findByLabelText(/Tavily 搜索 Key/), { target: { value: "temporary-fixture" } });
  view.unmount(); render(<SettingsPanel />);
  expect((await screen.findByLabelText(/Tavily 搜索 Key/) as HTMLInputElement).value).toBe("");
  expect(sent.every(s => !s.body)).toBe(true);
});

it("can replace all model and map fields supplied by the startup environment", async () => {
  for (const key of ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "AMAP_API_KEY"] as const)
    serviceResponse.fields[key].source = "environment";
  render(<SettingsPanel section="services" />);
  const modelKey = await screen.findByLabelText(/^模型 Key/);
  const modelUrl = screen.getByLabelText(/^模型地址/);
  const modelId = screen.getByLabelText(/^默认模型 ID/);
  const mapKey = screen.getByLabelText(/^高德 Key/);
  for (const control of [modelKey, modelUrl, modelId, mapKey])
    expect((control as HTMLInputElement).disabled).toBe(false);
  expect(screen.getAllByText(/保存替换值后/).length).toBe(4);
  fireEvent.change(modelKey, { target: { value: "replacement-model-fixture" } });
  fireEvent.change(modelUrl, { target: { value: "https://new.example/v1" } });
  fireEvent.change(modelId, { target: { value: "replacement-model" } });
  fireEvent.change(mapKey, { target: { value: "replacement-map-fixture" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({
    LLM_API_KEY: "replacement-model-fixture", LLM_BASE_URL: "https://new.example/v1",
    LLM_MODEL_ID: "replacement-model", AMAP_API_KEY: "replacement-map-fixture",
  }));
  await waitFor(() => expect((modelKey as HTMLInputElement).value).toBe(""));
  expect((mapKey as HTMLInputElement).value).toBe("");
  expect((screen.getByLabelText(/^搜索地址/) as HTMLInputElement).disabled).toBe(true);
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});

it("explains why changing a configured model endpoint requires a replacement key", async () => {
  render(<SettingsPanel section="services" />);
  fireEvent.change(await screen.findByLabelText(/^模型地址/), { target: { value: "https://new.example/v1" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  expect(await screen.findByText(/请同时输入用于新地址的模型 Key/)).toBeTruthy();
  expect(sent.filter(s => s.body?.changes)).toHaveLength(0);
});

it("preserves blank-as-unchanged when replacing only the map key", async () => {
  render(<SettingsPanel section="services" />);
  fireEvent.change(await screen.findByLabelText(/^模型地址/), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText(/^高德 Key/), { target: { value: "replacement-map-fixture" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({
    LLM_BASE_URL: "", AMAP_API_KEY: "replacement-map-fixture",
  }));
});

it("saves evaluated budget and optional memory switches", async () => {
  render(<SettingsPanel />);
  fireEvent.change(await screen.findByLabelText(/AI 预算档位/), { target: { value: "economy" } });
  fireEvent.change(screen.getByLabelText(/按需会话摘要/), { target: { value: "true" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({
    AI_BUDGET_TIER: "economy", AI_SUMMARY_ENABLED: "true",
  }));
});

it("uses field-specific placeholders and keeps currency as text", async () => {
  render(<SettingsPanel section="services" />);
  const intent = await screen.findByLabelText(/意图解析模型/);
  const price = screen.getByLabelText(/输入费率\/百万 token/);
  const currency = screen.getByLabelText(/费率币种/);
  expect(intent.getAttribute("placeholder")).toContain("留空沿用默认模型");
  expect(price.getAttribute("type")).toBe("number");
  expect(price.getAttribute("step")).toBe("0.001");
  expect(currency.getAttribute("type")).toBe("text");
  expect(currency.getAttribute("placeholder")).toContain("USD");
  fireEvent.change(currency, { target: { value: "usd" } });
  expect((currency as HTMLInputElement).value).toBe("USD");
});

it("shows recalculated cost subtotal instead of replacing it with partial unknown", async () => {
  usageResponse = {
    totals: { calls: 3, prompt_tokens: 1000, cached_input_tokens: 100, completion_tokens: 200,
      estimated_cost: 0.012345, cost_unknown: 1, cost_recalculated: 1, cache_detail_missing: 1, currency: "USD" },
    purposes: [{ purpose: "trip_plan", model_id: "gpt-test", calls: 3, prompt_tokens: 1000,
      cached_input_tokens: 100, completion_tokens: 200, estimated_cost: 0.012345, cost_unknown: 1,
      cost_recalculated: 1, cache_detail_missing: 1, currency: "USD" }],
    currency: "USD", cost_note: "历史调用已回算",
  };
  render(<SettingsPanel section="services" />);
  expect((await screen.findAllByText(/0\.012345 USD/)).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/含 1 次按当前费率回算/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/1 次无法估算/).length).toBeGreaterThan(0);
  expect(screen.queryByText("部分未知")).toBeNull();
});

it("keeps per-model zero prices and optional money currency as text", async () => {
  render(<SettingsPanel section="services" />);
  const prices = await screen.findByLabelText(/用途模型费率映射/);
  const currency = screen.getByLabelText(/金额限额币种/);
  const limit = screen.getByLabelText(/每日金额上限/);
  expect(currency.getAttribute("type")).toBe("text");
  expect(limit.getAttribute("type")).toBe("number");
  const raw = '[{"provider":"main","model":"small","input":1,"cached":0,"output":2,"currency":"CNY"}]';
  fireEvent.change(prices, { target: { value: raw } });
  fireEvent.change(currency, { target: { value: "cny" } });
  fireEvent.change(limit, { target: { value: "1.25" } });
  fireEvent.click(screen.getByText("保存服务配置"));
  await waitFor(() => expect(sent.find(s => s.body?.changes)?.body?.changes).toEqual({
    LLM_MODEL_PRICES: raw, AI_COST_CURRENCY: "CNY", AI_DAILY_COST_LIMIT: "1.25",
  }));
});

it("does not describe missing cache usage as a known miss", async () => {
  usageResponse = { totals: { calls: 1, prompt_tokens: 100, cached_input_tokens: 0, completion_tokens: 10,
    estimated_cost: 0, cost_unknown: 1, cache_detail_missing: 1, unknown_usage: 1 }, purposes: [], currency: "USD", cost_note: "unknown" };
  render(<SettingsPanel section="services" />);
  expect(await screen.findByText(/缓存拆分未知/)).toBeTruthy();
  expect(screen.getByText(/合计仅包含已知值/)).toBeTruthy();
});

it("tests all saved services and shows a closable report with success, failure and skip rows", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  fireEvent.click(screen.getByText("一键测试全部服务"));
  expect(await screen.findByText(/通过 1 项 · 失败 1 项 · 跳过 1 项/)).toBeTruthy();
  expect(screen.getByRole("region", { name: "服务测试结果" }).className).toBe("service-test-notice");
  expect(screen.getByText("文本推理请求通过。")).toBeTruthy();
  expect(screen.getByText(/认证失败，请检查已保存的 Key/)).toBeTruthy();
  expect(screen.getByText("未配置完整，已跳过。")).toBeTruthy();
  expect(screen.getByText(/本次测试的是已保存的新配置/)).toBeTruthy();
  expect(sent.find(row => row.url.endsWith("/settings/services/test"))?.body).toEqual({ service: "all" });
  fireEvent.click(screen.getByRole("button", { name: "关闭服务测试结果" }));
  expect(screen.queryByRole("region", { name: "服务测试结果" })).toBeNull();
});

it("automatically closes the completed report after 20 seconds", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByText("一键测试全部服务")); });
  expect(screen.getByRole("region", { name: "服务测试结果" })).toBeTruthy();
  act(() => { vi.advanceTimersByTime(19_999); });
  expect(screen.getByRole("region", { name: "服务测试结果" })).toBeTruthy();
  act(() => { vi.advanceTimersByTime(1); });
  expect(screen.queryByRole("region", { name: "服务测试结果" })).toBeNull();
});

it("pauses automatic dismissal while the pointer is over the report", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByText("一键测试全部服务")); });
  fireEvent.mouseEnter(screen.getByRole("region", { name: "服务测试结果" }));
  act(() => { vi.advanceTimersByTime(30_000); });
  expect(screen.getByRole("region", { name: "服务测试结果" })).toBeTruthy();
  fireEvent.mouseLeave(screen.getByRole("region", { name: "服务测试结果" }));
  act(() => { vi.advanceTimersByTime(20_000); });
  expect(screen.queryByRole("region", { name: "服务测试结果" })).toBeNull();
});

it("requires saving edited fields before starting service tests", async () => {
  render(<SettingsPanel section="services" />);
  fireEvent.change(await screen.findByLabelText(/^模型 Key/), { target: { value: "unsaved-model-fixture" } });
  expect((screen.getByText("一键测试全部服务") as HTMLButtonElement).disabled).toBe(true);
  expect(sent.some(row => row.url.endsWith("/settings/services/test"))).toBe(false);
});

it("keeps a keyboard-focused report open after the pointer leaves", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  vi.useFakeTimers();
  await act(async () => { fireEvent.click(screen.getByText("一键测试全部服务")); });
  const region = screen.getByRole("region", { name: "服务测试结果" });
  fireEvent.mouseEnter(region);
  fireEvent.focus(screen.getByRole("button", { name: "关闭服务测试结果" }));
  fireEvent.mouseLeave(region);
  act(() => { vi.advanceTimersByTime(30_000); });
  expect(screen.getByRole("region", { name: "服务测试结果" })).toBeTruthy();
});

it("keeps the report hidden if closed while a service test is pending", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  let finish!: (value: Response) => void;
  vi.mocked(fetch).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  fireEvent.click(screen.getByText("一键测试全部服务"));
  expect(screen.getByText(/正在测试已保存的模型/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭服务测试结果" }));
  await act(async () => { finish({ ok: true, json: async () => serviceTestResponse } as Response); });
  expect(screen.queryByRole("region", { name: "服务测试结果" })).toBeNull();
});

it("shows a request failure in the report and allows closing it", async () => {
  render(<SettingsPanel section="services" />);
  await screen.findByLabelText(/^模型 Key/);
  vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 409,
    json: async () => ({ detail: "服务测试正在运行，请等待完成后再试。" }) } as Response);
  fireEvent.click(screen.getByText("一键测试全部服务"));
  expect(await screen.findByText("服务测试正在运行，请等待完成后再试。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "关闭服务测试结果" }));
  expect(screen.queryByRole("region", { name: "服务测试结果" })).toBeNull();
});
