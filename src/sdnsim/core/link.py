import random


def _new_stats():
    return {
        "sent_packets": 0, "sent_bytes": 0, "dropped_loss": 0,
        "dropped_queue": 0, "dropped_error": 0,
    }


class Link:
    """Liên kết hai chiều giữa hai node. Mỗi chiều có bộ phát và hàng đợi riêng.

    delay        độ trễ lan truyền (giây).
    bandwidth    tốc độ (bit/giây); None = không giới hạn (không có thời gian phát, không hàng đợi).
    loss         xác suất mất mỗi gói (0..1). Gói mất vẫn chiếm bộ phát: nó đã được gửi đi.
    error_rate   xác suất lỗi khung (0..1) sau khi truyền; switch nhận loại gói và đếm RX error.
    queue_limit  số gói tối đa đang chờ hoặc đang phát ở mỗi chiều; đầy thì gói mới bị bỏ
                 (tail drop). Chỉ có nghĩa khi có bandwidth.
    rng          nguồn ngẫu nhiên cho loss/error; mặc định Random(0) để kết quả lặp lại được.

    Bộ đếm theo chiều (xem direction_stats):
        sent_packets/sent_bytes  gói được nhận vào bộ phát (kể cả gói rồi bị mất trên đường)
        dropped_queue            gói bị bỏ vì hàng đợi đầy (không được gửi)
        dropped_loss             gói đã gửi nhưng mất trên đường, không hiện ở bộ đếm cổng
        dropped_error            frame lỗi, bị loại ở đầu nhận và tính vào RX errors nếu là switch
    """

    def __init__(self, sched, a, a_port, b, b_port, delay=0.001, bandwidth=None,
                 loss=0.0, queue_limit=None, rng=None, error_rate=0.0):
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
            raise ValueError(f"delay phải là số không âm: {delay!r}")
        if bandwidth is not None and (not isinstance(bandwidth, (int, float))
                                      or isinstance(bandwidth, bool) or bandwidth <= 0):
            raise ValueError(f"bandwidth phải là số dương (bit/giây): {bandwidth!r}")
        if not isinstance(loss, (int, float)) or isinstance(loss, bool) or not 0.0 <= loss <= 1.0:
            raise ValueError(f"loss phải nằm trong khoảng 0..1: {loss!r}")
        if (not isinstance(error_rate, (int, float)) or isinstance(error_rate, bool)
                or not 0.0 <= error_rate <= 1.0):
            raise ValueError(f"error_rate phải nằm trong khoảng 0..1: {error_rate!r}")
        if queue_limit is not None:
            if bandwidth is None:
                raise ValueError("queue_limit chỉ có nghĩa khi link có bandwidth")
            if not isinstance(queue_limit, int) or isinstance(queue_limit, bool) or queue_limit < 1:
                raise ValueError(f"queue_limit phải là số nguyên >= 1: {queue_limit!r}")

        self.sched, self.delay = sched, delay
        self.bandwidth, self.loss = bandwidth, loss
        self.error_rate, self.queue_limit = error_rate, queue_limit
        self.rng = rng if rng is not None else random.Random(0)
        self.peer = {a: (b, b_port), b: (a, a_port)}
        self.stats = {a: _new_stats(), b: _new_stats()}
        self._busy_until = {a: 0.0, b: 0.0}      # lúc bộ phát của từng chiều rảnh
        self._queued = {a: 0, b: 0}              # số gói đang chờ hoặc đang phát
        a.ports[a_port] = self
        b.ports[b_port] = self

    def transmit(self, sender, pkt):
        node, port = self.peer[sender]
        stats = self.stats[sender]
        
        if self.bandwidth is None:
            wait = tx_time = 0.0                  # giữ đúng giá trị cũ: chỉ có `delay`
        else:
            if self.queue_limit is not None and self._queued[sender] >= self.queue_limit:
                stats["dropped_queue"] += 1
                return
            now = self.sched.now
            wait = max(0.0, self._busy_until[sender] - now)
            tx_time = pkt.size * 8 / self.bandwidth
            self._busy_until[sender] = now + wait + tx_time
            self._queued[sender] += 1
            self.sched.schedule(wait + tx_time, self._finished, sender)
        stats["sent_packets"] += 1
        stats["sent_bytes"] += pkt.size
        if self.loss and self.rng.random() < self.loss:
            stats["dropped_loss"] += 1
            return
        if self.error_rate and self.rng.random() < self.error_rate:
            self.sched.schedule(
                wait + tx_time + self.delay, self._receive_error, sender, node, port)
            return
        self.sched.schedule(wait + tx_time + self.delay, node.receive, port, pkt)

    def _receive_error(self, sender, node, port):
        self.stats[sender]["dropped_error"] += 1
        if hasattr(node, "record_rx_error"):
            node.record_rx_error(port)

    def _finished(self, sender):
        self._queued[sender] -= 1

    def direction_stats(self, sender):
        """Bộ đếm của chiều bắt đầu từ `sender`, kèm số gói đang chờ trong hàng đợi."""
        return {**self.stats[sender], "queued": self._queued[sender]}