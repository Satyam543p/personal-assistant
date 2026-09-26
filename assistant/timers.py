import asyncio
import logging
import time

logger = logging.getLogger("jarvis.timers")

class IdleTimerManager:
    def __init__(self, default_timeout):
        self.default_timeout = default_timeout
        self.resources = {}  # name -> { "unload_callback": fn, "timeout": float, "last_accessed": float, "task": asyncio.Task }
        self.lock = asyncio.Lock()

    async def register_or_update(self, name, unload_callback, timeout=None):
        """
        Register a lazy resource or update its access time to prevent unloading.
        """
        async with self.lock:
            await self._register_or_update_unlocked(name, unload_callback, timeout)

    async def _register_or_update_unlocked(self, name, unload_callback, timeout=None):
        timeout = timeout if timeout is not None else self.default_timeout
        now = time.time()
        
        # If already registered, cancel the existing timer task
        if name in self.resources:
            old_task = self.resources[name]["task"]
            if old_task and not old_task.done():
                old_task.cancel()
                
        # Set up the new watchdog task
        loop = asyncio.get_event_loop()
        task = loop.create_task(self._wait_and_unload(name, timeout))
        
        self.resources[name] = {
            "unload_callback": unload_callback,
            "timeout": timeout,
            "last_accessed": now,
            "task": task
        }
        logger.info(f"Registered/updated lazy resource '{name}' with timeout {timeout}s")

    async def touch(self, name):
        """
        Extend the life of a registered resource by updating its last-accessed time.
        """
        async with self.lock:
            if name in self.resources:
                unload_callback = self.resources[name]["unload_callback"]
                timeout = self.resources[name]["timeout"]
                await self._register_or_update_unlocked(name, unload_callback, timeout)
                logger.debug(f"Touched lazy resource '{name}'")
            else:
                logger.warning(f"Attempted to touch unregistered resource '{name}'")

    async def _wait_and_unload(self, name, timeout):
        try:
            await asyncio.sleep(timeout)
            async with self.lock:
                if name in self.resources:
                    res = self.resources.pop(name)
                    logger.info(f"Resource '{name}' idle timeout reached. Unloading...")
                    try:
                        if asyncio.iscoroutinefunction(res["unload_callback"]):
                            await res["unload_callback"]()
                        else:
                            res["unload_callback"]()
                        logger.info(f"Successfully unloaded '{name}'")
                    except Exception as e:
                        logger.error(f"Error unloading resource '{name}': {e}")
        except asyncio.CancelledError:
            # Task was cancelled because the resource was touched/updated
            pass

    async def cancel_all(self):
        """
        Cancel all pending unload timers and force unload all remaining resources.
        """
        async with self.lock:
            names = list(self.resources.keys())
            for name in names:
                res = self.resources.pop(name)
                if res["task"] and not res["task"].done():
                    res["task"].cancel()
                logger.info(f"Forced unloading resource '{name}' during shutdown")
                try:
                    if asyncio.iscoroutinefunction(res["unload_callback"]):
                        await res["unload_callback"]()
                    else:
                        res["unload_callback"]()
                except Exception as e:
                    logger.error(f"Error unloading resource '{name}' on shutdown: {e}")
