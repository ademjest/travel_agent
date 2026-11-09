"""Versioned model connections, profiles and deterministic purpose routing."""
from dataclasses import dataclass, field
import hashlib
import json
import re
from urllib.parse import urlsplit

from core.model_pricing import decimal_price, pricing_config
from core.model_policy import PURPOSE_MODEL_ENV, PURPOSE_ROLES, PURPOSE_OUTPUTS


CONFIG_KEY = 'LLM_CONFIG_JSON'
CAPABILITIES = {'text','json','tools','vision','embedding'}
IDENTITY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')


class ModelConfigError(ValueError):
    code = 'model_configuration'


def _text(value, *, limit=200, empty=False):
    if (not isinstance(value,str) or len(value)>limit or (not empty and not value.strip())
            or any(ord(c)<32 for c in value) or '${' in value):
        raise ModelConfigError('模型配置文字或长度无效。')
    return value.strip()


def _id(value):
    if not isinstance(value,str) or not IDENTITY.fullmatch(value):
        raise ModelConfigError('连接和模型档案 ID 须为字母开头的字母、数字、下划线或连字符。')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class ProviderConnection:
    id: str
    name: str
    base_url: str
    api_key: str = field(repr=False)
    protocol: str = 'openai_chat_completions'


@dataclass(frozen=True)
class ModelProfile:
    id: str
    name: str
    connection_id: str
    model_id: str
    capabilities: tuple = ('text','json','tools')
    kind: str = 'chat'
    token_parameter: str = 'max_completion_tokens'
    temperature: str = 'allow'
    pricing: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelConfiguration:
    connections: tuple = ()
    profiles: tuple = ()
    roles: dict = field(default_factory=dict)
    purpose_overrides: dict = field(default_factory=dict)
    cost_limits: dict = field(default_factory=dict)
    legacy: bool = False

    @property
    def version(self):
        return _digest(self.as_dict())

    @property
    def configured(self):
        profile = self.profile(self.roles.get('regular',''))
        connection = self.connection(profile.connection_id) if profile else None
        return bool(connection and connection.api_key and connection.base_url and profile.model_id)

    def connection(self, identity):
        return next((item for item in self.connections if item.id==identity),None)

    def profile(self, identity):
        return next((item for item in self.profiles if item.id==identity),None)

    def select(self, purpose, *, profile_id='', force_default=False):
        role = PURPOSE_ROLES.get(purpose,'regular')
        identity = profile_id or (self.roles.get('regular') if force_default else
            self.purpose_overrides.get(purpose) or self.roles.get(role) or
            (self.roles.get('regular') if role!='vision' else ''))
        profile = self.profile(identity)
        if not profile:
            raise ModelConfigError('该用途尚未配置可用模型档案；图片理解需单独选择视觉模型。')
        connection = self.connection(profile.connection_id)
        if not connection or not connection.api_key:
            raise ModelConfigError('所选模型连接未配置 Key，未向其他供应商重发。')
        return connection,profile

    def route_identity(self, connection, profile):
        return _digest({'connection_id':connection.id,'base_url':connection.base_url,
            'protocol':connection.protocol,'profile_id':profile.id,'model_id':profile.model_id,
            'capabilities':profile.capabilities,'kind':profile.kind,
            'token_parameter':profile.token_parameter,'temperature':profile.temperature})

    def matching_pricing(self, connection_id, profile_id, model_id, route_identity):
        """Price-only changes may supplement a historical call; route changes may not."""
        profile=self.profile(profile_id)
        connection=self.connection(connection_id)
        if (not profile or not connection or profile.connection_id!=connection_id or profile.model_id!=model_id
                or not route_identity or self.route_identity(connection,profile)!=route_identity):
            return None
        return {**profile.pricing,'version':profile.pricing.get('version') or self.version}

    def pricing_status(self):
        rows=[]
        for profile in self.profiles:
            purposes=[]
            for purpose in PURPOSE_OUTPUTS:
                role=PURPOSE_ROLES.get(purpose,'regular')
                selected=self.purpose_overrides.get(purpose) or self.roles.get(role) or (self.roles.get('regular') if role!='vision' else '')
                if selected==profile.id:purposes.append(purpose)
            rows.append({'profile_id':profile.id,'connection_id':profile.connection_id,'model_id':profile.model_id,
                'pricing':profile.pricing,'complete':all(decimal_price(profile.pricing.get(k)) is not None for k in ('input','output')),
                'purposes':purposes})
        return rows

    def as_dict(self, *, include_keys=False):
        return {'schema_version':1,'connections':[
            {'id':c.id,'name':c.name,'base_url':c.base_url,'protocol':c.protocol,
             **({'api_key':c.api_key} if include_keys else {'key_configured':bool(c.api_key)})} for c in self.connections],
            'profiles':[{'id':p.id,'name':p.name,'connection_id':p.connection_id,'model_id':p.model_id,
                'kind':p.kind,'capabilities':list(p.capabilities),'token_parameter':p.token_parameter,
                'temperature':p.temperature,'pricing':p.pricing} for p in self.profiles],
            'roles':dict(self.roles),'purpose_overrides':dict(self.purpose_overrides),
            'cost_limits':self.cost_limits}


def parse_config(raw):
    if raw=='':
        return ModelConfiguration()
    try:
        value=json.loads(raw) if isinstance(raw,str) else raw
    except (ValueError,TypeError):
        raise ModelConfigError('模型配置块 JSON 无效；未回退到旧供应商。') from None
    if (not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version']!=1
            or value.keys()-{'schema_version','connections','profiles','roles','purpose_overrides','cost_limits'}
            or len(json.dumps(value,ensure_ascii=False).encode())>65536):
        raise ModelConfigError('模型配置版本、字段或总长度无效。')
    connections=[];profiles=[]
    for key,limit in (('connections',12),('profiles',36)):
        rows=value.get(key,[])
        if not isinstance(rows,list) or len(rows)>limit:
            raise ModelConfigError('模型连接或档案数量超过上限。')
        ids=set()
        for row in rows:
            if not isinstance(row,dict):raise ModelConfigError('模型配置条目无效。')
            identity=_id(row.get('id'))
            if identity in ids:raise ModelConfigError('模型连接或档案 ID 重复。')
            ids.add(identity)
            name=_text(row.get('name',identity),limit=80)
            if key=='connections':
                if row.keys()-{'id','name','base_url','api_key','protocol'}:
                    raise ModelConfigError('模型连接包含不支持的字段。')
                url=_text(row.get('base_url'),limit=2048).rstrip('/')
                try:
                    parsed=urlsplit(url)
                    valid=(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
                        and not parsed.query and not parsed.fragment and parsed.port in (None,443))
                except ValueError:valid=False
                if not valid:raise ModelConfigError('模型连接须使用不含凭据或查询参数的公网 HTTPS 地址。')
                protocol=row.get('protocol','openai_chat_completions')
                if protocol!='openai_chat_completions':raise ModelConfigError('当前仅支持 OpenAI-compatible Chat Completions 协议。')
                secret=_text(row.get('api_key',''),limit=4096,empty=True)
                if secret and set(secret)<={'*','•'}:raise ModelConfigError('请填写实际 Key，不能保存掩码。')
                connections.append(ProviderConnection(identity,name,url,secret,protocol))
            else:
                if row.keys()-{'id','name','connection_id','model_id','capabilities','kind','token_parameter','temperature','pricing'}:
                    raise ModelConfigError('模型档案包含不支持的字段。')
                caps=row.get('capabilities',['text','json','tools'])
                kind=row.get('kind','chat');token=row.get('token_parameter','max_completion_tokens');temperature=row.get('temperature','allow')
                if (not isinstance(caps,list) or any(not isinstance(c,str) or c not in CAPABILITIES for c in caps)
                        or not isinstance(kind,str) or kind not in {'chat','embedding'}
                        or not isinstance(token,str) or token not in {'max_tokens','max_completion_tokens'}
                        or not isinstance(temperature,str) or temperature not in {'allow','omit'} or (kind=='chat' and 'text' not in caps)
                        or (kind=='embedding' and set(caps)!={'embedding'})):
                    raise ModelConfigError('模型能力、类型或参数适配无效。')
                price=row.get('pricing',{})
                if not isinstance(price,dict) or price.keys()-{'input','cached','output','currency','version'}:
                    raise ModelConfigError('模型价格字段无效。')
                try:
                    for field_name in ('input','cached','output'):decimal_price(price.get(field_name))
                except ValueError:raise ModelConfigError('模型费率须为非负有限数字。') from None
                currency=price.get('currency','USD')
                if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency):raise ModelConfigError('模型价格币种须为三位大写字母。')
                price={**price,'currency':currency,'version':_text(price.get('version',''),empty=True)}
                profiles.append(ModelProfile(identity,name,_id(row.get('connection_id')),_text(row.get('model_id')),
                    tuple(dict.fromkeys(caps)),kind,token,temperature,price))
    connection_ids={c.id for c in connections};profile_ids={p.id for p in profiles}
    if any(p.connection_id not in connection_ids for p in profiles):raise ModelConfigError('模型档案引用的连接不存在，请先重新分配。')
    roles=value.get('roles',{});overrides=value.get('purpose_overrides',{})
    if (not isinstance(roles,dict) or roles.keys()-{'basic','regular','complex','vision'}
            or not isinstance(overrides,dict) or len(overrides)>40):raise ModelConfigError('模型分工或高级用途覆盖无效。')
    if profiles and not roles.get('regular'):raise ModelConfigError('必须选择常规模型。')
    for purpose,identity in [*roles.items(),*overrides.items()]:
        if not isinstance(identity,str) or identity not in profile_ids:raise ModelConfigError('模型分工引用的档案不存在，请先重新分配。')
        profile=next(p for p in profiles if p.id==identity)
        if profile.kind!='chat' or ((purpose=='vision' or purpose=='image_extract') and 'vision' not in profile.capabilities):
            raise ModelConfigError('该分工需要聊天模型；图片理解还需要声明视觉能力。')
        if not re.fullmatch('[a-z][a-z0-9_]{0,63}',purpose):raise ModelConfigError('模型用途名称无效。')
    limits=value.get('cost_limits',{})
    if not isinstance(limits,dict) or len(limits)>12:raise ModelConfigError('按币种金额限额无效。')
    try:
        for currency,amounts in limits.items():
            if (not re.fullmatch('[A-Z]{3}',currency) or not isinstance(amounts,dict) or amounts.keys()-{'daily','monthly'}):raise ValueError()
            for amount in amounts.values():decimal_price(amount)
    except (ValueError,TypeError):raise ModelConfigError('按币种金额限额须为非负数字。') from None
    return ModelConfiguration(tuple(connections),tuple(profiles),roles,overrides,limits)


def legacy_config(values):
    model=values.get('LLM_MODEL_ID','').strip()
    if not model:return ModelConfiguration(legacy=True)
    connection=ProviderConnection('legacy',values.get('LLM_PROVIDER_ALIAS','').strip() or '原模型连接',
        values.get('LLM_BASE_URL','').strip().rstrip('/'),values.get('LLM_API_KEY','').strip())
    names=list(dict.fromkeys([model,*[values.get(k,'').strip() for k in PURPOSE_MODEL_ENV.values() if values.get(k,'').strip()]]))
    profiles=[]
    for name in names:
        try:price=pricing_config(name,model,values)
        except ValueError:raise ModelConfigError('原模型费率配置无效，请修正费率后重启。') from None
        profiles.append(ModelProfile('legacy-'+hashlib.sha256(name.encode()).hexdigest()[:12],name,connection.id,name,
            ('text','json','tools','vision'),pricing={k:v for k,v in price.items() if k in {'input','cached','output','currency','version'}}))
    by_name={p.model_id:p.id for p in profiles}
    overrides={purpose:by_name[values[key].strip()] for purpose,key in PURPOSE_MODEL_ENV.items() if values.get(key,'').strip()}
    limits={}
    if values.get('AI_DAILY_COST_LIMIT') or values.get('AI_MONTHLY_COST_LIMIT'):
        try:
            amounts={'daily':float(decimal_price(values.get('AI_DAILY_COST_LIMIT') or 0)),
                     'monthly':float(decimal_price(values.get('AI_MONTHLY_COST_LIMIT') or 0))}
        except ValueError:raise ModelConfigError('原模型金额限额无效，请修正后重启。') from None
        limits[(values.get('AI_COST_CURRENCY') or 'USD').upper()]=amounts
    return ModelConfiguration((connection,),tuple(profiles),{'regular':by_name[model],'vision':overrides.get('image_extract',by_name[model])},overrides,limits,True)


def resolve_config(raw, legacy_values):
    return legacy_config(legacy_values) if raw is None else parse_config(raw)
