# SDN Emulator (`sdnsim`)

**SDN Emulator** là bộ giả lập mạng **Software-Defined Networking (SDN) & OpenFlow 1.3** thuần Python chạy trực tiếp trên **Windows** (không cần máy ảo Linux hay WSL2), tích hợp:
- **Ứng dụng Desktop (PyWebView)** để thiết kế Topology trực quan (kéo thả thiết bị, cấu hình cổng, nối/xóa link, Host Console & Controller Console).
- **Web Dashboard (FastAPI + WebSocket)** đồng bộ thời gian thực 100% với Desktop (sơ đồ, cổng, lưu lượng, bảng Flow).
- **OpenFlow 1.3 TCP** tương thích với Controller ngoài viết bằng `os-ken`.
- **Wireshark Capture** hỗ trợ xuất `.pcap` chuẩn (gồm cả gói Host và OpenFlow 1.3) và phát Live qua UDP TZSP.

---

## Mục lục
- [Tính năng chính](#tính-năng-chính)
- [Kiến trúc & Cấu trúc thư mục](#kiến-trúc--cấu-trúc-thư-mục)
- [Yêu cầu & Cài đặt](#yêu-cầu--cài-đặt)
- [Khởi chạy ứng dụng](#khởi-chạy-ứng-dụng)
- [Hướng dẫn sử dụng chi tiết](#hướng-dẫn-sử-dụng-chi-tiết)
  - [1. Thiết kế & chỉnh sửa Topology](#1-thiết-kế--chỉnh-sửa-topology)
  - [2. Nối Link, cấu hình cổng và xóa Link](#2-nối-link-cấu-hình-cổng-và-xóa-link)
  - [3. Lưu & mở Topology dưới dạng file JSON](#3-lưu--mở-topology-dưới-dạng-file-json)
  - [4. Ping từ Host Console & theo dõi trên Web Dashboard](#4-ping-từ-host-console--theo-dõi-trên-web-dashboard)
  - [5. Controller Console & kết nối OpenFlow 1.3 (os-ken)](#5-controller-console--kết-nối-openflow-13-os-ken)
  - [6. Quan sát gói tin trên Wireshark (Live TZSP & xuất .pcap)](#6-quan-sát-gói-tin-trên-wireshark-live-tzsp--xuất-pcap)
  - [7. Chuyển đổi giao diện Sáng / Tối / Theo thiết bị](#7-chuyển-đổi-giao-diện-sáng--tối--theo-thiết-bị)
- [Chạy bộ kiểm thử (Tests)](#chạy-bộ-kiểm-thử-tests)
- [Lưu ý & Giới hạn thiết kế](#lưu-ý--giới-hạn-thiết-kế)

---

## Tính năng chính

- **Lõi mô phỏng sự kiện rời rạc (`Scheduler`)**:
  - Đồng hồ ảo cho kết quả tái lập; hỗ trợ `ARP` (có bảng ARP và thử lại khi mất gói), `ICMP Echo Request/Reply`, bảng Flow OpenFlow hỗ trợ `priority`, `idle_timeout`, bộ đếm gói/byte theo cổng và theo flow.
  - Mô phỏng thông số mạng thực tế cho cả **Link** (`delay`, `bandwidth`, `loss`, `queue_limit`, bộ đếm `sent/dropped_loss/dropped_queue`) lẫn **Switch** (`delay` xử lý, `bandwidth` chuyển mạch, `queue_limit`).
- **Giao thức OpenFlow 1.3 qua TCP**:
  - Hỗ trợ `HELLO`, `ECHO`, `FEATURES`, `MULTIPART` (`PORT_DESC`, `PORT_STATS`, `FLOW_STATS`), `PACKET_IN`, `PACKET_OUT`, `FLOW_MOD` (match `in_port`, `eth_dst`, `eth_src`, `idle_timeout`), `BARRIER`, `ERROR`.
  - Hỗ trợ cả **Controller nội bộ** (`LearningController`) và **Controller ngoài** viết bằng `os-ken`.
- **Ứng dụng Desktop Topology Studio (PyWebView)**:
  - Khởi động với vùng vẽ trắng để tự thiết kế hoặc mở từ file `.json`.
  - Kéo thả **Host**, **Switch**, **Controller** (mỗi loại có icon riêng); chuột trái để chọn/kéo, nhấn đúp để xem & sửa cấu hình, chuột phải để mở menu tính năng.
  - Chọn cổng tường minh khi nối Link (hỗ trợ cả Data Link và Control Link `Controller ↔ Switch`); trỏ chuột vào dây để xóa; bấm vào Data Link để chỉnh sửa thông số.
- **Web Dashboard đồng bộ 100%**:
  - Truy cập qua trình duyệt tại `http://127.0.0.1:8000/`.
  - Sơ đồ topo, tọa độ, số cổng, đường link và số liệu cập nhật trực tiếp qua WebSocket.
  - Mỗi Switch chia 2 bảng song song có đường kẻ chia cột: **Thống kê cổng** (trái) và **Bảng Flow** (phải).
- **Quan sát gói tin trên Wireshark**:
  - Xuất file `.pcap` chuẩn bao gồm cả gói **Data Plane** (`ARP`, `ICMP`) và **Control Plane** (`OpenFlow 1.3`).
  - Hỗ trợ phát gói **Live (UDP TZSP cổng 37008)** để xem trực tiếp trên Wireshark.

---

## Kiến trúc & Cấu trúc thư mục

```text
sdn-emulator/
├── desktop/
│   └── main.py                     # Điểm khởi chạy ứng dụng Desktop (PyWebView + Uvicorn nền)
├── src/sdnsim/
│   ├── core/                       # Lõi mô phỏng (không phụ thuộc UI/API/OpenFlow)
│   │   ├── scheduler.py            # Đồng hồ ảo & hàng đợi sự kiện (heapq)
│   │   ├── packet.py               # Định nghĩa Packet & kích thước khung thật
│   │   ├── link.py                 # Đường truyền (delay, bandwidth, loss, queue_limit)
│   │   ├── host.py                 # Host ảo (ARP table, ping, ping_ip, retry)
│   │   ├── switch.py               # Switch ảo (flow table, match, idle_timeout, delay/bw)
│   │   └── messages.py             # Thông điệp trừu tượng PacketIn, FlowMod, PacketOut
│   ├── control/
│   │   ├── channel.py              # Kênh điều khiển nội bộ
│   │   └── controllers/learning.py # LearningController nội bộ
│   ├── openflow/                   # Giao thức OpenFlow 1.3 qua TCP
│   │   ├── header.py, reader.py    # Header 8 byte & bộ tách khung TCP stream
│   │   ├── frame.py                # Đóng gói/giải gói Ethernet II, ARP, IPv4, ICMP + checksum
│   │   ├── codec.py                # Mã hóa/giải mã PACKET_IN, PACKET_OUT, FLOW_MOD, MULTIPART...
│   │   ├── session.py, channel.py  # Phiên OpenFlow TCP bất đồng bộ (asyncio)
│   │   └── agent.py
│   ├── topology/
│   │   ├── network.py              # build_network() & load_network_from_json()
│   │   └── model.py                # TopologyModel (CRUD Host, Switch, Controller, Link, vị trí)
│   ├── metrics/
│   │   └── collector.py            # Thu thập snapshot thống kê cổng, flow, link
│   ├── api/                        # FastAPI server cho chế độ chạy dòng lệnh
│   │   ├── app.py
│   │   └── main.py
│   ├── desktop/                    # Backend hợp nhất Desktop + Web
│   │   ├── sim.py                  # SimulationManager, ControlPlaneHub, LogHub
│   │   ├── server.py               # FastAPI + WebSocket (/desktop, /, /ws, /ws/logs, /api/...)
│   │   └── capture.py              # Bộ bắt gói xuất .pcap & phát Live TZSP
│   └── ui/
│       ├── script/
│       │   ├── index.js          
│       │   ├── desktop.js
│       ├── style/
│       │   ├── index.css          
│       │   ├── desktop.css         
│       └── template/
│           ├── index.html          # Giao diện Web Dashboard
│           └── desktop.html        # Giao diện Desktop Topology Studio
├── examples/
│   ├── os_ken_controller.py        # Controller học MAC + giám sát thống kê dùng os-ken
│   ├── ping_two_hosts.py
│   └── ping_three_hosts.py
├── topologies/                     # Các file topology JSON mẫu
│   ├── two_switch.json
│   ├── two_switch_idle5.json
│   └── two_switch_slow.json
├── tests/                          # Bộ test tự động (pytest)
├── docs/
│   └── notes.md
└── pyproject.toml
```

---

## Yêu cầu & Cài đặt

### Yêu cầu hệ thống
- **Windows 10 / 11** (có sẵn Edge WebView2 Runtime cho PyWebView).
- **Python 3.10 trở lên** (khuyến nghị 3.11 – 3.14; khi cài nhớ tích *Add python.exe to PATH*).
- *(Tùy chọn)* **Wireshark + Npcap** (tích *Support loopback traffic capture* khi cài Npcap) để xem gói tin trực tiếp.

### Các bước cài đặt (PowerShell)

1. Mở terminal tại thư mục gốc `sdn-emulator`, tạo và kích hoạt virtual environment:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   *(Nếu PowerShell báo lỗi chặn script, chạy một lần: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`)*

2. Cài đặt package `sdnsim` ở chế độ chỉnh sửa kèm các thư viện cần thiết:
   ```powershell
   pip install -e ".[dev]"
   pip install fastapi uvicorn websockets httpx os-ken pywebview
   ```

---

## Khởi chạy ứng dụng

### Cách 1: Chạy ứng dụng Desktop + Web Dashboard đồng bộ (Khuyên dùng)
```powershell
python desktop\main.py
```
- Cửa sổ **SDN Emulator — Desktop Topology Studio** sẽ mở ra với vùng vẽ trống.
- Đồng thời, máy chủ Web chạy tại **`http://127.0.0.1:8000/`** (bấm nút **`🌐 Mở Web`** trên góc phải ứng dụng Desktop hoặc tự mở trình duyệt tới địa chỉ này).

### Cách 2: Chỉ chạy Web API / Dashboard theo file JSON có sẵn (không dùng Desktop)
```powershell
$env:SDNSIM_TOPOLOGY = "topologies\two_switch.json"
uvicorn sdnsim.api.main:app
```
Nếu muốn nối với `os_ken_controller.py` ở terminal khác:
```powershell
# Terminal 1: chạy controller
python examples\os_ken_controller.py 6653 5

# Terminal 2: chạy simulator chế độ OpenFlow
$env:SDNSIM_CONTROLLER = "127.0.0.1:6653"
uvicorn sdnsim.api.main:app
```

---

## Hướng dẫn sử dụng chi tiết

### 1. Thiết kế & chỉnh sửa Topology
Trên thanh công cụ phía trên của ứng dụng Desktop:
- **Thêm thiết bị:** Bấm trực tiếp hoặc **kéo thả** các chip **`+ Host`**, **`+ Switch`**, **`+ Controller`** vào vị trí mong muốn trên vùng vẽ.
- **Thao tác chuột trên thiết bị:**
  - **Chuột trái (nhấn giữ & kéo):** Di chuyển vị trí thiết bị; vị trí mới tự động đồng bộ sang Web Dashboard.
  - **Chuột trái (nhấn đơn):** Chọn thiết bị (hiện khung viền nét đứt xanh và tóm tắt ở thanh gợi ý phía dưới). Nhấn `Delete` để xóa nhanh thiết bị đang chọn.
  - **Nhấn đúp chuột trái:** Mở cửa sổ **Xem & sửa cấu hình**:
    - **Host:** Tên, địa chỉ IP, địa chỉ MAC, xem trạng thái cổng 1.
    - **Switch:** Tên, số cổng dữ liệu (`1–64`), **Độ trễ xử lý (ms)**, **Băng thông chuyển mạch** (VD: `10Mbps`, `500Kbps`), xem trạng thái từng cổng và cổng `OF`.
    - **Controller:** Tên, chế độ (`Internal` hoặc `OpenFlow 1.3 TCP`), số cổng điều khiển (`1–64`), `idle_timeout` (giây), địa chỉ IP & cổng TCP (`6653`), xem danh sách Switch đang nối.
  - **Chuột phải:** Mở menu chức năng nhanh của thiết bị (Mở Console, Sửa cấu hình, Nối Link, Xóa).

### 2. Nối Link, cấu hình cổng và xóa Link
- **Tạo kết nối:**
  1. Bấm **`🔗 Nối Link`** trên thanh công cụ (hoặc chuột phải vào thiết bị → `Nối Link`).
  2. Nhấn chuột trái vào **thiết bị nguồn** → chọn cổng muốn nối trong danh sách cổng hiện ra.
  3. Nhấn chuột trái vào **thiết bị đích** → chọn cổng đích:
     - **Nối Data Link (`Host`/`Switch` ↔ `Switch`):** Chọn cổng `1..N` (Host dùng cổng `1`) và có thể cấu hình thêm *Độ trễ (ms)*, *Băng thông*, *Mất gói (%)*, *Hàng đợi tối đa (gói)*.
     - **Nối Control Link (`Controller` ↔ `Switch`):** Hỗ trợ nối từ cả 2 phía; chọn `Cổng Control 1..N` trên Controller và `Cổng Control (OF)` trên Switch. *(Switch chưa nối với Controller sẽ hiện viền đỏ `"Chưa nối Ctrl"` và không cài được flow).*
- **Sửa thông số Data Link đang có:** Bấm trực tiếp vào đoạn dây nối màu xám để chỉnh sửa lại độ trễ, băng thông, mất gói, giới hạn hàng đợi.
- **Xóa Link:** Trỏ chuột vào bất kỳ đoạn dây nào (cả Data Link lẫn Control Link tím), nút **`✕`** màu đỏ sẽ hiện ra tại trung điểm dây — bấm **`✕`** để xóa.

### 3. Lưu & mở Topology dưới dạng file JSON
- Bấm **`💾 Lưu Topo`** để lưu toàn bộ mạng (bao gồm thiết bị, tọa độ, cấu hình cổng, thông số switch/link/controller) thành file `.json`.
- Bấm **`📂 Mở Topo`** để nạp lại file `.json` đã lưu hoặc thử ngay các file mẫu trong thư mục `topologies/`:
  - `two_switch.json`: Topo 2 switch, 3 host mặc định.
  - `two_switch_idle5.json`: Topo kèm `idle_timeout = 5.0` giây.
  - `two_switch_slow.json`: Topo băng thông thấp (`100Kbps`), hàng đợi 3 gói, mất gói 10% để quan sát tắc nghẽn.

### 4. Ping từ Host Console & theo dõi trên Web Dashboard
1. Chuột phải vào một **Host** (VD: `h1`) → chọn **`💻 Mở Console (Ping)`**.
2. Bạn có thể chọn host đích và bấm nút **`Ping`** / **`Ping All`**, hoặc nhập lệnh trực tiếp vào terminal:
   - `ping h2 -c 3` hoặc `ping 10.0.0.2 -c 5 -i 0.2`: Gửi các gói ICMP ping.
   - `pingall`: Ping tới toàn bộ host khác trong mạng.
   - `arp`: Xem bảng ánh xạ IP → MAC hiện tại của host.
   - `ifconfig`: Xem IP, MAC, trạng thái nối cổng 1 và số lần `rtts` / `arp_failures`.
   - `clear` / `help`: Xóa màn hình console / xem hướng dẫn lệnh.
3. Mở trang **Web Dashboard** (`http://127.0.0.1:8000/`):
   - Quan sát sơ đồ topo đồng bộ 100% với Desktop (nhấp nháy cam khi có gói đi qua dây).
   - Bảng **Hosts**: Theo dõi số lần ping và RTT gần nhất.
   - Bảng **Liên kết**: Theo dõi độ trễ, băng thông, tỷ lệ mất gói, **hàng đợi**, số gói gửi/mất/tràn hàng đợi theo cả hai chiều.
   - Từng **Switch** hiển thị độ trễ xử lý, băng thông, `table-miss` và 2 bảng song song: **Thống kê cổng** (trái) và **Bảng Flow** (phải, kèm đếm ngược `idle_timeout`).

### 5. Controller Console & kết nối OpenFlow 1.3 (`os-ken`)
- Chuột phải vào **Controller** (VD: `c0`) → chọn **`🖥️ Mở Console (Xem Logging)`** để theo dõi các sự kiện `PACKET_IN`, `FLOW_MOD`, `PACKET_OUT`, `FLOW hết hạn`, hỗ trợ lọc log, tạm dừng và xóa log.
- **Dùng với `os-ken` thật:**
  1. Nhấn đúp vào `c0` → đổi Chế độ sang **`OpenFlow 1.3 TCP (os-ken / bên ngoài)`**, đặt port `6653` (và `idle_timeout` nếu muốn).
  2. Mở Controller Console của `c0` → bấm nút **`Chạy os-ken`** (hoặc tự chạy `python examples\os_ken_controller.py 6653 5` ngoài terminal).
  3. Khi các đường nét đứt chuyển từ đỏ sang tím là Switch đã handshake OpenFlow thành công với `os-ken`.

### 6. Quan sát gói tin trên Wireshark (Live TZSP & xuất `.pcap`)
- **Xem lại đầy đủ bằng file `.pcap` (khuyên dùng, giữ đúng thời gian ảo):**
  1. Thực hiện ping trên Host Console.
  2. Bấm **`📥 .pcap (N)`** trên thanh công cụ Desktop để lưu file `sdnsim_capture.pcap`.
  3. Mở file bằng Wireshark: bạn sẽ thấy đầy đủ cả gói **Data Plane** (`ARP`, `ICMP`) lẫn gói **Control Plane** (`OpenFlow 1.3` — `OFPT_PACKET_IN`, `OFPT_FLOW_MOD`, `OFPT_PACKET_OUT`) trên cùng dòng thời gian.
- **Xem Live theo thời gian thực:**
  1. Bấm **`📡 Wireshark Live`** trên thanh công cụ Desktop.
  2. Mở Wireshark → chọn card **`Adapter for loopback traffic capture`**.
  3. Dùng bộ lọc hiển thị:
     - Xem gói Host (ARP/ICMP): `tzsp && !icmp.type == 3`
     - Chỉ xem `PACKET_IN`, `PACKET_OUT`, `FLOW_MOD`: `openflow_v4.type == 10 || openflow_v4.type == 13 || openflow_v4.type == 14`
     - Xem cả hai: `(tzsp && !icmp.type == 3) || openflow_v4.type == 10 || openflow_v4.type == 13 || openflow_v4.type == 14`

### 7. Chuyển đổi giao diện Sáng / Tối / Theo thiết bị
- Ở góc phải thanh công cụ trên cả **Desktop Studio** và **Web Dashboard** có cụm **`Giao diện`** với 3 nút icon: **Sáng** (mặt trời), **Tối** (mặt trăng) và **Theo thiết bị** (màn hình — tự đổi theo giao diện sáng/tối của Windows). Lựa chọn được lưu tự động và đồng bộ qua `localStorage`.

---

## Chạy bộ kiểm thử (Tests)

Chạy toàn bộ unit test và integration test từ thư mục gốc:
```powershell
pytest -v
```

---

## Lưu ý & Giới hạn thiết kế

- Dự án là **bộ giả lập mạng thuần Python** phục vụ học tập, trực quan hóa và kiểm thử logic SDN/OpenFlow trên Windows mà không cần VM Linux; không dùng để đo thông lượng hay độ trễ phần cứng thực tế như Open vSwitch trên kernel Linux.
- Trong lõi `sdnsim` hiện tại, mỗi **Host** chỉ sử dụng cố định **cổng 1**; mỗi **Switch** hỗ trợ từ 1 đến 64 cổng dữ liệu kèm 1 cổng điều khiển riêng (`OF`).
- Vòng lặp đồng hồ ảo mặc định tiến theo từng bước (`step = 0.05s`), nên ở chế độ OpenFlow TCP ngoài, gói tin đầu tiên đi qua controller sẽ bị làm tròn độ trễ theo bước mô phỏng (~100 ms cho lượt đi + về), còn các gói khớp flow sau đó phản hồi theo đúng độ trễ mạng đã cấu hình.