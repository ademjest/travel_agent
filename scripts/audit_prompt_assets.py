"""Inventory application prompt assembly, skills and schemas without model calls."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
RUNTIME_DIRS=('adapters','agents','app','core','infrastructure','services','tools')


def audit():
    contexts=[];systems=[];templates=[]
    runtime_files=[]
    for folder in RUNTIME_DIRS:
        for path in sorted((ROOT/folder).rglob('*.py')):
            runtime_files.append(path)
            text=path.read_text(encoding='utf-8');tree=ast.parse(text)
            def item(node):
                return {'path':path.relative_to(ROOT).as_posix(),'line':node.lineno,
                    'source_sha256':hashlib.sha256((ast.get_source_segment(text,node) or '').encode()).hexdigest()}
            for node in ast.walk(tree):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='model_call_context':
                    contexts.append({**item(node),'purpose':ast.unparse(node.args[0]) if node.args else '',
                        'prompt_version':next((ast.unparse(k.value) for k in node.keywords if k.arg=='prompt_version'),''),
                        'stage_id':next((k.value.value for k in node.keywords if k.arg=='stage_id' and isinstance(k.value,ast.Constant)),''),
                        'category':'technical' if path.name in {'model_gateway.py','service_probes.py'} else 'business'})
                if isinstance(node,ast.Dict):
                    pairs={k.value:v for k,v in zip(node.keys,node.values) if isinstance(k,ast.Constant) and isinstance(k.value,str)}
                    if isinstance(pairs.get('role'),ast.Constant) and pairs['role'].value=='system' and 'content' in pairs:
                        systems.append({**item(pairs['content']),'content_kind':type(pairs['content']).__name__})
                if isinstance(node,(ast.Assign,ast.AugAssign)):
                    targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                    if any(isinstance(t,ast.Name) and t.id in {'SYSTEM_PROMPT','PROMPT'} for t in targets):
                        templates.append(item(node))
                if isinstance(node,ast.FunctionDef) and node.name in {'_system_prompt','_intent_prompt','writing_messages'}:
                    templates.append(item(node))
    skills=[{'path':p.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((ROOT/'skills').rglob('*')) if p.is_file()]
    sys.path.insert(0,str(ROOT))
    from core.tool_result import TOOLS_BY_NAME
    tools=[{'name':name,'sha256':hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
           for name,value in sorted(TOOLS_BY_NAME.items())]
    from infrastructure.skill_manager import get_skill_manager
    registry=get_skill_manager().status()
    return {'schema_version':'prompt-inventory-v2','runtime_file_count':len(runtime_files),
        'model_contexts':contexts,'system_assemblies':systems,'registry':registry,
        'template_definitions':templates,'application_skill_files':skills,'tool_schemas':tools,
        'note':'Static assembly/source hashes, not hashes of fully assembled live prompts; no authorization is delegated to skills.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--status',action='store_true',help='Show explicit stage versions and asset hashes')
    args=parser.parse_args();result=audit()
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['registry'] if args.status else
        {k:len(result[k]) for k in ('model_contexts','system_assemblies','application_skill_files','tool_schemas')},ensure_ascii=False,indent=2))
