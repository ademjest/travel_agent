// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SidebarResizeHandle } from "./SidebarResizeHandle";

beforeEach(() => {
  class TestPointerEvent extends MouseEvent {
    pointerId: number;
    constructor(type: string, options: PointerEventInit) { super(type, options); this.pointerId = options.pointerId || 1; }
  }
  vi.stubGlobal("PointerEvent", TestPointerEvent);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const root = this.closest(".app") as HTMLElement;
    const width = this.classList.contains("app") ? Number(root.dataset.viewport)
      : this.classList.contains("sidebar") ? Number.parseFloat(root.style.getPropertyValue("--sidebar-width")) || 240
      : Number.parseFloat(root.style.getPropertyValue("--details-width")) || 320;
    return { width, height: 900, x: 0, y: 0, top: 0, bottom: 900, left: 0, right: width, toJSON: () => ({}) };
  });
  HTMLElement.prototype.setPointerCapture = vi.fn();
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true);
  HTMLElement.prototype.releasePointerCapture = vi.fn();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function layout(viewport = 1440) {
  const { container } = render(<div className="app details-open" data-viewport={viewport}>
    <aside className="sidebar"><SidebarResizeHandle side="left" viewport={viewport} /></aside>
    <main />
    <aside className="details-panel"><SidebarResizeHandle side="right" viewport={viewport} /></aside>
  </div>);
  return container.firstElementChild as HTMLElement;
}

it("drags the left edge right and the right edge left to widen their own panels", () => {
  const root = layout();
  const left = screen.getByRole("separator", { name: "调整会话侧栏宽度" });
  fireEvent.pointerDown(left, { pointerId: 1, clientX: 240, button: 0 });
  fireEvent.pointerMove(left, { pointerId: 1, clientX: 440 });
  fireEvent.pointerUp(left, { pointerId: 1 });
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("440px");
  expect(left.dataset.resizing).toBe("false");
  const right = screen.getByRole("separator", { name: "调整旅行侧栏宽度" });
  fireEvent.pointerDown(right, { pointerId: 2, clientX: 1120, button: 0 });
  fireEvent.pointerMove(right, { pointerId: 2, clientX: 1000 });
  fireEvent.pointerUp(right, { pointerId: 2 });
  expect(root.style.getPropertyValue("--details-width")).toBe("440px");
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("440px");
});

it("keeps room for the chat and supports keyboard resizing in either direction", () => {
  const root = layout();
  const left = screen.getByRole("separator", { name: "调整会话侧栏宽度" });
  fireEvent.keyDown(left, { key: "End" });
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("840px");
  fireEvent.keyDown(left, { key: "Home" });
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("176px");
  const right = screen.getByRole("separator", { name: "调整旅行侧栏宽度" });
  fireEvent.keyDown(right, { key: "ArrowLeft" });
  expect(root.style.getPropertyValue("--details-width")).toBe("330px");
  fireEvent.keyDown(right, { key: "ArrowRight" });
  expect(root.style.getPropertyValue("--details-width")).toBe("320px");
});

it("stops resizing on pointer cancellation and ignores a different pointer", () => {
  const root = layout();
  const left = screen.getByRole("separator", { name: "调整会话侧栏宽度" });
  fireEvent.pointerDown(left, { pointerId: 1, clientX: 240, button: 0 });
  fireEvent.pointerMove(left, { pointerId: 2, clientX: 500 });
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("");
  fireEvent.pointerCancel(left, { pointerId: 1 });
  fireEvent.pointerMove(left, { pointerId: 1, clientX: 500 });
  expect(root.style.getPropertyValue("--sidebar-width")).toBe("");
  expect(left.dataset.resizing).toBe("false");
});

it("keeps a small-screen drawer within the viewport", () => {
  const root = layout(390);
  const right = screen.getByRole("separator", { name: "调整旅行侧栏宽度" });
  fireEvent.keyDown(right, { key: "End" });
  expect(root.style.getPropertyValue("--details-width")).toBe("359px");
  fireEvent.keyDown(right, { key: "Home" });
  expect(root.style.getPropertyValue("--details-width")).toBe("240px");
});
