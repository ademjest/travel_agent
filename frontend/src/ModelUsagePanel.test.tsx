// @vitest-environment jsdom
import {cleanup,render,screen,within} from '@testing-library/react';
import {afterEach,expect,it} from 'vitest';
import {ModelIdentity,ModelUsagePanel} from './ModelUsagePanel';
import type {ModelUsage} from './ModelUsagePanel';
afterEach(cleanup);
const usage:ModelUsage={totals:{calls:3,prompt_tokens:1000,cached_input_tokens:0,completion_tokens:100,
 estimated_cost:.01,cost_unknown:1,unknown_usage:1,cache_detail_missing:1,currency:'USD'},
 purposes:[{purpose:'intent_parse',model_id:'gpt-example',profile_name:'gpt-example',connection_id:'legacy_unknown',connection_name:'',
 calls:3,prompt_tokens:1000,cached_input_tokens:0,completion_tokens:100,estimated_cost:.01,cost_unknown:1,cache_detail_missing:1}],
 currency:'USD',cost_note:'费用为本地估算。',budget:{tier:'economy',daily_limit:0,monthly_limit:20000,day_reserved:200,month_reserved:1000,
 day_actual_tokens:100,day_inflight:100,day_cutover:'2026-10-10T00:00:00Z',month_cutover:'2026-11-01T00:00:00Z'}};

it('shows a single model ID and only meaningful confirmed labels',()=>{
 const {rerender}=render(<ModelIdentity row={usage.purposes[0]}/>);
 expect(screen.getAllByText('gpt-example')).toHaveLength(1);
 expect(screen.queryByText(/历史归属未确认/)).toBeNull();
 rerender(<ModelIdentity row={{model_id:'gpt-example',profile_name:'日常',connection_id:'provider',connection_name:'供应商'}}/>);
 expect(screen.getByText('供应商 · 日常')).toBeTruthy();expect(screen.getAllByText('gpt-example')).toHaveLength(1);
});
it('separates summaries, budget explanation and readable usage columns',()=>{
 const {container}=render(<ModelUsagePanel usage={usage}/>);
 expect(screen.getByText('1,100')).toBeTruthy();expect(screen.getByText('经济输出档位')).toBeTruthy();
 expect(screen.getByRole('columnheader',{name:'调用次数'})).toBeTruthy();
 expect(screen.getByText('缓存 0 · 1次未提供')).toBeTruthy();
 const detail=screen.getByText('预算与统计说明').closest('details')!;
 expect(detail.open).toBe(false);expect(detail.textContent).toContain('未保存连接或档案身份');
 expect(screen.getByRole('table').textContent).not.toContain('历史归属未确认');
 expect(within(screen.getByRole('table')).getAllByText('gpt-example')).toHaveLength(1);
 expect(container.querySelectorAll('.model-usage-summary > div')).toHaveLength(3);
});
it('keeps multi-currency amounts separate and unknown costs explicit',()=>{
 const mixed=structuredClone(usage);mixed.totals.cost_breakdown=[{currency:'USD',amount:.01},{currency:'CNY',amount:.02}];
 render(<ModelUsagePanel usage={mixed}/>);
 expect(screen.getByText('0.010000 USD；0.020000 CNY')).toBeTruthy();
 expect(screen.getAllByText('1 次无法估算').length).toBeGreaterThan(0);
});
