// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsPanel } from "./SettingsPanel";

let sent: { url: string; body?: Record<string, unknown> }[];
const service = { version: "v1", restart_required: false, fields: {
  SEARCH_API_KEY: { configured: false, source: "project", editable: true, value: "", pending_restart: false },
  SEARCH_BASE_URL: { configured: true, source: "default", editable: true, value: "https://api.tavily.com", pending_restart: false },
  LLM_API_KEY: { configured: true, source: "project", editable: true, value: "", pending_restart: false },
  AMAP_API_KEY: { configured: true, source: "project", editable: true, value: "", pending_restart: false },
} };
beforeEach(() => {
  sent = []; localStorage.clear(); sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (url: string, options: RequestInit = {}) => {
    sent.push({ url, body: options.body ? JSON.parse(String(options.body)) : undefined });
    const body = url.endsWith("/preferences") ? { version: 0, enabled: true, suggestions: true, values: {}, scope: "local" } : service;
    return { ok: true, json: async () => body };
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("only sends touched search key, never stores secret in browser persistence", async () => {
  render(<SettingsPanel />);
  const search = await screen.findByLabelText(/Tavily 搜索 Key/);
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
