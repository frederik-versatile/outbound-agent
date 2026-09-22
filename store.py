"""Durable key/value storage for exactly the state that must survive between
separate process invocations: OAuth tokens, the drafts audit trail, the
learned style notes, and the run log.

Render Cron Jobs (the hosting this project targets — see render.yaml) reset
their filesystem on every run: "cron jobs can't provision or access a
persistent disk" per Render's own docs. So anything written by one cron
invocation that a LATER invocation needs to read (a refreshed OAuth token,
a draft's audit entry that poll_for_edits.py reads hours later, the style
guide a drafting run needs to see) cannot live on local disk in production.

Two backends, selected by whether REDIS_URL is set:
- LocalFileStore: plain files under the project root, for local dev/testing
  (the default — REDIS_URL unset).
- RedisStore: Render's Key Value (Redis-compatible) service, or any other
  Redis-compatible URL, for production. Point REDIS_URL at the same
  instance when running a one-time setup_oauth_*.py script locally as the
  cron jobs use, so tokens land where the cron jobs will look for them.

Per-run scratch data (accounts_scored.json, stakeholders.json,
drafts_preview/*.md) is NOT stored here — those files are only read within
the same process run that wrote them (orchestrator.py's three stages run
sequentially in one invocation), so plain local disk under state/<id>/runs/
is fine for them even in production. See deployment.py's run_dir().
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path


class Store(ABC):
    @abstractmethod
    def read_text(self, key: str) -> str | None:
        """Returns None if the key doesn't exist, never raises for that case."""

    @abstractmethod
    def write_text(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    def append_line(self, key: str, line: str, *, max_lines: int | None = None) -> None:
        """Read-modify-write append, for the small bounded jsonl logs this
        project keeps (edit_diffs_log.jsonl, run_log.jsonl). Not built for
        high write volume — these logs are a handful of writes per run."""
        existing = self.read_text(key)
        lines = existing.splitlines() if existing else []
        lines.append(line)
        if max_lines is not None:
            lines = lines[-max_lines:]
        self.write_text(key, "\n".join(lines) + "\n")


class LocalFileStore(Store):
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def read_text(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text() if path.exists() else None

    def write_text(self, key: str, value: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class RedisStore(Store):
    def __init__(self, redis_url: str):
        import redis  # deferred import: local dev without REDIS_URL never needs this dependency present

        self._client = redis.Redis.from_url(redis_url, decode_responses=True)

    def read_text(self, key: str) -> str | None:
        return self._client.get(key)

    def write_text(self, key: str, value: str) -> None:
        self._client.set(key, value)

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(key))


def build_store(root: Path) -> Store:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        return RedisStore(redis_url)
    return LocalFileStore(root)
