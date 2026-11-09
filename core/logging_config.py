"""Shared UTF-8 rotating logs; workflow events carry the only request details."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def exception_metadata(error):
    details={'error_type':type(error).__name__,'error_code':'missing_field' if isinstance(error,KeyError) else 'internal_error'}
    root=Path(__file__).resolve().parents[1]
    for frame,line in traceback.walk_tb(error.__traceback__):
        try:path=Path(frame.f_code.co_filename).resolve().relative_to(root)
        except ValueError:continue
        details['error_location']=f"{path.as_posix()}:{line}:{frame.f_code.co_name.strip('<>')}"
    return details


class SafeLogFilter(logging.Filter):
    def __init__(self, console=False):
        super().__init__();self.console=console

    def filter(self, record):
        if record.name=='uvicorn.access':
            status=getattr(record,'travel_http_status',record.args[-1] if isinstance(record.args,tuple) and record.args else 0)
            record.travel_http_status=status
            if isinstance(status,int) and status<400 and (self.console or os.getenv('APP_LOG_LEVEL','INFO')!='DEBUG'):return False
            if isinstance(status,int) and status>=400:
                record.levelno=logging.ERROR if status>=500 else logging.WARNING
                record.levelname=logging.getLevelName(record.levelno)
        if record.name.startswith(('httpx','httpcore','mcp.','urllib3','openai','botpy')):
            if record.levelno<logging.WARNING:return False
            record.msg='外部服务日志：'+record.name+'；请结合工作流错误类型检查。';record.args=()
        if record.args and not getattr(record,'workflow',False):
            def safe(value):
                return value if type(value) in (int,float,bool) or isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',value) else '[已省略]'
            if isinstance(record.args,tuple):record.args=tuple(safe(v) for v in record.args)
            elif isinstance(record.args,dict):record.args={k:safe(v) for k,v in record.args.items()}
        text=record.getMessage()
        text=re.sub(r'https?://[^\s]+','[服务地址已隐藏]',text)
        text=re.sub(r'(?i)(?:bearer\s+|(?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;]+','[凭据已隐藏]',text)
        record.msg=text;record.args=()
        if record.exc_info:
            details=exception_metadata(record.exc_info[1])
            record.msg+='；错误类型='+details['error_type']
            if details.get('error_location'):record.msg+='；位置='+details['error_location']
            record.exc_info=None;record.exc_text=None
        return True


class WorkflowFormatter(logging.Formatter):
    def __init__(self,fmt,zone='Asia/Shanghai'):
        super().__init__(fmt);self.zone=zone

    def formatTime(self,record,datefmt=None):
        return datetime.fromtimestamp(record.created,ZoneInfo(self.zone)).isoformat(timespec='seconds')

    def format(self,record):
        text=super().format(record)
        details=getattr(record,'workflow_details',None)
        if details and os.getenv('APP_LOG_LEVEL','INFO')=='DEBUG':
            text+=' '+json.dumps(details,ensure_ascii=False,sort_keys=True)
        return text


def configure_logging(root, filename='travel-web.log'):
    root.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger()
    for handler in tuple(logger.handlers):
        if getattr(handler,'travel_managed',False):logger.removeHandler(handler);handler.close()
    for name in ('uvicorn','uvicorn.error','uvicorn.access'):
        child=logging.getLogger(name)
        child.handlers.clear();child.propagate=True
    handlers=[RotatingFileHandler(root/filename,maxBytes=5*1024*1024,backupCount=5,encoding='utf-8'),logging.StreamHandler()]
    for index,handler in enumerate(handlers):
        handler.travel_managed=True;handler.addFilter(SafeLogFilter(console=index==1))
        handler.setFormatter(WorkflowFormatter('%(asctime)s %(levelname)s %(message)s',zone='UTC' if index==0 else 'Asia/Shanghai'))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if os.getenv('APP_LOG_LEVEL','INFO').upper()=='DEBUG' else logging.INFO)
