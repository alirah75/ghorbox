"""Opt-in hourly maintenance; no identity or historical published summary deletion."""

import asyncio

from app.core.logging import Event, log_event
from app.services.maintenance_service import MaintenanceService


async def run_maintenance(*domains):
    while True:
        for domain in domains:
            try:
                await MaintenanceService(domain).run(apply=True)
            except Exception:
                log_event(Event.MAINTENANCE_FAILED)
        await asyncio.sleep(3600)
