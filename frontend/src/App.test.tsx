// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { emptyContext } from "./api";
import type { Conversation, DeletionJob } from "./api";

let sent: { url: string; options: RequestInit }[];
let failSubmission = false;
let reply = "";
let conversationList: Conversation[] = [];
let deletionJob: DeletionJob | null = null;
let deletionFails = false;
let messagesGate: Promise<void> | null = null;
const sampleConversation = (
  id = "local-conversation",
  title = "新的旅行",
): Conversation => ({
  id,
  title,
  created_at: "2026-09-18T00:00:00Z",
  updated_at: "2026-09-18T00:00:00Z",
});
beforeEach(() => {
  localStorage.clear();
  sent = [];
  failSubmission = false;
  reply = "";
  conversationList = [];
  deletionJob = null;
  deletionFails = false;
  messagesGate = null;
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.open = true;
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.open = false;
    },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options: RequestInit = {}) => {
      sent.push({ url, options });
      let body: unknown = [];
      let status = 200;
      if (url.endsWith("/bootstrap"))
        body = {
          csrf_token: "csrf",
          version: "test",
          llm_configured: true,
          amap_configured: true,
        };
      else if (url.endsWith("/deletion-summary")) {
        const identity = url.split("/")[3];
        body = {
          id: identity,
          title:
            conversationList.find((c) => c.id === identity)?.title ||
            "测试会话",
          messages: 2,
          attachments: 1,
          trips: 1,
          active_tasks: 1,
          reminders: 1,
          scheduled_queries: 0,
          policy_watches: 0,
        };
      } else if (options.method === "DELETE") {
        const identity = url.split("/").at(-1)!;
        conversationList = conversationList.filter((c) => c.id !== identity);
        deletionJob = {
          conversation_id: identity,
          state: deletionFails ? "failed" : "completed",
          requested_at: "2026-09-18T00:00:00Z",
          updated_at: "2026-09-18T00:00:00Z",
          error: deletionFails ? "文件被占用" : "",
        };
        body = deletionJob;
      } else if (url.endsWith("/conversation-deletions"))
        body =
          deletionJob && deletionJob.state !== "completed" ? [deletionJob] : [];
      else if (url.includes("/conversation-deletions/")) {
        if (url.endsWith("/retry") && deletionJob)
          deletionJob = { ...deletionJob, state: "completed", error: "" };
        body = deletionJob || { detail: "not found" };
        status = deletionJob ? 200 : 404;
      } else if (url.endsWith("/conversations") && options.method === "POST") {
        body = sampleConversation();
        conversationList = [body as Conversation];
      } else if (url.endsWith("/conversations")) body = conversationList;
      else if (url.endsWith("/context")) body = emptyContext;
      else if (url.endsWith("/uploads"))
        body = {
          id: "upload-one",
          filename: "notes.md",
          content_type: "text/markdown",
          size: 4,
        };
      else if (url.endsWith("/messages") && options.method === "POST") {
        if (failSubmission) {
          status = 503;
          body = { detail: "队列暂满" };
        } else body = { job_id: 1, status: "accepted" };
      } else if (url.includes("/messages")) {
        if (messagesGate) await messagesGate;
        body = {
          items: reply
            ? [
                {
                  id: "d:1",
                  role: "assistant",
                  kind: "reply",
                  content: reply,
                  created_at: "2026-09-18T00:00:00Z",
                  uploads: [],
                  job_id: null,
                  job_status: null,
                },
              ]
            : [],
          has_more: false,
          before: null,
        };
      }
      return { ok: status === 200, status, json: async () => body };
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function ready() {
  render(<App />);
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: /计划一场旅行/,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
}

describe("local conversation interactions", () => {
  it("deletion requires confirmation and cancelling the dialog preserves data", async () => {
    conversationList = [sampleConversation("a", "测试 A")];
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "删除会话 测试 A" }));
    await screen.findByRole("button", { name: "删除并停止关联任务" });
    expect(sent.some((call) => call.options.method === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(
      screen.queryByText(
        "此操作不可恢复。其他会话和 QQ 数据不会被删除；其他会话仍在使用的共享文件会保留。",
      ),
    ).toBeNull();
    expect(sent.some((call) => call.options.method === "DELETE")).toBe(false);
    expect(
      screen.getByRole("button", { name: "删除会话 测试 A" }),
    ).toBeTruthy();
  });

  it("confirmed deletion clears its draft while preserving a different active conversation", async () => {
    conversationList = [
      sampleConversation("a", "测试 A"),
      sampleConversation("b", "测试 B"),
    ];
    localStorage.setItem("travel-active", "b");
    localStorage.setItem("travel-draft:a", "应被清除");
    localStorage.setItem("travel-draft:b", "保留 B 草稿");
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "删除会话 测试 A" }));
    const confirm = await screen.findByRole("button", {
      name: "删除并停止关联任务",
    });
    await waitFor(() =>
      expect((confirm as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(confirm);
    await screen.findByText("会话及关联数据已删除");
    expect(
      screen.queryByRole("button", { name: "删除会话 测试 A" }),
    ).toBeNull();
    expect(localStorage.getItem("travel-draft:a")).toBeNull();
    expect(
      (screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value,
    ).toBe("保留 B 草稿");
    const deleted = sent.find((call) => call.options.method === "DELETE")!;
    expect(new Headers(deleted.options.headers).get("X-CSRF-Token")).toBe(
      "csrf",
    );
  });

  it("file cleanup failure stays visible and supports retry", async () => {
    conversationList = [sampleConversation("a", "测试 A")];
    deletionFails = true;
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "删除会话 测试 A" }));
    const confirm = await screen.findByRole("button", {
      name: "删除并停止关联任务",
    });
    await waitFor(() =>
      expect((confirm as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(confirm);
    await screen.findByText("会话已停用，清理尚未完成");
    fireEvent.click(screen.getByRole("button", { name: "重试清理" }));
    await screen.findByText("会话及关联数据已删除");
    expect(
      sent.some(
        (call) => call.url.endsWith("/retry") && call.options.method === "POST",
      ),
    ).toBe(true);
  });

  it("a cross-tab deletion discards a late message response and does not restore the draft", async () => {
    conversationList = [sampleConversation("a", "测试 A")];
    localStorage.setItem("travel-active", "a");
    localStorage.setItem("travel-draft:a", "旧草稿");
    reply = "迟到数据不应显示";
    let release!: () => void;
    messagesGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await ready();
    await waitFor(() =>
      expect(
        sent.some((call) => call.url === "/api/conversations/a/messages"),
      ).toBe(true),
    );
    await act(async () => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "travel-deleted",
          newValue: JSON.stringify({ id: "a" }),
        }),
      );
      release();
    });
    expect(screen.queryByText("迟到数据不应显示")).toBeNull();
    expect(
      (screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value,
    ).toBe("");
    expect(localStorage.getItem("travel-draft:a")).toBeNull();
    expect(localStorage.getItem("travel-active")).toBeNull();
  });
  it("file selection uploads first and submits only a scoped attachment identity", async () => {
    await ready();
    fireEvent.change(screen.getByLabelText("选择旅行资料"), {
      target: {
        files: [new File(["note"], "notes.md", { type: "text/markdown" })],
      },
    });
    await screen.findByText("已上传 · 发送后处理");
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() =>
      expect(
        sent.some(
          (item) =>
            item.url.endsWith("/messages") && item.options.method === "POST",
        ),
      ).toBe(true),
    );
    const submitted = sent.find(
      (item) =>
        item.url.endsWith("/messages") && item.options.method === "POST",
    )!;
    expect(JSON.parse(String(submitted.options.body)).upload_ids).toEqual([
      "upload-one",
    ]);
    const uploaded = sent.find((item) => item.url.endsWith("/uploads"))!;
    expect((uploaded.options.body as FormData).get("conversation_id")).toBe(
      "local-conversation",
    );
  });

  it("does not render raw HTML or executable links from a model reply", async () => {
    await ready();
    reply =
      '[危险链接](javascript:alert(1))\n\n<script id="model-script">alert(1)</script>';
    fireEvent.click(screen.getByRole("button", { name: /开启新对话/ }));
    await screen.findByText("危险链接");
    expect(document.querySelector("#model-script")).toBeNull();
    expect(screen.getByText("危险链接").getAttribute("href")).not.toContain(
      "javascript:",
    );
  });
  it("examples fill an editable draft without submitting a model request", async () => {
    await ready();
    fireEvent.click(screen.getByRole("button", { name: /出发前查一查/ }));
    expect(
      (screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value,
    ).toContain("武汉");
    expect(sent.filter((item) => item.options.method === "POST")).toHaveLength(
      0,
    );
  });

  it("Chinese input composition does not send; ordinary Enter submits once with CSRF", async () => {
    await ready();
    const input = screen.getByLabelText("旅行问题");
    fireEvent.change(input, { target: { value: "查询天气 武汉" } });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229, isComposing: true });
    expect(
      sent.filter(
        (item) =>
          item.url.endsWith("/messages") && item.options.method === "POST",
      ),
    ).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
    await waitFor(() =>
      expect(
        sent.filter(
          (item) =>
            item.url.endsWith("/messages") && item.options.method === "POST",
        ),
      ).toHaveLength(1),
    );
    const message = sent.find(
      (item) =>
        item.url.endsWith("/messages") && item.options.method === "POST",
    )!;
    expect(new Headers(message.options.headers).get("X-CSRF-Token")).toBe(
      "csrf",
    );
    expect(JSON.parse(String(message.options.body)).content).toBe(
      "查询天气 武汉",
    );
  });

  it("failed submissions preserve the draft and retry with the same request identity", async () => {
    await ready();
    failSubmission = true;
    const input = screen.getByLabelText("旅行问题");
    fireEvent.change(input, { target: { value: "不要丢失这条消息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByRole("alert");
    expect((input as HTMLTextAreaElement).value).toBe("不要丢失这条消息");
    failSubmission = false;
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "发送消息" })),
    );
    const calls = sent.filter(
      (item) =>
        item.url.endsWith("/messages") && item.options.method === "POST",
    );
    expect(calls).toHaveLength(2);
    expect(JSON.parse(String(calls[0].options.body)).client_request_id).toBe(
      JSON.parse(String(calls[1].options.body)).client_request_id,
    );
  });
});
