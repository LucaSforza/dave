import logging
import time

from .controller import ClusterController
from .settings import Settings
from .store import RedisStore
from .transport import SSHTransport


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    store = RedisStore(settings.redis_url, lock_seconds=settings.worker_lock_seconds)
    controller = ClusterController(store, SSHTransport(settings), settings)
    while True:
        try:
            controller.tick()
        except Exception:
            logging.exception("Controller pass unavailable; retrying without submitting blindly")
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
