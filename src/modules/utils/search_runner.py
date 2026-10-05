import asyncio
from contextlib import nullcontext

from rich.live import Live
from rich.text import Text


async def collect_results(tasks, config, total_sites, progress_message):
    """Collect site checks, cancelling unfinished work at the request deadline."""
    loop = asyncio.get_running_loop()
    pending = {asyncio.create_task(task) for task in tasks}
    results = []
    deadline = getattr(config, "deadline_at", None)
    callback = getattr(config, "progress_callback", None)
    completed = 0
    partial = False

    def render():
        percent = int((completed / total_sites) * 100) if total_sites else 100
        return Text.from_markup(
            f"🛰️  {progress_message} — [green1]{percent}%[/green1] ({completed}/{total_sites})"
        )

    live_context = (
        nullcontext(None)
        if callback
        else Live(render(), refresh_per_second=10, console=config.console)
    )

    try:
        with live_context as live:
            while pending:
                timeout = None if deadline is None else max(0, deadline - loop.time())
                done, pending = await asyncio.wait(
                    pending,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    partial = bool(pending)
                    break

                for task in done:
                    result = task.result()
                    results.append(result)
                    completed += 1
                    if callback:
                        callback(
                            {
                                "type": "progress",
                                "completed": completed,
                                "total": total_sites,
                                "result": result,
                            }
                        )
                    else:
                        live.update(render())
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return {
        "results": results,
        "completed": completed,
        "total": total_sites,
        "partial": partial,
    }
