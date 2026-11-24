import logging
import time

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.config.resources import cache, engine
from src.services import directory
from src.services.execution import JobFailure, fail, service_one

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")


def check():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    heartbeat = cache.get("workbench:heartbeat")
    return heartbeat is not None and time.time() - float(heartbeat) < 90


def service_rebuild():
    """缩容影子重建优先推进；失败不影响普通批次队列。"""
    try:
        with engine.begin() as db:
            return directory.service_rebuild(db)
    except SQLAlchemyError:
        logger.exception("Directory rebuild iteration unavailable")
        return False


def work():
    while True:
        try:
            cache.set("workbench:heartbeat", time.time(), ex=90)
            try:
                if service_rebuild() or service_one():
                    continue
            except JobFailure as exc:
                logger.exception("Execution %s failed", exc.run_id)
                fail(exc)
                continue
            cache.blpop("workbench:wake", timeout=1)
        except (RedisError, SQLAlchemyError):
            logger.exception("Worker connection unavailable")
            time.sleep(2)


if __name__ == "__main__":
    work()
