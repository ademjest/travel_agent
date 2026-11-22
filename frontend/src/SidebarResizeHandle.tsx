import { useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";

export function SidebarResizeHandle({ side, viewport }: { side: "left" | "right"; viewport: number }) {
  const handle = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointer: number; x: number; width: number } | null>(null);
  const [value, setValue] = useState(0);
  const [resizing, setResizing] = useState(false);

  useLayoutEffect(() => {
    const panel = handle.current?.parentElement;
    const measure = () => setValue(Math.round(panel?.getBoundingClientRect().width || 0));
    measure();
    if (!panel || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(panel);
    return () => observer.disconnect();
  }, [viewport]);

  function resize(requested: number) {
    const root = handle.current?.closest<HTMLElement>(".app");
    if (!root) return;
    const inline = side === "left" ? viewport >= 768 : viewport >= 1280;
    const otherInline = side === "left" ? viewport >= 1280 && root.classList.contains("details-open") : viewport >= 768;
    const other = root.querySelector<HTMLElement>(side === "left" ? ".details-panel" : ".sidebar");
    const available = root.getBoundingClientRect().width || viewport;
    const max = inline ? available - (otherInline ? other?.getBoundingClientRect().width || 0 : 0) - 280 : available * 0.92;
    const min = Math.min(side === "left" ? 176 : 240, max);
    const next = Math.round(Math.max(min, Math.min(max, requested)));
    root.style.setProperty(side === "left" ? "--sidebar-width" : "--details-width", `${next}px`);
    setValue(next);
  }

  function begin(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointer: event.pointerId, x: event.clientX, width: event.currentTarget.parentElement!.getBoundingClientRect().width };
    setResizing(true);
  }

  function end(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current = null;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function keydown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = event.currentTarget.parentElement!.getBoundingClientRect().width;
    const delta = (event.key === "ArrowRight" ? 10 : -10) * (side === "left" ? 1 : -1);
    resize(event.key === "Home" ? 0 : event.key === "End" ? viewport : current + delta);
  }

  return <div ref={handle} className={`sidebar-resize sidebar-resize--${side}`} role="separator"
    tabIndex={0} aria-orientation="vertical" aria-valuemin={0} aria-valuemax={viewport}
    aria-valuenow={value} aria-valuetext={`${value} 像素`}
    aria-label={side === "left" ? "调整会话侧栏宽度" : "调整旅行侧栏宽度"}
    title="拖动调整宽度，也可使用左右方向键" data-resizing={resizing}
    onPointerDown={begin} onPointerMove={event => {
      const start = drag.current;
      if (start?.pointer === event.pointerId) resize(start.width + (event.clientX - start.x) * (side === "left" ? 1 : -1));
    }} onPointerUp={end} onPointerCancel={end}
    onLostPointerCapture={() => { drag.current = null; setResizing(false); }} onKeyDown={keydown} />;
}
