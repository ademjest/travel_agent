import type { FactRow, TripDiff } from './api';
import { useState } from 'react';

export function TripStatePanel({facts, draft, onEdit}: {facts: FactRow[]; draft?: FactRow[]; onEdit?: (field:string,value:string)=>Promise<void>}) {
  const [budget,setBudget]=useState('');
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  async function edit(field:string,value:string) {
    setBusy(true);setMessage('');
    try { await onEdit?.(field,value);setMessage('已提交修改要求，请核对生成的预览后确认。'); }
    catch (error) { setMessage(error instanceof Error ? error.message : '操作未完成，请刷新后重试。'); }
    finally { setBusy(false); }
  }
  const pending = new Map((draft || []).map(row => [row.field, row]));
  const rows = [...facts, ...(draft || []).filter(row => !facts.some(old => old.field === row.field))];
  if (!rows.length && !onEdit) return null;
  return <section aria-label="当前旅行需求">
    <h3>当前需求</h3>
    {rows.map(row => {
      const saved = facts.find(old => old.field === row.field);
      const next = pending.get(row.field);
      return <div key={row.field}>
        <strong>{row.label}</strong>：{saved?.value || '未明确'}
        {next && next.value !== saved?.value && <p>待确认：{next.value}（尚未保存）</p>}
        <details><summary>查看依据</summary><p>{next?.source || row.source}</p></details>
      </div>;
    })}
    {onEdit && <details><summary>调整需求</summary>
      <form onSubmit={event=>{event.preventDefault();void edit('hotel_budget',budget);}}>
        <label>住宿预算（人民币/间/晚）<input required pattern="[1-9][0-9]{0,5}(-[1-9][0-9]{0,5})?" value={budget} onChange={event=>setBudget(event.target.value)} placeholder="例如 320 或 300-400" /></label>
        <button disabled={busy} type="submit">生成预算调整预览</button>
      </form>
      <button disabled={busy} onClick={()=>void edit('night','false')}>调整为不夜游</button>
      {message && <p role="status">{message}</p>}
    </details>}
  </section>;
}

export function TripChangeDiff({diff}: {diff: TripDiff}) {
  return <section aria-label="本次累计变更">
    <h3>本次累计变更</h3>
    <p>相对已保存版本 {diff.base_version}</p>
    <ul>{diff.changes.map(row => <li key={row.field}>{row.label}：{row.before} → {row.after}</li>)}</ul>
    <p>活动安排：{diff.changed_days.length ? `第${diff.changed_days.join('、')}天变化` : '保持不变'}</p>
    <p>酒店候选：{diff.hotel_changed ? '已变化' : '保持不变'}</p>
    <p>活动间路线证据：{diff.routes_changed ? '有变化' : '未记录变化'}</p>
    <p>关联提醒与待核实事项请展开完整预览核对。</p>
    <p>{diff.price_note}</p>
  </section>;
}
