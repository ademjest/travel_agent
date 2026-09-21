import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Bell,
  Check,
  ChevronRight,
  CircleHelp,
  CloudSun,
  Compass,
  Copy,
  FileText,
  ImagePlus,
  Info,
  LoaderCircle,
  Map,
  MapPin,
  Menu,
  MessageSquare,
  Navigation,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  Pencil,
  Plus,
  Route,
  Settings2,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { SettingsPanel } from "./SettingsPanel";
import { ResearchPanel } from "./ResearchPanel";
import { api, ApiError, bootstrap, dateText, emptyContext, label } from "./api";
import type {
  Bootstrap,
  Context,
  Conversation,
  Message,
  MessagePage,
  Notice,
  Status,
  Upload,
  DeletionJob,
  DeletionSummary,
} from "./api";

const suggestions = [
  {
    icon: Route,
    name: "计划一场旅行",
    hint: "目的地、天数，交给我来安排",
    text: "帮我规划武汉三天行程，带老人，使用公共交通，必去湖北省博物馆。",
  },
  {
    icon: CloudSun,
    name: "出发前查一查",
    hint: "天气与交通，让出行更从容",
    text: "武汉明天的天气怎么样？",
  },
  {
    icon: ImagePlus,
    name: "读懂旅行资料",
    hint: "上传攻略、截图或行程文档",
    text: "帮我分析这份旅行资料。",
    upload: true,
  },
  {
    icon: Bell,
    name: "记住重要时刻",
    hint: "预约、抢票与出发提醒",
    text: "明天提醒我预约博物馆。",
  },
];
type DraftFile = {
  key: string;
  file: File;
  upload?: Upload;
  error?: string;
  busy: boolean;
};

function Markdown({ text }: { text: string }) {
  const display = text.startsWith("旅行行程：")
    ? text
        .replace(/^(第 \d+ 天 · [^\n]+)$/gm, "\n### $1\n")
        .replace(/^地点依据：/gm, "\n地点依据：")
    : text;
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          img: ({ alt }) => (
            <span className="muted">[图片链接：{alt || "请查看原始资料"}]</span>
          ),
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {display}
      </ReactMarkdown>
    </div>
  );
}

export default function App() {
  const [config, setConfig] = useState<Bootstrap | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState("");
  const activeRef = useRef("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [context, setContext] = useState<Context>(emptyContext);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<DraftFile[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(true);
  const [ready, setReady] = useState(false);
  const [sidebar, setSidebar] = useState(false);
  const [details, setDetails] = useState(() => window.innerWidth >= 1280);
  const [width, setWidth] = useState(window.innerWidth);
  const [tab, setTab] = useState("trip");
  const [modal, setModal] = useState<
    "notifications" | "status" | "delete" | null
  >(null);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const deleteTargetRef = useRef("");
  const [deleteSummary, setDeleteSummary] = useState<DeletionSummary | null>(
    null,
  );
  const [deleteError, setDeleteError] = useState("");
  const [deleteSending, setDeleteSending] = useState(false);
  const [deletionJobs, setDeletionJobs] = useState<DeletionJob[]>([]);
  const deletionJobsRef = useRef<DeletionJob[]>([]);
  const deletedIds = useRef(new Set<string>());
  const deletionChannel = useRef<BroadcastChannel | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [newMessages, setNewMessages] = useState(false);
  const [copied, setCopied] = useState("");
  const [tick, setTick] = useState(Date.now());
  const input = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const feed = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const request = useRef<{ signature: string; id: string } | null>(null);
  const lastMessage = useRef("");
  const dialog = useRef<HTMLDialogElement>(null);
  const historyLoaded = useRef(false);

  const report = (reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 410) return;
    setError(reason instanceof Error ? reason.message : "操作未完成，请重试。");
  };
  const forgetConversation = useCallback((identity: string) => {
    if (typeof identity !== "string" || !identity) return;
    if (deleteTargetRef.current === identity) {
      deleteTargetRef.current = "";
      setDeleteTarget(null);
      setDeleteSummary(null);
      setModal(null);
    }
    deletedIds.current.add(identity);
    localStorage.removeItem(`travel-draft:${identity}`);
    if (localStorage.getItem("travel-active") === identity)
      localStorage.removeItem("travel-active");
    setConversations((old) => old.filter((item) => item.id !== identity));
    setNotices((old) =>
      old.filter((item) => item.conversation_id !== identity),
    );
    if (activeRef.current === identity) {
      activeRef.current = "";
      setActive("");
      setMessages([]);
      setContext(emptyContext);
      setFiles([]);
      setDraft("");
      setRenaming(false);
      setHasMore(false);
      setNewMessages(false);
      setError("");
      request.current = null;
      lastMessage.current = "";
      historyLoaded.current = false;
    }
  }, []);
  const choose = useCallback((identity: string) => {
    if (deletedIds.current.has(identity)) return;
    activeRef.current = identity;
    setActive(identity);
    setMessages([]);
    setContext(emptyContext);
    setFiles([]);
    setDraft(localStorage.getItem(`travel-draft:${identity}`) || "");
    setSidebar(false);
    setError("");
    setHasMore(false);
    setRenaming(false);
    nearBottom.current = true;
    lastMessage.current = "";
    historyLoaded.current = false;
    localStorage.setItem("travel-active", identity);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const configValue = await bootstrap();
        const list = await api<Conversation[]>("/conversations");
        if (cancelled) return;
        setConfig(configValue);
        setConversations(
          list.filter((item) => !deletedIds.current.has(item.id)),
        );
        setReady(true);
        const saved = localStorage.getItem("travel-active");
        if (saved && list.some((c) => c.id === saved)) choose(saved);
      } catch (reason) {
        if (!cancelled) report(reason);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [choose]);

  const refresh = useCallback(async (identity: string) => {
    const [page, ctx, list, notifications] = await Promise.all([
      api<MessagePage>(`/conversations/${identity}/messages`),
      api<Context>(`/conversations/${identity}/context`),
      api<Conversation[]>("/conversations"),
      api<Notice[]>("/notifications"),
    ]);
    if (identity !== activeRef.current || deletedIds.current.has(identity))
      return;
    setMessages((old) =>
      historyLoaded.current
        ? [
            ...old.filter((m) => !page.items.some((n) => n.id === m.id)),
            ...page.items,
          ].sort(
            (a, b) =>
              a.created_at.localeCompare(b.created_at) ||
              a.id.localeCompare(b.id),
          )
        : page.items,
    );
    setContext(ctx);
    setConversations(list.filter((item) => !deletedIds.current.has(item.id)));
    setNotices(
      notifications.filter(
        (item) => !deletedIds.current.has(item.conversation_id),
      ),
    );
    setOnline(true);
    setError((old) => (old.startsWith("暂时连接不到本地服务") ? "" : old));
    if (!historyLoaded.current) {
      setHasMore(page.has_more);
      setHistoryCursor(page.before);
    }
    const lastId = page.items.at(-1)?.id || "";
    if (lastId !== lastMessage.current) {
      if (nearBottom.current)
        requestAnimationFrame(() => {
          if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
        });
      else setNewMessages(true);
      lastMessage.current = lastId;
    }
    return ctx.jobs.length > 0;
  }, []);

  useEffect(() => {
    if (!active || !ready) return;
    let cancelled = false,
      timer: ReturnType<typeof setTimeout>,
      failures = 0;
    let inFlight = false;
    const update = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      clearTimeout(timer);
      let processing = false;
      try {
        processing = !!(await refresh(active));
        failures = 0;
      } catch (reason) {
        if (
          !cancelled &&
          activeRef.current === active &&
          !deletedIds.current.has(active)
        ) {
          setOnline(false);
          report(reason);
          failures++;
        }
      } finally {
        inFlight = false;
        if (!cancelled)
          timer = setTimeout(
            update,
            document.hidden || !processing
              ? 15000
              : Math.min(1500 * 2 ** failures, 15000),
          );
      }
    };
    void update();
    const focus = () => {
      if (!document.hidden) void update();
    };
    document.addEventListener("visibilitychange", focus);
    window.addEventListener("focus", focus);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", focus);
      window.removeEventListener("focus", focus);
    };
  }, [active, ready, refresh, context.jobs.length > 0]);

  useEffect(() => {
    const local = (event: Event) =>
      forgetConversation((event as CustomEvent<string>).detail);
    const storage = (event: StorageEvent) => {
      if (event.key === "travel-deleted" && event.newValue) {
        try {
          forgetConversation(JSON.parse(event.newValue).id);
        } catch {
          /* Ignore unrelated malformed storage events. */
        }
      }
    };
    window.addEventListener("travel:conversation-deleted", local);
    window.addEventListener("storage", storage);
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel("travel-conversations");
      channel.onmessage = (event) => {
        if (event.data?.type === "deleted" && typeof event.data.id === "string")
          forgetConversation(event.data.id);
      };
      deletionChannel.current = channel;
    }
    return () => {
      window.removeEventListener("travel:conversation-deleted", local);
      window.removeEventListener("storage", storage);
      deletionChannel.current?.close();
      deletionChannel.current = null;
    };
  }, [forgetConversation]);

  useEffect(() => {
    if (!ready) return;
    let disposed = false,
      timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let pending = false;
      try {
        const listed = await api<DeletionJob[]>("/conversation-deletions");
        const finished = await Promise.all(
          deletionJobsRef.current
            .filter(
              (job) =>
                job.state !== "completed" &&
                !listed.some(
                  (item) => item.conversation_id === job.conversation_id,
                ),
            )
            .map((job) =>
              api<DeletionJob>(
                `/conversation-deletions/${job.conversation_id}`,
              ),
            ),
        );
        if (disposed) return;
        for (const job of listed) forgetConversation(job.conversation_id);
        const changes = new globalThis.Map<string, DeletionJob>(
          [...deletionJobsRef.current, ...listed, ...finished].map((job) => [
            job.conversation_id,
            job,
          ]),
        );
        deletionJobsRef.current = [...changes.values()];
        setDeletionJobs(deletionJobsRef.current);
        pending = deletionJobsRef.current.some((job) =>
          ["pending", "cleaning"].includes(job.state),
        );
      } catch (reason) {
        if (!disposed) report(reason);
      }
      if (!disposed) timer = setTimeout(poll, pending ? 1500 : 15000);
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [
    ready,
    forgetConversation,
    deletionJobs.some((job) => ["pending", "cleaning"].includes(job.state)),
  ]);

  useEffect(() => {
    if (!ready || active) return;
    const update = () =>
      api<Notice[]>("/notifications")
        .then((items) =>
          setNotices(
            items.filter(
              (item) => !deletedIds.current.has(item.conversation_id),
            ),
          ),
        )
        .catch(report);
    void update();
    const timer = setInterval(update, 15000);
    return () => clearInterval(timer);
  }, [ready, active]);
  useEffect(() => {
    if (active && !deletedIds.current.has(active))
      localStorage.setItem(`travel-draft:${active}`, draft);
  }, [active, draft]);
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    if (input.current) {
      input.current.style.height = "auto";
      input.current.style.height = `${Math.min(input.current.scrollHeight, 160)}px`;
    }
  }, [draft]);
  useEffect(() => {
    if (modal) dialog.current?.showModal();
    else dialog.current?.close();
    if (modal !== "delete") deleteTargetRef.current = "";
  }, [modal]);

  async function openDelete(conversation: Conversation) {
    deleteTargetRef.current = conversation.id;
    setDeleteTarget(conversation);
    setDeleteSummary(null);
    setDeleteError("");
    setModal("delete");
    try {
      const summary = await api<DeletionSummary>(
        `/conversations/${conversation.id}/deletion-summary`,
      );
      if (deleteTargetRef.current === conversation.id)
        setDeleteSummary(summary);
    } catch (reason) {
      if (deleteTargetRef.current === conversation.id)
        setDeleteError((reason as Error).message);
    }
  }
  function acceptDeletion(job: DeletionJob) {
    forgetConversation(job.conversation_id);
    deletionChannel.current?.postMessage({
      type: "deleted",
      id: job.conversation_id,
    });
    localStorage.setItem(
      "travel-deleted",
      JSON.stringify({ id: job.conversation_id, time: Date.now() }),
    );
    deletionJobsRef.current = [
      ...deletionJobsRef.current.filter(
        (item) => item.conversation_id !== job.conversation_id,
      ),
      job,
    ];
    setDeletionJobs(deletionJobsRef.current);
    setModal(null);
  }
  async function confirmDelete() {
    if (!deleteTarget || !deleteSummary || deleteSending) return;
    const identity = deleteTarget.id;
    setDeleteSending(true);
    setDeleteError("");
    try {
      acceptDeletion(
        await api<DeletionJob>(`/conversations/${identity}`, {
          method: "DELETE",
        }),
      );
    } catch (reason) {
      try {
        acceptDeletion(
          await api<DeletionJob>(`/conversation-deletions/${identity}`),
        );
      } catch {
        setDeleteError((reason as Error).message);
      }
    } finally {
      setDeleteSending(false);
    }
  }
  async function retryDeletion(identity: string) {
    try {
      acceptDeletion(
        await api<DeletionJob>(`/conversation-deletions/${identity}/retry`, {
          method: "POST",
        }),
      );
    } catch (reason) {
      report(reason);
    }
  }
  function dismissDeletion(identity: string) {
    deletionJobsRef.current = deletionJobsRef.current.filter(
      (job) => job.conversation_id !== identity,
    );
    setDeletionJobs(deletionJobsRef.current);
  }
  useEffect(() => {
    const resize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  useEffect(() => {
    const selector =
      sidebar && width < 768
        ? ".sidebar"
        : details && width < 1280
          ? ".details-panel"
          : null;
    if (!selector || modal) return;
    const panel = document.querySelector<HTMLElement>(selector);
    const previous = document.activeElement as HTMLElement | null;
    const controls = () =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          "button:not(:disabled), a[href], input",
        ) || [],
      );
    controls()[0]?.focus();
    const trap = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setSidebar(false);
        setDetails(false);
      }
      if (event.key !== "Tab") return;
      const elements = controls(),
        first = elements[0],
        last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      previous?.focus();
    };
  }, [sidebar, details, width, modal]);

  async function ensureConversation() {
    if (activeRef.current) return activeRef.current;
    const created = await api<Conversation>("/conversations", {
      method: "POST",
    });
    setConversations((old) => [created, ...old]);
    choose(created.id);
    return created.id;
  }
  async function newConversation() {
    if (!ready || sending) return;
    try {
      const created = await api<Conversation>("/conversations", {
        method: "POST",
      });
      setConversations((old) => [created, ...old]);
      choose(created.id);
      input.current?.focus();
    } catch (reason) {
      report(reason);
    }
  }
  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (
      !ready ||
      sending ||
      files.some((f) => f.busy || f.error) ||
      (!draft.trim() && !files.length)
    )
      return;
    const text = draft.trim(),
      selectedFiles = [...files];
    setSending(true);
    setError("");
    let sendingConversation = activeRef.current;
    try {
      const identity = await ensureConversation();
      sendingConversation = identity;
      const body = {
        content: text,
        upload_ids: selectedFiles.flatMap((f) =>
          f.upload ? [f.upload.id] : [],
        ),
      };
      const signature = JSON.stringify({ identity, ...body });
      if (request.current?.signature !== signature)
        request.current = { signature, id: crypto.randomUUID() };
      await api(`/conversations/${identity}/messages`, {
        method: "POST",
        body: JSON.stringify({
          ...body,
          client_request_id: request.current.id,
        }),
      });
      if (activeRef.current === identity) {
        setDraft("");
        setFiles([]);
        nearBottom.current = true;
      }
      request.current = null;
      await refresh(identity);
    } catch (reason) {
      if (!deletedIds.current.has(sendingConversation)) {
        setDraft(text);
        report(reason);
      }
    } finally {
      setSending(false);
      input.current?.focus();
    }
  }
  async function addFiles(incoming: File[]) {
    if (!incoming.length || !ready) return;
    if (files.length + incoming.length > 8) {
      setError("每条消息最多添加 8 个附件。");
      return;
    }
    if (incoming.some((f) => f.size > 5 * 1024 * 1024)) {
      setError("每个附件不能超过 5 MB。");
      return;
    }
    try {
      const previousDraft = draft;
      const identity = await ensureConversation();
      setDraft(previousDraft);
      const additions = incoming.map((file) => ({
        file,
        key: crypto.randomUUID(),
        busy: true,
      }));
      setFiles((old) => [...old, ...additions]);
      setError("");
      for (const addition of additions) {
        const data = new FormData();
        data.set("conversation_id", identity);
        data.set("file", addition.file);
        try {
          const upload = await api<Upload>("/uploads", {
            method: "POST",
            body: data,
          });
          if (activeRef.current === identity)
            setFiles((old) =>
              old.map((f) =>
                f.key === addition.key ? { ...f, busy: false, upload } : f,
              ),
            );
        } catch (reason) {
          if (activeRef.current === identity)
            setFiles((old) =>
              old.map((f) =>
                f.key === addition.key
                  ? { ...f, busy: false, error: (reason as Error).message }
                  : f,
              ),
            );
        }
      }
    } catch (reason) {
      report(reason);
    }
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing &&
      event.keyCode !== 229
    ) {
      event.preventDefault();
      void send();
    }
  }
  async function cancel(job: number) {
    try {
      await api(`/tasks/${job}/cancel`, { method: "POST" });
      await refresh(activeRef.current);
    } catch (reason) {
      report(reason);
    }
  }
  async function confirm(id: string, version: number) {
    try {
      setSending(true);
      await api(`/conversations/${active}/confirmations/${id}`, {
        method: "POST",
        body: JSON.stringify({
          version,
          client_request_id: `confirm-${id}-${version}`,
        }),
      });
      await refresh(active);
    } catch (reason) {
      report(reason);
    } finally {
      setSending(false);
    }
  }
  function fill(text: string) {
    setDraft(text);
    input.current?.focus();
    if (window.innerWidth < 1024) setDetails(false);
  }
  async function openStatus() {
    setModal("status");
    try {
      setStatus(await api<Status>("/status"));
    } catch (reason) {
      report(reason);
    }
  }
  async function loadOlder() {
    const identity = activeRef.current;
    try {
      const page = await api<MessagePage>(
        `/conversations/${active}/messages?before=${encodeURIComponent(historyCursor || "")}`,
      );
      if (identity !== activeRef.current || deletedIds.current.has(identity))
        return;
      const height = feed.current?.scrollHeight || 0;
      historyLoaded.current = true;
      setMessages((old) => [
        ...page.items,
        ...old.filter((m) => !page.items.some((n) => n.id === m.id)),
      ]);
      setHasMore(page.has_more);
      setHistoryCursor(page.before);
      requestAnimationFrame(() => {
        if (feed.current)
          feed.current.scrollTop += feed.current.scrollHeight - height;
      });
    } catch (reason) {
      report(reason);
    }
  }
  const current = conversations.find((c) => c.id === active);
  const unread = notices.filter((n) => !n.read_at).length;
  const busy = context.jobs.length > 0;

  return (
    <div
      className={`app ${details ? "details-open" : ""} ${sidebar ? "sidebar-open" : ""}`}
    >
      {(sidebar || details) && (
        <button
          className="drawer-scrim"
          aria-label="关闭侧栏"
          onClick={() => {
            setSidebar(false);
            setDetails(false);
          }}
        />
      )}
      <aside
        className="sidebar"
        inert={width < 768 && !sidebar}
        aria-label="会话导航"
      >
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setSidebar(false);
          }}
        >
          <span className="brand-mark">
            <Navigation size={22} fill="currentColor" />
          </span>
          <span>
            彼岸<small>把远方，变成日常</small>
          </span>
        </a>
        <button
          className="new-conversation"
          onClick={newConversation}
          disabled={!ready || sending}
        >
          <Plus size={18} />
          开启新对话<span>＋</span>
        </button>
        <div className="sidebar-label">
          我的旅途{" "}
          <span>{conversations.length.toString().padStart(2, "0")}</span>
        </div>
        <nav className="conversation-list" aria-label="历史会话">
          {!conversations.length && (
            <p className="sidebar-empty">
              每一段旅程，
              <br />
              都从一个想法开始。
            </p>
          )}
          {conversations.map((c) => (
            <div className="conversation-row" key={c.id}>
              <button
                onClick={() => choose(c.id)}
                className={
                  c.id === active ? "conversation selected" : "conversation"
                }
              >
                <MessageSquare size={16} />
                <span>{c.title}</span>
                {c.id === active && <span className="selected-dot" />}
              </button>
              <button
                className="delete-conversation"
                aria-label={`删除会话 ${c.title}`}
                title="删除会话"
                onClick={() => openDelete(c)}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button onClick={() => setModal("notifications")}>
            <Bell size={18} />
            旅途通知{unread > 0 && <span className="count">{unread}</span>}
          </button>
          <button onClick={openStatus}>
            <Settings2 size={18} />
            状态与设置
            <ChevronRight size={14} />
          </button>
          <div className="local-note">
            <span className="avatar">我</span>
            <div>
              我的本地空间
              <small>
                <span className="dot" />
                数据独立保存在本机
              </small>
            </div>
            <ShieldCheck size={17} />
          </div>
        </div>
      </aside>

      <main
        className="workspace"
        inert={(sidebar && width < 768) || (details && width < 1280)}
      >
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            aria-label="打开会话列表"
            onClick={() => setSidebar(!sidebar)}
          >
            <Menu size={20} />
          </button>
          <div className="breadcrumb">
            <span>旅行工作区</span>
            <ChevronRight size={14} />
            <strong>{current?.title || "新的出发"}</strong>
            {active && (
              <button
                className="icon-button tiny"
                aria-label="重命名会话"
                onClick={() => {
                  setTitleDraft(current?.title || "");
                  setRenaming(!renaming);
                }}
              >
                <Pencil size={13} />
              </button>
            )}
          </div>
          <div className="topbar-actions">
            {current && (
              <button
                className="icon-button"
                aria-label="删除当前会话"
                title="删除当前会话"
                onClick={() => openDelete(current)}
              >
                <Trash2 size={17} />
              </button>
            )}
            <span className={`connection ${online ? "" : "offline"}`}>
              <span className="dot" />
              {online ? "本地模式" : "连接中断"}
            </span>
            <span className="top-divider" />
            <button
              className="icon-button"
              aria-label={details ? "收起旅行面板" : "展开旅行面板"}
              onClick={() => setDetails(!details)}
            >
              {details ? (
                <PanelRightClose size={19} />
              ) : (
                <PanelRightOpen size={19} />
              )}
            </button>
          </div>
        </header>
        {renaming && (
          <form
            className="rename-form"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api(`/conversations/${active}`, {
                  method: "PATCH",
                  body: JSON.stringify({ title: titleDraft }),
                });
                setRenaming(false);
                await refresh(active);
              } catch (r) {
                report(r);
              }
            }}
          >
            <input
              aria-label="会话标题"
              autoFocus
              value={titleDraft}
              maxLength={80}
              onChange={(e) => setTitleDraft(e.target.value)}
            />
            <button type="submit">保存</button>
            <button type="button" onClick={() => setRenaming(false)}>
              取消
            </button>
          </form>
        )}
        <div
          className="feed"
          ref={feed}
          onScroll={() => {
            const f = feed.current;
            nearBottom.current =
              !!f && f.scrollHeight - f.scrollTop - f.clientHeight < 100;
            if (nearBottom.current) setNewMessages(false);
          }}
        >
          {!messages.length ? (
            <section className="welcome">
              <div className="welcome-kicker">
                <span className="compass-badge">
                  <Compass size={24} strokeWidth={1.4} />
                </span>{" "}
                YOUR NEXT CHAPTER
              </div>
              <h1>
                下一程，<span>从这里开始。</span>
              </h1>
              <p className="welcome-description">
                告诉我你想去的地方。一起把灵感整理成行程，
                <br className="desktop-break" />
                把值得期待的时刻，好好记住。
              </p>
              <div className="suggestions">
                {suggestions.map(({ icon: Icon, name, hint, text, upload }) => (
                  <button
                    key={name}
                    disabled={!ready}
                    onClick={() => {
                      fill(text);
                      if (upload) fileInput.current?.click();
                    }}
                  >
                    <span className="suggestion-icon">
                      <Icon size={21} strokeWidth={1.6} />
                    </span>
                    <span>
                      <strong>{name}</strong>
                      <small>{hint}</small>
                    </span>
                    <ArrowRight size={16} />
                  </button>
                ))}
              </div>
              <div className="welcome-footnote">
                <ShieldCheck size={14} />
                <span>独立的本地会话空间 · QQ 离线也可以继续探索</span>
              </div>
            </section>
          ) : (
            <div className="messages">
              <div className="conversation-date">
                <span />
                {new Date(messages[0].created_at).toLocaleDateString("zh-CN", {
                  month: "long",
                  day: "numeric",
                })}
                <span />
              </div>
              {hasMore && (
                <button className="older-button" onClick={loadOlder}>
                  加载更早的消息
                </button>
              )}
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`message ${message.role} ${message.kind === "progress" ? "progress-message" : ""}`}
                >
                  {message.role === "assistant" && (
                    <span className="assistant-avatar">
                      <Navigation size={15} fill="currentColor" />
                    </span>
                  )}
                  <div className="message-main">
                    <div className="message-meta">
                      {message.role === "assistant"
                        ? message.kind === "notification"
                          ? "彼岸 · 旅途通知"
                          : "彼岸"
                        : "你"}
                      <time>{dateText(message.created_at)}</time>
                    </div>
                    <div className="message-body">
                      {message.uploads.length > 0 && (
                        <div className="message-attachments">
                          {message.uploads.map((u) => (
                            <a
                              key={u.id}
                              href={`/api/uploads/${u.id}`}
                              target="_blank"
                              rel="noreferrer"
                              className="attachment-link"
                            >
                              {u.content_type.startsWith("image/") ? (
                                <img
                                  src={`/api/uploads/${u.id}`}
                                  alt={u.filename}
                                />
                              ) : (
                                <FileText size={20} />
                              )}
                              <span>{u.filename}</span>
                            </a>
                          ))}
                        </div>
                      )}
                      {message.kind === "progress" ? (
                        <p className="muted">{message.content}</p>
                      ) : (
                        <Markdown text={message.content} />
                      )}
                    </div>
                    {message.job_status &&
                      ["failed", "cancelled", "retry"].includes(
                        message.job_status,
                      ) && (
                        <small className="message-state">
                          {label[message.job_status]}
                          {message.job_status === "failed" && (
                            <button onClick={() => fill(message.content)}>
                              编辑后重试
                            </button>
                          )}
                        </small>
                      )}
                    {message.role === "assistant" &&
                      message.kind !== "progress" && (
                        <button
                          className="copy-button"
                          aria-label="复制回复"
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(
                                message.content,
                              );
                              setCopied(message.id);
                            } catch {
                              setError("复制失败，请手动选择文字。");
                            }
                          }}
                        >
                          {copied === message.id ? (
                            <Check size={13} />
                          ) : (
                            <Copy size={13} />
                          )}
                          {copied === message.id ? "已复制" : "复制"}
                        </button>
                      )}
                  </div>
                </article>
              ))}
              {context.confirmations.map((c) => (
                <section className="confirmation" key={`${c.id}-${c.version}`}>
                  <div className="confirmation-heading">
                    <Info size={18} />
                    <strong>有一项变更需要你确认</strong>
                  </div>
                  <Markdown text={c.preview} />
                  <div className="confirmation-actions">
                    <button
                      className="primary-button"
                      disabled={sending || busy}
                      onClick={() => confirm(c.id, c.version)}
                    >
                      确认这次变更
                    </button>
                    <button onClick={() => fill("取消当前任务")}>
                      暂不执行
                    </button>
                    <small>以当前预览版本为准</small>
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>

        <div className="composer-area">
          {deletionJobs.length > 0 && (
            <div className="deletion-notices" aria-live="polite">
              {deletionJobs.map((job) => (
                <div
                  className={`deletion-notice ${job.state}`}
                  key={job.conversation_id}
                >
                  {job.state === "completed" ? (
                    <Check size={16} />
                  ) : job.state === "failed" ? (
                    <Info size={16} />
                  ) : (
                    <LoaderCircle size={16} className="spin" />
                  )}
                  <span>
                    {job.title && <small>{job.title}</small>}
                    <strong>
                      {job.state === "completed"
                        ? "会话及关联数据已删除"
                        : job.state === "failed"
                          ? "会话已停用，清理尚未完成"
                          : "会话已停用，正在清理关联数据"}
                    </strong>
                    <small>
                      {job.error ||
                        (job.state === "completed"
                          ? "不会再执行该会话的任务与提醒。"
                          : "删除已受理，关闭网页不会中断清理。")}
                    </small>
                  </span>
                  {job.state === "failed" && (
                    <button onClick={() => retryDeletion(job.conversation_id)}>
                      重试清理
                    </button>
                  )}
                  {job.state === "completed" && (
                    <button
                      className="icon-button tiny"
                      aria-label="关闭删除完成提示"
                      onClick={() => dismissDeletion(job.conversation_id)}
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          {newMessages && (
            <button
              className="new-messages"
              onClick={() => {
                if (feed.current)
                  feed.current.scrollTop = feed.current.scrollHeight;
                setNewMessages(false);
                nearBottom.current = true;
              }}
            >
              <ArrowDown size={14} />
              有新消息
            </button>
          )}
          {context.jobs.length > 0 && (
            <div className="job-bar" aria-live="polite">
              <LoaderCircle size={15} className="spin" />
              <span>
                {label[context.jobs[0].status]}
                <small>
                  {" "}
                  ·{" "}
                  {Math.max(
                    0,
                    Math.floor(
                      (tick - Date.parse(context.jobs[0].created_at)) / 1000,
                    ),
                  )}{" "}
                  秒
                  {context.jobs.length > 1
                    ? ` · 共 ${context.jobs.length} 项`
                    : ""}
                </small>
              </span>
              <button onClick={() => cancel(context.jobs[0].id)}>
                取消请求
              </button>
            </div>
          )}
          {error && (
            <div className="error-banner" role="alert">
              <Info size={16} />
              <span>{error}</span>
              <button
                className="icon-button tiny"
                aria-label="关闭提示"
                onClick={() => setError("")}
              >
                <X size={15} />
              </button>
              {!ready && (
                <button onClick={() => window.location.reload()}>重试</button>
              )}
            </div>
          )}
          {config && (!config.llm_configured || !config.amap_configured) && (
            <div className="config-note">
              <CircleHelp size={14} />
              {!config.llm_configured
                ? "模型尚未配置，行程规划与图片理解暂不可用。"
                : "高德尚未配置，天气和路线查询暂不可用。"}
              <button onClick={openStatus}>查看配置</button>
            </div>
          )}
          <form
            className="composer"
            onSubmit={send}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              void addFiles(Array.from(e.dataTransfer.files));
            }}
          >
            {files.length > 0 && (
              <div className="draft-attachments">
                {files.map((f) => (
                  <div
                    className={`draft-attachment ${f.error ? "attachment-error" : ""}`}
                    key={f.key}
                  >
                    {f.busy ? (
                      <LoaderCircle size={18} className="spin" />
                    ) : f.upload?.content_type.startsWith("image/") ? (
                      <img src={`/api/uploads/${f.upload.id}`} alt="附件预览" />
                    ) : (
                      <FileText size={18} />
                    )}
                    <span>
                      <strong>{f.file.name}</strong>
                      <small>
                        {f.busy
                          ? "正在上传…"
                          : f.error || "已上传 · 发送后处理"}
                      </small>
                    </span>
                    <button
                      type="button"
                      aria-label={`移除 ${f.file.name}`}
                      onClick={() =>
                        setFiles((old) => old.filter((x) => x.key !== f.key))
                      }
                    >
                      <X size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              ref={input}
              aria-label="旅行问题"
              placeholder="想去哪里？写下你的旅行想法…"
              value={draft}
              maxLength={8000}
              rows={2}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={keyDown}
              onPaste={(e) => {
                const images = Array.from(e.clipboardData.files).filter((f) =>
                  f.type.startsWith("image/"),
                );
                if (images.length) {
                  e.preventDefault();
                  void addFiles(images);
                }
              }}
            />
            <div className="composer-tools">
              <button
                className="attach-button"
                type="button"
                disabled={!ready || files.some((f) => f.busy)}
                onClick={() => fileInput.current?.click()}
              >
                <Paperclip size={18} />
                <span>添加资料</span>
              </button>
              <span className="composer-hint">图片、文档，或直接拖到这里</span>
              <button
                className="send-button"
                aria-label="发送消息"
                type="submit"
                disabled={
                  !ready ||
                  sending ||
                  files.some((f) => f.busy || f.error) ||
                  (!draft.trim() && !files.length)
                }
              >
                {sending ? (
                  <LoaderCircle className="spin" size={19} />
                ) : (
                  <ArrowUp size={21} />
                )}
              </button>
            </div>
            <input
              ref={fileInput}
              type="file"
              className="visually-hidden"
              aria-label="选择旅行资料"
              multiple
              accept=".txt,.md,.docx,.xlsx,.jpg,.jpeg,.png,.webp"
              onChange={(e) => {
                void addFiles(Array.from(e.target.files || []));
                e.target.value = "";
              }}
            />
          </form>
          <div className="composer-footer">
            <span>重要行程与预约信息，请结合官方来源核对</span>
            <span>Enter 发送 · Shift + Enter 换行</span>
          </div>
        </div>
      </main>

      <aside className="details-panel">
        <header className="details-header">
          <span>
            <Map size={17} />
            本次旅行
          </span>
          <button
            className="icon-button"
            aria-label="关闭旅行面板"
            onClick={() => setDetails(false)}
          >
            <PanelRightClose size={18} />
          </button>
        </header>
        <ResearchPanel conversation={active} onPlan={() => { setDraft("根据研究结果规划行程"); input.current?.focus(); }} />
        <div className="detail-tabs" role="tablist" aria-label="旅行资料">
          {[
            ["trip", "行程"],
            ["reminder", "提醒"],
            ["document", "资料"],
          ].map(([key, name]) => (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={tab === key ? "active" : ""}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="detail-content">
          {tab === "trip" &&
            (context.trips.length ? (
              context.trips.map((trip) => (
                <section className="trip-card" key={trip.trip_id}>
                  <div className="trip-title">
                    <MapPin size={16} />
                    <h2>{trip.spec.destination}</h2>
                    <span>{trip.spec.day_count} 天</span>
                  </div>
                  <p className="muted">
                    {trip.spec.start_date || "日期待定"} · 版本 {trip.version}
                  </p>
                  {trip.spec.preferences?.length ? (
                    <div className="tags">
                      {trip.spec.preferences.map((p) => (
                        <span key={p}>{p}</span>
                      ))}
                    </div>
                  ) : null}
                  <div className="days">
                    {trip.plan.days.map((day) => (
                      <div className="day" key={day.day_index}>
                        <span className="day-marker">
                          {day.day_index.toString().padStart(2, "0")}
                        </span>
                        <div>
                          <h3>
                            第 {day.day_index} 天 <small>{day.date}</small>
                          </h3>
                          {day.activities.map((a, i) => (
                            <p key={i}>
                              <small>{a.period}</small>
                              {a.name ||
                                trip.sources[a.poi_id || ""]?.name ||
                                "待确认地点"}
                            </p>
                          ))}
                          <button
                            onClick={() =>
                              fill(
                                `修改行程 ${trip.trip_id} 的第${day.day_index}天：`,
                              )
                            }
                          >
                            调整这一天 <ArrowRight size={12} />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ))
            ) : (
              <div className="detail-empty">
                <div className="route-art">
                  <MapPin size={26} />
                  <span />
                  <Navigation size={24} />
                </div>
                <h2>让目的地，成为计划</h2>
                <p>
                  和我聊聊你的目的地与期待。
                  <br />
                  保存的行程会出现在这里。
                </p>
                <button onClick={() => fill(suggestions[0].text)}>
                  开始规划 <ArrowRight size={14} />
                </button>
              </div>
            ))}
          {tab === "reminder" && (
            <>
              {!context.reminders.length &&
                !context.reservations.length &&
                !context.scheduled_queries.length &&
                !context.policy_watches.length && (
                  <div className="detail-empty">
                    <Bell size={34} strokeWidth={1.2} />
                    <h2>把重要时刻交给我</h2>
                    <p>
                      预约提醒与定时查询，
                      <br />
                      都可以在对话里安排。
                    </p>
                    <button onClick={() => fill("明天提醒我预约博物馆。")}>
                      设置提醒 <ArrowRight size={14} />
                    </button>
                  </div>
                )}
              {context.reservations.map((plan) => (
                <section className="reminder-card" key={plan.code}>
                  <MapPin size={16} />
                  <div>
                    <strong>预约计划 {plan.code}</strong>
                    <small>
                      {plan.status === "draft"
                        ? "待核对草稿"
                        : label[plan.status] || plan.status}{" "}
                      · 版本 {plan.version}
                    </small>
                    {plan.items.map((item) => (
                      <div key={item.code}>
                        <p>
                          {item.name} · {item.visit_date || "日期待补充"}
                        </p>
                        {item.reminders.map((r, i) => (
                          <small key={i}>
                            {dateText(r.time)} · {label[r.status] || r.status}
                          </small>
                        ))}
                      </div>
                    ))}
                    <button onClick={() => fill("查看预约提醒")}>
                      查看预约详情
                    </button>
                  </div>
                </section>
              ))}
              {context.reminders.map((r) => (
                <section className="reminder-card" key={r.reminder_id}>
                  <Bell size={16} />
                  <div>
                    <strong>{r.title}</strong>
                    <p>{dateText(r.scheduled_at_utc)}</p>
                    <small>
                      {label[r.status] || r.status} ·{" "}
                      {label[r.delivery_status] || r.delivery_status}
                    </small>
                    {r.status === "active" && (
                      <button onClick={() => fill(`取消提醒 ${r.reminder_id}`)}>
                        取消这条提醒
                      </button>
                    )}
                  </div>
                </section>
              ))}
              {context.scheduled_queries.map((q) => (
                <section className="reminder-card" key={q.query_id}>
                  <CloudSun size={16} />
                  <div>
                    <strong>
                      定时
                      {{
                        weather: "天气",
                        forecast: "天气预报",
                        traffic: "路况",
                      }[q.kind] || "查询"}
                    </strong>
                    <p>{dateText(q.due_at)}</p>
                    <small>{label[q.status] || q.status}</small>
                    {q.status === "active" && (
                      <button
                        onClick={() => fill(`取消定时查询 ${q.query_id}`)}
                      >
                        取消查询
                      </button>
                    )}
                  </div>
                </section>
              ))}
              {context.policy_watches.map((w) => (
                <section className="reminder-card" key={w.watch_id}>
                  <Info size={16} />
                  <div>
                    <strong>{w.entity} · 规则监测</strong>
                    <p>截至 {dateText(w.ends_at)}</p>
                    <small>{label[w.status] || w.status}</small>
                    {["active", "queued"].includes(w.status) && (
                      <button
                        onClick={() => fill(`停止规则监测 ${w.watch_id}`)}
                      >
                        停止监测
                      </button>
                    )}
                  </div>
                </section>
              ))}
              <p className="detail-tip">
                关闭网页后，本地服务仍需保持运行。新通知会保存在这里，不会发送到
                QQ。
              </p>
            </>
          )}
          {tab === "document" && (
            <>
              {!context.documents.length ? (
                <div className="detail-empty">
                  <FileText size={34} strokeWidth={1.2} />
                  <h2>带上你的旅行灵感</h2>
                  <p>
                    攻略文档、行程表、预约截图，
                    <br />
                    添加到对话，让计划更有依据。
                  </p>
                  <button onClick={() => fileInput.current?.click()}>
                    添加资料 <Plus size={14} />
                  </button>
                </div>
              ) : (
                context.documents.map((d) => (
                  <section className="document-card" key={d.id}>
                    <FileText size={21} />
                    <div>
                      <strong>{d.filename}</strong>
                      <small>{dateText(d.created_at)} · 已导入</small>
                      <button
                        onClick={() =>
                          fill(`根据文档「${d.filename}」规划行程`)
                        }
                      >
                        用于行程规划 <ArrowRight size={12} />
                      </button>
                    </div>
                  </section>
                ))
              )}
              <p className="detail-tip">
                支持
                TXT、Markdown、Word、Excel。图片在对话中分析，每个文件不超过 5
                MB。
              </p>
            </>
          )}
        </div>
        <div className="details-foot">
          <Compass size={15} />
          计划可以改变，期待一直都在。
        </div>
      </aside>

      <dialog
        ref={dialog}
        className="modal"
        onCancel={(event) => {
          if (deleteSending) event.preventDefault();
          else setModal(null);
        }}
        onClick={(e) => {
          if (e.target === dialog.current && !deleteSending) setModal(null);
        }}
      >
        <div className="modal-inner">
          <header>
            <h2>
              {modal === "delete"
                ? "删除聊天会话"
                : modal === "notifications"
                  ? "旅途通知"
                  : "本地空间状态"}
            </h2>
            <button
              className="icon-button"
              aria-label="关闭窗口"
              disabled={deleteSending}
              onClick={() => setModal(null)}
            >
              <X size={19} />
            </button>
          </header>
          {modal === "delete" && (
            <div className="delete-dialog">
              <p>
                将永久删除 <strong>“{deleteTarget?.title}”</strong>{" "}
                及其关联数据。
              </p>
              <p>
                聊天记忆、附件、行程和通知会被清理；进行中的任务、未到期提醒、定时查询和规则监测将停止。
              </p>
              {deleteSummary ? (
                <dl className="delete-summary">
                  {(
                    [
                      ["消息", deleteSummary.messages],
                      ["附件", deleteSummary.attachments],
                      ["行程", deleteSummary.trips],
                      ["进行中任务", deleteSummary.active_tasks],
                      ["待处理提醒", deleteSummary.reminders],
                      ["定时查询", deleteSummary.scheduled_queries],
                      ["规则监测", deleteSummary.policy_watches],
                    ] as [string, number][]
                  ).map(([name, count]) => (
                    <div key={name}>
                      <dt>{name}</dt>
                      <dd>{count}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                !deleteError && (
                  <p className="delete-loading">
                    <LoaderCircle className="spin" size={15} />
                    正在检查关联数据…
                  </p>
                )
              )}
              <p className="delete-warning">
                此操作不可恢复。其他会话和 QQ
                数据不会被删除；其他会话仍在使用的共享文件会保留。
              </p>
              {deleteError && (
                <p className="delete-error" role="alert">
                  {deleteError}
                </p>
              )}
              <div className="delete-actions">
                <button
                  autoFocus
                  disabled={deleteSending}
                  onClick={() => setModal(null)}
                >
                  取消
                </button>
                <button
                  className="danger-button"
                  disabled={!deleteSummary || deleteSending}
                  onClick={confirmDelete}
                >
                  {deleteSending ? (
                    <>
                      <LoaderCircle size={15} className="spin" />
                      正在提交…
                    </>
                  ) : (
                    "删除并停止关联任务"
                  )}
                </button>
              </div>
            </div>
          )}
          {modal === "notifications" && (
            <div className="notice-list">
              {!notices.length && (
                <div className="detail-empty">
                  <Bell size={32} />
                  <h3>暂时没有新通知</h3>
                  <p>到期提醒和定时查询结果会保存在这里。</p>
                </div>
              )}
              {notices.map((n) => (
                <button
                  key={n.id}
                  className={`notice ${n.read_at ? "" : "unread"}`}
                  onClick={async () => {
                    try {
                      await api(`/notifications/${n.id}/read`, {
                        method: "POST",
                      });
                      choose(n.conversation_id);
                      setModal(null);
                    } catch (r) {
                      report(r);
                    }
                  }}
                >
                  <span className="notice-title">
                    {n.title}
                    <time>{dateText(n.created_at)}</time>
                  </span>
                  <span>{n.content}</span>
                </button>
              ))}
            </div>
          )}
          {modal === "status" && (
            <div className="status-content">
              <p>
                这里是你的单使用者本地空间，与 QQ 的会话和提醒数据相互独立。
              </p>
              {status ? (
                <>
                  <div className="status-row">
                    <span>模型服务</span>
                    <b className={status.llm_configured ? "good" : "warning"}>
                      {status.llm_configured ? "已配置" : "未配置"}
                    </b>
                  </div>
                  <div className="status-row">
                    <span>高德服务</span>
                    <b className={status.amap_configured ? "good" : "warning"}>
                      {status.amap_configured ? "已配置" : "未配置"}
                    </b>
                  </div>
                  {Object.entries(status.tasks).map(([name, task]) => (
                    <div className="status-row" key={name}>
                      <span>
                        {{
                          "web-inbox": "任务处理",
                          "web-outbox": "消息投递",
                          "web-reminders": "提醒调度",
                          "web-maintenance": "资料维护",
                          "web-deletions": "会话清理",
                        }[name] || name}
                      </span>
                      <b className={task.running ? "good" : "warning"}>
                        {task.running ? "运行中" : "已停止"}
                      </b>
                    </div>
                  ))}
                  <p className="detail-tip">
                    已配置表示配置项完整，不代表外部服务当前一定可用。请通过实际查询验证连接。
                  </p>
                  <SettingsPanel />
                  <details>
                    <summary>配置与数据说明</summary>
                    <p>
                      在项目配置文件中填写模型地址、模型名称、模型密钥及高德密钥，修改后重启服务。密钥仅在后端读取。
                    </p>
                    <p>
                      默认网页端口 8080，Web 数据保存在
                      data/web。电脑休眠或关闭后端时，定时任务会暂停。
                    </p>
                  </details>
                  <small>彼岸 v{status.version} · 本地网页版</small>
                </>
              ) : (
                <p>正在读取服务状态…</p>
              )}
            </div>
          )}
        </div>
      </dialog>
    </div>
  );
}
