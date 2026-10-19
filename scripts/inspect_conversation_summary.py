"""Explicit local read-only summary inspection. Never initializes a MemoryStore."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from infrastructure.conversation_summary_repository import ConversationSummaryRepository
from scripts.evaluate_token_memory import ReadOnlyStore
from core.summary_contract import PROMPT_VERSION, load_summary_prompt


def inspect(database,scope,owner,platform='web',include_content=False):
    store=ReadOnlyStore(database)
    from core.chat_transport import ChatEvent
    raw_scope=scope.removeprefix(platform+':') if platform!='qq_official' else scope
    event=ChatEvent(platform,'group','readonly-inspection',raw_scope,owner,'')
    # Repository construction performs schema initialization. Its read methods do not.
    repo=object.__new__(ConversationSummaryRepository)
    repo.store=store
    with store._connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_summaries'").fetchone():
            return {'scope':event.storage_scope_id,'owner':owner,'record_present':False,'valid_record':False}
    metadata=repo.metadata(event)
    current=repo.latest(event)
    fields=('status','format_version','version','covered_turn_id','progress_json','model_id',
            'prompt_version','prompt_hash','updated_at','lease_until','retry_after')
    prompt_hash = load_summary_prompt()[1]
    result={'scope':event.storage_scope_id,'owner':owner,'record_present':metadata is not None,
            'valid_record':current is not None,
            'metadata':{field:metadata.get(field) for field in fields} if metadata else None,
            'current_prompt_version': PROMPT_VERSION,
            'prompt_matches_current': bool(metadata and metadata['prompt_version'] == PROMPT_VERSION
                                           and metadata.get('prompt_hash') == prompt_hash),
            'summary_chars':len(metadata['summary']) if metadata else 0,
            'note':'Read-only source/dependency validity check; does not establish runtime lifecycle readiness, injection or real-model quality.'}
    if include_content and metadata:
        result['summary']=metadata['summary']
        result['items']=json.loads(metadata['items_json'])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--scope',required=True,help='Conversation storage scope, e.g. web:<conversation-id>')
    parser.add_argument('--owner',required=True)
    parser.add_argument('--platform',choices=('web','onebot','qq_official'),default='web')
    parser.add_argument('--include-content',action='store_true',help='Explicitly print selected local summary content')
    args=parser.parse_args()
    print(json.dumps(inspect(args.database,args.scope,args.owner,args.platform,args.include_content),ensure_ascii=False,indent=2))
