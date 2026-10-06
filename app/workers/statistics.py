"""In-process weekly scheduler; persisted snapshots/outbox provide restart safety."""

import asyncio

from app.core.logging import Event, log_event
from app.services.statistics_service import StatisticsService


async def run_statistics(*domains):
    while True:
        for domain in domains:
            try:
                await StatisticsService(domain).tick()
            except Exception:
                log_event(Event.STATISTICS_FAILED)
        await asyncio.sleep(60)
