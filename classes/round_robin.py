import asyncio
import logging

logger = logging.getLogger(__name__)


class AsyncRoundRobin:
    def __init__(self, items=None):
        self.items = items or []
        self.index = 0
        self.lock = asyncio.Lock()
        logger.debug(f"AsyncRoundRobin initialized with items: {self.items}")

    async def add(self, item):
        async with self.lock:
            if isinstance(item, list):
                for i in item:
                    if i not in self.items:
                        self.items.append(i)
            elif item not in self.items:
                self.items.append(item)
            logger.debug(f"Added item(s) to AsyncRoundRobin: {item}")

    async def get_all(self):
        async with self.lock:
            return self.items.copy()

    async def get_next(self):
        async with self.lock:
            if not self.items:
                logger.debug("No items in AsyncRoundRobin")
                return None
            item = self.items[self.index]
            self.index = (self.index + 1) % len(self.items)
            logger.debug(f"Get next returned: {item}")
            return item

    def get_current(self):
        if not self.items:
            logger.debug("No items in AsyncRoundRobin")
            return None
        item = self.items[self.index]
        logger.debug(f"Get current returned: {item}")
        return item

    async def delete(self, item):
        async with self.lock:
            if item in self.items:
                index = self.items.index(item)
                self.items.remove(item)
                if index < self.index:
                    self.index -= 1
                elif self.index == len(self.items):
                    self.index = 0
                logger.debug(f"Deleted item from AsyncRoundRobin: {item}")
            else:
                logger.debug(f"Item not found in AsyncRoundRobin: {item}")
