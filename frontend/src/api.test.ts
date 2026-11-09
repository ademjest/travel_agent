// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("preserves request cancellation instead of reporting a network failure", async () => {
  const controller = new AbortController();
  controller.abort();
  const reason = new DOMException("Aborted", "AbortError");
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(reason));
  await expect(api("/conversations", { signal: controller.signal })).rejects.toBe(reason);
});

it("reports an actual network failure as an API connection error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Network failure")));
  await expect(api("/conversations")).rejects.toBeInstanceOf(ApiError);
});
