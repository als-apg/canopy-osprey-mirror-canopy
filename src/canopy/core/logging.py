import sys
import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Configure application logging. Idempotent, safe to call repeatedly."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
