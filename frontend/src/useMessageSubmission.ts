import { useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, RefObject } from "react";
import { api } from "./api";
import type { Upload,DraftAction } from "./api";
import type { ConversationState } from "./useConversation";

export function useMessageSubmission(conversation: ConversationState, input: RefObject<HTMLTextAreaElement | null>) {
  const { active, activeRef, draft, setDraft, quotedMessage, setQuotedMessage, files, setFiles, setError, ready, deletedIds, nearBottom, report, refresh, ensureConversation } = conversation;
  const [sending, setSending] = useState(false);
  const request = useRef<{ signature: string; id: string } | null>(null);
  const draftRequests=useRef(new Map<string,{id:string;busy:boolean}>());
  const [draftSending,setDraftSending]=useState<Record<string,boolean>>({});
  async function actDraft(id:string,version:number,action:DraftAction) {
    const identity=activeRef.current,key=`${identity}:${id}:${version}:${action}`;
    const previous=draftRequests.current.get(key);
    if(previous?.busy || !identity)return;
    const entry={id:previous?.id || crypto.randomUUID(),busy:true};draftRequests.current.set(key,entry);
    setDraftSending(old=>({...old,[key]:true}));
    try {
      await api(`/conversations/${identity}/drafts/${id}/actions`,{method:"POST",body:JSON.stringify({action,version,client_request_id:entry.id})});
      await refresh(identity);draftRequests.current.delete(key);
    } catch(reason) {
      if(activeRef.current===identity && !deletedIds.current.has(identity))report(reason);
      await refresh(identity).catch(()=>{});
    } finally {
      entry.busy=false;setDraftSending(old=>{const next={...old};delete next[key];return next;});
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
        ...(quotedMessage ? { quoted_message_id: quotedMessage.id } : {}),
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
        setQuotedMessage(null);
        nearBottom.current = true;
      }
      request.current = null;
      await refresh(identity);
    } catch (reason) {
      if (activeRef.current === sendingConversation && !deletedIds.current.has(sendingConversation)) {
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
      if (activeRef.current !== identity || deletedIds.current.has(identity)) return;
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
  return { sending, send, addFiles, keyDown, cancel, confirm,actDraft,draftSending };
}
