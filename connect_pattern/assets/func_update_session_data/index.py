"""
Adds custom data to an Amazon Connect AI agent (Q in Connect) session.

Invoke this Lambda from a contact flow *after* a Connect assistant block. It
retrieves the Q in Connect session attached to the contact and calls
``UpdateSessionData`` so the parameters configured in the Invoke Lambda
block become ``{{$.Custom.<KEY>}}`` variables in your AI prompts.

Every key/value pair sent in the Invoke Lambda block becomes a Custom
session variable, so each contact flow declares its own schema independently
— one Lambda serves any number of flows.

Environment variables
---------------------
NAMESPACE (optional, default: "Custom")
    Session-data namespace. "Custom" is currently the only supported value
    for use in AI prompts via ``{{$.Custom.<KEY>}}``.
"""

import json
import logging
import os
import re
import boto3

from typing import Any
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Clients are created at module load so they are reused across invocations.
_connect = boto3.client("connect")
_qconnect = boto3.client("qconnect")

_NAMESPACE: str = os.environ.get("NAMESPACE", "Custom")

# Session ARN format:
#   arn:aws:wisdom:<region>:<account>:session/<assistant-id>/<session-id>
_SESSION_ARN_PATTERN = re.compile(
    r"^arn:aws[A-Za-z\-]*:wisdom:[^:]+:\d+:session/"
    r"(?P<assistant_id>[^/]+)/(?P<session_id>[^/]+)$"
)


def _parse_session_arn(session_arn: str) -> tuple[str, str]:
    match = _SESSION_ARN_PATTERN.match(session_arn)

    if not match:
        raise ValueError(f"Unrecognised Q in Connect session ARN: {session_arn!r}")

    return match["assistant_id"], match["session_id"]


def _get_session_ids(instance_arn: str, contact_id: str) -> tuple[str, str]:
    """Find the Q in Connect session attached to the contact via DescribeContact."""
    response = _connect.describe_contact(
        InstanceId=instance_arn,
        ContactId=contact_id,
    )

    wisdom_info = response.get("Contact", {}).get("WisdomInfo") or {}
    session_arn = wisdom_info.get("SessionArn")

    if not session_arn:
        raise RuntimeError(
            "Contact has no WisdomInfo.SessionArn. Ensure a 'Connect assistant' "
            "block runs before this Lambda in the contact flow."
        )

    return _parse_session_arn(session_arn)


def handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    logger.debug("Received event: %s", json.dumps(event, default=str))

    contact_data = event["Details"]["ContactData"]
    instance_arn = contact_data["InstanceARN"]
    contact_id = contact_data["ContactId"]
    parameters = event["Details"].get("Parameters") or {}

    if not parameters:
        logger.info("No parameters supplied; nothing to set.")
        return {"status": "success", "keysSet": ""}

    try:
        assistant_id, session_id = _get_session_ids(instance_arn, contact_id)

        data = [
            {"key": k, "value": {"stringValue": str(v)}}
            for k, v in parameters.items()
        ]

        logger.info(
            "Updating Q in Connect session %s on assistant %s with %d key(s): %s",
            session_id, assistant_id, len(data), [d["key"] for d in data],
        )

        _qconnect.update_session_data(
            assistantId=assistant_id,
            sessionId=session_id,
            namespace=_NAMESPACE,
            data=data,
        )
    except ClientError as e:
        logger.exception("UpdateSessionData failed")
        return {"status": "error", "errorCode": e.response["Error"]["Code"]}
    except (KeyError, ValueError, RuntimeError) as e:
        logger.exception("Bad input or missing session context")
        return {"status": "error", "errorCode": type(e).__name__}

    return {
        "status": "success",
        "keysSet": ",".join(d["key"] for d in data),
    }
