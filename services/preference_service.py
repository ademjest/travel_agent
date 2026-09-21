import json
import re

from infrastructure.preference_repository import CHOICES, INTERESTS, LABELS


def preference_request(text):
    return text.strip() == '我的偏好' or bool(re.search(r'(?:查看|保存|更新|忘记|删除|清除|暂停应用|启用|关闭|开启).{0,8}偏好|记住了哪些|帮我记住|请记住|记住[，,:： ]|我一般.{0,12}(?:喜欢|偏爱)|以后.{0,12}(?:默认|优先|喜欢)', text))


class PreferenceService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def extract(text):
        values = {}
        if re.search(r'小王|同行|朋友|父母|他喜欢|她喜欢|这次|本次|今天|不喜欢|不偏爱|不优先|不是|不要(?:公共|步行|驾车|紧凑|轻松)', text):
            return values
        for key, options in CHOICES.items():
            hits = [option for option in options if option in text]
            if len(hits) == 1:
                values[key] = hits[0]
        if '不夜游' in text or '不安排晚上' in text:
            values['night'] = '不安排夜游'
        interests = [value for value in INTERESTS if value in text]
        if interests:
            values['interests'] = interests
        origin = re.search(r'从([\u4e00-\u9fffA-Za-z]{2,15})出发', text)
        if origin:
            values['departure_city'] = origin[1]
        return values

    def handle(self, event, claim):
        text = event.content.strip()
        if not preference_request(text):
            return None
        if re.search(r'不要记住|别记住|不用记住|不要保存|不保存|只是举例|例如|引用|网页写|文件写', text):
            return '没有保存长期偏好。'
        repo = self.repository
        state = repo.snapshot(event)
        if re.search(r'查看.*偏好|记住了哪些|我的偏好$', text):
            return f"偏好范围：{state['scope']}；{'已启用' if state['enabled'] else '暂停应用'}。\n" + (repo.describe(state['values']) or '尚未保存偏好。')
        if text in ('暂停应用偏好', '启用偏好', '关闭偏好建议', '开启偏好建议'):
            reply = '已更新偏好设置。'
            repo.update(event, state['version'], {}, enabled=text == '启用偏好' if '应用' in text or text == '启用偏好' else None,
                suggestions=text == '开启偏好建议' if '建议' in text else None, claim=claim, reply=reply)
            return reply
        if re.search(r'忘记|删除|清除', text):
            if re.search(r'所有|全部', text):
                values, clear = {}, True
            else:
                values = {key: None for key, label in LABELS.items() if label in text or (key == 'budget' and '预算' in text)}
                clear = False
                if not values:
                    return '请说明要忘记的字段，例如“忘记我的交通偏好”，或“忘记所有偏好”。'
            reply = '已忘记指定偏好，后续请求不再应用；已有行程和聊天记录不自动删除。'
            repo.update(event, state['version'], values, clear=clear, claim=claim, reply=reply)
            return reply
        if text == '保存偏好建议':
            candidate = repo.candidate(event)
            if not candidate:
                return '当前会话没有有效偏好建议，请重新描述。'
            values, version = json.loads(candidate['values_json']), candidate['version']
        else:
            values, version = self.extract(text), state['version']
        if not values:
            return '没有更改长期偏好。请明确要长期保存的节奏、交通、兴趣、夜游或回复风格；单次/他人要求仅用于本次任务。预算请在偏好面板填写完整单位。'
        explicit = bool(re.search(r'记住|保存|更新偏好|以后.*默认|以后.*优先', text))
        if not explicit:
            if repo.suggest(event, version, values):
                return '可保存的偏好建议：' + repo.describe(values) + '。回复“保存偏好建议”后跨会话生效；目前未保存。'
            return '偏好建议已关闭，未保存。需要长期保存时请明确说“记住”。'
        reply = '已保存偏好（' + state['scope'] + '）：' + repo.describe(values) + '。可查看、修改或忘记。'
        repo.update(event, version, values, claim=claim, reply=reply)
        return reply
