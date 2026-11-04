"""Small, auditable bilingual fallback for memory-system domain concepts.

This is search vocabulary, never generated evidence. It runs only when the
original lexical query has no results; unknown intents keep ordinary retrieval.
"""
import re

_DELIVERY = re.compile(r"\b(?:events?|messages?|packets?|requests?|tasks?|jobs?|delivery|notifications?)\b", re.I)
_DURABILITY = re.compile(r"\b(?:loss|lost|losing|crash(?:es|ed)?|shutdown|durable|durability|persist(?:ence|ent)?|survive)\b|power[ -]?(?:cut|failure|outage)", re.I)
_WORKFLOW = re.compile(r"\b(?:memory|tasks?|jobs?|pipeline|uploads?|capture|processing|requests?)\b", re.I)
_STAGE = re.compile(r"\b(?:lifecycle|progress|stages?|status|states?|phases?)\b", re.I)
_DISPLAY = re.compile(r"\b(?:show|display|distinguish|track|tell|see|visibility|know)\b", re.I)


def query_alias(query):
    if _WORKFLOW.search(query) and _STAGE.search(query) and _DISPLAY.search(query):
        return "采集 后台 生效", "workflow_state_visibility"
    if _DELIVERY.search(query) and _DURABILITY.search(query):
        return "持久化 outbox 落盘", "durable_delivery"
    return None, None
