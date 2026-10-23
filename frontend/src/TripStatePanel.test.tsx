// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {TripStatePanel,TripChangeDiff} from './TripStatePanel';
afterEach(cleanup);
it('distinguishes saved budget from pending budget and its source',()=>{
  render(<TripStatePanel facts={[{field:'budget',label:'住宿预算',value:'420元/间/晚',source:'旧要求'}]}
    draft={[{field:'budget',label:'住宿预算',value:'320元/间/晚',source:'用户更正'}]}/>);
  expect(screen.getByText(/420元/)).toBeTruthy();
  expect(screen.getByText(/待确认：320元\/间\/晚（尚未保存）/)).toBeTruthy();
  expect(screen.queryByText('查看依据')).toBeNull();
  expect(screen.queryByText('用户更正')).toBeNull();
});
it('submits a typed budget operation and never says it was saved',async()=>{
  const edit=vi.fn().mockResolvedValue(undefined);
  render(<TripStatePanel facts={[]} onEdit={edit}/>);
  fireEvent.change(screen.getByLabelText('住宿预算（人民币/间/晚）'),{target:{value:'320'}});
  fireEvent.click(screen.getByText('生成预算调整预览'));
  await waitFor(()=>expect(edit).toHaveBeenCalledWith('hotel_budget','320'));
  expect(screen.getByRole('status').textContent).toContain('核对');
});
it('shows cumulative base version and unverified prices',()=>{
  render(<TripChangeDiff diff={{base_version:3,changes:[{field:'budget',label:'预算',before:'420',after:'320'}],
    changed_days:[],hotel_changed:false,routes_changed:false,price_note:'房价尚未核实'}}/>);
  expect(screen.getByText('相对已保存版本 3')).toBeTruthy();
  expect(screen.getByText('预算：420 → 320')).toBeTruthy();
  expect(screen.getByText('房价尚未核实')).toBeTruthy();
});
