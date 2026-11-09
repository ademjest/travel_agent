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
let failDraftAction=false;
let reply = "";
let hotelPresentation: string | undefined;
let userRequest = false;
let referenceReply = false;
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
  failDraftAction=false;
  reply = "";
  hotelPresentation=undefined;
  userRequest = false;
  referenceReply = false;
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
      else if (url.includes("/drafts/") && url.endsWith("/actions")) {
        body = failDraftAction ? {detail:"草案动作暂时不可用"} : { job_id: 1, status: "accepted" }; status=failDraftAction ? 503 : 202;
      }
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
                  hotel_presentation: hotelPresentation ? {text:hotelPresentation} : undefined,
                  created_at: "2026-09-18T00:00:00Z",
                  uploads: [],
                  job_id: null,
                  job_status: null,
                  ...(referenceReply ? { reference: { kind: "rail", label: "武汉站 → 南京南 · 2026-10-30 · 上午" } } : {}),
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
  it("renders and copies the locally rechecked hotel summary instead of old raw prose",async()=>{
    reply="旧回复中冗长的供应商原文";hotelPresentation="本地重新整理：高级大床房约210元，条件待核对";
    conversationList=[sampleConversation("hotel","酒店报价")];localStorage.setItem("travel-active","hotel");
    const writeText=vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText}});
    render(<App/>);await screen.findByText(hotelPresentation);
    expect(screen.queryByText(reply)).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"复制回复"}));
    await waitFor(()=>expect(writeText).toHaveBeenCalledWith(hotelPresentation));
  });

  it("quotes ordinary user and assistant messages through the same input contract",async()=>{
    reply="普通助手历史内容";userRequest=true;conversationList=[sampleConversation("a","普通聊天")];
    localStorage.setItem("travel-active","a");render(<App/>);
    await screen.findByText("普通助手历史内容");
    const quotes=screen.getAllByRole("button",{name:"引用"});expect(quotes).toHaveLength(2);
    fireEvent.click(quotes[0]);expect(screen.getByText("引用消息")).toBeTruthy();
    const input=screen.getByLabelText("旅行问题");fireEvent.change(input,{target:{value:"解释这条问题"}});
    fireEvent.click(screen.getByRole("button",{name:"发送消息"}));
    await waitFor(()=>expect(sent.some(c=>c.options.method==="POST" && c.url.endsWith("/messages"))).toBe(true));
    const request=sent.find(c=>c.options.method==="POST" && c.url.endsWith("/messages"))!;
    expect(JSON.parse(String(request.options.body)).quoted_message_id).toBe("u:request");
  });
  it("submits an expired draft action bound to its identity and version",async()=>{
    reply="已生成的完整草案";conversationList=[sampleConversation("a","过期草案")];localStorage.setItem("travel-active","a");
    contextResponse={...emptyContext,confirmations:[{id:"expired",version:1,preview:reply,command:"确认行程修改",
      expires_at:"2026-01-01T00:00:00Z",state:"expired",can_confirm:false,unavailable_reason:"确认已过期，草案仍保留。",reference_message_id:"d:1"}]};
    render(<App/>);await screen.findByText("确认已过期，草案仍保留");
    expect(screen.getByRole("button",{name:"确认已过期"}).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button",{name:"重新核对并生成预览"}));
    await waitFor(()=>expect(sent.some(c=>c.url.endsWith("/drafts/expired/actions"))).toBe(true));
    const request=sent.find(c=>c.url.endsWith("/drafts/expired/actions"))!;
    expect(JSON.parse(String(request.options.body))).toMatchObject({action:"revalidate",version:1});
    expect(JSON.parse(String(request.options.body)).client_request_id).toBeTruthy();
    expect((screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value).toBe("");
    expect(sent.some(c=>c.options.method==="POST" && c.url.endsWith("/messages"))).toBe(false);
  });
  it("retries a bound draft action with one request id and preserves input and quote",async()=>{
    reply="完整草案";conversationList=[sampleConversation("a","草案重试")];localStorage.setItem("travel-active","a");
    contextResponse={...emptyContext,confirmations:[{id:"retry-draft",version:3,preview:reply,command:"确认行程修改",expires_at:"2030-10-01T00:00:00Z",reference_message_id:"d:1"}]};
    render(<App/>);fireEvent.click(await screen.findByRole("button",{name:"引用草案继续修改"}));
    fireEvent.change(screen.getByLabelText("旅行问题"),{target:{value:"尚未发送的要求"}});
    failDraftAction=true;fireEvent.click(screen.getByRole("button",{name:"放弃这份草案"}));
    await screen.findByText("草案动作暂时不可用");
    await waitFor(()=>expect((screen.getByRole("button",{name:"放弃这份草案"}) as HTMLButtonElement).disabled).toBe(false));
    failDraftAction=false;fireEvent.click(screen.getByRole("button",{name:"放弃这份草案"}));
    await waitFor(()=>expect(sent.filter(c=>c.url.endsWith("/drafts/retry-draft/actions"))).toHaveLength(2));
    const bodies=sent.filter(c=>c.url.endsWith("/drafts/retry-draft/actions")).map(c=>JSON.parse(String(c.options.body)));
    expect(bodies[0]).toEqual(bodies[1]);expect(bodies[0]).toMatchObject({action:"discard",version:3});
    expect((screen.getByLabelText("旅行问题") as HTMLTextAreaElement).value).toBe("尚未发送的要求");
    expect(screen.getByText("引用消息")).toBeTruthy();
    expect(sent.some(c=>c.options.method==="POST" && c.url.endsWith("/messages"))).toBe(false);
  });
  it("allows targeted discard during revalidation while disabling save and duplicate review",async()=>{
    reply="正在核对的草案";conversationList=[sampleConversation("a","核对中")];localStorage.setItem("travel-active","a");
    contextResponse={...emptyContext,confirmations:[{id:"running",version:2,preview:reply,command:"确认行程修改",expires_at:"2030-10-01T00:00:00Z",
      draft_state:{id:"running",version:2,state:"ready",base_version:1,can_revalidate:true,can_discard:true,operation:{action:"revalidate",status:"running",job_id:7}}}]};
    render(<App/>);await screen.findByText("正在重新核对这份草案…");
    expect((screen.getByRole("button",{name:"保存这版行程"}) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button",{name:"重新核对并生成预览"}) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button",{name:"放弃这份草案"}) as HTMLButtonElement).disabled).toBe(false);
  });
  it("quotes a historical railway reply and clears its reference when switching conversations", async () => {
    reply = "铁路查询结果"; referenceReply = true;
    conversationList = [sampleConversation("a", "窗口 A"), sampleConversation("b", "窗口 B")];
    localStorage.setItem("travel-active", "a"); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "引用" }));
    expect(screen.getByText("引用消息")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消引用" }));
    expect(screen.queryByText("引用消息")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "引用" }));
    const input = screen.getByLabelText("旅行问题");
    fireEvent.change(input, { target: { value: "改成明天上午九点到十点" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(sent.some(c => c.options.method === "POST" && c.url.endsWith("/messages"))).toBe(true));
    const request = sent.find(c => c.options.method === "POST" && c.url.endsWith("/messages"))!;
    expect(JSON.parse(String(request.options.body)).quoted_message_id).toBe("d:1");
    await waitFor(() => expect(screen.queryByText("引用消息")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "引用" }));
    fireEvent.click(screen.getByText("窗口 B"));
    expect(screen.queryByText("引用消息")).toBeNull();
  });

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
    expect(screen.getByRole("button", { name: "保存这版行程" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: /开启新对话/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新核对并生成预览" }));
    await waitFor(()=>expect(sent.some(call=>call.url.endsWith("/drafts/draft-a/actions"))).toBe(true));
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
    fireEvent.click(screen.getByRole("button", { name: "保存这版行程" }));
    await waitFor(() => expect(sent.some(call => call.url.endsWith("/confirmations/draft-a"))).toBe(true));
    const call = sent.find(call => call.url.endsWith("/confirmations/draft-a"))!;
    expect(JSON.parse(String(call.options.body)).version).toBe(4);
  });
  it("keeps saved activity introductions out of the compact itinerary sidebar", async () => {
    conversationList = [sampleConversation("a", "成都参考")];
    localStorage.setItem("travel-active", "a");
    contextResponse = { ...emptyContext, trips: [{ trip_id: "TR-a", version: 1,
      facts: [{ field: "budget", label: "住宿预算", value: "420元/间/晚", source: "旧要求" }],
      draft_facts: [{ field: "budget", label: "住宿预算", value: "320元/间/晚", source: "待确认要求" }],
      spec: { destination: "成都", day_count: 1 }, sources: { B1: { name: "文殊坊", address: "测试街" } },
      plan: { days: [{ day_index: 1, date: "2026-10-22", activities: [{ poi_id: "B1", period: "下午",
        description: { intro: { text: "川西风格的院落间分布着茶馆和非遗工坊。", source_ids: [1] },
          why_visit: "留出自由游览时间。", tips: [{ text: "可以挑一两条街巷慢慢逛。", source_ids: [1], kind: "advice" }] } }] }] } }] };
    render(<App />);
    fireEvent.click(await screen.findByRole("tab", { name: "行程" }));
    await screen.findByText("文殊坊");
    expect(screen.queryByText("景点介绍")).toBeNull();
    expect(screen.queryByText(/川西风格的院落/)).toBeNull();
    expect(screen.queryByText(/可以挑一两条街巷/)).toBeNull();
    expect(screen.queryByText(/安排理由：留出自由游览时间/)).toBeNull();
    expect(screen.queryByText(/小时级时间、预约场次和交通加总尚未校验/)).toBeNull();
    expect(screen.queryByText(/携程问道参考 · 来源/)).toBeNull();
    fireEvent.click(screen.getByText("旅行偏好与预算"));
    expect(screen.getByText(/420元/)).toBeTruthy();
    expect(screen.queryByText(/320元/)).toBeNull();
    expect(screen.queryByText("调整需求")).toBeNull();
    expect(screen.queryByText("生成预算调整预览")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "调整这一天" }));
    expect((screen.getByRole("textbox", { name: "旅行问题" }) as HTMLTextAreaElement).value).toBe("修改行程 TR-a 的第1天：");
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
