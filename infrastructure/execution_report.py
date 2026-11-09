"""Read existing diagnostic metadata and explain it without replaying a request."""
from contextlib import closing
import json
import re
from pathlib import Path
import sqlite3

from infrastructure.execution_diagnostics import metadata


STAGES = {'request': '接收与处理', 'model': '模型调用', 'summary': '会话摘要',
          'context': '准备回答资料', 'tool': '工具查询', 'delivery': '回复投递', 'retrieval': '资料检索',
          'routing':'需求分流','planning':'行程规划','draft_action':'草案操作'}
PURPOSES = {'intent_parse': '理解本次意图', 'conversation_summary': '更新会话摘要',
            'tool_reasoning': '分析并回答', 'tool_answer': '整理查询结果',
            'research_synthesis': '整理研究结果', 'research_targets':'提取原方案研究对象',
            'hotel_quote_extract':'整理酒店参考报价',
            'research_fact_synthesis':'核验景点事实', 'travel_answer_write':'整理旅行回答', 'trip_plan': '生成行程'}
PURPOSES['trip_plan_compare']='比较与融合独立行程草案'
ERRORS = {'rate_limit': '上游服务限流', 'temporary_upstream': '上游服务暂时不可用',
          'rate_limited': '上游服务限流', 'network_error': '网络连接未成功',
          'unauthorized': '服务认证未通过', 'forbidden': '服务拒绝访问', 'not_found': '页面不存在',
          'timeout': '请求超时', 'permission_denied': '没有所需权限',
          'invalid_argument': '请求参数不符合要求', 'not_available': '服务未配置或不可用',
          'attempt_limit': '本次请求的尝试额度已用完'}
ERRORS.update(tls_error='TLS 握手或证书校验失败', connect_error='连接未成功或连接超时', read_timeout='读取响应超时', response_limit='响应超过读取上限')


def export_timeline(database, event_key):
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        present = db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_diagnostics'").fetchone()
        rows = db.execute('SELECT * FROM execution_diagnostics WHERE event_key=? ORDER BY id',
                          (event_key,)).fetchall() if present else []
        model_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='model_calls'").fetchone()
        model_calls = db.execute('SELECT count(*) FROM model_calls WHERE event_key=?', (event_key,)).fetchone()[0] if model_table else 0
    return {'event_key': event_key, 'timeline': [
        {**{k: row[k] for k in row.keys() if k != 'details_json'}, 'details': json.loads(row['details_json'])}
        for row in rows], 'model_calls': model_calls, 'content_included': False, 'replayed': False}


def explain(row):
    stage, details = row['stage'], metadata(row['details'])
    status = details.get('status', '')
    warning = False
    if stage=='draft_action':
        action='重新核对草案' if details.get('name')=='revalidate' else '放弃草案'
        failed=status in {'failed','unknown'}
        return {'id':row['id'],'stage':stage,'title':action,'created_at':row['created_at'],
            'description':action+({'started':'已开始，正在核对指定版本。','completed':'已完成。','reused':'已复用原操作，没有重复执行。',
                'cancelled':'已取消，迟到结果不会发布。'}.get(status,'尚未完成，原内容及失败阶段已保留。' if failed else '状态待核对。')),
            'warning':failed,'details':details}
    if stage=='planning' and re.match(r'(?:amap|wendao)_plan_|plan_compare_',status):
        name='高德独立草案' if status.startswith('amap') else '问道独立草案' if status.startswith('wendao') else '草案选择与融合'
        warning=status.endswith(('failed','unknown','partial'))
        state='已完成' if status.endswith('completed') else '未执行或采用回退' if status.endswith('skipped') else '未完整完成'
        return {'id':row['id'],'stage':stage,'title':name,'created_at':row['created_at'],
                'description':name+'：'+state+'。'+('最终选择：'+details['decision_source'] if details.get('decision_source') else ''),
                'warning':warning,'details':details}
    if stage == 'routing':
        if status=='intent_failed':
            return {'id':row['id'],'stage':stage,'title':'意图解析未完成','created_at':row['created_at'],
                'description':'意图解析未完成，尚未分流执行业务；原请求已保留，无需改写已提供的信息。',
                'warning':True,'details':details}
        warning = status in {'research_rejected', 'research_needs_input', 'research_partial', 'research_scope_rejected', 'research_answer_fallback'}
        text = {'research_accepted':'研究字段已校验，开始只读研究。',
                'selected':'已按本次请求分流到对应业务。','read_selected':'只读查询范围已校验，开始执行。',
                'read_completed':'只读查询已返回，事实以本次回执为准。','read_incomplete':'只读查询仍有未完成项，未当作全部成功。',
                'intent_repair':'意图字段需修复，正在进行一次受限协议修复。',
                'research_repair':'研究核心字段需修复，正在进行一次受限重新解析。',
                'research_rejected':'研究请求未通过业务校验，本次尚未搜索。',
                'research_scope_rejected':'返回资料未能对应本次研究对象，已拦截无关内容，未进入写作。',
                'research_needs_input':'研究需要确认目标方案或日期，尚未搜索。',
                'research_partial':'研究部分完成，仍有景点或主题缺少可核验依据。',
                'research_answer_fallback':'写作未完成，已使用保存资料生成回答。',
                'research_completed':'研究已完成，具体事实仍需结合依据和适用日期核对。'}.get(status,
            '已完整识别请求，可直接分流。' if status == 'complete' else '规则未完整覆盖，保留完整请求进行意图解析。')
        if status=='selected' and details.get('route_owner')=='clarification':
            text='本次进入澄清或失败兜底；不代表已经执行业务，请结合模型记录核对。'
        if status == 'research_answer_fallback' and details.get('reason') == 'core_input_budget':
            text = '写作输入超限，已使用保存资料生成回答。'
        if details.get('ignored_fields'):
            text += ' 已忽略说明性扩展字段，未因此增加解析调用。'
    elif stage == 'planning':
        label={'intercity_outbound':'去程查询','intercity_inbound':'返程查询','transport_validation':'交通窗口校验'}.get(details.get('name'))
        text = ({'started':label+'开始。','checkpoint_reused':label+'复用保存快照，未重新查询。','completed':label+f"完成，候选{details.get('candidate_count',0)}个。"}.get(status,
                 label+'尚未完成，保留已有车次与城市草稿。') if label else {'local_recommendations':'本次仅更新相关住宿或餐饮候选，活动安排保持。',
                'routes_only':'本次仅更新交通证据。','transport_only':'本次仅调整交通并核对受影响窗口，未重新生成两版草案。'}.get(status,'本次需要整体核对行程。')
                )
        warning=status in {'failed','unavailable','needs_input','empty','incomplete','conflict','partial'}
        if details.get('name')=='trip_create' and status=='failed':
            text='首次创建尚未通过校验，没有可确认草案；需求与已取得资料已保留。'
        if details.get('error_code')=='input_projection_limit':
            limit=details.get('validation',{})
            text=f"系统组装的规划输入为{limit.get('actual','未知')}字符，超过本步骤{limit.get('max','未知')}字符上限；该步骤未发送模型请求，原行程和资料保留。"
        if details.get('name')=='transport_validation' and status=='completed':
            text='交通窗口参考已核对，未等同于逐分钟营业和预约核验。'
        if details.get('name')=='route_review':
            text=(f"市内路线仍有{details.get('remaining_count',0)}项待确认；辅助资料不足已明确标注，不代表全部路线已核验。"
                  if status=='partial' else '市内路线核对步骤完成，仍按查询时路网参考。')
    elif stage == 'summary':
        if status == 'published':
            text = '摘要已更新。'
        elif status == 'threshold_not_met':
            text = '对话尚未达到更新摘要的条件，属于正常情况。'
        elif status in {'disabled', 'not_needed'}:
            text = '本次未启用或不需要更新摘要。'
        elif status in {'validation_failed', 'decode_failed', 'output_truncated', 'model_failed', 'prompt_failed', 'publish_rejected'}:
            warning = True
            text = '摘要更新未成功；' + ('已沿用有效旧版。' if details.get('previous_reused') else '本次未发布新摘要。')
            code = details.get('validation', {}).get('code')
            if code in {'value_scope', 'number_currency_unit', 'source_mismatch'}:
                text += '正文数值、单位或引用依据未通过检查。'
        else:
            text = '摘要暂未更新，具体状态见技术详情。'
    elif stage == 'context':
        text = ('本次回答已使用会话摘要。' if details.get('summary_injected')
                else '本次回答未注入会话摘要；是否正常需结合摘要记录判断。')
        if details.get('summary_information_incomplete'):
            warning = True
            text = '尚未覆盖的新材料无法完整纳入，请重新确认当前要求；本次不采用旧摘要确认最新约束。'
    elif stage == 'tool':
        if status=='started':
            return {'id':row['id'],'stage':stage,'title':STAGES[stage],'created_at':row['created_at'],
                    'description':'只读工具查询开始。','warning':False,'details':details}
        warning = status in {'failed', 'needs_input'}
        text = {'completed': '工具已返回结果。', 'checkpoint_reused': '复用已保存的工具结果，未再次查询。',
                'needs_input': '工具需要补充信息。', 'skipped':'预算或条件不足，本项尚未执行。'}.get(status, '工具状态见技术详情。')
        if status == 'failed':
            text = ERRORS.get(details.get('error_code'), '工具查询失败，记录不足以进一步确认原因。')
        if type(details.get('attempts')) is int:
            text += f" 本条工具记录的尝试次数：{details['attempts']}。"
        if details.get('name')=='rail_query' and 'mcp_submissions' in details:
            text+=f" 逻辑查询{details.get('logical_count',0)}次；MCP实际提交{details['mcp_submissions']}次；远端内部HTTP次数未知。"
        if details.get('name') == 'official_link_discovery':
            text = f"官网入口匹配：可展示{details.get('covered_count', 0)}/{details.get('target_count', 0)}个；包括已确认官网和搜索匹配候选。入口身份不代表景点规则已核实。"
    elif stage == 'retrieval':
        warning = status in {'failed', 'unavailable'}
        text = ('本地语义检索不可用，已保留关键词检索。' if warning else
                f"语义检索返回{details.get('candidate_count', 0)}个候选；是否支持答案仍需核对原文。")
    elif stage == 'model':
        warning = status == 'budget_blocked' or bool(details.get('error_type'))
        text = PURPOSES.get(details.get('purpose'), '模型处理') + '：'
        text += ('预算不足，调用已阻止。' if status == 'budget_blocked' else
                 '调用未成功。' if details.get('error_type') else
                 '调用完成；不代表内容已通过业务校验。' if status == 'completed' else '调用开始。')
        code=details.get('error_code') or {'APITimeoutError':'timeout','APIConnectionError':'connection'}.get(details.get('error_type'))
        if code:
            label={'model_configuration':'模型配置或能力不匹配，未换供应商重发','timeout':'请求超时','connection':'连接未成功','authentication':'鉴权失败','rate_limit':'服务限流',
                   'upstream_error':'上游服务错误','invalid_request':'接口请求错误'}.get(code,code)
            text+=label+'。'
        if details.get('timeout_phase'):
            text+='超时阶段：'+{'connect':'连接','read':'读取响应','write':'发送请求','pool':'等待连接池','unknown':'尚不明确'}.get(details['timeout_phase'],'尚不明确')+'。'
        if type(details.get('timeout_seconds')) in (int,float):text+=f"本次SDK超时配置 {details['timeout_seconds']:g} 秒。"
    elif stage == 'delivery':
        warning = status not in {'sent'}
        text = '回复已发送。' if status == 'sent' else '投递尚未确认成功，具体状态见技术详情。'
    else:
        text = {'received':'请求已接收，等待处理。','claimed': '请求已开始处理。', 'output_prepared': '回复已准备，等待投递。',
                'cancelled':'请求已取消；迟到结果不应用到当前草稿。',
                'failed':'请求处理失败，尚未取得完整结果。','retry':'请求未完成，等待受限重试。',
                'task_conflict': '任务版本已变化，本次提交被阻止。'}.get(status, '请求状态见技术详情。')
        warning = status in {'task_conflict','failed','retry'}
        if status=='business_failed':
            warning=True
            text='业务处理失败：'+details.get('error_type','未知内部错误')+'。'
            if details.get('error_location'):text+='错误位置：'+details['error_location']+'。'
    return {'id': row['id'], 'stage': stage, 'title': STAGES.get(stage, '处理记录'),
            'created_at': row['created_at'], 'description': text, 'warning': warning, 'details': details}


def web_report(database, job):
    trace = export_timeline(database, job['event_key'])
    timeline = [explain(row) for row in trace['timeline']]
    def recovery_key(row):
        details = row['details']
        field = {'model':'call_id','tool':'operation_key','delivery':'outbox_id'}.get(row['stage'])
        if row['stage'] == 'summary':
            return ('summary',)
        return (row['stage'],details[field]) if field and details.get(field) else None
    last = {recovery_key(row): row for row in timeline if recovery_key(row) is not None}
    for row in timeline:
        final = last.get(recovery_key(row)) if recovery_key(row) is not None else None
        row['recovered'] = bool(row['warning'] and final and final['id'] > row['id'] and not final['warning']
            and final['details'].get('status') in {'completed','published','sent','checkpoint_reused'})
    degraded = any(row['warning'] and not row['recovered'] for row in timeline)
    status = job['status']
    state = {'completed': '处理已完成', 'failed': '处理失败', 'cancelled': '请求已取消',
             'blocked':'请求受阻',
             'retry': '等待重试', 'running': '正在处理', 'pending': '等待处理'}.get(status, '状态待核对')
    if status == 'completed' and degraded:
        state = '处理已完成，有降级或异常记录'
    elif status == 'completed' and any(row['recovered'] for row in timeline):
        state = '处理已完成，先前异常已恢复'
    note = ('记录中存在异常，请展开相应环节核对；任务完成不等于每个环节都成功。' if degraded
            else '这里只展示已记录的处理步骤，不能据此证明回答内容准确。')
    routes=[row for row in timeline if row['stage']=='routing' and row['details'].get('status') in {'selected','intent_failed'}]
    if status=='completed' and any(row['details'].get('status')=='business_failed' for row in timeline):
        state='请求已结束，业务处理失败'
        note='错误提示已处理或发送，不代表业务操作成功；请展开失败环节查看错误类型和代码位置。'
    elif status=='completed' and routes and routes[-1]['details']['status']=='intent_failed':
        state='请求已结束，意图解析未完成，业务未执行'
        note='错误提示已处理或发送；完整请求已保留，无需改写已提供的信息。'
    if not timeline:
        note = '尚无可用过程记录，可能尚未处理、记录已过保留期或来自旧版本；无法确认具体原因。'
    report = {'job_id': job['id'], 'job_status': status, 'state': state, 'note': note,
              'model_calls': trace['model_calls'], 'timeline': timeline, 'replayed': False}
    report['report_text'] = '\n'.join([
        f"请求 #{job['id']}：{state}", note, f"已登记模型调用：{trace['model_calls']} 次",
        *[f"{row['created_at']} · {row['title']}：{row['description']}\n  {json.dumps(row['details'], ensure_ascii=False)}"
          for row in timeline],
        '此报告只读取已有元数据，不包含聊天正文，不重新执行查询或模型调用。'])
    return report
