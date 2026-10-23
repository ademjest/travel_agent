"""Shared summary wire contract; the user-reviewed document is the prompt source."""
import hashlib
from pathlib import Path

MAX_ITEMS = 12
MAX_TEXT_CHARS = 1800
MAX_SOURCES = 4
MAX_QUOTE_CHARS = 500
KINDS = frozenset({'decision', 'correction', 'unresolved', 'reference'})
PROMPT_VERSION = 'conversation-summary-v4'
PROMPT_PATH = Path(__file__).resolve().parents[1] / 'prompts/conversation_summary.md'
OUTPUT_SCHEMA = {
    'type': 'object', 'required': ['items'], 'additionalProperties': False,
    'properties': {'items': {'type': 'array', 'maxItems': MAX_ITEMS, 'items': {
        'type': 'object', 'required': ['kind', 'text', 'sources'], 'additionalProperties': False,
        'properties': {'kind': {'type': 'string', 'enum': sorted(KINDS)},
                       'text': {'type': 'string', 'minLength': 1, 'maxLength': MAX_TEXT_CHARS},
                       'sources': {'type': 'array', 'minItems': 1, 'maxItems': MAX_SOURCES, 'items': {
                           'type': 'object', 'required': ['message_id', 'quote'], 'additionalProperties': False,
                           'properties': {'message_id': {'type': 'integer'},
                                          'quote': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUOTE_CHARS}}}}}}}},
}


class SummaryValidationError(ValueError):
    def __init__(self, code, path, **limits):
        self.details = {'code': code, 'path': path, **limits}
        super().__init__(f'{code}: {path}')


def load_summary_prompt():
    # Fail closed on accidental contract drift, without modifying the reviewed text.
    prompt = PROMPT_PATH.read_text(encoding='utf-8')
    required = (f'最多{MAX_ITEMS}项', f'最多{MAX_TEXT_CHARS}字符',
                f'1—{MAX_SOURCES}条来源', f'1—{MAX_QUOTE_CHARS}字符', *KINDS)
    if not all(fragment in prompt for fragment in required):
        raise ValueError('摘要提示词与共享契约不一致。')
    return prompt, hashlib.sha256(prompt.encode()).hexdigest()
