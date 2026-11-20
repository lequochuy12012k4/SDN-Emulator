"""Controller học MAC chạy trên thư viện os-ken, không cần lệnh osken-manager.

os-ken 4.x/5.x không còn kèm trình quản lý và ứng dụng mẫu, nên file này tự dựng lại
phần khởi động (giống os_ken.cmd.manager của các bản cũ) và chứa luôn ứng dụng.

Chạy:  python examples\\os_ken_controller.py [cổng] [idle_timeout]
         cổng mặc định 6653 (nghe tại 127.0.0.1); idle_timeout lấy từ .env,
         mặc định 0 = không hết hạn. Tham số dòng lệnh sẽ ghi đè cấu hình .env.
         Ví dụ: python examples\\os_ken_controller.py 6653 5   -> flow hết hạn sau 5 giây không có gói
Chỉ nghe trên 127.0.0.1: không mở cổng điều khiển ra mạng.
"""

import logging
import os
import sys
from pathlib import Path

from os_ken import cfg
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.lib.packet import ethernet, packet
from os_ken.ofproto import ofproto_v1_3
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

class LearningSwitch(app_manager.OSKenApp):
    """Học MAC theo từng switch; flow match in_port + eth_dst + eth_src (như simple_switch_13)."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}
        # AppManager nạp lại file này như một module riêng nên không truyền được tham số qua
        # biến module; main() đặt giá trị vào biến môi trường để ứng dụng đọc ở đây.
        self.idle_timeout = int(os.environ.get("SDNSIM_OSKEN_IDLE_TIMEOUT", "0"))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # Flow table-miss: đẩy mọi gói chưa có flow lên controller.
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, parser.OFPMatch(), actions)

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


def main(port: int = 6653, idle_timeout: int | None = None) -> None:
    if idle_timeout is None:
        idle_timeout = int(os.environ.get("SDNSIM_OSKEN_IDLE_TIMEOUT", "0"))
    os.environ["SDNSIM_OSKEN_IDLE_TIMEOUT"] = str(idle_timeout)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    cfg.CONF(args=["--ofp-listen-host", "127.0.0.1", "--ofp-tcp-listen-port", str(port)],
             project="os_ken", default_config_files=[])
    manager = app_manager.AppManager.get_instance()
    manager.load_apps(["os_ken.controller.ofp_handler", __file__])
    services = manager.instantiate_apps(**manager.create_contexts())
    timeout_note = f", idle_timeout={idle_timeout}s" if idle_timeout else ""
    print(f"controller os-ken đang nghe tại 127.0.0.1:{port}{timeout_note} (Ctrl+C để dừng)", flush=True)
    try:
        hub.joinall(services)
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6653,
         int(sys.argv[2]) if len(sys.argv) > 2 else None)