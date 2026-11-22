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
  } catch (reason) {
    if (options.signal?.aborted || (reason instanceof Error && reason.name === "AbortError")) throw reason;
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
  reference?: { kind: "rail" | "message"; label: string };
  rail_results?: RailResultReference[];
  trip_presentation?: TripPresentation;
  plan_comparison?: boolean;
  hotel_actions?: HotelAction[];
  hotel_presentation?: {text:string};
}
export interface HotelAction {
  kind: "select" | "accept_dates" | "reject_dates" | "view_original";
  label: string; task_id?: string; task_version?: number; query_index?: number;
  candidate_set_id?: string; candidate_set_version?: number; candidate_id?: string; selectable?: boolean;
  proposal_id?: string; proposal_version?: number; source_event_id?: string; revision?: string; reference_text?: string;
}
export interface HotelOriginal {
  text: string; cursor: number; next_cursor: number | null; total_chars: number;
  source_event_id: string; revision: string; historical: boolean; queried_at: string;
}
export interface RailResultReference { id: string; origin: string; destination: string }
export interface RailTrain {
  train_code: string; origin_station: string; destination_station: string;
  departure_at: string; arrival_at: string; duration_minutes: number;
  seats: { name: string | null; price_cny: number | null; availability: string }[];
  quoted_seat?: IntercityLeg["selected_seat"];
}
export interface RailResultPage {
  id: string;
  request: { origin: string; destination: string; travel_date: string; departure_window: number[];
    train_preference: string; origin_scope: string; destination_scope: string; seat_class?: string; seat_requirement?: string };
  status: string; queried_at: string; total_count: number; matched_count: number; time_span: string[];
  page: number; page_count: number; page_size: number; period: string;
  items: { train_code: string; departure_date: string; options: RailTrain[] }[];
  filters: { id: string; label: string }[];
  query_actions: { period: string; label: string }[];
  incomplete: boolean; legacy_partial: boolean;
  candidate_limit?: number | null;
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
  progress?: { stage: string; created_at: string; description: string };
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
  draft_state?:DraftState|null;
  state?: "ready" | "expired" | "stale";
  reference_message_id?: string | null;
  diff?: TripDiff | null;
  id: string;
  version: number;
  command: string;
  preview: string | null;
  can_confirm?: boolean;
  unavailable_reason?: string;
  expires_at: string;
}
export interface DraftState {
  id:string;version:number;state:"ready"|"expired"|"stale"|"saved"|"discarded"|"superseded"|"unavailable";
  base_version:number;latest:{id:string;version:number};can_revalidate:boolean;can_discard:boolean;
  operation?:{action:"revalidate"|"discard";status:string;job_id:number;message?:string}|null;
}
export type DraftAction="revalidate"|"discard";
export interface Trip {
  editing_message_id?: string | null;
  comparison_message_id?: string | null;
  preview?: Trip;
  is_preview?: boolean;
  trip_id: string;
  version: number;
  facts?: FactRow[];
  draft_facts?: FactRow[];
  draft_version?: number | null;
  draft_id?: string | null;
  spec: {
    destination?: string;
    start_date?: string;
    date_is_tentative?: boolean;
    date_reference_day?: string;
    date_preference_text?: string;
    day_count?: number;
    requested_day_count?: number;
    duration_adjustment?: string;
    planning_window?: {start: string; end: string; start_text: string; end_text: string};
    preferences?: string[];
  };
  plan: {
    hotel_quotes?: {
      result_status?:string;result_note?:string;reference_text?:string;
      request_id?:string;retrieved_at?:string;note?:string;requested_poi_ids?:string[];raw_text?:string;
      coverage?:{asked:number;returned:number;bound:number;comparable:number};
      stay?:{check_in?:string;check_out?:string;rooms?:number;adults?:number;children_ages?:number[]};
      offers:{offer_id:string;amap_poi_id:string;hotel_name:string;room_name:string;currency:string;price_basis?:string;
        nightly_prices:{date:string;amount:string}[];reported_total?:string|null;calculated_total?:string|null;
        taxes_and_fees_status:string;applicability_status:string;source_url?:string|null;status?:string;issues?:string[];
        breakfast?:string|null;cancellation?:string|null;eligibility?:string|null;
        average_price?:string;from_price?:string;budget_difference?:string|null;numeric_comparison?:string;comparison_basis?:string}[];
    };
    hotel_ids?:string[];
    hotel_recommendations?:{poi_id:string;status?:string;price_status?:string}[];
    hotel_review_note?:string;
    dining_options?:{day_index:number;meal:string;candidates:{poi_id:string}[]}[];
    intercity?: IntercityPlan;
    days: {
      kind?: "transport" | "rest" | "visit";
      day_index: number;
      date?: string;
      activities: {
        subactivities?: { poi_id: string }[];
        name?: string;
        poi_id?: string;
        period?: string;
        reason?: string;
        description?: {
          intro?: { text: string; source_ids: number[] };
          why_visit?: string;
          tips?: { text: string; source_ids: number[]; kind: string }[];
          source_status?: string;
        };
      }[];
    }[];
  };
  sources: Record<string, { name?: string; address?: string }>;
}
export interface IntercityLeg {
  status: string; provider?: string; queried_at?: string; selected?: RailTrain; candidates?: RailTrain[];
  selected_seat?: {name: string | null; seat_class?: string | null; price_cny: number | null; availability: string; currency?: string} | null;
  candidate_truncated?: boolean;
  missing_fields?: string[]; request?: {mode?: string; mode_source?: string; travel_date?: string; seat_class?: string; seat_requirement?: string};
}
export interface TripPresentation {
  lifecycle?:DraftState;
  trip: Trip; prefix: string; suffix: string; is_preview: boolean;
  sections: ({kind: "text"; text: string} | {kind: "transport"; direction: "outbound" | "inbound"})[];
}
export interface PlanComparison {
  message_id: string; final_trip: Trip | null; final_note: string; previous_message_id: string | null;
  final_text: string; final_presentation?: TripPresentation | null;
  decision: {status?: string; choice?: string; reason?: string; repaired?: boolean};
  branches: Record<string, {status: string; created_at: string; plan?: Trip["plan"]; places?: Trip["sources"];
    choice?: string; reason?: string; error_code?:string; error_summary?:string; error_type?:string; validation_path?:string; failure_stage?:string; days_grounded?: boolean; schedule_text?: string;
    data?: {report?: string; sources?: {provider?: string; text?: string}[]}}>;
}
export interface IntercityPlan {
  issues?: {code: string; direction?: string; day_index?: number | null; path?: string; message: string}[];
  day_count_basis?: string; basis_source?: string;
  completeness: string; outbound?: IntercityLeg; inbound?: IntercityLeg; limitations?: string[];
  hotel_nights?: number; ticket_cost?: {total_cny: number | null; unknown_segments: number};
  station_transfers?: Record<string,{status: string; duration_minutes?: number}>;
  availability_windows?: Record<string,{earliest: string; latest: string; allowed_periods: string[]}>;
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
  cost_supplemented?:number; unknown_pricing?:number; unknown_identity?:number;
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
  supplier_usage?:{wendao_submissions:number|null;cost:null;usage:null;note:string};
  rounds?:{event_key:string;started_at:string;user_message?:string;totals:Omit<ConversationUsageTotals,"total_tokens"|"running_calls">;
    calls:{call_id:string;purpose:string;model_id:string;prompt_tokens:number|null;completion_tokens:number|null;
      elapsed_seconds:number|null;cost:number|null;currency:string;origin:string;unknown:string;pricing_version?:string}[]}[];
  conversation_id: string;
  totals: ConversationUsageTotals;
  purposes: (Omit<ConversationUsageTotals, "total_tokens" | "running_calls"> & { purpose: string; model_id: string; connection_id?: string; profile_id?: string; connection_name?:string; profile_name?:string })[];
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
export interface RailMCPStatus {
  alias: string; enabled?: boolean; configured?: boolean; expired?: boolean;
  transport?: "sse" | "streamable_http"; timeout_seconds?: number; expires_at?: string; state?: string;
  runtime?: RailMCPStatus;
}
