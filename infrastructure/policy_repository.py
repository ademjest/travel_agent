import json


class PolicyRepository:
    def __init__(self, store):
        self.store = store
        with store._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS booking_policy_evidence (
                fingerprint TEXT PRIMARY KEY, entity TEXT NOT NULL, source_url TEXT NOT NULL,
                excerpt TEXT NOT NULL, policy_json TEXT NOT NULL,
                first_retrieved_at TEXT NOT NULL, last_checked_at TEXT NOT NULL
            )''')

    def record(self, policy):
        data = policy.to_dict()
        with self.store._connect() as connection:
            connection.execute('''INSERT INTO booking_policy_evidence VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET last_checked_at=excluded.last_checked_at
            ''', (policy.fingerprint, policy.entity, policy.source_url, policy.excerpt,
                  json.dumps(data, ensure_ascii=False), policy.retrieved_at, policy.retrieved_at))
        return data
