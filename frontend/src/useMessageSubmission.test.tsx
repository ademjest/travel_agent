// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useMessageSubmission } from "./useMessageSubmission";
import type { ConversationState } from "./useConversation";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function conversation() {
  return {
    active: "first", activeRef: { current: "first" }, ready: true, draft: "第一会话的草稿", files: [],
    deletedIds: { current: new Set<string>() }, nearBottom: { current: true },
    setDraft: vi.fn(), setFiles: vi.fn(), setQuotedMessage: vi.fn(), setError: vi.fn(), report: vi.fn(), refresh: vi.fn(),
    ensureConversation: vi.fn(async () => "first"),
  };
}

it("does not restore an old draft when a submission fails after switching conversations", async () => {
  const state = conversation();
  let reject!: (reason: Error) => void;
  const pending = new Promise<Response>((_resolve, fail) => { reject = fail; });
  const fetch = vi.fn(() => pending);
  vi.stubGlobal("fetch", fetch);
  const { result } = renderHook(() => useMessageSubmission(state as unknown as ConversationState, { current: null }));
  let sending!: Promise<void>;
  act(() => { sending = result.current.send(); });
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  state.activeRef.current = "second";
  await act(async () => { reject(new Error("late network failure")); await sending; });
  expect(state.setDraft).not.toHaveBeenCalled();
  expect(state.report).not.toHaveBeenCalled();
});

it("does not attach files to a new conversation after an earlier conversation resolves late", async () => {
  const state = conversation();
  let resolve!: (identity: string) => void;
  const pending = new Promise<string>((done) => { resolve = done; });
  state.ensureConversation = vi.fn(() => pending);
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ id: "upload" }) }));
  vi.stubGlobal("fetch", fetch);
  const { result } = renderHook(() => useMessageSubmission(state as unknown as ConversationState, { current: null }));
  let uploading!: Promise<void>;
  act(() => { uploading = result.current.addFiles([new File(["text"], "case.txt")]); });
  state.activeRef.current = "second";
  await act(async () => { resolve("first"); await uploading; });
  expect(state.setDraft).not.toHaveBeenCalled();
  expect(state.setFiles).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

it("keeps a failed quote for retry and changes the request identity when the quote changes", async () => {
  const state = { ...conversation(), quotedMessage: { id: "d:1", label: "铁路查询 A" } };
  let fail = true;
  const fetch = vi.fn(async () => ({ ok: !fail, status: fail ? 503 : 202, json: async () => ({}) }));
  vi.stubGlobal("fetch", fetch);
  const { result, rerender } = renderHook(() => useMessageSubmission(state as unknown as ConversationState, { current: null }));
  await act(async () => { await result.current.send(); });
  expect(state.setQuotedMessage).not.toHaveBeenCalled();
  await act(async () => { await result.current.send(); });
  const bodies = () => fetch.mock.calls.map((call) => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)));
  expect(bodies()[0].quoted_message_id).toBe("d:1");
  expect(bodies()[1].client_request_id).toBe(bodies()[0].client_request_id);
  state.quotedMessage = { id: "d:2", label: "铁路查询 B" }; rerender(); fail = false;
  await act(async () => { await result.current.send(); });
  expect(bodies()[2].client_request_id).not.toBe(bodies()[0].client_request_id);
  expect(bodies()[2].quoted_message_id).toBe("d:2");
  expect(state.setQuotedMessage).toHaveBeenCalledWith(null);
});
