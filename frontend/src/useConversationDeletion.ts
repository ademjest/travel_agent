import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { api } from "./api";
import type { Conversation, DeletionJob, DeletionSummary } from "./api";
import type { ConversationState } from "./useConversation";
import type { Modal } from "./ConversationDialogs";
import { forgetResearchView } from "./ResearchPanel";

export function useConversationDeletion(conversation: ConversationState, modal: Modal, setModal: Dispatch<SetStateAction<Modal>>) {
  const { ready, report } = conversation;
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);
  const deleteTargetRef = useRef("");
  const [deleteSummary, setDeleteSummary] = useState<DeletionSummary | null>(
    null,
  );
  const [deleteError, setDeleteError] = useState("");
  const [deleteSending, setDeleteSending] = useState(false);
  const [deletionJobs, setDeletionJobs] = useState<DeletionJob[]>([]);
  const deletionJobsRef = useRef<DeletionJob[]>([]);
  const deletionChannel = useRef<BroadcastChannel | null>(null);
  const forgetConversation = useCallback((identity: string) => {
    if (typeof identity !== "string" || !identity) return;
    if (deleteTargetRef.current === identity) {
      deleteTargetRef.current = "";
      setDeleteTarget(null);
      setDeleteSummary(null);
      setModal(null);
    }
    forgetResearchView(identity);
    conversation.forgetConversation(identity);
  }, [conversation.forgetConversation, setModal]);
  useEffect(() => {
    if (modal !== "delete") deleteTargetRef.current = "";
  }, [modal]);
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
  return { deleteTarget, deleteSummary, deleteError, deleteSending, deletionJobs, openDelete, confirmDelete, retryDeletion, dismissDeletion };
}
