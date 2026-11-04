"""Opt-in automatic extraction transport; source/activation policy stays in automation."""
from __future__ import annotations
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .automation import AutomaticModel, ModelUnavailable, validate_items, validation_error_code
from .extraction import MODEL_ID


class ProviderFailure(ModelUnavailable):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward a configured bearer to a redirect target.


def exact_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


class CompatibleAutomaticModel(AutomaticModel):
    def __init__(self, config, data_dir):
        if not isinstance(config.get("model"),str) or not isinstance(config.get("base_url"),str) or not isinstance(config.get("token_env"),str):
            raise ValueError("Invalid provider configuration")
        self.model_id = config["model"]
        self.base_url = config["base_url"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        parsed.port  # Validate malformed numeric ports before accepting configuration.
        if (parsed.scheme not in {"https","http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1","localhost","::1"})):
            raise ValueError("Invalid extraction endpoint")
        self.data_dir = Path(data_dir)
        self.token_env = config["token_env"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_env):
            raise ValueError("Invalid credential reference")
        self.response_format = config.get("response_format", "json_schema")
        if self.response_format not in {"json_schema","json_object"}:
            raise ValueError("Invalid structured format")
        self.timeout = config.get("timeout_seconds",45)
        if isinstance(self.timeout,bool) or not isinstance(self.timeout,(int,float)) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise ValueError("Invalid provider timeout")
        if type(config.get("audit_responses",False)) is not bool:
            raise ValueError("Invalid audit option")
        thinking = config.get("thinking")
        if "thinking" in config and (not isinstance(thinking, dict)
                or set(thinking) != {"type"}
                or not isinstance(thinking["type"], str)
                or thinking["type"] not in {"enabled", "disabled"}):
            raise ValueError("Invalid thinking option")
        self.thinking = dict(thinking) if thinking is not None else None
        self.audit_dir = Path(data_dir)/"extraction-attempts" if config.get("audit_responses",False) else None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def describe(self):
        description = {"provider":"openai_compatible","model":self.model_id,
            "base_url":self.base_url,"response_format":self.response_format,
            "local_schema_and_evidence_validation":True,"max_output_repairs":1}
        if self.thinking is not None:
            description["thinking"] = dict(self.thinking)
        return description

    def _token(self):
        from .credentials import credential
        return credential(self.data_dir, self.token_env)

    def available(self):
        return bool(self._token())

    def extract(self, messages):
        if not self.available():
            raise ProviderFailure("MODEL_AUTH_MISSING")
        return super().extract(messages)

    def _chat(self, prompt, schema):
        fmt = ({"type":"json_schema","json_schema":{"name":"automatic_memory","strict":True,"schema":schema}}
            if self.response_format == "json_schema" else {"type":"json_object"})
        payload={"model":self.model_id,"messages":[{"role":"user","content":prompt+"\nOutput JSON schema:\n"+json.dumps(schema,ensure_ascii=False)}],
            "temperature":0,"stream":False,"max_tokens":4096,"response_format":fmt}
        if self.thinking is not None:
            payload["thinking"] = dict(self.thinking)
        token=self._token()
        if not token:raise ProviderFailure("MODEL_AUTH_MISSING")
        from .provider_budget import before_call
        before_call()
        req=urllib.request.Request(self.base_url+"/chat/completions",
            data=json.dumps(payload,ensure_ascii=False).encode("utf8"),
            headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
        try:
            with self.opener.open(req,timeout=self.timeout) as response:
                raw=response.read(256*1024+1)
                declared=response.headers.get("Content-Length")
                if declared is not None and declared.strip().isdigit() and len(raw)<int(declared):
                    raise ProviderFailure("MODEL_NETWORK_ERROR")
        except urllib.error.HTTPError as error:
            code = ("MODEL_RATE_LIMITED" if error.code==429 else "MODEL_AUTH_REJECTED" if error.code in {401,403}
                else "MODEL_PROVIDER_UNAVAILABLE" if error.code>=500 else "MODEL_PROVIDER_REQUEST_REJECTED")
            try:delay=float(error.headers.get("Retry-After","0"))
            except (TypeError,ValueError):delay=0
            if not math.isfinite(delay):delay=0
            error.close()
            raise ProviderFailure(code,min(600,max(0,delay))) from None
        except (OSError,http.client.HTTPException) as error:
            timeout=isinstance(error,TimeoutError) or isinstance(getattr(error,"reason",None),TimeoutError)
            raise ProviderFailure("MODEL_TIMEOUT" if timeout else "MODEL_NETWORK_ERROR") from None
        if len(raw)>256*1024:
            raise ValueError("MODEL_PROVIDER_ENVELOPE_INVALID")
        try:
            value=json.loads(raw,object_pairs_hook=exact_object)
            if not isinstance(value,dict):raise ValueError()
            choices=value["choices"]
            if not isinstance(choices,list) or len(choices)!=1:
                raise ValueError()
            choice=choices[0]
            if not isinstance(choice,dict) or not isinstance(choice.get("message"),dict):raise ValueError()
            message=choice["message"]
            if message.get("refusal"):
                raise ProviderFailure("MODEL_REFUSED")
            if choice.get("finish_reason")=="length":
                raise ValueError("MODEL_OUTPUT_TRUNCATED")
            if choice.get("finish_reason")!="stop" or not isinstance(message.get("content"),str):
                raise ValueError()
            from .provider_budget import response
            response(value.get("model"), value.get("usage"))
            return message["content"],value.get("model")
        except ProviderFailure:
            raise
        except (ValueError,KeyError,TypeError) as error:
            if str(error)=="MODEL_OUTPUT_TRUNCATED":raise
            raise ValueError("MODEL_PROVIDER_ENVELOPE_INVALID") from None

    def _audit(self, ident, attempt, messages, content, observed_model, code):
        # Raw response audit is explicit opt-in and local to this data directory.
        # Never persist headers, credentials, HTTP error bodies, or full prompts.
        if self.audit_dir is None:return
        self.audit_dir.mkdir(parents=True,exist_ok=True)
        doc={"request_id":ident,"attempt":attempt,"at":time.time(),"provider":"openai_compatible",
            "model":self.model_id,"observed_model":observed_model,"code":code,
            "source_message_ids":[m["id"] for m in messages],
            "source_sha256":hashlib.sha256(json.dumps(messages,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            "response_content":content}
        path=self.audit_dir/(ident+"-"+str(attempt)+".json")
        with path.open("x",encoding="utf8") as stream:json.dump(doc,stream,ensure_ascii=False,indent=2)

    def _generate(self, prompt, schema, messages):
        ident=uuid.uuid4().hex
        repair=""
        for attempt in range(2):
            content=None;observed=None
            try:
                content,observed=self._chat(prompt+repair,schema)
                raw=json.loads(content,object_pairs_hook=exact_object)
                validate_items(raw,messages)  # Includes exact fields/types and source/topic evidence.
                self._audit(ident,attempt,messages,content,observed,"ACCEPTED")
                return raw
            except ProviderFailure as error:
                self._audit(ident,attempt,messages,None,None,error.code)
                raise
            except (ValueError,KeyError,TypeError) as error:
                code=validation_error_code(error)
                self._audit(ident,attempt,messages,content,observed,code)
                if attempt==1:raise
                repair=("\n程序校验未通过："+code+"。请重新输出严格符合前述schema的JSON。"
                    "所有字段必须完整且无多余字段；message_id必须来自输入，quote必须是完整原句，"
                    "topic必须是该quote中逐字出现的具体短语；不要概括主题、删除限定或新增事实。")


class UnavailableAutomaticModel:
    def available(self):return False
    def describe(self):return {"provider":"unavailable","code":"MODEL_CONFIG_INVALID"}
    def extract(self,messages):raise ProviderFailure("MODEL_CONFIG_INVALID")


def configured_models(config_path):
    path=Path(config_path)
    if not path.exists():
        return CompatibleAutomaticModel({"provider":"openai_compatible", "model":"deepseek-flash",
            "base_url":"https://api.deepseek.com", "token_env":"DEEPSEEK_API_KEY",
            "response_format":"json_object", "thinking":{"type":"disabled"},
            "timeout_seconds":60}, path.parent), None
    try:
        config=json.loads(path.read_text("utf-8-sig"))
        if not isinstance(config,dict):raise ValueError("Invalid provider configuration")
        backup = config.pop("fallback", None)
        allowed={"provider","model","base_url","token_env","response_format","timeout_seconds","audit_responses","thinking"}
        def make(value):
            if not isinstance(value,dict) or set(value)-allowed:raise ValueError("Invalid provider configuration")
            value = dict(value)
            provider=value["provider"]
            model=value.get("model")
            if not isinstance(model,str) or not 1<=len(model)<=200:raise ValueError("Invalid model")
            value["model"]=model
            if provider=="ollama":raise ValueError("Local extraction has been retired")
            if provider=="openai_compatible":return CompatibleAutomaticModel(value,path.parent)
            raise ValueError("Unknown provider")
        primary = make(config)
        try: fallback = make(backup) if backup is not None else None
        except (ValueError,KeyError,TypeError,OSError): fallback = UnavailableAutomaticModel()
        return primary, fallback
    except (ValueError,KeyError,TypeError,OSError):
        return UnavailableAutomaticModel(), None


def configured_model(config_path):
    return configured_models(config_path)[0]
