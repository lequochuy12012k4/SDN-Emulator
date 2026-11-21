"""Kênh điều khiển thật: mỗi switch mô phỏng giữ một kết nối OpenFlow TCP tới controller.

Có cùng giao diện với sdnsim.control.channel.Channel mà Switch và Metrics dùng
(register, packet_in, packet_in_count, packet_in_by_switch), nhưng không có controller
trong tiến trình: phía bên kia là bất kỳ controller OpenFlow 1.3 nào.

Hai đồng hồ:
  - PACKET_IN gửi đi ngay khi switch bị table-miss (ghi vào bộ đệm TCP, không chờ).
  - Thông điệp từ controller đến theo giờ thật, nhưng được áp dụng vào switch sau
    `down_latency` giây GIỜ ẢO tính từ lúc nhận, theo đúng thứ tự đến. Nhờ vậy mỗi
    lần đi vòng qua controller luôn tốn ít nhất `down_latency` trong mô phỏng, và
    FLOW_MOD luôn được áp dụng trước PACKET_OUT gửi sau nó.
  - Điều này chỉ có tác dụng khi ai đó chạy mô phỏng (sched.run) trong cùng event loop.

Hỗ trợ multipart mô tả/thống kê OpenFlow 1.3, cùng GROUP ALL/INDIRECT và METER DROP/KBPS
trong phạm vi được mô phỏng.
Thông điệp bị từ chối (codec không hiểu, cổng không tồn tại, loại không hỗ trợ) được
trả lời bằng ERROR và phiên vẫn giữ nguyên. Chỉ khung hỏng (sai độ dài header) mới
làm đóng phiên.
"""

import asyncio
import logging
from collections import Counter
from itertools import count

from .codec import (
    OFPBAC_BAD_OUT_PORT, OFPBAC_BAD_TYPE, OFPBMC_BAD_FIELD, OFPBRC_BAD_LEN,
    OFPBRC_BAD_MULTIPART, OFPBRC_BAD_PORT, OFPBRC_BAD_TYPE, OFPET_BAD_ACTION,
    OFPET_BAD_MATCH, OFPET_BAD_REQUEST, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN,
    OFPMP_AGGREGATE, OFPMP_DESC, OFPMP_FLOW, OFPMP_GROUP, OFPMP_GROUP_DESC,
    OFPMP_METER, OFPMP_METER_CONFIG, OFPMP_PORT_DESC, OFPMP_PORT_STATS,
    OFPMP_TABLE,         OFPG_ALL, OFPG_ANY, OFPG_MAX, OFPGC_ADD, OFPGC_DELETE, OFPGC_MODIFY,
    OFPGMFC_BAD_BUCKET, OFPGMFC_BAD_COMMAND, OFPGMFC_BAD_TYPE,
    OFPGMFC_GROUP_EXISTS, OFPGMFC_GROUP_IN_USE, OFPGMFC_INVALID_GROUP,
    OFPGMFC_UNKNOWN_GROUP, OFPMMFC_BAD_BAND, OFPMMFC_BAD_COMMAND,
    OFPMMFC_BAD_FLAGS, OFPMMFC_INVALID_METER, OFPMMFC_METER_EXISTS,
    OFPMMFC_METER_IN_USE, OFPMMFC_UNKNOWN_METER, OFPMMFC_UNSUPPORTED,
    OFPGT_ALL, OFPGT_INDIRECT, OFPM_ALL, OFPM_MAX, OFPMC_ADD, OFPMC_DELETE,
    OFPMC_MODIFY, OFPMF_BURST, OFPMF_KBPS, OFPMF_SUPPORTED, OFPMBT_DROP,
    OFPP_ANY, OFPET_GROUP_MOD_FAILED, OFPET_METER_MOD_FAILED,
    OFPT_BARRIER_REPLY, OFPT_BARRIER_REQUEST, OFPT_GROUP_MOD,
    OFPT_METER_MOD, OFPT_MULTIPART_REQUEST, OFPT_SET_CONFIG, OFPTT_ALL,
    AggregateStats, BucketStats, FlowStats, GroupBucket, GroupDesc, GroupStats, MeterConfig,
    MeterStats, PortDesc, PortStats, TableStats, pack_aggregate_stats_reply,
    pack_desc_stats_reply, pack_error, pack_flow_stats_reply, pack_group_desc_reply,
    pack_group_stats_reply, pack_meter_config_reply, pack_meter_stats_reply,
    pack_packet_in, pack_port_desc_reply, pack_port_stats_reply, pack_table_stats_reply,
    parse_flow_mod, parse_flow_mod_cookie, parse_flow_stats_request, parse_group_mod,
    parse_group_desc_request, parse_group_stats_request, parse_meter_mod, parse_meter_stats_request,
    parse_multipart_request, parse_packet_out, parse_port_stats_request,
)
from .header import OFPT_FLOW_MOD, OFPT_PACKET_OUT, Message
from .session import OpenFlowSession

log = logging.getLogger("sdnsim.openflow")


class OpenFlowChannel:
    def __init__(self, sched, host: str = "127.0.0.1", port: int = 6653,
                 down_latency: float = 0.001):
        self.sched, self.host, self.port, self.down_latency = sched, host, port, down_latency
        self.switches = {}
        self.dpids = {}
        self.packet_in_count = 0
        self.packet_in_by_switch = Counter()
        self.errors = []                     # (tên switch, loại lỗi, mã lỗi) đã gửi cho controller
        self._sessions: dict[str, OpenFlowSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._xid = count(1)

    # ------------------------------------------------------ giao diện cho Switch

    def register(self, sw) -> None:
        self.switches[sw.name] = sw
        self.dpids[sw.name] = len(self.dpids) + 1

    def packet_in(self, sw, in_port, pkt) -> None:
        self.packet_in_count += 1
        self.packet_in_by_switch[sw.name] += 1
        session = self._sessions.get(sw.name)
        if session is None:
            sw.port_stats[in_port]["rx_dropped"] += 1
            return              # chưa kết nối (hoặc đã mất kết nối): gói bị bỏ
        if not self._send(sw.name, session, pack_packet_in(in_port, pkt, xid=next(self._xid))):
            sw.port_stats[in_port]["rx_dropped"] += 1

    # --------------------------------------------------------------- vòng đời

    async def start(self) -> None:
        """Kết nối các switch chưa có phiên tới controller."""
        for name in self.switches:
            if name in self._sessions:
                continue
            session = OpenFlowSession(self.dpids[name])
            try:
                await session.connect(self.host, self.port)
                await session.handshake()
            except BaseException:
                await session.close()
                raise
            self._sessions[name] = session
            self._tasks[name] = asyncio.create_task(self._serve(name, session))

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in list(self._sessions.values()):
            await session.close()
        self._tasks.clear()
        self._sessions.clear()

    def connected(self, name: str) -> bool:
        return name in self._sessions

    # ------------------------------------------------------------- nội bộ

    async def _serve(self, name: str, session: OpenFlowSession) -> None:
        try:
            await session.serve(lambda msg: self._on_message(name, session, msg))
        except ValueError:
            pass                # khung hỏng: bỏ phiên (quy ước của MessageReader)
        finally:
            if self._sessions.get(name) is session:
                self._sessions.pop(name, None)
            if self._tasks.get(name) is asyncio.current_task():
                self._tasks.pop(name, None)
            await session.close()

    def _send(self, name: str, session: OpenFlowSession, msg: Message) -> bool:
        try:
            session.send_nowait(msg)
        except ConnectionError:
            if self._sessions.get(name) is session:
                self._sessions.pop(name, None)
            return False
        return True

    def _later(self, fn, *args) -> None:
        self.sched.schedule(self.down_latency, fn, *args)

    def _error(self, name, session, err_type, code, request) -> None:
        self.errors.append((name, err_type, code))
        log.warning("%s: từ chối thông điệp type=%s, trả ERROR loại %s mã %s",
                    name, request.type, err_type, code)
        self._send(name, session, pack_error(err_type, code, request))

    @staticmethod
    def _port_exists(sw, out_port) -> bool:
        return out_port == "FLOOD" or (isinstance(out_port, int) and out_port in sw.ports)

    def _on_message(self, name: str, session: OpenFlowSession, msg: Message) -> None:
        sw = self.switches[name]
        if msg.type == OFPT_PACKET_OUT:
            self._on_packet_out(sw, session, msg)
        elif msg.type == OFPT_FLOW_MOD:
            self._on_flow_mod(sw, session, msg)
        elif msg.type == OFPT_GROUP_MOD:
            self._on_group_mod(sw, session, msg)
        elif msg.type == OFPT_METER_MOD:
            self._on_meter_mod(sw, session, msg)
        elif msg.type == OFPT_MULTIPART_REQUEST:
            self._on_multipart(sw, session, msg)
        elif msg.type == OFPT_BARRIER_REQUEST:
            # Trả lời sau các thông điệp đã xếp lịch trước đó, đúng nghĩa của barrier.
            self._later(self._send, name, session, Message(OFPT_BARRIER_REPLY, xid=msg.xid))
        elif msg.type == OFPT_SET_CONFIG:
            pass                # miss_send_len và cờ: switch mô phỏng luôn gửi nguyên gói
        else:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE, msg)

    def _on_multipart(self, sw, session, msg: Message) -> None:
        """Trả lời các yêu cầu multipart được simulator hỗ trợ."""
        try:
            mp_type, _flags, body = parse_multipart_request(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
            return
        if mp_type == OFPMP_DESC:
            if body:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
                return
            for reply in pack_desc_stats_reply(sw.name, self.dpids[sw.name], xid=msg.xid):
                self._send(sw.name, session, reply)
            return
        if mp_type == OFPMP_TABLE:
            if body:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
                return
            table = TableStats(0, len(sw.flows), sw.table_lookups, sw.table_matched)
            for reply in pack_table_stats_reply([table], xid=msg.xid):
                self._send(sw.name, session, reply)
            return
        if mp_type == OFPMP_PORT_STATS:
            self._on_port_stats(sw, session, msg, body)
            return
        if mp_type == OFPMP_FLOW:
            self._on_flow_stats(sw, session, msg, body)
            return
        if mp_type == OFPMP_AGGREGATE:
            self._on_aggregate_stats(sw, session, msg, body)
            return
        if mp_type in (OFPMP_GROUP, OFPMP_GROUP_DESC):
            try:
                if mp_type == OFPMP_GROUP:
                    group_id = parse_group_stats_request(body)
                else:
                    parse_group_desc_request(body)
                    group_id = OFPG_ALL
            except ValueError:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
                return
            groups = {
                gid: group for gid, group in sw.groups.items()
                if group_id == OFPG_ALL or gid == group_id
            }
            if mp_type == OFPMP_GROUP:
                now = self.sched.now
                stats = [
                    GroupStats(
                        gid,
                        sum(flow.get("group_id") == gid for flow in sw.flows),
                        group["packets"], group["bytes"],
                        int(now - group["installed_at"]),
                        int(((now - group["installed_at"]) % 1) * 1e9),
                        [BucketStats(bucket["packets"], bucket["bytes"])
                         for bucket in group["buckets"]],
                    )
                    for gid, group in sorted(groups.items())
                ]
                replies = pack_group_stats_reply(stats, xid=msg.xid)
            else:
                desc = [
                    GroupDesc(group["type"], gid, [
                        GroupBucket(bucket["out_port"], bucket["weight"],
                                    bucket["watch_port"], bucket["watch_group"])
                        for bucket in group["buckets"]
                    ])
                    for gid, group in sorted(groups.items())
                ]
                replies = pack_group_desc_reply(desc, xid=msg.xid)
            for reply in replies:
                self._send(sw.name, session, reply)
            return
        if mp_type in (OFPMP_METER, OFPMP_METER_CONFIG):
            try:
                meter_id = parse_meter_stats_request(body)
            except ValueError:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
                return
            meters = {
                mid: meter for mid, meter in sw.meters.items()
                if meter_id == OFPM_ALL or mid == meter_id
            }
            now = self.sched.now
            if mp_type == OFPMP_METER:
                stats = [
                    MeterStats(
                        mid,
                        sum(flow.get("meter_id") == mid for flow in sw.flows),
                        meter["packets"], meter["bytes"],
                        int(now - meter["installed_at"]),
                        int(((now - meter["installed_at"]) % 1) * 1e9),
                        [BucketStats(meter["dropped_packets"], meter["dropped_bytes"])],
                    )
                    for mid, meter in sorted(meters.items())
                ]
                replies = pack_meter_stats_reply(stats, xid=msg.xid)
            else:
                configs = [
                    MeterConfig(meter["flags"], mid, meter["rate"], meter["burst_size"])
                    for mid, meter in sorted(meters.items())
                ]
                replies = pack_meter_config_reply(configs, xid=msg.xid)
            for reply in replies:
                self._send(sw.name, session, reply)
            return
        if mp_type != OFPMP_PORT_DESC:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_MULTIPART, msg)
            return
        dpid = self.dpids[sw.name]
        ports = [PortDesc(p, f"02:00:00:00:{dpid & 0xFF:02x}:{p & 0xFF:02x}",
                          f"{sw.name}-eth{p}"[:15], sw.ports[p].bandwidth)
                 for p in sorted(sw.ports)]
        # Gửi ngay, không qua hàng đợi giờ ảo: đây là thông tin cấu hình, không phải lưu lượng.
        self._send(sw.name, session, pack_port_desc_reply(ports, xid=msg.xid))

    def _port_stats(self, sw, port: int, now: float) -> PortStats:
        counters = sw.port_stats.get(port, {})
        link = sw.ports[port]
        return PortStats(
            port,
            rx_packets=counters.get("rx_packets", 0), tx_packets=counters.get("tx_packets", 0),
            rx_bytes=counters.get("rx_bytes", 0), tx_bytes=counters.get("tx_bytes", 0),
            rx_dropped=counters.get("rx_dropped", 0),
            # Gói bị hàng đợi của link bỏ là gói switch không gửi đi được: tính vào tx_dropped.
            # Gói mất trên đường (loss) không ai đếm được ở hai đầu nên không xuất hiện ở đây.
            tx_dropped=(counters.get("tx_dropped", 0)
                        + link.stats[sw]["dropped_queue"] if hasattr(link, "stats") else 0),
            # Không có mô hình lỗi khung/CRC; các bộ đếm lỗi chỉ tăng khi có nguồn lỗi được mô phỏng.
            rx_errors=counters.get("rx_errors", 0), tx_errors=counters.get("tx_errors", 0),
            rx_frame_err=counters.get("rx_frame_err", 0),
            rx_over_err=counters.get("rx_over_err", 0),
            rx_crc_err=counters.get("rx_crc_err", 0), collisions=counters.get("collisions", 0),
            duration_sec=int(now), duration_nsec=int((now % 1) * 1e9),
        )

    def _on_port_stats(self, sw, session, msg: Message, body: bytes) -> None:
        try:
            port_no = parse_port_stats_request(body)
        except ValueError:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
            return
        if port_no == OFPP_ANY:
            ports = sorted(sw.ports)
        elif port_no in sw.ports:
            ports = [port_no]
        else:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_PORT, msg)
            return
        now = self.sched.now
        stats = [self._port_stats(sw, p, now) for p in ports]
        for reply in pack_port_stats_reply(stats, xid=msg.xid):
            self._send(sw.name, session, reply)

    def _flow_stats_for_request(self, sw, request, now: float) -> list[FlowStats]:
        flows = []
        if request.table_id not in (0, OFPTT_ALL):
            return flows
        for f in sw.flows:
            if any(f["match"].get(field) != value for field, value in request.match.items()):
                continue
            group_id = f.get("group_id")
            if request.out_group is not None and group_id != request.out_group:
                continue
            if request.out_port is not None:
                if group_id is None:
                    if f["out_port"] != request.out_port:
                        continue
                else:
                    group = sw.groups.get(group_id)
                    if group is None or not any(
                            bucket["out_port"] == request.out_port
                            for bucket in group["buckets"]):
                        continue
            if (f.get("cookie", 0) & request.cookie_mask) != (request.cookie & request.cookie_mask):
                continue
            age = max(0.0, now - f.get("installed_at", f["last_hit"]))
            flows.append(FlowStats(
                dict(f["match"]), f["out_port"], f["priority"], f["idle_timeout"],
                f["packets"], f["bytes"], int(age), int((age % 1) * 1e9),
                f.get("cookie", 0), group_id, f.get("meter_id")))
        return flows

    def _on_flow_stats(self, sw, session, msg: Message, body: bytes) -> None:
        try:
            request = parse_flow_stats_request(body)
        except ValueError as exc:
            if "match" in str(exc):
                self._error(sw.name, session, OFPET_BAD_MATCH, OFPBMC_BAD_FIELD, msg)
            else:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
            return
        flows = self._flow_stats_for_request(sw, request, self.sched.now)
        for reply in pack_flow_stats_reply(flows, xid=msg.xid):
            self._send(sw.name, session, reply)

    def _on_aggregate_stats(self, sw, session, msg: Message, body: bytes) -> None:
        try:
            request = parse_flow_stats_request(body)
        except ValueError as exc:
            if "match" in str(exc):
                self._error(sw.name, session, OFPET_BAD_MATCH, OFPBMC_BAD_FIELD, msg)
            else:
                self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
            return
        flows = self._flow_stats_for_request(sw, request, self.sched.now)
        aggregate = AggregateStats(
            sum(flow.packet_count for flow in flows),
            sum(flow.byte_count for flow in flows),
            len(flows),
        )
        for reply in pack_aggregate_stats_reply(aggregate, xid=msg.xid):
            self._send(sw.name, session, reply)
    def _on_packet_out(self, sw, session, msg: Message) -> None:
        try:
            out_port, in_port, pkt = parse_packet_out(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_TYPE, msg)
            return
        if not self._port_exists(sw, out_port):
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT, msg)
            return
        self._later(sw.packet_out, pkt, out_port, in_port)

    def _on_flow_mod(self, sw, session, msg: Message) -> None:
        try:
            spec = parse_flow_mod(msg)
            cookie = parse_flow_mod_cookie(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, msg)
            return
        if spec.match == {} and spec.out_port == "CONTROLLER":
            return              # flow table-miss: switch mô phỏng đã tự gửi PACKET_IN khi miss
        self._later(self._apply_flow_mod, sw, session, msg, spec, cookie)

    def _apply_flow_mod(self, sw, session, msg, spec, cookie) -> None:
        if spec.out_port == "CONTROLLER":
            self._error(sw.name, session, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, msg)
            return
        if spec.group_id is not None and spec.group_id not in sw.groups:
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_UNKNOWN_GROUP, msg)
            return
        if spec.meter_id is not None and spec.meter_id not in sw.meters:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_UNKNOWN_METER, msg)
            return
        if spec.group_id is None and not self._port_exists(sw, spec.out_port):
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT, msg)
            return
        log.info("%s nhận FLOW_MOD: match=%s -> %s, priority=%s, idle_timeout=%s",
                 sw.name, spec.match, spec.out_port, spec.priority, spec.idle_timeout)
        sw.flow_mod(
            spec.match, spec.out_port, spec.priority, spec.idle_timeout,
            cookie, spec.group_id, spec.meter_id)

    def _on_group_mod(self, sw, session, msg: Message) -> None:
        try:
            spec = parse_group_mod(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_BUCKET, msg)
            return
        self._later(self._apply_group_mod, sw, session, msg, spec)

    def _apply_group_mod(self, sw, session, msg, spec) -> None:
        if spec.command not in (OFPGC_ADD, OFPGC_MODIFY, OFPGC_DELETE):
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_COMMAND, msg)
            return
        if spec.command == OFPGC_DELETE:
            if spec.buckets:
                self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_BUCKET, msg)
                return
            group_ids = list(sw.groups) if spec.group_id == OFPG_ALL else [spec.group_id]
            if spec.group_id != OFPG_ALL and spec.group_id not in sw.groups:
                self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_UNKNOWN_GROUP, msg)
                return
            if any(flow.get("group_id") in group_ids for flow in sw.flows):
                self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_GROUP_IN_USE, msg)
                return
            sw.group_mod(spec.command, spec.group_type, spec.group_id, spec.buckets)
            return

        if not 0 <= spec.group_id < OFPG_MAX:
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_INVALID_GROUP, msg)
            return
        if spec.group_type not in (OFPGT_ALL, OFPGT_INDIRECT):
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_TYPE, msg)
            return
        if spec.command == OFPGC_ADD and spec.group_id in sw.groups:
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_GROUP_EXISTS, msg)
            return
        if spec.command == OFPGC_MODIFY:
            current = sw.groups.get(spec.group_id)
            if current is None:
                self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_UNKNOWN_GROUP, msg)
                return
            if current["type"] != spec.group_type:
                self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_TYPE, msg)
                return
        if (not spec.buckets
                or (spec.group_type == OFPGT_INDIRECT and len(spec.buckets) != 1)
                or any(bucket.weight != 0
                       or bucket.watch_port != OFPP_ANY
                       or bucket.watch_group != OFPG_ANY
                       or bucket.out_port not in sw.ports
                       for bucket in spec.buckets)):
            self._error(sw.name, session, OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_BUCKET, msg)
            return
        sw.group_mod(spec.command, spec.group_type, spec.group_id, spec.buckets)

    def _on_meter_mod(self, sw, session, msg: Message) -> None:
        try:
            spec = parse_meter_mod(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_BAD_BAND, msg)
            return
        self._later(self._apply_meter_mod, sw, session, msg, spec)

    def _apply_meter_mod(self, sw, session, msg, spec) -> None:
        if spec.command not in (OFPMC_ADD, OFPMC_MODIFY, OFPMC_DELETE):
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_BAD_COMMAND, msg)
            return
        if spec.command == OFPMC_DELETE:
            meter_ids = list(sw.meters) if spec.meter_id == OFPM_ALL else [spec.meter_id]
            if spec.meter_id != OFPM_ALL and spec.meter_id not in sw.meters:
                self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_UNKNOWN_METER, msg)
                return
            if any(flow.get("meter_id") in meter_ids for flow in sw.flows):
                self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_METER_IN_USE, msg)
                return
            sw.meter_mod(spec.command, spec.flags, spec.meter_id, None, 0)
            return

        if not 0 < spec.meter_id < OFPM_MAX:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_INVALID_METER, msg)
            return
        if spec.flags & ~OFPMF_SUPPORTED:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_BAD_FLAGS, msg)
            return
        if not spec.flags & OFPMF_KBPS:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_UNSUPPORTED, msg)
            return
        if spec.command == OFPMC_ADD and spec.meter_id in sw.meters:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_METER_EXISTS, msg)
            return
        if spec.command == OFPMC_MODIFY and spec.meter_id not in sw.meters:
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_UNKNOWN_METER, msg)
            return
        if (spec.rate is None or spec.rate <= 0
                or (spec.flags & OFPMF_BURST and spec.burst_size <= 0)
                or (not spec.flags & OFPMF_BURST and spec.burst_size != 0)):
            self._error(sw.name, session, OFPET_METER_MOD_FAILED, OFPMMFC_BAD_BAND, msg)
            return
        sw.meter_mod(spec.command, spec.flags, spec.meter_id, spec.rate, spec.burst_size)