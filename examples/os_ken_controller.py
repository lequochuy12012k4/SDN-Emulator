"""Controller học MAC chạy trên thư viện os-ken, không cần lệnh osken-manager.

os-ken 4.x/5.x không còn kèm trình quản lý và ứng dụng mẫu, nên file này tự dựng lại
phần khởi động (giống os_ken.cmd.manager của các bản cũ) và chứa luôn ứng dụng.

Chạy:  python examples\\os_ken_controller.py [cổng] [idle_timeout]
         cổng mặc định 6653 (nghe tại 127.0.0.1); idle_timeout là số giây nguyên, mặc định 0 = không hết hạn.
         Ví dụ: python examples\\os_ken_controller.py 6653 5   -> flow hết hạn sau 5 giây không có gói
Chỉ nghe trên 127.0.0.1: không mở cổng điều khiển ra mạng.

Giám sát (tùy chọn): đặt biến môi trường SDNSIM_OSKEN_STATS_LOG=<đường dẫn file> thì controller
hỏi thống kê cổng và flow của mọi switch mỗi SDNSIM_OSKEN_STATS_INTERVAL giây (mặc định 1) và ghi
mỗi trả lời thành một dòng JSON vào file đó. Không đặt thì không hỏi gì cả.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from os_ken import cfg
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls,
)
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, packet
from os_ken.ofproto import ofproto_v1_3
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

class LearningSwitch(app_manager.OSKenApp):
    """Học MAC theo từng switch; flow match in_port + eth_dst + eth_src (như simple_switch_13)."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        # AppManager nạp lại file này như một module riêng nên không truyền được tham số qua
        # biến module; main() đặt giá trị vào biến môi trường để ứng dụng đọc ở đây.
        self.idle_timeout = int(os.environ.get("SDNSIM_OSKEN_IDLE_TIMEOUT", "0"))
        self.datapaths = {}
        self.stats_log = os.environ.get("SDNSIM_OSKEN_STATS_LOG")
        self._log_lock = threading.Lock()
        if self.stats_log:
            interval = float(os.environ.get("SDNSIM_OSKEN_STATS_INTERVAL", "1"))
            self.logger.info("statistics polling enabled: %s every %ss", self.stats_log, interval)
            hub.spawn(self._monitor, interval)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # Flow table-miss: đẩy mọi gói chưa có flow lên controller.
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, parser.OFPMatch(), actions)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def on_state_change(self, ev):
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[ev.datapath.id] = ev.datapath
        else:
            self.datapaths.pop(ev.datapath.id, None)

    def _monitor(self, interval):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(interval)

    def _write_log(self, record):
        with self._log_lock, open(self.stats_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats(self, ev):
        if self.stats_log:
            self._write_log({"kind": "port", "dpid": ev.msg.datapath.id, "ports": [
                {"port_no": s.port_no, "rx_packets": s.rx_packets, "tx_packets": s.tx_packets,
                 "rx_bytes": s.rx_bytes, "tx_bytes": s.tx_bytes,
                 "rx_dropped": s.rx_dropped, "tx_dropped": s.tx_dropped,
                 "rx_errors": s.rx_errors, "tx_errors": s.tx_errors,
                 "rx_frame_err": s.rx_frame_err, "rx_over_err": s.rx_over_err,
                 "rx_crc_err": s.rx_crc_err, "collisions": s.collisions}
                for s in ev.msg.body]})

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats(self, ev):
        if self.stats_log:
            self._write_log({"kind": "flow", "dpid": ev.msg.datapath.id, "flows": [
                {"match": dict(s.match.items()), "priority": s.priority,
                 "idle_timeout": s.idle_timeout, "packet_count": s.packet_count,
                 "byte_count": s.byte_count}
                for s in ev.msg.body]})

    def _add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        instructions = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=instructions, idle_timeout=idle_timeout))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]
        eth = packet.Packet(msg.data).get_protocols(ethernet.ethernet)[0]

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self._add_flow(dp, 1, match, actions, self.idle_timeout)
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))


def main(port: int = 6653, idle_timeout: int = 0) -> None:
    os.environ["SDNSIM_OSKEN_IDLE_TIMEOUT"] = str(idle_timeout)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    cfg.CONF(args=["--ofp-listen-host", "127.0.0.1", "--ofp-tcp-listen-port", str(port)],
             project="os_ken", default_config_files=[])
    manager = app_manager.AppManager.get_instance()
    manager.load_apps(["os_ken.controller.ofp_handler", __file__])
    services = manager.instantiate_apps(**manager.create_contexts())
    timeout_note = f", idle_timeout={idle_timeout}s" if idle_timeout else ""
    print(f"os-ken controller listening on 127.0.0.1:{port}{timeout_note} (Ctrl+C to stop)", flush=True)
    try:
        hub.joinall(services)
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6653,
         int(sys.argv[2]) if len(sys.argv) > 2 else 0)