"""Preference management; current trip changes never implicitly update defaults."""
import json
import re

from core.preference_contract import (CHOICES, INTERESTS, LABELS, preference_fields,
                                      preference_change, explicit_preference_write)


COMMANDS = {'我的偏好','查看我的偏好','查看偏好','记住了哪些','保存偏好建议',
            '暂停应用偏好','启用偏好','关闭偏好建议','开启偏好建议','忘记所有偏好','忘记全部偏好'}


def preference_request(text):
    return text.strip() in COMMANDS or bool(re.match(
        r'^(?:(?:请|帮我)|我(?:希望|想让)你)?(?:记住|保存.*偏好|更新.*偏好|忘记|删除.*偏好|清除.*偏好)'
        r'|^我(?:一般|通常|平时)(?:喜欢|偏爱)|^以后.{0,12}(?:默认|优先)|^(?:不要|别|不用)(?:记住|保存)',text.strip()))


def command_values(text):
    """A complete, small command grammar. Uncovered clauses require semantic input."""
    body = re.sub(r'^(?:(?:请|帮我))?(?:记住|保存(?:我的)?偏好|更新(?:我的)?偏好)[，,:： ]*','',text.strip())
    clauses = [c.strip() for c in re.split(r'[，,。；;]|但是|但|并且|而且|且',body) if c.strip()]
    values, denied = {}, {}
    for clause in clauses:
        match = re.fullmatch(r'(?:我)?(?:一般|通常|平时|以后)?(?:都)?'
            r'(?P<verb>不喜欢|不偏爱|不要|默认|更喜欢|喜欢|偏爱|优先|偏好|使用|选择)?(?P<value>.+?)'
            r'(?:节奏|出行|回复)?',clause)
        if not match:
            return None
        raw, verb = match['value'], match['verb']
        raw = {'不夜游':'不安排夜游','不安排晚上':'不安排夜游'}.get(raw,raw)
        choices = [(key,raw) for key,options in CHOICES.items() if raw in options]
        interests = re.split(r'、|和',raw)
        if interests and all(v in INTERESTS for v in interests):
            choices.append(('interests',interests))
        city = re.fullmatch(r'从([\u4e00-\u9fffA-Za-z]{2,15})出发',raw)
        if city:
            choices.append(('departure_city',city[1]))
        if len(choices) != 1:
            return None
        key, value = choices[0]
        if verb in {'不喜欢','不偏爱','不要'}:
            denied.setdefault(key,[]).append(value)
        elif key in values and values[key] != value:
            return None
        else:
            values[key] = value
    if any(value in denied.get(key,[]) for key,value in values.items()):
        return None
    return values


class PreferencesTool:
    def __init__(self,repository):
        self.repository = repository

    @staticmethod
    def extract(text):
        return command_values(text) or {}

    def snapshot(self,event):
        return {**self.repository.snapshot(event),'fields':preference_fields()}

    def update(self,event,version,values,**options):
        return {**self.repository.update(event,version,values,**options),'fields':preference_fields()}

    def _save(self,event,claim,values,*,suggest=False,version=None):
        state = self.repository.snapshot(event)
        version = state['version'] if version is None else version
        if not values:
            return '没有更改长期偏好。请明确要保存的本人长期偏好；单次要求用于当前行程。'
        if suggest:
            if self.repository.suggest(event,version,values):
                return '可保存的偏好建议：'+self.repository.describe(values)+'。回复“保存偏好建议”后跨会话生效；目前未保存。'
            return '偏好建议已关闭，未保存。'
        reply = '已保存偏好（'+state['scope']+'）：'+self.repository.describe(values)+'。可查看、修改或忘记。'
        self.repository.update(event,version,values,claim=claim,reply=reply)
        return reply

    def handle_intent(self,event,claim,ir,*,mode='execute'):
        if len(ir.operations)!=1 or ir.operations[0].domain!='preferences':
            return '偏好请求与其他操作混在一起，请分别说明；尚未修改偏好。'
        op = ir.operations[0]
        if op.operation not in {'update','suggest','forget'}:
            return '不支持的偏好操作，尚未修改。'
        if ir.action not in {'update':{'create','update','clarify'},'suggest':{'read','clarify'},
                             'forget':{'delete','clarify'}}[op.operation]:
            return '偏好操作与动作不一致，尚未修改。'
        change = preference_change(op.values,event.content)
        if (change['subject'],change['scope'],change['polarity']) != ('self','long_term','affirmed'):
            return '没有保存长期偏好。请明确这是你本人的长期要求；他人或本次要求不写入个人默认。'
        if op.operation!='forget' and (not change['values'] or any(v is None for v in change['values'].values())):
            return '保存偏好需要明确字段值；遗忘请单独说明。'
        if op.operation!='forget' and command_values(event.content) != change['values']:
            subject_clause = (r'(?:我|本人)(?:以后|一般|通常|平时|一直|长期)?(?:更|也|都|比较)?'
                              r'(?:喜欢|偏爱|偏好|希望|选择|使用|想|从)[^，,。；;\n]+')
            if not all(re.fullmatch(subject_clause,quote) for quote in change['evidence'].values()):
                return '偏好原话中的主体还不明确，尚未保存。请用“我长期喜欢……”说明。'
        if ir.action=='clarify' or ir.missing_fields or ir.requires_confirmation or mode!='execute':
            return '已识别偏好内容，但保存依据尚需确认；请明确要保存的本人长期要求。'
        if op.operation=='suggest':
            return self._save(event,claim,change['values'],suggest=True)
        if not change['authorization'] or not explicit_preference_write(event.content,forget=op.operation=='forget'):
            return '没有保存长期偏好，请明确说“记住我的长期偏好”或说明要忘记的字段。'
        if op.operation=='forget':
            if not change['values'] or any(value is not None for value in change['values'].values()):
                return '忘记偏好只接受指定字段，不同时保存新值。'
            if not all(re.fullmatch(r'(?:忘记|删除|清除)(?:我的)?'+re.escape(LABELS[key]),quote)
                       for key,quote in change['evidence'].items()):
                return '请明确要忘记的本人偏好字段，尚未修改。'
            reply = '已忘记指定偏好，后续请求不再应用；已有行程和聊天记录不自动删除。'
            self.repository.update(event,self.repository.snapshot(event)['version'],change['values'],claim=claim,reply=reply)
            return reply
        return self._save(event,claim,change['values'])

    def handle(self,event,claim):
        text = event.content.strip()
        if not preference_request(text):
            return None
        repo, state = self.repository, self.repository.snapshot(event)
        if text in {'我的偏好','查看我的偏好','查看偏好','记住了哪些'}:
            return f"偏好范围：{state['scope']}；{'已启用' if state['enabled'] else '暂停应用'}。\n"+(repo.describe(state['values']) or '尚未保存偏好。')
        switches = {'暂停应用偏好':{'enabled':False},'启用偏好':{'enabled':True},
                    '关闭偏好建议':{'suggestions':False},'开启偏好建议':{'suggestions':True}}
        if text in switches:
            reply = '已更新偏好设置。'
            repo.update(event,state['version'],{},**switches[text],claim=claim,reply=reply)
            return reply
        forgotten = re.fullmatch(r'(?:忘记|删除|清除)(?:我的)?(所有偏好|全部偏好|'+'|'.join(map(re.escape,LABELS.values()))+')',text)
        if forgotten:
            clear = forgotten[1] in {'所有偏好','全部偏好'}
            values = {key:None for key,label in LABELS.items() if label==forgotten[1]}
            reply = '已忘记指定偏好，后续请求不再应用；已有行程和聊天记录不自动删除。'
            repo.update(event,state['version'],values,clear=clear,claim=claim,reply=reply)
            return reply
        if text=='保存偏好建议':
            candidate=repo.candidate(event)
            if not candidate:
                return '当前会话没有有效偏好建议，请重新描述。'
            return self._save(event,claim,json.loads(candidate['values_json']),version=candidate['version'])
        values = command_values(text)
        if not values:
            return '没有保存长期偏好。请明确要长期保存的本人偏好；单次或他人要求不写入个人默认。'
        return self._save(event,claim,values,suggest=not explicit_preference_write(text))
