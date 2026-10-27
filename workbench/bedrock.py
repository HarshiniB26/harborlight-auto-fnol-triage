"""Optional, non-authoritative Bedrock annotation.

``rules.py`` and ``draft.py`` do not import this module. A model failure becomes
a warning string and cannot change classification, routing, citations, or approval.
"""

import json
import os

from tools.aws_config import bedrock_enabled

UNAVAILABLE = "Model note unavailable; deterministic draft is unchanged."


def advisory_annotation(summary, *, client=None, model_id=None, enabled=None):
    """Return a warning string, or None when Bedrock is disabled.

    ``summary`` may contain only the case id, classification, and routing.
    Document text is not sent and is not logged.
    """

    if enabled is None:
        enabled = bedrock_enabled()
    if not enabled:
        return None
    model_id = model_id or os.environ.get("FNOL_BEDROCK_MODEL_ID", "").strip()
    if not model_id:
        return UNAVAILABLE
    if client is None:
        client = _client()
        if client is None:
            return UNAVAILABLE
    prompt = (
        "You assist an insurance handler. Do not decide coverage, payment, liability, "
        "or approval. Reply with one short reminder that a person must approve the draft. "
        f"Case {summary.get('case_id')} is classified {summary.get('classification')} "
        f"and routed {summary.get('routing')}."
    )
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 200,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    try:
        response = client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        raw = response["body"]
        payload = raw.read() if hasattr(raw, "read") else raw
        if isinstance(payload, str):
            payload = payload.encode()
        parsed = json.loads(payload)
    except (OSError, TypeError, ValueError, KeyError, TimeoutError):
        return UNAVAILABLE
    except Exception:
        return UNAVAILABLE
    texts = []
    for part in parsed.get("content") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"].strip())
    text = " ".join(item for item in texts if item).strip()
    if not text:
        return UNAVAILABLE
    return f"Non-authoritative model note ({model_id}): {text[:500]}"


def _client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        return None
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION") or None,
        config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 1}),
    )
