"""Fixed, bounded ping contract for Core NATS worker V1."""

import json

from .nats_config import validate_worker_id

PING_SUBJECT = "xiaozhi.v1.worker.ping"
QUEUE_GROUP = "xiaozhi-workers"
MAX_REQUEST_BYTES = 4096
MAX_REPLY_BYTES = 1024
MAX_REPLY_SUBJECT_BYTES = 512


def targeted_ping_subject(worker_id: str) -> str:
    return f"xiaozhi.v1.worker.{validate_worker_id(worker_id)}.ping"


def ping_response(worker_id: str) -> bytes:
    payload = json.dumps(
        {
            "protocol": "xiaozhi-worker-v1",
            "worker_id": validate_worker_id(worker_id),
            "status": "ok",
            "capabilities": [],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_REPLY_BYTES:
        raise ValueError("Worker ping reply exceeds its fixed size limit")
    return payload


def valid_reply_subject(subject: str) -> bool:
    return (
        0 < len(subject) <= MAX_REPLY_SUBJECT_BYTES
        and subject.isascii()
        and all(token for token in subject.split("."))
        and not any(
            char.isspace() or ord(char) < 33 or ord(char) == 127 or char in "*>"
            for char in subject
        )
    )
