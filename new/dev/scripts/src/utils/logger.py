import logging
from rich.logging import RichHandler
from rich.console import Console

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
