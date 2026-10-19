// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ErrorBoundary, PageRecovery } from "./ErrorBoundary";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Broken(): never { throw new Error("fixture render failure"); }

it("contains card rendering errors and recovers for a new card version", () => {
  vi.spyOn(console,"error").mockImplementation(() => {});
  const view = render(<><nav>会话导航</nav><ErrorBoundary resetKey="v1" fallback={<p>重新生成预览</p>}><Broken /></ErrorBoundary></>);
  expect(screen.getByText("会话导航")).toBeTruthy();
  expect(screen.getByText("重新生成预览")).toBeTruthy();
  view.rerender(<><nav>会话导航</nav><ErrorBoundary resetKey="v2" fallback={<p>重新生成预览</p>}><p>新预览</p></ErrorBoundary></>);
  expect(screen.getByText("新预览")).toBeTruthy();
});

it("offers a home recovery action when the application crashes", () => {
  vi.spyOn(console,"error").mockImplementation(() => {});
  render(<ErrorBoundary fallback={<PageRecovery />}><Broken /></ErrorBoundary>);
  expect(screen.getByRole("button", {name:"返回首页"})).toBeTruthy();
  expect(screen.getByRole("alert")).toBeTruthy();
});
