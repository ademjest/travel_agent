"""Offline U1 workflow: fixed provider answer, independent navigation, recovery and source limits."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from core.chat_transport import ChatEvent
from infrastructure.memory_store import MemoryStore
from services.official_sources import OfficialSources
from tools.research.tool import ResearchTool


class Snapshots:
    configured = True

    def __init__(self, *, identity=True):
        self.searches = self.reads = self.provider_calls = 0
        self.identity = identity

    def search(self, query, **kwargs):
        self.searches += 1
        return [{'url':'https://wlt.wuhan.gov.cn/guide'}]

    def read(self, url, **kwargs):
        self.reads += 1
        text = ('武汉市文化和旅游局\n测试博物馆官方网站' if '.gov.cn/' in url else
                '测试博物馆\n地址：武汉市测试路1号\n'+('主办单位：测试博物馆' if self.identity else '运营主体未知'))
        return {'url':url,'status':'read','text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest(),
                'retrieved_at':'2026-10-26T02:00:00+00:00','links':[
                    {'url':'https://museum.example/visit','text':'测试博物馆官方网站'}] if '.gov.cn/' in url else []}

    def query(self, event, repository, query, **kwargs):
        self.provider_calls += 1
        text = ('### 测试博物馆 [target-1]\n介绍：青瓷与城墙遗址记录城市文化。\n'
                '开放时间：09:00-17:00。\n普通票价：成人票30元。\n预约规则：需提前实名预约。')
        return {'status':'completed','physical_calls':1,'text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest(),
                'retrieved_at':'2026-10-26T02:00:00+00:00'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    output=parser.parse_args().output
    results=[]
    with tempfile.TemporaryDirectory() as folder:
        for name, only, identity in (('navigation_without_fact_promotion',False,True),
                                     ('only_wendao',True,True),('authority_without_operator',False,False)):
            store=MemoryStore(Path(folder)/(name+'.db'))
            snapshots=Snapshots(identity=identity)
            tool=ResearchTool(store,snapshots,reader=snapshots.read,wendao=snapshots,official_sources=OfficialSources([]))
            event=ChatEvent('onebot','group',name,'scope','owner',('只用问道，' if only else '')+'介绍方案景点的门票和预约信息')
            plan={'title':'武汉示例行程','version':1,'destination':'武汉','duration_options':[1]}
            scope={'city':'武汉','targets':[{'name':'测试博物馆','day_index':1,'visit_mode':'entry'}],
                   'fields':{'topics':['opening_hours','ticket_price','reservation_rules']},
                   'requested_domains':[],'excluded_domains':[],'strict_sources':False,'language':'zh-cn'}
            store.research.save(event,'searching',{'query':event.content,'sources':[],'target_scope':scope,
                'plan_snapshot':deepcopy(plan),'dates':{'visit_start_date':'2026-10-27','visit_end_date':'2026-10-27'}})
            reply=tool.handle_intent(event,None,request={'plan':plan,'original_request':event.content})
            saved=store.research.get(event)
            count=(snapshots.searches,snapshots.reads,snapshots.provider_calls)
            tool.handle_intent(event,None,request={'plan':plan,'original_request':event.content})
            assert count==(snapshots.searches,snapshots.reads,snapshots.provider_calls)
            assert not any(f['status']=='official_verified' for f in saved['facts'])
            assert ('https://museum.example/visit' in reply)==(not only)
            assert not only or snapshots.searches==snapshots.reads==0
            assert snapshots.searches<=2 and snapshots.provider_calls==1
            assert all(s['type']!='web' for s in saved['sources'])  # No new fact-gap enrichment.
            results.append({'case':name,'passed':True,'searches':snapshots.searches,'pages':snapshots.reads,
                            'scripted_provider_calls':snapshots.provider_calls,'model_calls':0,
                            'verified_links':len(saved.get('official_links',{}))})
    result={'schema_version':'official-navigation-eval-v1','mode':'offline','real_api_calls':0,
            'summary':{'total':len(results),'passed':len(results)},'results':results}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
