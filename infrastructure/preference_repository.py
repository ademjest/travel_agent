import json
from datetime import datetime, timezone


LABELS = {'pace': '行程节奏', 'transport': '交通偏好', 'interests': '兴趣类别', 'night': '夜间活动',
          'departure_city': '常用出发城市', 'budget': '默认预算', 'reply_style': '回复风格'}
CHOICES = {'pace': ('轻松', '适中', '紧凑'), 'transport': ('公共交通', '步行', '驾车'),
           'night': ('不安排夜游', '可以夜游'), 'reply_style': ('简洁', '详细', '表格')}
INTERESTS = ('博物馆', '历史街区', '自然景观', '美食', '公园', '艺术')


class PreferenceConflict(ValueError):
    pass


def owner_scope(event):
    return event.platform, event.sender_id, '' if event.platform == 'web' else event.scope_id


def validate_preferences(values):
    if not isinstance(values, dict) or set(values) - LABELS.keys():
        raise ValueError('不支持的偏好字段。')
    for key, value in values.items():
        if value is None:
            continue
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError('偏好值不在支持范围内。')
        if key == 'interests' and (not isinstance(value, list) or not value or len(value) > 6 or any(v not in INTERESTS for v in value)):
            raise ValueError('请选择支持的兴趣类别。')
        if key == 'departure_city':
            import re
            if not isinstance(value, str) or not re.fullmatch(r'[\u4e00-\u9fffA-Za-z· ]{2,30}', value):
                raise ValueError('请提供城市名称，不保存详细住址。')
        if key == 'budget':
            if (not isinstance(value, dict) or set(value) != {'amount', 'currency', 'basis', 'period', 'category'}
                    or type(value['amount']) not in (int, float) or not 0 < value['amount'] <= 1000000
                    or value['currency'] not in ('CNY', 'USD', 'EUR') or value['basis'] not in ('每人', '总额')
                    or value['period'] not in ('每天', '全程') or value['category'] not in ('住宿', '餐饮', '交通', '旅行总预算')):
                raise ValueError('预算需要金额、币种、每人/总额、每天/全程及适用项目。')


class PreferenceRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS preference_profiles (
                    platform TEXT, owner TEXT, scope TEXT, version INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1, suggestions INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(platform,owner,scope));
                CREATE TABLE IF NOT EXISTS user_preferences (
                    platform TEXT, owner TEXT, scope TEXT, key TEXT, value_json TEXT NOT NULL,
                    source_event TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(platform,owner,scope,key));
                CREATE TABLE IF NOT EXISTS preference_candidates (
                    platform TEXT, owner TEXT, scope TEXT, conversation TEXT, values_json TEXT NOT NULL,
                    version INTEGER NOT NULL, source_event TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(platform,owner,scope,conversation));
            ''')

    def snapshot(self, event):
        identity = owner_scope(event)
        with self.store._connect() as db:
            profile = db.execute('SELECT * FROM preference_profiles WHERE platform=? AND owner=? AND scope=?', identity).fetchone()
            rows = db.execute('SELECT key,value_json,updated_at FROM user_preferences WHERE platform=? AND owner=? AND scope=? ORDER BY key', identity).fetchall()
        return {'version': profile['version'] if profile else 0, 'enabled': bool(profile['enabled']) if profile else True,
            'suggestions': bool(profile['suggestions']) if profile else True,
            'values': {row['key']: json.loads(row['value_json']) for row in rows},
            'updated_at': max((row['updated_at'] for row in rows), default=''),
            'scope': '同一本地使用者的所有网页会话' if event.platform == 'web' else '本人在当前群'}

    def update(self, event, version, values, *, enabled=None, suggestions=None, clear=False, claim=None, reply=''):
        validate_preferences(values)
        if type(version) is not int or any(v is not None and type(v) is not bool for v in (enabled, suggestions)):
            raise ValueError('偏好版本或设置无效。')
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if claim and event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            if claim:
                row = db.execute('SELECT * FROM processed_events WHERE event_id=? AND claim_token=?', (event.event_key, claim.claim_token)).fetchone()
                if not row or row['status'] != 'processing':
                    raise PreferenceConflict('事件处理权已变化。')
                if row['prepared_reply'] is not None:
                    return self.snapshot(event)
            self.apply_update(db, event, version, values, enabled=enabled, suggestions=suggestions, clear=clear,
                              source_event=event.event_key if claim else 'settings')
            if claim:
                db.execute('UPDATE processed_events SET prepared_reply=?,prepared_memory_content=? WHERE event_id=? AND claim_token=?',
                           (reply, event.content, event.event_key, claim.claim_token))
        return self.snapshot(event)

    def apply_update(self, db, event, version, values, *, enabled=None, suggestions=None, clear=False, source_event=None):
        """Apply a validated profile patch inside the caller's transaction."""
        validate_preferences(values)
        identity = owner_scope(event)
        now = datetime.now(timezone.utc).isoformat()
        db.execute('INSERT OR IGNORE INTO preference_profiles(platform,owner,scope) VALUES (?,?,?)', identity)
        cursor = db.execute('''UPDATE preference_profiles SET version=version+1,
            enabled=coalesce(?,enabled), suggestions=coalesce(?,suggestions)
            WHERE platform=? AND owner=? AND scope=? AND version=?''', (enabled, suggestions, *identity, version))
        if cursor.rowcount != 1:
            raise PreferenceConflict('偏好已经变化，请刷新后重试；未恢复旧偏好。')
        # Every explicit edit invalidates in-flight candidates, including deletions.
        db.execute('DELETE FROM preference_candidates WHERE platform=? AND owner=? AND scope=?', identity)
        if clear:
            db.execute('DELETE FROM user_preferences WHERE platform=? AND owner=? AND scope=?', identity)
        for key, value in values.items():
            if value is None:
                db.execute('DELETE FROM user_preferences WHERE platform=? AND owner=? AND scope=? AND key=?', (*identity, key))
            else:
                db.execute('''INSERT INTO user_preferences VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(platform,owner,scope,key) DO UPDATE SET value_json=excluded.value_json,
                    source_event=excluded.source_event,updated_at=excluded.updated_at''',
                    (*identity, key, json.dumps(value, ensure_ascii=False), source_event or event.event_key, now))

    def suggest(self, event, version, values):
        validate_preferences(values)
        identity = owner_scope(event)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if event.platform == 'web' and getattr(self.store, 'web_lifecycle', None):
                self.store.web_lifecycle.check(event.scope_id, db)
            db.execute('INSERT OR IGNORE INTO preference_profiles(platform,owner,scope) VALUES (?,?,?)', identity)
            profile = db.execute('SELECT * FROM preference_profiles WHERE platform=? AND owner=? AND scope=?', identity).fetchone()
            if profile['version'] != version or not profile['suggestions']:
                return False
            db.execute('INSERT OR REPLACE INTO preference_candidates VALUES (?,?,?,?,?,?,?,?)',
                (*identity, event.scope_id, json.dumps(values, ensure_ascii=False), version, event.event_key, datetime.now(timezone.utc).isoformat()))
        return True

    def candidate(self, event):
        with self.store._connect() as db:
            row = db.execute('''SELECT * FROM preference_candidates WHERE platform=? AND owner=? AND scope=?
                AND conversation=? AND julianday(created_at)>julianday('now','-1 day')''', (*owner_scope(event), event.scope_id)).fetchone()
        return dict(row) if row else None

    def defaults(self, event):
        state = self.snapshot(event)
        return state if state['enabled'] and '本次不用偏好' not in event.content else {**state, 'values': {}}

    @staticmethod
    def describe(values):
        return '；'.join(f'{LABELS[k]}：' + (('、'.join(v)) if isinstance(v, list) else json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v)) for k, v in values.items())
