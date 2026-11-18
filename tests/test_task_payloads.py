import pytest

from credit.task_payloads import InvalidTaskPayload, TaskPayloadCipher


def test_task_payload_cipher_round_trips_bound_to_request_id():
    cipher = TaskPayloadCipher({"key-1": b"a" * 32}, active_key_id="key-1")
    token = cipher.encrypt("question text", request_id="request-1")

    assert cipher.decrypt(token, request_id="request-1") == "question text"


def test_task_payload_cipher_rejects_tampering_and_wrong_request_id():
    cipher = TaskPayloadCipher({"key-1": b"b" * 32}, active_key_id="key-1")
    token = cipher.encrypt("sensitive question", request_id="request-1")
    key_id, encoded = token.split(".", 1)
    changed = key_id + "." + ("A" if encoded[0] != "A" else "B") + encoded[1:]

    with pytest.raises(InvalidTaskPayload):
        cipher.decrypt(changed, request_id="request-1")
    with pytest.raises(InvalidTaskPayload):
        cipher.decrypt(token, request_id="request-2")


def test_task_payload_cipher_keeps_old_key_for_decryption_during_rotation():
    old = TaskPayloadCipher({"old": b"o" * 32}, active_key_id="old")
    old_token = old.encrypt("queued question", request_id="request-3")
    rotated = TaskPayloadCipher(
        {"old": b"o" * 32, "new": b"n" * 32}, active_key_id="new",
    )

    new_token = rotated.encrypt("new question", request_id="request-4")
    assert rotated.decrypt(old_token, request_id="request-3") == "queued question"
    assert rotated.decrypt(new_token, request_id="request-4") == "new question"


def test_task_payload_cipher_rejects_non_256_bit_keys():
    with pytest.raises(ValueError, match="32 bytes"):
        TaskPayloadCipher({"bad": b"x" * 31}, active_key_id="bad")


def test_request_fingerprint_is_stable_keyed_and_does_not_expose_question():
    cipher = TaskPayloadCipher({"key-a": b"a" * 32}, active_key_id="key-a")
    other = TaskPayloadCipher({"key-a": b"b" * 32}, active_key_id="key-a")
    fingerprint = cipher.fingerprint('{"question":"private question"}')

    assert fingerprint == cipher.fingerprint('{"question":"private question"}')
    assert fingerprint != cipher.fingerprint('{"question":"different question"}')
    assert fingerprint != other.fingerprint('{"question":"private question"}')
    assert "private question" not in fingerprint
