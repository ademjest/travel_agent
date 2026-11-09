// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,within} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import {ModelSettings} from './ModelSettings';
import type {ModelConfigStatus,ModelConfigInput} from './ModelSettings';

afterEach(cleanup);
const value:ModelConfigStatus={schema_version:1,valid:true,legacy:true,configured:true,source:'legacy',pending_restart:false,
 connections:[{id:'old',name:'原连接',base_url:'https://example.com/v1',key_configured:true,protocol:'openai_chat_completions'}],
 profiles:[{id:'first',name:'常用',connection_id:'old',model_id:'shared',kind:'chat',capabilities:['text','json','tools'],
   token_parameter:'max_completion_tokens',temperature:'allow',pricing:{currency:'USD'}},
   {id:'second',name:'摘要专用',connection_id:'old',model_id:'summary',kind:'chat',capabilities:['text','json'],
   token_parameter:'max_tokens',temperature:'omit',pricing:{currency:'CNY'}}],
 roles:{regular:'first'},purpose_overrides:{conversation_summary:'second'},cost_limits:{}};

it('preserves legacy overrides and never fills saved keys into the form',()=>{
 const change=vi.fn();render(<ModelSettings value={value} revision="one" disabled={false} unsaved={false} onChange={change} onProbe={vi.fn()}/>);
 expect((screen.getByLabelText(/连接 Key/) as HTMLInputElement).value).toBe('');
 expect((screen.getByLabelText('conversation_summary') as HTMLSelectElement).value).toBe('second');
 expect((screen.getByLabelText('基础任务') as HTMLSelectElement).value).toBe('');
 expect(screen.getByText(/尚未迁移/)).toBeTruthy();
});

it('distinguishes retain, clear, undo and replacement without browser persistence',()=>{
 const change=vi.fn();const view=render(<ModelSettings value={value} revision="one" disabled={false} unsaved={false} onChange={change} onProbe={vi.fn()}/>);
 fireEvent.click(screen.getByText('清除连接 Key'));
 expect((change.mock.lastCall![0] as ModelConfigInput).connections[0].api_key).toBeNull();
 fireEvent.click(screen.getByText('撤销清除 Key'));
 expect((change.mock.lastCall![0] as ModelConfigInput).connections[0].api_key).toBe('');
 fireEvent.change(screen.getByLabelText(/连接 Key/),{target:{value:'new-secret'}});
 expect((change.mock.lastCall![0] as ModelConfigInput).connections[0].api_key).toBe('new-secret');
 expect(localStorage.length).toBe(0);expect(sessionStorage.length).toBe(0);
 view.rerender(<ModelSettings value={structuredClone(value)} revision="one" disabled={false} unsaved={false} onChange={change} onProbe={vi.fn()}/>);
 expect((screen.getByLabelText(/连接 Key/) as HTMLInputElement).value).toBe('');
});

it('requires explicit saved-profile capability probes and keeps model roles separate',()=>{
 const probe=vi.fn(),change=vi.fn();const view=render(<ModelSettings value={value} revision="one" disabled={false} unsaved={false} onChange={change} onProbe={probe}/>);
 expect(probe).not.toHaveBeenCalled();
 fireEvent.click(within(screen.getByRole('group',{name:'模型 常用'})).getByText('测试工具调用'));
 expect(probe).toHaveBeenCalledWith('first','tools');
 fireEvent.change(screen.getByLabelText('复杂任务'),{target:{value:'second'}});
 expect((change.mock.lastCall![0] as ModelConfigInput).roles.complex).toBe('second');
 expect((change.mock.lastCall![0] as ModelConfigInput).roles.regular).toBe('first');
 view.rerender(<ModelSettings value={value} revision="one" disabled={false} unsaved={true} onChange={change} onProbe={probe}/>);
 expect(within(screen.getByRole('group',{name:'模型 常用'})).getByText('测试工具调用').hasAttribute('disabled')).toBe(true);
});

it('prevents deleting referenced connections and models while keeping currencies independent',()=>{
 render(<ModelSettings value={value} revision="one" disabled={false} unsaved={false} onChange={vi.fn()} onProbe={vi.fn()}/>);
 expect(screen.getByText('删除连接').hasAttribute('disabled')).toBe(true);
 expect(screen.getAllByText('删除模型').every(b=>b.hasAttribute('disabled'))).toBe(true);
 expect(screen.getByLabelText('USD 日上限')).toBeTruthy();expect(screen.getByLabelText('CNY 日上限')).toBeTruthy();
 expect((screen.getByLabelText('图片理解') as HTMLSelectElement).options.length).toBe(1);
});

it('clears stale prices when the actual model changes',()=>{
 const configured=structuredClone(value);configured.profiles[0].pricing={input:2,output:10,currency:'USD'};
 const change=vi.fn();render(<ModelSettings value={configured} revision="one" disabled={false} unsaved={false} onChange={change} onProbe={vi.fn()}/>);
 fireEvent.change(within(screen.getByRole('group',{name:'模型 常用'})).getByLabelText('实际模型 ID'),{target:{value:'another-model'}});
 expect((change.mock.lastCall![0] as ModelConfigInput).profiles[0].pricing).toEqual({currency:'USD'});
});
