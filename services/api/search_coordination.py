"""Cross-process OpenSearch rebuild gate backed by the commerce SQL database.

A durable row fences incremental sync while a full catalog generation is built.
The rebuild waits for already leased deliveries to finish before alias promotion;
importers are free to write new products/outbox entries throughout the build.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update

LEASE_SECONDS = 300


class SearchRebuildBusy(RuntimeError):
    """Another reindex is running or incremental sync did not drain."""


def begin_rebuild(session_factory, state_model, outbox_model, *, wait_seconds: float = 35,
                  sleep=time.sleep, clock=lambda: datetime.now(timezone.utc)) -> str:
    """Lease the rebuild gate, then wait for previously claimed syncs to ACK.

    In-flight syncs may still write to the old alias, but cannot start once the
    gate has been acquired. Lease expiry permits recovery if the process dies.
    """
    owner = str(uuid.uuid4())
    with session_factory.begin() as db:
        now = clock()
        acquired = db.execute(update(state_model).where(
            state_model.id == 1,
            or_(state_model.owner.is_(None), state_model.lease_until <= now),
        ).values(owner=owner, lease_until=now + timedelta(seconds=LEASE_SECONDS)))
        if acquired.rowcount != 1:
            raise SearchRebuildBusy('A full search index rebuild is already active')
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            now = clock()
            with session_factory() as db:
                in_flight = db.scalar(select(func.count()).select_from(outbox_model).where(
                    outbox_model.kind == 'search', outbox_model.delivered.is_(False),
                    outbox_model.lease_until > now))
            if not in_flight:
                break
            if time.monotonic() >= deadline:
                raise SearchRebuildBusy(f'{in_flight} search deliveries are still in flight; retry later')
            sleep(min(.25, max(0, deadline - time.monotonic())))
            renew_rebuild(session_factory, state_model, owner, clock=clock)
    except Exception:
        end_rebuild(session_factory, state_model, owner)
        raise
    return owner


def renew_rebuild(session_factory, state_model, owner: str,
                  *, clock=lambda: datetime.now(timezone.utc)) -> None:
    """Refuse to promote if the lock expired or was taken over."""
    now = clock()
    with session_factory.begin() as db:
        changed = db.execute(update(state_model).where(
            state_model.id == 1, state_model.owner == owner,
            state_model.lease_until > now,
        ).values(lease_until=now + timedelta(seconds=LEASE_SECONDS)))
        if changed.rowcount != 1:
            raise SearchRebuildBusy('Rebuild lease lost; index must not be promoted')


def end_rebuild(session_factory, state_model, owner: str) -> None:
    """Never unlock a gate owned by a different indexer."""
    with session_factory.begin() as db:
        db.execute(update(state_model).where(state_model.id == 1, state_model.owner == owner)
                   .values(owner=None, lease_until=None))


def sync_gate_open(db, state_model, *, clock=lambda: datetime.now(timezone.utc)) -> bool:
    """Atomic no-op UPDATE serializes gate check with lease acquisition.

    Lock is held until the caller commits the Outbox claim: a rebuild cannot
    start between the status check and claim commit, on SQLite or PostgreSQL.
    """
    now = clock()
    updated = db.execute(update(state_model).where(
        state_model.id == 1,
        or_(state_model.owner.is_(None), state_model.lease_until <= now),
    ).values(lease_until=state_model.lease_until))
    return updated.rowcount == 1
