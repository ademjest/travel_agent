import asyncio


class WebReplyRenderer:
    def render(self, channel, command_content, reply_text):
        if command_content.strip().lower() in {'帮助', 'help', '菜单', '旅行面板'}:
            return {'kind': 'reply', 'text': ('你好，我是彼岸。可以直接告诉我你的旅行问题：\n\n'
                '- 查天气、地点和交通：例如“武汉站到湖北省博物馆怎么坐地铁”。\n'
                '- 规划和调整行程：说明城市、日期、天数与同行偏好。\n'
                '- 整理资料：点击“添加资料”，发送图片、Word、Excel、TXT 或 Markdown。\n'
                '- 设置提醒：例如“明天上午九点提醒我预约博物馆”。\n'
                '- 定时查询与规则监测：例如“1分钟后告诉我武汉天气”。\n\n'
                '当前网页使用独立会话与测试数据，无需 QQ 在线。关闭网页后，后端仍需运行才能执行提醒。')}
        if command_content.strip() == '状态':
            return {'kind': 'reply', 'text': '本地网页服务正在运行。请打开左侧“状态与设置”查看模型、高德及后台任务状态。QQ 在线状态不影响本网页。'}
        text = reply_text.replace('本群 @ 你', '当前网页会话通知你').replace('群共享资料', '当前会话资料')
        return {'text': text, 'kind': 'reply'}

    def render_reminder(self, recipient_id, text):
        return {'text': text, 'kind': 'notification'}


class WebTransport:
    def __init__(self, repository):
        self.repository = repository

    async def send(self, message):
        return await asyncio.to_thread(self.repository.deliver, message)
