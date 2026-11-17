import heapq
import itertools


class Scheduler:
    """Đồng hồ ảo + hàng đợi sự kiện."""

    def __init__(self):
        self.now = 0.0
        self._q = []
        self._seq = itertools.count()

    def schedule(self, delay, fn, *args):
        heapq.heappush(self._q, (self.now + delay, next(self._seq), fn, args))

    def run(self):
        while self._q:
            t, _, fn, args = heapq.heappop(self._q)
            self.now = t
            fn(*args)