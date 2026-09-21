"""Fence DB mutations made by background jobs, including synchronous worker threads."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3


class ExecutionRevoked(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionScope:
    database_path: str
    job_id: int
    token: str

    def validate(self, connection):
        now = datetime.now(timezone.utc).isoformat()
        row = connection.execute('''SELECT 1 FROM inbox_jobs WHERE id=? AND status='running'
            AND claim_token=? AND lease_until>? AND deadline_at>?''',
            (self.job_id, self.token, now, now)).fetchone()
        if row is None:
            raise ExecutionRevoked('任务已取消、超时或处理权已变化，停止保存结果。')


CURRENT_EXECUTION = ContextVar('travel_execution_scope', default=None)


def ensure_execution_active():
    scope = CURRENT_EXECUTION.get()
    if scope is None:
        return
    connection = sqlite3.connect(scope.database_path, timeout=2)
    try:
        scope.validate(connection)
    finally:
        connection.close()


class FencedConnection:
    def __init__(self, connection, scope):
        self.connection = connection
        self.scope = scope
        self.did_write = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def _before(self, sql):
        verb = re.sub(r'^\s*(?:--[^\n]*\n\s*)*', '', sql).split(None, 1)[0].upper()
        if verb in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER'}:
            if not self.connection.in_transaction:
                self.connection.execute('BEGIN IMMEDIATE')
            self.scope.validate(self.connection)
            self.did_write = True

    def execute(self, sql, parameters=()):
        self._before(sql)
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        self._before(sql)
        return self.connection.executemany(sql, parameters)

    def executescript(self, sql):
        raise ExecutionRevoked('后台业务任务不能执行数据库结构脚本。')

    def commit(self):
        if self.did_write:
            self.scope.validate(self.connection)
        self.connection.commit()


def connection_for_scope(connection, database_path):
    scope = CURRENT_EXECUTION.get()
    if scope is not None and Path(scope.database_path).resolve() == Path(database_path).resolve():
        return FencedConnection(connection, scope)
    return connection
