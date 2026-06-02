release: alembic upgrade head
bot: python main.py
worker: rq worker ${RQ_QUEUE_NAME:-launches} --url $REDIS_URL
maintenance: python maintenance.py
