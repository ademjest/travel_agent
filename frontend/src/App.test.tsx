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
import type { Conversation, ConversationUsageReport, DeletionJob, Notice } from "./api";

let sent: { url: string; options: RequestInit }[];
let failSubmission = false;
let reply = "";
let userRequest = false;
let conversationList: Conversation[] = [];
let deletionJob: DeletionJob | null = null;
let deletionFails = false;
let messagesGate: Promise<void> | null = null;
let contextResponse: unknown;
let usageByConversation: Record<string, ConversationUsageReport>;
let contextGates: Record<string, Promise<void> | undefined>;
let notificationsResponse: Notice[] = [];
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
  userRequest = false;
  conversationList = [];
  deletionJob = null;
  deletionFails = false;
  messagesGate = null;
  contextResponse = emptyContext;
  usageByConversation = {};
  contextGates = {};
  notificationsResponse = [];
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
          planning_artifacts: 1,
          tool_artifacts: 1,
          conversation_summaries: 1,
          active_tasks: 1,
          reminders: 1,
          scheduled_queries: 0,
          policy_watches: 0,
        };
      } else if (options.method === "DELETE" && url.includes("/plans/")) {
        body = { status: "deleted" };
        const value = contextResponse as typeof emptyContext;
        contextResponse = { ...value, planning_artifacts: [] };
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
      else if (url.endsWith("/notifications")) body = notificationsResponse;
      else if (url.endsWith("/context")) {
        const identity = url.split("/")[3];
        body = usageByConversation[identity]
          ? { ...(contextResponse as object), model_usage: usageByConversation[identity] } : contextResponse;
        if (contextGates[identity]) await contextGates[identity];
      }
      else if (url.endsWith("/diagnostics")) body = {
        job_id: 7, job_status: "completed", state: "处理已完成", note: "诊断测试", model_calls: 0,
        timeline: [], report_text: "诊断测试", replayed: false,
      };
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
        if (userRequest) (body as { items: unknown[] }).items.unshift({
          id: "u:request", role: "user", kind: "message", content: "ping", uploads: [],
          created_at: "2026-10-16T00:00:00Z", job_id: 7, job_status: "completed",
        });
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
  it("isolates conversation usage and ignores a late response from the previous conversation", async () => {
    const report = (identity: string, total: number): ConversationUsageReport => ({
      conversation_id: identity, currency: "USD", cost_note: "本地估算", scope_note: "本会话", purposes: [],
      totals: { calls: 1, prompt_tokens: total - 20, cached_input_tokens: 0, completion_tokens: 20,
        reasoning_tokens: 0, total_tokens: total, running_calls: 0, estimated_cost: 0, cost_unknown: 1,
        cost_recalculated: 0, cache_detail_missing: 0, unknown_usage: 0, cost_breakdown: [] },
    });
    conversationList = [sampleConversation("a", "窗口 A"), sampleConversation("b", "窗口 B")];
    usageByConversation = { a: report("a", 120), b: report("b", 500) };
    localStorage.setItem("travel-active", "a");
    render(<App />);
    await screen.findByText("120 token · 1 次模型调用");
    let release!: () => void;
    contextGates.a = new Promise(resolve => { release = resolve; });
    usageByConversation.a = report("a", 999);
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(sent.filter(call => call.url === "/api/conversations/a/context").length).toBeGreaterThan(1));
    fireEvent.click(screen.getByText("窗口 B"));
    await screen.findByText("500 token · 1 次模型调用");
    await act(async () => { release(); });
    expect(screen.queryByText("120 token · 1 次模型调用")).toBeNull();
    expect(screen.queryByText("999 token · 1 次模型调用")).toBeNull();
    expect(screen.getByText("500 token · 1 次模型调用")).toBeTruthy();
  });

  it("opens diagnostics from a user message without asking for an event key", async () => {
    conversationList = [sampleConversation("a", "测试会话")];
    localStorage.setItem("travel-active", "a");
    userRequest = true;
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "查看处理过程" }));
    await screen.findByText("诊断测试", { selector: "p" });
    expect(sent.some((item) => item.url === "/api/conversations/a/jobs/7/diagnostics")).toBe(true);
    expect(screen.getByRole("heading", { name: "消息处理过程" })).toBeTruthy();
  });
  it("exposes local status, service configuration and preferences as separate sidebar modules", async () => {
    await ready();
    expect(screen.getByRole("button", { name: /本地空间状态/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /服务配置/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /偏好设置/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /状态与设置/ })).toBeNull();
  });

  it("shows an in-page toast when a new journey notification arrives", async () => {
    conversationList = [sampleConversation("a", "西安行程")];
    localStorage.setItem("travel-active", "a");
    render(<App />);
    await waitFor(() => expect(sent.some(call => call.url.endsWith("/notifications"))).toBe(true));
    notificationsResponse = [{ id: 9, content: "提醒你：预约陕西历史博物馆", created_at: "2026-09-25T02:00:00Z",
      read_at: null, conversation_id: "a", title: "西安行程" }];
    window.dispatchEvent(new Event("focus"));
    expect(await screen.findByText("提醒你：预约陕西历史博物馆")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /西安行程.*提醒你：预约陕西历史博物馆/ }));
    expect(await screen.findByRole("heading", { name: "旅途通知" })).toBeTruthy();
  });
  it("shows planning drafts separately and fills an adoption request", async () => {
    conversationList = [sampleConversation("a", "开封攻略")];
    localStorage.setItem("travel-active", "a");
    contextResponse = { ...emptyContext, planning_artifacts: [{
      artifact_id:"P-one", ordinal:1, kind:"exploratory", title:"开封参考方案（2天／3天）",
      destination:"开封", duration_options:[2,3], summary:"包含两套开封路线。", status:"active",
      version:1, created_at:"2026-09-22T00:00:00Z"
    }] };
    render(<App />);
    await screen.findByRole("tab", {name:"方案"});
    fireEvent.click(screen.getByRole("tab", {name:"方案"}));
    await screen.findByText("开封参考方案（2天／3天）");
    fireEvent.click(screen.getByRole("button", {name:"采用 3 天方案"}));
    expect((screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value).toBe("采用方案 1 的 3 天安排");
  });

  it.each([null, {}, ""])("restores a conversation with invalid preview %j without blanking the page", async (preview) => {
    conversationList = [sampleConversation("a", "测试 A")];
    localStorage.setItem("travel-active", "a");
    reply = "修改预览已生成";
    contextResponse = { ...emptyContext, confirmations: [{
      id: "draft-a", version: 2, preview, command: "确认行程修改", expires_at: "2030-10-01T00:00:00Z"
    }] };
    render(<App />);
    await screen.findByText(/完整预览暂时不可用/);
    expect(screen.getByRole("button", { name: "确认这次变更" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: /开启新对话/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新生成预览" }));
    expect((screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value).toBe("重新生成行程预览");
    expect(sent.some(call => call.url.includes("/confirmations/"))).toBe(false);
  });

  it("renders a delivered confirmation and submits its exact version", async () => {
    conversationList = [sampleConversation("a", "测试 A")];
    localStorage.setItem("travel-active", "a");
    reply = "请查看确认卡片";
    contextResponse = { ...emptyContext, confirmations: [{
      id: "draft-a", version: 4, preview: "完整的行程和提醒预览", can_confirm: true,
      command: "确认行程修改", expires_at: "2030-10-01T00:00:00Z"
    }] };
    render(<App />);
    await screen.findByText("完整的行程和提醒预览");
    fireEvent.click(screen.getByRole("button", { name: "确认这次变更" }));
    await waitFor(() => expect(sent.some(call => call.url.endsWith("/confirmations/draft-a"))).toBe(true));
    const call = sent.find(call => call.url.endsWith("/confirmations/draft-a"))!;
    expect(JSON.parse(String(call.options.body)).version).toBe(4);
  });
  it("rereads persistent activity introductions and tips in the trip details", async () => {
    conversationList = [sampleConversation("a", "成都参考")];
    localStorage.setItem("travel-active", "a");
    contextResponse = { ...emptyContext, trips: [{ trip_id: "TR-a", version: 1,
      spec: { destination: "成都", day_count: 1 }, sources: { B1: { name: "文殊坊", address: "测试街" } },
      plan: { days: [{ day_index: 1, date: "2026-10-22", activities: [{ poi_id: "B1", period: "下午",
        description: { intro: { text: "川西风格的院落间分布着茶馆和非遗工坊。", source_ids: [1] },
          why_visit: "留出自由游览时间。", tips: [{ text: "可以挑一两条街巷慢慢逛。", source_ids: [1], kind: "advice" }] } }] }] } }] };
    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "行程" }));
    fireEvent.click(await screen.findByText("介绍与玩法"));
    expect(screen.getByText(/川西风格的院落/)).toBeTruthy();
    expect(screen.getByText(/可以挑一两条街巷/)).toBeTruthy();
    expect(screen.queryByText(/小时级时间、预约场次和交通加总尚未校验/)).toBeNull();
    expect(screen.queryByText(/携程问道参考 · 来源/)).toBeNull();
    expect(sent.some(s => s.options.method === "POST")).toBe(false);
  });
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
