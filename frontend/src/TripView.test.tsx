// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {TripView} from './TripView';
import type {Trip,RailTrain} from './api';
afterEach(cleanup);
it('shows server-projected unverified price evidence without making a query',()=>{
 const fill=vi.fn();
 const value:Trip={trip_id:'reference',version:1,spec:{destination:'测试城'},sources:{},plan:{days:[],hotel_quotes:{
  offers:[],retrieved_at:'2030-01-01T08:00:00Z',result_status:'reference_only',reference_text:'参考房型：210 CNY/间/晚；门店未核实，不用于预算通过。'}}};
 render(<TripView trips={[value]} onFill={fill} onStart={()=>{}}/>);
 expect(screen.getByText(/210 CNY/)).toBeTruthy();expect(screen.queryByText(/本批没有可比较报价/)).toBeNull();expect(fill).not.toHaveBeenCalled();
});
it('distinguishes a missing query from a failed request without a false timestamp',()=>{
 const value:Trip={trip_id:'pending',version:1,spec:{destination:'测试城'},sources:{},
  plan:{days:[],hotel_quotes:{offers:[],note:'请一并补充房间数、成人数。'}}};
 const {rerender}=render(<TripView trips={[value]} onFill={()=>{}} onStart={()=>{}}/>);
 expect(screen.getByText('住宿报价尚未查询')).toBeTruthy();
 expect(screen.queryByText(/获取时间未知|本批没有可比较报价/)).toBeNull();
 value.plan.hotel_quotes!.request_id='attempted';
 rerender(<TripView trips={[value]} onFill={()=>{}} onStart={()=>{}}/>);
 expect(screen.getByText('报价查询未取得可用结果')).toBeTruthy();
});
it('expanding hotel quote details is local and refresh requires an explicit message',()=>{
 const fill=vi.fn();
 const value:Trip={trip_id:'quote',version:1,spec:{destination:'测试城'},sources:{h:{name:'云泽酒店'}},
  plan:{days:[],hotel_ids:['h'],hotel_quotes:{retrieved_at:'2030-01-01T08:00:00Z',offers:[{
   offer_id:'q',amap_poi_id:'h',hotel_name:'云泽酒店',room_name:'双床房',currency:'CNY',
   nightly_prices:[{date:'2030-01-07',amount:'399'},{date:'2030-01-08',amount:'599'}],
   taxes_and_fees_status:'unknown',applicability_status:'applicable',status:'excluded',issues:['已列房费高于预算上限']}]}}};
 render(<TripView trips={[value]} onFill={fill} onStart={()=>{}}/>);
 fireEvent.click(screen.getByText('云泽酒店 · 双床房 · 查看价格详情'));
 expect(fill).not.toHaveBeenCalled();expect(screen.getByText('2030-01-08：599 CNY/间/晚')).toBeTruthy();
 expect(screen.getByText('已列产品不符合预算')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'刷新报价（联网查询）'}));
 expect(fill).toHaveBeenCalledWith(expect.stringContaining('只查价，不修改或保存'));
});
it('separates pending hotel prices from budget candidates',()=>{
 const value:Trip={trip_id:'hotel',version:1,spec:{destination:'测试城'},sources:{h:{name:'如家酒店'}},
  plan:{days:[],hotel_ids:['h'],hotel_recommendations:[{poi_id:'h',status:'partial',price_status:'unknown'}]}};
 render(<TripView trips={[value]} onFill={()=>{}} onStart={()=>{}}/>);
 expect(screen.getByText('住宿候选：预算尚未核实，暂不能确认具体酒店')).toBeTruthy();
 expect(screen.getByText('待核价（未确认符合预算）：如家酒店')).toBeTruthy();
});
it('keeps an old over-budget identity visible without recommending it',()=>{
 const value:Trip={trip_id:'hotel',version:1,spec:{destination:'测试城'},sources:{h:{name:'超预算旧酒店'}},
  plan:{days:[],hotel_ids:['h'],hotel_review_note:'请重新核对住宿候选',
   hotel_recommendations:[{poi_id:'h',status:'conflict',price_status:'reference_over_budget'}]}};
 render(<TripView trips={[value]} onFill={()=>{}} onStart={()=>{}}/>);
 expect(screen.getByText('未通过当前筛选的原候选：超预算旧酒店')).toBeTruthy();
 expect(screen.queryByText('住宿候选：超预算旧酒店')).toBeNull();
 expect(screen.getByRole('status').textContent).toContain('重新核对');
});
it('shows a comparable reference-price candidate separately from pending prices',()=>{
 const value:Trip={trip_id:'hotel',version:1,spec:{destination:'测试城'},sources:{h:{name:'参考房价酒店'},u:{name:'未知价格酒店'}},
  plan:{days:[],hotel_ids:['h','u'],hotel_recommendations:[{poi_id:'h',status:'partial',price_status:'reference_in_budget'},
   {poi_id:'u',status:'partial',price_status:'unknown'}]}};
 render(<TripView trips={[value]} onFill={()=>{}} onStart={()=>{}}/>);
 expect(screen.getByText('住宿候选：参考房价酒店')).toBeTruthy();
 expect(screen.getByText('待核价（未确认符合预算）：未知价格酒店')).toBeTruthy();
});
const train:RailTrain={train_code:'G1',origin_station:'甲城南',destination_station:'乙城南',
  departure_at:'2026-11-01T08:00+08:00',arrival_at:'2026-11-01T10:00+08:00',duration_minutes:120,
  seats:[{name:'二等座',price_cny:100,availability:'available'}]};
const trip:Trip={trip_id:'TR-test',version:1,is_preview:true,spec:{destination:'乙城',day_count:2,start_date:'2026-11-01'},
 sources:{p1:{name:'城市公园'}},plan:{days:[{day_index:1,date:'2026-11-01',kind:'transport',activities:[]},
 {day_index:2,date:'2026-11-02',activities:[{poi_id:'p1',period:'上午'}]}],
 intercity:{completeness:'partial',day_count_basis:'travel_included',basis_source:'default',
 outbound:{status:'completed',selected:train,selected_seat:{name:'二等座',price_cny:100,availability:'available'},request:{mode_source:'default'},queried_at:'2026-10-29T00:00:00Z'},
 inbound:{status:'failed',missing_fields:[]},ticket_cost:{total_cny:null,unknown_segments:1}}}};
it('shows outbound before touring and inbound afterwards without claiming tickets are bought',()=>{
 const fill=vi.fn();const {container}=render(<TripView trips={[trip]} onFill={fill} onStart={vi.fn()}/>);
 const text=container.textContent || '';
 expect(text.indexOf('去程交通')).toBeLessThan(text.indexOf('城市公园'));
 expect(text.indexOf('城市公园')).toBeLessThan(text.indexOf('返程交通'));
 expect(screen.getByText(/草稿预览，尚未保存/)).toBeTruthy();
 expect(screen.getByText(/交通日，预留给往返/)).toBeTruthy();
 expect(screen.getByText(/部分票价未知/)).toBeTruthy();
 expect(screen.getByText(/二等座/)).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'调整返程'}));
 expect(fill).toHaveBeenCalledWith(expect.stringContaining('返程改为'));
});
it('reads a legacy trip without traffic placeholders or refresh side effects',()=>{
 const legacy=structuredClone(trip);delete legacy.plan.intercity;delete legacy.is_preview;
 render(<TripView trips={[legacy]} onFill={vi.fn()} onStart={vi.fn()}/>);
 expect(screen.queryByLabelText('去程交通')).toBeNull();
 expect(screen.queryByText(/部分票价未知/)).toBeNull();
});

const base:Trip={trip_id:'trip',version:1,spec:{destination:'南京',day_count:3},plan:{days:[]},sources:{}};
it('distinguishes a bounded calendar from the original requested duration',()=>{
  const trip:Trip={...base,spec:{...base.spec,requested_day_count:4,
    duration_adjustment:'原天数要求4天；按明确起止时间安排3个自然日。',
    planning_window:{start:'2026-10-30',end:'2026-11-01',start_text:'10月30日下午',end_text:'11月1日下午'}}};
  render(<TripView trips={[trip]} onFill={()=>{}} onStart={()=>{}}/>);
  expect(screen.getByText('3 个自然日')).toBeTruthy();
  expect(screen.getByText(/首末日按可用时段/)).toBeTruthy();
  expect(screen.getByRole('status').textContent).toContain('按明确起止时间');
});
it('keeps historical trips readable without new temporal fields',()=>{
  render(<TripView trips={[base]} onFill={()=>{}} onStart={()=>{}}/>);
  expect(screen.getByText('3 天')).toBeTruthy();
  expect(screen.queryByRole('status')).toBeNull();
});
