"""Live search-policy checks with temporary state and no QQ delivery."""
import json
import argparse
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from openai import OpenAI
from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from infrastructure.search_client import SearchClient
from services.research_service import ResearchService
from infrastructure.web_reader import public_text_issue


class RecordingSearch(SearchClient):
    def __init__(self,key):
        super().__init__(key)
        self.calls=[]

    def search(self,query,**kwargs):
        self.calls.append({'query':query,**kwargs})
        print(json.dumps({'search':self.calls[-1]},ensure_ascii=False),flush=True)
        rows=super().search(query,**kwargs)
        print(json.dumps({'results':[{'host':urlsplit(r['url']).hostname,'path':urlsplit(r['url']).path,
            'title':r['title']} for r in rows]},ensure_ascii=False),flush=True)
        return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=('all','china','international','xiaohongshu'),default='all')
    selected=parser.parse_args().case
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    assert all(os.getenv(k) for k in ('SEARCH_API_KEY','LLM_API_KEY','LLM_BASE_URL','LLM_MODEL_ID')), 'Search and model configuration required'
    with tempfile.TemporaryDirectory() as folder, OpenAI(api_key=os.environ['LLM_API_KEY'],
            base_url=os.environ['LLM_BASE_URL'],timeout=90,max_retries=1) as client:
        store=MemoryStore(Path(folder)/'test.db')
        results=[]
        cases={'china':('china','帮我搜索武汉三日游攻略'),
               'international':('international','帮我搜索京都三日游攻略'),
               'xiaohongshu':('china','帮我搜索武汉三日游攻略，只看小红书')}
        for name in (('china','international') if selected=='all' else (selected,)):
            region,query=cases[name]
            search=RecordingSearch(os.environ['SEARCH_API_KEY'])
            service=ResearchService(store,search,client,os.environ['LLM_MODEL_ID'])
            event=ChatEvent('web','group',region,region,'test-owner',query)
            reply=service.handle(event,store.begin_event(event.event_key))
            data=store.research.get(event)
            assert data['search_plan']['region']==region, data['search_plan']
            assert 1<=len(search.calls)<=4
            expected='xiaohongshu.com' if region=='china' else 'tripadvisor.com'
            assert any(expected in c.get('include_domains',[]) for c in search.calls)
            if name=='xiaohongshu':
                assert data['search_plan']['strict_sources'] and len(search.calls)==1
                assert search.calls[0]['include_domains']==['xiaohongshu.com']
            else:
                assert data.get('sources'),reply
            assert '选源策略' in reply
            assert not any(s['status']=='read' and public_text_issue(s['text']) for s in data['sources'])
            results.append({'case':name,'region':region,'calls':len(search.calls),'stage':data['stage'],
                'sources':[{'host':urlsplit(s['url']).hostname,'status':s['status'],
                            'access_error':s.get('access_error',''),'text_characters':len(s.get('text','')),
                            'excerpt':s.get('text','')[:160]} for s in data['sources']]})
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)
        print(json.dumps({'result':'passed','cases':results},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
