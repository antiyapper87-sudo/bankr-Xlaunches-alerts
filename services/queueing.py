from __future__ import annotations

from redis import Redis
from rq import Queue, Retry

from settings import settings


def get_redis_connection() -> Redis:
    return Redis.from_url(settings.redis_url)


def get_launch_queue() -> Queue:
    return Queue(settings.rq_queue_name, connection=get_redis_connection())


def enqueue_launch_enrichment(ca: str) -> str:
    ca = ca.lower()
    queue = get_launch_queue()
    job = queue.enqueue(
        "worker.enrich_launch",
        ca,
        job_id=f"enrich:{ca}",
        retry=Retry(max=3, interval=[10, 30, 90]),
        job_timeout=60,
        result_ttl=3600,
    )
    return job.id
