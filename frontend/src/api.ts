let csrf = "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method))
    headers.set("X-CSRF-Token", csrf);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(
      0,
      "暂时连接不到本地服务。请确认网页服务仍在运行，连接恢复后会自动同步。",
    );
  }
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 410 && typeof value.conversation_id === "string") {
      window.dispatchEvent(
        new CustomEvent("travel:conversation-deleted", {
          detail: value.conversation_id,
        }),
      );
    }
    const message =
      typeof value.detail === "string"
        ? value.detail
        : response.status === 422
          ? "输入格式不正确，请检查消息和附件。"
          : "这次操作没有完成，请稍后重试。";
    throw new ApiError(response.status, message);
  }
  return value as T;
}

export interface Bootstrap {
  csrf_token: string;
  version: string;
  llm_configured: boolean;
  amap_configured: boolean;
}
export async function bootstrap() {
  const value = await api<Bootstrap>("/bootstrap");
  csrf = value.csrf_token;
  return value;
}
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}
export interface DeletionJob {
  title?: string | null;
  conversation_id: string;
  state: "pending" | "cleaning" | "failed" | "completed";
  requested_at: string;
  updated_at: string;
  error: string;
}
export interface DeletionSummary {
  id: string;
  title: string;
  messages: number;
  attachments: number;
  active_tasks: number;
  reminders: number;
  trips: number;
  planning_artifacts: number;
  tool_artifacts: number;
  conversation_summaries: number;
  scheduled_queries: number;
  policy_watches: number;
}
export interface Upload {
  id: string;
  filename: string;
  content_type: string;
  size: number;
}
export interface Message {
  id: string;
  role: "user" | "assistant";
  kind: string;
  content: string;
  created_at: string;
  uploads: Upload[];
  job_id: number | null;
  job_status: string | null;
}
export interface MessagePage {
  items: Message[];
  has_more: boolean;
  before: string | null;
}
export interface Job {
  id: number;
  status: string;
  created_at: string;
  updated_at: string;
}
export interface ExecutionReport {
  retrieval_status?: import("./RuntimeStatusPanel").RetrievalStatus;
  job_id: number;
  job_status: string;
  state: string;
  note: string;
  model_calls: number;
  timeline: { id: number; stage: string; title: string; created_at: string; description: string; warning: boolean; recovered?: boolean; details: Record<string, unknown> }[];
  report_text: string;
  replayed: boolean;
}
export interface Confirmation {
  diff?: TripDiff | null;
  id: string;
  version: number;
  command: string;
  preview: string | null;
  can_confirm?: boolean;
  unavailable_reason?: string;
  expires_at: string;
}
export interface Trip {
  trip_id: string;
  version: number;
  facts?: FactRow[];
  draft_facts?: FactRow[];
  draft_version?: number | null;
  draft_id?: string | null;
  spec: {
    destination?: string;
    start_date?: string;
    day_count?: number;
    preferences?: string[];
  };
  plan: {
    days: {
      day_index: number;
      date?: string;
      activities: {
        name?: string;
        poi_id?: string;
        period?: string;
        reason?: string;
      }[];
    }[];
  };
  sources: Record<string, { name?: string; address?: string }>;
}
export interface FactRow { field: string; label: string; value: string; source: string }
export interface TripDiff {
  base_version: number;
  changes: {field: string; label: string; before: string; after: string}[];
  changed_days: number[];
  hotel_changed: boolean;
  routes_changed: boolean;
  price_note: string;
}
export interface PlanningArtifact {
  artifact_id: string;
  ordinal: number;
  kind: "exploratory" | "research_backed";
  title: string;
  destination: string;
  duration_options: number[];
  summary: string;
  status: string;
  version: number;
  created_at: string;
}
export interface Reminder {
  reminder_id: string;
  title: string;
  scheduled_at_utc: string;
  status: string;
  delivery_status: string;
}
export interface ScheduledQuery {
  query_id: string;
  kind: string;
  status: string;
  due_at: string;
  arguments_json: string;
}
export interface PolicyWatch {
  watch_id: string;
  entity: string;
  status: string;
  ends_at: string;
}
export interface ConversationUsageTotals {
  calls: number;
  prompt_tokens: number;
  cached_input_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  running_calls: number;
  estimated_cost: number;
  cost_unknown: number;
  cost_recalculated: number;
  cache_detail_missing: number;
  unknown_usage: number;
  cost_breakdown: { currency: string; amount: number }[];
}
export interface ConversationUsageReport {
  conversation_id: string;
  totals: ConversationUsageTotals;
  purposes: (Omit<ConversationUsageTotals, "total_tokens" | "running_calls"> & { purpose: string; model_id: string })[];
  currency: string;
  cost_note: string;
  scope_note: string;
}
export interface Context {
  model_usage?: ConversationUsageReport;
  trips: Trip[];
  planning_artifacts: PlanningArtifact[];
  reminders: Reminder[];
  documents: { id: number; filename: string; created_at: string }[];
  confirmations: Confirmation[];
  jobs: Job[];
  scheduled_queries: ScheduledQuery[];
  policy_watches: PolicyWatch[];
  reservations: {
    code: string;
    status: string;
    version: number;
    items: {
      name: string;
      visit_date: string | null;
      code: string;
      reminders: { time: string; status: string }[];
    }[];
  }[];
}
export interface Notice {
  id: number;
  content: string;
  created_at: string;
  read_at: string | null;
  conversation_id: string;
  title: string;
}
export interface Status {
  version: string;
  llm_configured: boolean;
  amap_configured: boolean;
  data_isolated: boolean;
  tasks: Record<string, { running: boolean; restart_count: number }>;
  queue: Record<string, number>;
}
export const emptyContext: Context = {
  trips: [],
  planning_artifacts: [],
  reminders: [],
  documents: [],
  confirmations: [],
  jobs: [],
  scheduled_queries: [],
  policy_watches: [],
  reservations: [],
};
export const label: Record<string, string> = {
  pending: "排队中",
  running: "处理中",
  retry: "正在重试",
  completed: "已完成",
  failed: "未完成",
  cancelled: "已取消",
  active: "已安排",
  sent: "已通知",
  queued: "等待执行",
  blocked: "已暂停",
  missed: "已错过",
  ready: "就绪",
};
export const dateText = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
