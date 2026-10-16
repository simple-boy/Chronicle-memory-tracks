"""Participant contract validation, independent of any inference service."""
import json
from datetime import datetime, timezone


class ContractError(ValueError):
    def __init__(self, reason, status=422):
        self.reason, self.status = reason, status
        super().__init__(reason)


def canonical(value):
    try:
        return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
    except (TypeError, ValueError):
        raise ContractError("invalid_json_value") from None


def identifier(value,field):
    if not isinstance(value,str) or not value.strip(): raise ContractError(field+"_required")
    try: size=len(value.encode("utf-8"))
    except UnicodeError: raise ContractError("invalid_unicode") from None
    if size>4096: raise ContractError("identifier_exceeds_declared_capacity",413)
    return value


def timestamp(value):
    if isinstance(value,bool) or not isinstance(value,int):
        raise ContractError("timestamp_must_be_unix_milliseconds")
    try: datetime.fromtimestamp(value/1000,timezone.utc)
    except (ValueError,OverflowError,OSError): raise ContractError("timestamp_out_of_range") from None
    return value


def validate_add(body):
    if not isinstance(body,dict): raise ContractError("json_object_required")
    for key in ("user_id","session_id","request_id"): identifier(body.get(key),key)
    if not isinstance(body.get("messages"),list) or not body["messages"]:
        raise ContractError("messages_must_be_nonempty_array")
    for message in body["messages"]:
        if not isinstance(message,dict) or not isinstance(message.get("role"),str) or message.get("role") not in {"user","assistant"}:
            raise ContractError("role_must_be_user_or_assistant")
        text=message.get("content")
        if not isinstance(text,str) or not text.strip(): raise ContractError("coding_content_must_be_nonempty_string")
        if "timestamp" in message: timestamp(message["timestamp"])
    # Also reject invalid values in extra envelope fields. Preserve them as source, not as authority.
    try: canonical(body).encode("utf-8")
    except UnicodeError: raise ContractError("invalid_unicode") from None


def validate_search(body,max_results=100):
    if not isinstance(body,dict): raise ContractError("json_object_required")
    identifier(body.get("user_id"),"user_id")
    if not isinstance(body.get("query"),str) or not body["query"].strip():
        raise ContractError("query_must_be_nonempty_string")
    k=body.get("top_k")
    if isinstance(k,bool) or not isinstance(k,int) or not 1<=k<=max_results:
        raise ContractError("top_k_out_of_range")
    if "options" in body and (not isinstance(body["options"],list) or
        any(not isinstance(x,str) or not x.strip() for x in body["options"])):
        raise ContractError("options_must_be_string_array")
    try: canonical(body).encode("utf-8")
    except UnicodeError: raise ContractError("invalid_unicode") from None
