"""A2A conversation persistence — stores interactions to disk so compaction can't erase them.

Format matches ~/inbox/conversations/{agent}/{date}.md for consistency.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
_CONV_DIR = Path(_HERMES_HOME) / "a2a_conversations"
_lock = Lock()
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per conversation file

# ---------------------------------------------------------------------------
# Idempotency Store
# ---------------------------------------------------------------------------

_IDEM_TTL_SECONDS = 24 * 3600  # 24 hours default


class IdempotencyStore:
    """Thread-safe store for idempotency keys with TTL eviction.

    Stores a mapping of idempotencyKey → (task_id, payload_hash, result, expiry).
    On replay with the same key + payload, returns the cached result.
    On replay with the same key + different payload, raises KeyConflictError.
    """

    def __init__(self, ttl_seconds: int = _IDEM_TTL_SECONDS):
        self._store: dict[str, tuple[str, str, dict, float]] = {}  # key → (task_id, payload_hash, result, expires_at)
        self._lock = Lock()
        self._ttl = ttl_seconds

    def _payload_hash(payload: dict) -> str:
        """Stable hash of the JSON-RPC params for conflict detection."""
        # Normalize: sort keys and use JSON
        import json
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]

    def get(self, idempotency_key: str) -> tuple[str, dict] | None:
        """Return (task_id, result) if key exists and has not expired, else None."""
        with self._lock:
            entry = self._store.get(idempotency_key)
            if entry is None:
                return None
            task_id, payload_hash, result, expires_at = entry
            if time.time() > expires_at:
                del self._store[idempotency_key]
                return None
            return task_id, result

    def check_conflict(self, idempotency_key: str, payload: dict) -> tuple[bool, Optional[str]]:
        """Check if key exists with a different payload.

        Returns (is_conflict, task_id).
        is_conflict=True  → same key, DIFFERENT payload → reject (-38004).
        is_conflict=False → key is free, or same payload is in use (replay allowed).
        """
        with self._lock:
            entry = self._store.get(idempotency_key)
            if entry is None:
                return False, None
            task_id, stored_hash, result, expires_at = entry
            if time.time() > expires_at:
                del self._store[idempotency_key]
                return False, None
            incoming_hash = IdempotencyStore._payload_hash(payload)
            if incoming_hash != stored_hash:
                return True, task_id
            return False, task_id

    def set(self, idempotency_key: str, task_id: str, payload: dict, result: dict) -> None:
        """Store or update an idempotency key entry."""
        with self._lock:
            expires_at = time.time() + self._ttl
            self._store[idempotency_key] = (
                task_id,
                IdempotencyStore._payload_hash(payload),
                result,
                expires_at,
            )

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns count evicted."""
        with self._lock:
            now = time.time()
            expired = [k for k, (_, _, _, exp) in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
            return len(expired)


# Module-level singleton
_idem_store: Optional[IdempotencyStore] = None
_idem_store_lock = Lock()


def get_idempotency_store() -> IdempotencyStore:
    global _idem_store
    with _idem_store_lock:
        if _idem_store is None:
            _idem_store = IdempotencyStore()
        return _idem_store


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------


def save_exchange(
    agent_name: str,
    task_id: str,
    inbound_text: str,
    outbound_text: str,
    metadata: dict | None = None,
    direction: str = "inbound",
) -> Path:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H:%M:%S")
    safe_name = "".join(c if c.isalnum() or c in "-_.@ " else "_" for c in agent_name.lower())
    directory = _CONV_DIR / safe_name
    filepath = directory / f"{today}.md"

    intent = (metadata or {}).get("intent", "")
    reply_to = (metadata or {}).get("reply_to_task_id", "")

    entry_lines = [f"## {timestamp} | task:{task_id}"]
    if intent:
        entry_lines[0] += f" | {intent}"
    if reply_to:
        entry_lines[0] += f" | reply_to:{reply_to}"
    entry_lines.append("")

    if direction == "outbound":
        entry_lines.append(f"**\u2192 me:** {outbound_text}")
        entry_lines.append("")
        entry_lines.append(f"**\u2190 {safe_name}:** (waiting for reply\u2026)")
    else:
        entry_lines.append(f"**\u2190 {safe_name}:** {inbound_text}")
        entry_lines.append("")
        entry_lines.append(f"**\u2192 reply:** {outbound_text}")

    entry_lines.append("")
    entry_lines.append("---")
    entry_lines.append("")

    new_content = "\n".join(entry_lines)

    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        # Rotate if file exceeds max size
        if filepath.exists() and filepath.stat().st_size > _MAX_FILE_SIZE:
            rotated = filepath.with_name(filepath.stem + f"_old_{now.strftime('%H%M%S')}" + filepath.suffix)
            filepath.rename(rotated)
        existing = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
        tmp_path = filepath.with_name(filepath.name + f".tmp.{os.getpid()}")  # Unique temp file per process
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(existing + new_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)

    return filepath


def update_exchange(
    agent_name: str,
    task_id: str,
    inbound_text: str,
) -> bool:
    """Update the inbound text of an existing exchange (e.g. replace 'waiting' with actual reply)."""
    safe_name = "".join(c if c.isalnum() or c in "-_.@ " else "_" for c in agent_name.lower())
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    filepath = _CONV_DIR / safe_name / f"{today}.md"

    if not filepath.exists():
        return False

    with _lock:
        content = filepath.read_text(encoding="utf-8")
        # Find the entry with this task_id and replace the waiting placeholder
        marker = f"task:{task_id}"
        start = content.find(marker)
        if start == -1:
            return False
        block_start = content.rfind("## ", 0, start)
        if block_start == -1:
            return False
        block_end = content.find("\n---\n", start)
        if block_end == -1:
            block_end = len(content)
        else:
            block_end += len("\n---\n")

        block = content[block_start:block_end]
        updated_block = block.replace(
            f"**\u2190 {safe_name}:** (waiting for reply\u2026)",
            f"**\u2190 {safe_name}:** {inbound_text}",
            1,
        )
        if updated_block == block:
            return False
        updated = content[:block_start] + updated_block + content[block_end:]
        filepath.write_text(updated, encoding="utf-8")
    return True
