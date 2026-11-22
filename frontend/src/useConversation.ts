import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { api, ApiError, bootstrap, emptyContext } from "./api";
import type { Bootstrap, Conversation, Context, Message, MessagePage, Notice, Upload } from "./api";

export type DraftFile = {
  key: string;
  file: File;
  upload?: Upload;
  error?: string;
  busy: boolean;
};

export function useConversation(setSidebar: Dispatch<SetStateAction<boolean>>) {
  const [config, setConfig] = useState<Bootstrap | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState("");
  const activeRef = useRef("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [context, setContext] = useState<Context>(emptyContext);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [noticeToast, setNoticeToast] = useState<Notice | null>(null);
  const [draft, setDraft] = useState("");
  const [quotedMessage, setQuotedMessage] = useState<{ id: string; label: string } | null>(null);
  const [files, setFiles] = useState<DraftFile[]>([]);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(true);
  const [ready, setReady] = useState(false);
  const deletedIds = useRef(new Set<string>());
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [newMessages, setNewMessages] = useState(false);
  const feed = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const lastMessage = useRef("");
  const lastConfirmations = useRef("");
  const seenNoticeIds = useRef(new Set<number>());
  const historyLoaded = useRef(false);
  const report = (reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 410) return;
    setError(reason instanceof Error ? reason.message : "操作未完成，请重试。");
  };
  const receiveNotices = useCallback((items: Notice[]) => {
    const visible = items.filter((item) => !deletedIds.current.has(item.conversation_id));
    const fresh = visible.find(item => !item.read_at && !seenNoticeIds.current.has(item.id));
    if (fresh) setNoticeToast(fresh);
    visible.forEach(item => seenNoticeIds.current.add(item.id));
    setNotices(visible);
  }, []);
  const forgetConversation = useCallback((identity: string) => {
    if (typeof identity !== "string" || !identity) return;
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
      setQuotedMessage(null);
      setRenaming(false);
      setHasMore(false);
      setNewMessages(false);
      setError("");
      lastMessage.current = "";
      lastConfirmations.current = "";
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
    setQuotedMessage(null);
    setSidebar(false);
    setError("");
    setHasMore(false);
    setRenaming(false);
    nearBottom.current = true;
    lastMessage.current = "";
    lastConfirmations.current = "";
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
    receiveNotices(notifications);
    setOnline(true);
    setError((old) => (old.startsWith("暂时连接不到本地服务") ? "" : old));
    if (!historyLoaded.current) {
      setHasMore(page.has_more);
      setHistoryCursor(page.before);
    }
    const lastId = page.items.at(-1)?.id || "";
    const confirmationsKey=ctx.confirmations.map(item=>`${item.id}:${item.version}:${item.can_confirm}`).join("|");
    if (lastId !== lastMessage.current || confirmationsKey!==lastConfirmations.current && !!ctx.confirmations.length) {
      if (nearBottom.current)
        requestAnimationFrame(() => {
          if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
        });
      else setNewMessages(true);
      lastMessage.current = lastId;
    }
    lastConfirmations.current=confirmationsKey;
    return ctx.jobs.length > 0;
  }, [receiveNotices]);
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
    if (!ready || active) return;
    const update = () =>
      api<Notice[]>("/notifications")
        .then(receiveNotices)
        .catch(report);
    void update();
    const timer = setInterval(update, 15000);
    return () => clearInterval(timer);
  }, [ready, active, receiveNotices]);
  useEffect(() => {
    if (active && !deletedIds.current.has(active))
      localStorage.setItem(`travel-draft:${active}`, draft);
  }, [active, draft]);
  useEffect(() => {
    if (!noticeToast) return;
    const timer = setTimeout(() => setNoticeToast(null), 10000);
    return () => clearTimeout(timer);
  }, [noticeToast]);
  async function ensureConversation() {
    if (activeRef.current) return activeRef.current;
    const created = await api<Conversation>("/conversations", {
      method: "POST",
    });
    setConversations((old) => [created, ...old]);
    choose(created.id);
    return created.id;
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
  return { config, conversations, setConversations, active, activeRef, messages, context, notices, noticeToast, setNoticeToast, draft, setDraft, quotedMessage, setQuotedMessage, files, setFiles, error, setError, online, ready, deletedIds, renaming, setRenaming, titleDraft, setTitleDraft, hasMore, newMessages, setNewMessages, feed, nearBottom, report, forgetConversation, choose, refresh, ensureConversation, loadOlder };
}

export type ConversationState = ReturnType<typeof useConversation>;
