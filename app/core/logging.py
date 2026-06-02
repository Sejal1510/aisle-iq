import logging
import sys
import structlog

def setup_logging(environment: str, debug: bool) -> None:
    """Configure structured logging using structlog."""
    
    # Set the standard logging level based on debug mode
    log_level = logging.DEBUG if debug else logging.INFO

    # Clear existing handlers to prevent duplicate logs
    logging.getLogger().handlers.clear()
    
    # Configure standard library logging wrapper
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Use human-readable logs in development, and JSON logs in production
    if environment == "development":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
