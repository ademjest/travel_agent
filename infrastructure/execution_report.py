"""Read existing diagnostic metadata and explain it without replaying a request."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from infrastructure.execution_diagnostics import metadata


STAGES = {'request': '接收与处理', 'model': '模型调用', 'summary': '会话摘要',
          'context': '准备回答资料', 'tool': '工具查询', 'delivery': '回复投递', 'retrieval': '资料检索',
          'routing':'需求分流','planning':'行程修改范围'}
PURPOSES = {'intent_parse': '理解本次意图', 'conversation_summary': '更新会话摘要',
            'tool_reasoning': '分析并回答', 'tool_answer': '整理查询结果',
            'research_synthesis': '整理研究结果', 'research_targets':'提取原方案研究对象',
            'research_fact_synthesis':'核验景点事实', 'trip_plan': '生成行程'}
ERRORS = {'rate_limit': '上游服务限流', 'temporary_upstream': '上游服务暂时不可用',
          'rate_limited': '上游服务限流', 'network_error': '网络连接未成功',
          'unauthorized': '服务认证未通过', 'forbidden': '服务拒绝访问', 'not_found': '页面不存在',
          'timeout': '请求超时', 'permission_denied': '没有所需权限',
          'invalid_argument': '请求参数不符合要求', 'not_available': '服务未配置或不可用',
          'attempt_limit': '本次请求的尝试额度已用完'}


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
    if stage == 'routing':
        warning = status in {'research_rejected', 'research_needs_input', 'research_partial'}
        text = {'research_accepted':'研究字段已校验，开始只读研究。',
                'research_repair':'研究核心字段需修复，正在进行一次受限重新解析。',
                'research_rejected':'研究请求未通过业务校验，本次尚未搜索。',
                'research_needs_input':'研究需要确认目标方案或日期，尚未搜索。',
                'research_partial':'研究部分完成，仍有景点或主题缺少可核验依据。',
                'research_completed':'研究已完成，具体事实仍需结合依据和适用日期核对。'}.get(status,
            '已完整识别请求，可直接分流。' if status == 'complete' else '规则未完整覆盖，保留完整请求进行意图解析。')
        if details.get('ignored_fields'):
            text += ' 已忽略说明性扩展字段，未因此增加解析调用。'
    elif stage == 'planning':
        text = {'local_recommendations':'本次仅更新相关住宿或餐饮候选，活动安排保持。',
                'routes_only':'本次仅更新交通证据。'}.get(status,'本次需要整体核对行程。')
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
        warning = status in {'failed', 'needs_input'}
        text = {'completed': '工具已返回结果。', 'checkpoint_reused': '复用已保存的工具结果，未再次查询。',
                'needs_input': '工具需要补充信息。'}.get(status, '工具状态见技术详情。')
        if status == 'failed':
            text = ERRORS.get(details.get('error_code'), '工具查询失败，记录不足以进一步确认原因。')
        if type(details.get('attempts')) is int:
            text += f" 本条工具记录的尝试次数：{details['attempts']}。"
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
    elif stage == 'delivery':
        warning = status not in {'sent'}
        text = '回复已发送。' if status == 'sent' else '投递尚未确认成功，具体状态见技术详情。'
    else:
        text = {'claimed': '请求已开始处理。', 'output_prepared': '回复已准备，等待投递。',
                'task_conflict': '任务版本已变化，本次提交被阻止。'}.get(status, '请求状态见技术详情。')
        warning = status == 'task_conflict'
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
             'retry': '等待重试', 'running': '正在处理', 'pending': '等待处理'}.get(status, '状态待核对')
    if status == 'completed' and degraded:
        state = '处理已完成，有降级或异常记录'
    elif status == 'completed' and any(row['recovered'] for row in timeline):
        state = '处理已完成，先前异常已恢复'
    note = ('记录中存在异常，请展开相应环节核对；任务完成不等于每个环节都成功。' if degraded
            else '这里只展示已记录的处理步骤，不能据此证明回答内容准确。')
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
