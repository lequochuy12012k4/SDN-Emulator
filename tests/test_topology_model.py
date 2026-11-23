import pytest

from sdnsim.topology.model import TopologyModel


def test_crud_host_switch_and_ports():
    m = TopologyModel()
    h1 = m.add_host()
    h2 = m.add_host()
    s1 = m.add_switch(ports=4)
    assert (h1["name"], h2["name"], s1["name"]) == ("h1", "h2", "s1")

    m.add_link("h1", 1, "s1", 2)
    m.add_link("h2", 1, "s1", 3)
    ports = {p["port"]: p for p in m.port_list("s1")}
    assert ports[1]["used"] is False
    assert ports[2] == {"port": 2, "used": True, "peer": "h1:1"}

    # Không thể giảm số cổng Switch xuống dưới cổng đang nối (cổng 3)
    with pytest.raises(ValueError):
        m.update_switch("s1", ports=2)

    # Đổi tên switch cập nhật cả link
    m.update_switch("s1", name="core1", ports=6)
    assert all(l["b"] == "core1" for l in m.links)

    # Xóa link
    assert m.delete_link("h1", 1, "core1", 2) is True
    assert len(m.links) == 1

    # Xóa host kéo theo xóa link nối vào nó
    m.delete_host("h2")
    assert m.links == []


def test_rejects_duplicate_port_and_host_port_over_1():
    m = TopologyModel()
    m.add_host("h1", "00:00:00:00:00:01", "10.0.0.1")
    m.add_host("h2", "00:00:00:00:00:02", "10.0.0.2")
    m.add_switch("s1", ports=2)
    m.add_link("h1", 1, "s1", 1)

    with pytest.raises(ValueError):
        m.add_link("h2", 1, "s1", 1)  # cổng 1 của s1 đã dùng

    with pytest.raises(ValueError):
        m.add_link("h2", 2, "s1", 2)  # host chỉ có cổng 1

def test_controller_crud_and_control_links():
    m = TopologyModel(with_default_controller=False)
    c0 = m.add_controller(ports=4)
    s1 = m.add_switch(ports=4)
    cl = m.add_control_link(c0["name"], 2, s1["name"])
    assert cl == {"controller": "c0", "c_port": 2, "switch": "s1"}

    # Không thể giảm cổng controller dưới cổng đang dùng (2)
    with pytest.raises(ValueError):
        m.update_controller("c0", ports=1)

    # Một switch không thể nối 2 controller cùng lúc
    c1 = m.add_controller(ports=4)
    with pytest.raises(ValueError):
        m.add_control_link(c1["name"], 1, "s1")

    assert m.delete_control_link("c0", 2, "s1") is True
    assert m.control_links == []


def test_controllers_use_osken_and_legacy_internal_specs_are_upgraded():
    assert TopologyModel().controllers["c0"]["mode"] == "openflow"

    legacy = TopologyModel.from_spec({
        "switches": [{"name": "s1"}],
        "controllers": [{
            "name": "c0", "mode": "internal", "host": "127.0.0.1", "port": 6653,
        }],
        "control_links": [{"controller": "c0", "c_port": 1, "switch": "s1"}],
    })
    assert legacy.controllers["c0"]["mode"] == "openflow"

    m = TopologyModel(with_default_controller=False)
    with pytest.raises(ValueError, match="os-ken"):
        m.add_controller(mode="internal")