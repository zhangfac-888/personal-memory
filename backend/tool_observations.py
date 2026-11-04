"""Bounded, typed tool observations. Transport completion is not task success."""
from __future__ import annotations
import json,re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

SOURCE_PREFIX='PM_TOOL_OBSERVATION_V1 '
MAX_MEMORY_SOURCE=1000
class ToolObservation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str=Field(min_length=1,max_length=160,pattern=r'^[A-Za-z0-9_.:-]+$')
    call_id: str=Field(min_length=1,max_length=200,pattern=r'^[A-Za-z0-9_-]+$')
    state: Literal['result','error']
    truncated: bool=Field(strict=True)

def source_text(message):
    observation=message.get('tool_observation')
    if message.get('role')!='tool' or not observation:return message.get('text','')
    meta=ToolObservation.model_validate(observation).model_dump()
    return SOURCE_PREFIX+json.dumps(meta,ensure_ascii=False,separators=(',',':'),sort_keys=True)+'\n'+message['text']

def extractable(message):
    meta=message.get('tool_observation')
    if not meta:return True  # Existing host protocols keep their bounded policy.
    value=ToolObservation.model_validate(meta)
    return (message.get('role')=='tool' and not value.truncated
            and '[REDACTED]' not in message['text'] and len(source_text(message))<=MAX_MEMORY_SOURCE
            and 'personal_memory' not in value.name.casefold() and 'personal-memory' not in value.name.casefold())

def policy(item,message):
    if not message.get('tool_observation'):return item
    if item.get('type')!='Fact' or item.get('source_role')!='tool' or not extractable(message):return None
    return {**item,'active':bool(item.get('active')) and message['tool_observation']['state']=='result','plan':None}

def stored_metadata(text):
    if not text.startswith(SOURCE_PREFIX):return None
    head,sep,_=text.partition('\n')
    if not sep:return None
    try:return ToolObservation.model_validate(json.loads(head[len(SOURCE_PREFIX):])).model_dump()
    except (ValueError,TypeError):return None