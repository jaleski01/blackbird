import logging
import traceback
from pathlib import Path

def logError(e, message, config):
    if getattr(config, "suppress_sensitive_logs", False):
        frames = traceback.extract_tb(e.__traceback__)
        locations = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in frames[-8:]
        ) or "unknown"
        logging.error(
            "Blackbird operation failed (%s; frames=%s)",
            type(e).__name__,
            locations,
        )
        return

    if str(e) != "":
        error = str(e)
    else:
        error = repr(e)
    stacktrace = traceback.format_exc()
    
    logging.error(f"{message} | {error}")
    logging.error(stacktrace)
    if config.verbose:
        config.console.print(f"⛔  {message}")
        config.console.print("     | An error occurred:")
        config.console.print(f"     | {error}")
