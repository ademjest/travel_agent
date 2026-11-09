import type { FactRow, TripDiff } from './api';

export function TripStatePanel({facts}: {facts: FactRow[]}) {
  if (!facts.length) return null;
  return <section aria-label="当前旅行需求">
    <details className="trip-preferences">
    <summary>旅行偏好与预算</summary>
    {facts.map(row => <div key={row.field}>
      <strong>{row.label}</strong>：{row.value || '未明确'}
    </div>)}
    </details>
  </section>;
}

export function TripChangeDiff({diff}: {diff: TripDiff}) {
  return <section aria-label="本次累计变更">
    <h3>本次累计变更</h3>
    <p>相对已保存版本 {diff.base_version}</p>
    <ul>{diff.changes.filter(row=>!(["未明确","未指定","已清除"].includes(row.before) && ["未明确","未指定","已清除"].includes(row.after))).map(row => <li key={row.field}>{row.label}：{row.before} → {row.after}</li>)}</ul>
    <p>活动安排：{diff.changed_days.length ? `第${diff.changed_days.join('、')}天变化` : '保持不变'}</p>
    <p>酒店候选：{diff.hotel_changed ? '已变化' : '保持不变'}</p>
    <p>活动间路线证据：{diff.routes_changed ? '有变化' : '未记录变化'}</p>
    <p>关联提醒与待核实事项请展开完整预览核对。</p>
    <p>{diff.price_note}</p>
  </section>;
}
