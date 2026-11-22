// @vitest-environment jsdom
import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,it} from 'vitest';
import {TripStatePanel,TripChangeDiff} from './TripStatePanel';
afterEach(cleanup);
it('shows saved requirements without sidebar editing controls or evidence',()=>{
  render(<TripStatePanel facts={[{field:'budget',label:'住宿预算',value:'420元/间/晚',source:'旧要求'}]}/>);
  expect(screen.getByText(/420元/)).toBeTruthy();
  expect(screen.queryByText('调整需求')).toBeNull();
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(screen.queryByRole('button')).toBeNull();
  expect(screen.queryByText('查看依据')).toBeNull();
  expect(screen.queryByText('旧要求')).toBeNull();
});
it('does not show an empty preferences panel',()=>{
  render(<TripStatePanel facts={[]}/>);
  expect(screen.queryByRole('region',{name:'当前旅行需求'})).toBeNull();
});
it('shows cumulative base version and unverified prices',()=>{
  render(<TripChangeDiff diff={{base_version:3,changes:[{field:'budget',label:'预算',before:'420',after:'320'}],
    changed_days:[],hotel_changed:false,routes_changed:false,price_note:'房价尚未核实'}}/>);
  expect(screen.getByText('相对已保存版本 3')).toBeTruthy();
  expect(screen.getByText('预算：420 → 320')).toBeTruthy();
  expect(screen.getByText('房价尚未核实')).toBeTruthy();
});
