"""Dependency bundle passed to every graph node. Package A owns this file."""

import datetime as dt
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from config.settings import Settings
from db.repo import Repo
from models import InvoiceState, Provider
from obs import EventLogger, utc_now


@dataclass
class Deps:
    repo: Repo
    provider: Provider
    settings: Settings
    as_of: dt.date
    log: EventLogger
    payment_fail_rate: float = 0.0  # probability 0..1, not money
    sleep: Callable[[float], None] = time.sleep  # tests pass a recorder / no-op
    rng: random.Random = field(default_factory=random.Random)  # payment failure injection
    clock: Callable[[], dt.datetime] = utc_now
    node_hook: Callable[[str, InvoiceState], None] | None = None  # tests: raise here to inject a crash

    @property
    def budget_month(self) -> str:
        return self.as_of.strftime("%Y-%m")
