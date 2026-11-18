"""Load task encryption configuration without exposing key material."""

import json
import os
from collections.abc import Mapping

from credit.task_payloads import TaskPayloadCipher


def load_task_payload_cipher(environ: Mapping[str, str] | None = None) -> TaskPayloadCipher:
    values = os.environ if environ is None else environ
    raw_keyring = values.get("CREDIT_TASK_KEYRING", "")
    active_key_id = values.get("CREDIT_TASK_ACTIVE_KEY_ID", "")
    try:
        encoded_keys = json.loads(raw_keyring)
        if not isinstance(encoded_keys, dict) or not encoded_keys:
            raise ValueError
        keys = {}
        for key_id, encoded in encoded_keys.items():
            if not isinstance(key_id, str) or not isinstance(encoded, str) or len(encoded) != 64:
                raise ValueError
            keys[key_id] = bytes.fromhex(encoded)
        return TaskPayloadCipher(keys, active_key_id=active_key_id)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(
            "task encryption requires CREDIT_TASK_KEYRING JSON with 32-byte hex keys "
            "and a valid CREDIT_TASK_ACTIVE_KEY_ID"
        ) from None
