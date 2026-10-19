// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RuntimeStatusPanel, RetrievalStatusView } from "./RuntimeStatusPanel";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("handles an old backend without breaking settings", async () => {
  vi.stubGlobal("fetch",vi.fn(async () => ({ok:false,status:404,json:async () => ({detail:"Not Found"})})));
  render(<RuntimeStatusPanel />);
  expect(await screen.findByText(/后端版本暂未确认/)).toBeTruthy();
});
it("distinguishes configured, loaded, and partially indexed states", () => {
  render(<RetrievalStatusView status={{configuration:{state:"configured"},encoder:{state:"not_loaded"},
    index:{state:"partial",total:40,indexed:32,pending:8}}} />);
  expect(screen.getByText(/模型加载：尚未加载/)).toBeTruthy();
  expect(screen.getByText(/部分就绪.*32\/40/)).toBeTruthy();
});
