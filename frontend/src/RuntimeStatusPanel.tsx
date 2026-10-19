import { useEffect, useState } from "react";
import { api } from "./api";

declare const __FRONTEND_BUILD_ID__: string;
const states: Record<string, string> = {
  disabled: "未启用", configured: "配置有效", dependency_missing: "启动环境缺少依赖",
  invalid_configuration: "配置或模型文件无效", not_loaded: "尚未加载", loading: "正在加载",
  ready: "就绪", failed: "失败", empty: "没有资料", building: "构建中", partial: "部分就绪",
  awaiting_check: "等待后台检查", unknown: "尚未确认",
};
export type RetrievalStatus = {
  configuration?: { state: string; error_type?: string };
  encoder?: { state: string; error_type?: string };
  index?: { state: string; total?: number; indexed?: number; pending?: number; checked_at?: string; error_type?: string };
};
export function RetrievalStatusView({ status }: { status: RetrievalStatus }) {
  return <div className="retrieval-status">
    <p>语义检索：{states[status.configuration?.state || "unknown"] || "尚未确认"}</p>
    {status.encoder && <p>模型加载：{states[status.encoder.state] || "尚未确认"}</p>}
    {status.index && <p>本会话索引：{states[status.index.state] || "尚未确认"}
      {status.index.total != null && ` · 已索引 ${status.index.indexed ?? 0}/${status.index.total}，待处理 ${status.index.pending ?? 0}`}
      {status.index.checked_at && ` · 状态时间 ${status.index.checked_at}`}
      {status.index.error_type && ` · 错误类别 ${status.index.error_type}`}</p>}
  </div>;
}
export function RuntimeStatusPanel() {
  const [value, setValue] = useState<{ backend_id: string; model: string; retrieval: { state: string } } | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    api<{ backend_id: string; model: string; retrieval: { state: string } }>("/runtime-status")
      .then((v) => { if (active && typeof v.backend_id === "string") setValue(v); else if (active) setError(true); })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, []);
  return <section aria-label="当前运行版本">
    <p>网页版本：{__FRONTEND_BUILD_ID__}</p>
    {error && <p>后端版本暂未确认；若刚升级网页，请重启新版后端后刷新。</p>}
    {value && <><p>后端版本：{value.backend_id} · 默认模型：{value.model || "未配置"}</p>
      <RetrievalStatusView status={{ configuration: value.retrieval }} /></>}
  </section>;
}
