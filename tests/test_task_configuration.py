import json

import pytest

from credit.task_configuration import load_task_payload_cipher


def test_keyring_loader_selects_active_key_without_logging_key_material():
    key_a = "a" * 64
    key_b = "b" * 64
    cipher = load_task_payload_cipher({
        "CREDIT_TASK_KEYRING": json.dumps({"old": key_a, "active-v2": key_b}),
        "CREDIT_TASK_ACTIVE_KEY_ID": "active-v2",
    })

    encrypted = cipher.encrypt("question", request_id="request-1")
    assert cipher.decrypt(encrypted, request_id="request-1") == "question"
    assert key_a not in encrypted and key_b not in encrypted


@pytest.mark.parametrize("settings", [
    {},
    {"CREDIT_TASK_KEYRING": "not-json", "CREDIT_TASK_ACTIVE_KEY_ID": "active"},
    {"CREDIT_TASK_KEYRING": json.dumps({"active": "not-a-key"}),
     "CREDIT_TASK_ACTIVE_KEY_ID": "active"},
    {"CREDIT_TASK_KEYRING": json.dumps({"active": "a" * 64}),
     "CREDIT_TASK_ACTIVE_KEY_ID": "missing"},
])
def test_invalid_task_keyring_is_rejected(settings):
    with pytest.raises(ValueError, match="task encryption"):
        load_task_payload_cipher(settings)
