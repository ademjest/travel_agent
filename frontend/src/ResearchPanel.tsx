import { useEffect, useState } from "react";
import { api } from "./api";

interface Research { stage: string; updated_at: string; report: string; source_count: number }
export function ResearchPanel({ conversation, onPlan }: { conversation: string; onPlan: () => void }) {
  const [rows, setRows] = useState<Research[]>([]);
  useEffect(() => {
    let active = true;
    setRows([]);
    if (!conversation) return;
    const update = () => { void api<Research[]>(`/conversations/${conversation}/research`).then(v => { if (active) setRows(v); }).catch(() => {}); };
    update(); const timer = setInterval(update, 4000);
    return () => { active = false; clearInterval(timer); };
  }, [conversation]);
  if (!rows.length) return <p className="detail-tip">可发送“帮我搜索武汉三日游攻略”。搜索和网页资料仅保存在当前会话。</p>;
  const row = rows[0];
  return <section className="service-settings"><h3>当前会话研究</h3>
    <p role="status">{{ searching: "查找来源", reading: "阅读页面", synthesizing: "整理结果", completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已取消", blocked: "已停止" }[row.stage] || row.stage} · {row.source_count} 个来源</p>
    <small>详细结论、检索时间及引用链接见聊天回复。</small>
    {row.report && ["completed", "partial"].includes(row.stage) && <button onClick={onPlan}>填入基于研究结果的规划请求</button>}
  </section>;
}
