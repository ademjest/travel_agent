"""User-visible management of conversation planning artifacts."""
import re

from infrastructure.planning_repository import PlanningConflict


def planning_request(text):
    return text.strip() in {'查看方案草稿','我的方案草稿'} or bool(re.fullmatch(r'删除方案草稿\s+P-[a-f0-9]{12}',text.strip()))


class PlanningService:
    def __init__(self,repository):
        self.repository=repository

    def handle(self,event,claim=None):
        text=event.content.strip()
        if not planning_request(text):
            return None
        self.repository.ensure_history(event)
        if text in {'查看方案草稿','我的方案草稿'}:
            plans=self.repository.list_for_owner(event)
            if not plans:
                return '当前会话还没有方案草稿。探索性规划和联网研究方案会出现在这里。'
            return '\n'.join(f"方案 {p['ordinal']}：{p['title']}（{p['artifact_id']}，版本 {p['version']}）"
                for p in plans)
        artifact_id=re.search(r'P-[a-f0-9]{12}',text)[0]
        artifact=self.repository.get(event,artifact_id)
        if not artifact or artifact['status']!='active':
            return '没有找到当前会话中可删除的这份方案草稿。'
        try:
            self.repository.delete(event,artifact_id,artifact['version'])
        except PlanningConflict as error:
            return str(error)
        return '已删除方案草稿；已保存行程、研究报告和其他方案不受影响。'
