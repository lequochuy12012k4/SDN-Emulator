// =============================================================================
// TIỆN ÍCH CƠ BẢN VÀ BIẾN TOÀN CỤC
// =============================================================================
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const escHtml = esc;

const on = (id, event, handler) => {
  const el = $(id);
  if (el) el.addEventListener(event, handler);
};

const setClick = (id, handler) => {
  const el = $(id);
  if (el) el.onclick = handler;
};

const svg = $("canvas");
const stage = $("stage");
const ctxMenu = $("ctxMenu");
const portPicker = $("portPicker");

let spec = { hosts: [], switches: [], links: [], controllers: [], control_links: [] };
let oskenRunning = false;
let liveSnap = null;
let selectedNode = null;

// Trạng thái nối link
let linkMode = false;
let linkDraft = null; // { kind, a, a_port, isControl }
let mousePos = { x: 0, y: 0 };
let dragState = null; // { kind, name, obj, startX, startY, origX, origY, moved }

// Trạng thái Canvas vô hạn (Zoom & Pan)
let zoom = 1.0;
let pan = { x: 0, y: 0 };
let isPanning = false;
let panStart = { x: 0, y: 0 };

// Trạng thái Console & Capture
let activeHostConsole = null;
let hostLocalHistory = {};
let allLogs = [];
let ccOpen = false;
let ccCtrlName = null;
let ccPaused = false;
let liveTzsp = false;

const webUrl = `${location.protocol}//${location.host}/`;
if ($("webUrlText")) $("webUrlText").textContent = webUrl;

document.addEventListener("contextmenu", e => {
  if (!e.target.closest(".term, input, select, textarea")) {
    e.preventDefault();
  }
});

function showToast(msg) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = "none"; }, 3200);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function svgEl(tag, attrs = {}, text = null) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== null) el.textContent = text;
  return el;
}

// =============================================================================
// TỌA ĐỘ VÀ THU PHÓNG (ZOOM & PAN)
// =============================================================================
function toCanvasCoords(clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  return {
    x: (clientX - rect.left - pan.x) / zoom,
    y: (clientY - rect.top - pan.y) / zoom,
  };
}

function toScreenCoords(canvasX, canvasY) {
  return {
    x: canvasX * zoom + pan.x,
    y: canvasY * zoom + pan.y,
  };
}

function updateViewportTransform() {
  const vp = $("viewport");
  if (vp) {
    vp.setAttribute("transform", `translate(${pan.x}, ${pan.y}) scale(${zoom})`);
  }
  const zoomText = $("zoomLevel");
  if (zoomText) {
    zoomText.textContent = `${Math.round(zoom * 100)}%`;
  }
}

function fitView() {
  if (!svg) return;
  const allNodes = [...(spec.hosts || []), ...(spec.switches || []), ...(spec.controllers || [])];
  if (!allNodes.length) {
    zoom = 1.0;
    pan = { x: 0, y: 0 };
    updateViewportTransform();
    return;
  }

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const n of allNodes) {
    minX = Math.min(minX, n.x - 90);
    maxX = Math.max(maxX, n.x + 90);
    minY = Math.min(minY, n.y - 70);
    maxY = Math.max(maxY, n.y + 70);
  }

  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const contentW = Math.max(100, maxX - minX);
  const contentH = Math.max(100, maxY - minY);
  const padding = 60;

  const scaleX = (rect.width - padding * 2) / contentW;
  const scaleY = (rect.height - padding * 2) / contentH;
  zoom = Math.max(0.2, Math.min(1.3, Math.min(scaleX, scaleY)));

  pan.x = (rect.width - contentW * zoom) / 2 - minX * zoom;
  pan.y = (rect.height - contentH * zoom) / 2 - minY * zoom;

  updateViewportTransform();
}

// =============================================================================
// GIAO TIẾP BACKEND API
// =============================================================================
async function api(url, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || "Lỗi máy chủ");
  return data;
}

async function loadInitial() {
  try {
    const data = await api("/api/topology");
    spec = data.spec || { hosts: [], switches: [], links: [], controllers: [], control_links: [] };
    oskenRunning = Boolean(data.osken_running);
    render();
    fitView();
    refreshCaptureStatus();
  } catch (err) {
    setTimeout(loadInitial, 250);
  }
}

async function commitSpec(newSpec) {
  const res = await api("/api/topology", "PUT", newSpec);
  spec = res.spec;
  liveSnap = res.snapshot;
  if (selectedNode && !findNode(selectedNode)) selectedNode = null;
  render();
}

function cloneSpec() {
  return JSON.parse(JSON.stringify(spec));
}

function findNode(name) {
  const c = (spec.controllers || []).find(x => x.name === name);
  if (c) return { kind: "ctrl", obj: c };
  const h = (spec.hosts || []).find(x => x.name === name);
  if (h) return { kind: "host", obj: h };
  const s = (spec.switches || []).find(x => x.name === name);
  if (s) return { kind: "sw", obj: s };
  return null;
}

function usedPortsOf(name) {
  const map = {};
  const node = findNode(name);
  if (!node) return map;
  if (node.kind === "ctrl") {
    for (const cl of (spec.control_links || [])) {
      if (cl.controller === name) map[cl.c_port] = `${cl.switch}:OF`;
    }
    return map;
  }
  for (const l of (spec.links || [])) {
    if (l.a === name) map[l.a_port] = `${l.b}:${l.b_port}`;
    if (l.b === name) map[l.b_port] = `${l.a}:${l.a_port}`;
  }
  return map;
}

function switchCtrlLink(swName) {
  return (spec.control_links || []).find(cl => cl.switch === swName) || null;
}

function allNames() {
  return new Set([
    ...(spec.hosts || []).map(x => x.name),
    ...(spec.switches || []).map(x => x.name),
    ...(spec.controllers || []).map(x => x.name),
  ]);
}

function nextHostDefaults(x = null, y = null) {
  let i = 1;
  const names = allNames();
  while (names.has(`h${i}`)) i++;
  const macs = new Set((spec.hosts || []).map(h => h.mac.toLowerCase()));
  const ips = new Set((spec.hosts || []).map(h => h.ip));
  let m = 1;
  const fmtMac = n => [5, 4, 3, 2, 1, 0].map(k => ((n >> (8 * k)) & 255).toString(16).padStart(2, "0")).join(":");
  while (macs.has(fmtMac(m))) m++;
  let p = 1;
  while (ips.has(`10.0.${Math.floor((p - 1) / 254)}.${(p - 1) % 254 + 1}`)) p++;
  return {
    name: `h${i}`,
    mac: fmtMac(m),
    ip: `10.0.${Math.floor((p - 1) / 254)}.${(p - 1) % 254 + 1}`,
    ports: 1,
    x: x ?? (160 + ((spec.hosts || []).length % 5) * 140),
    y: y ?? 350,
  };
}

function nextSwitchDefaults(x = null, y = null) {
  let i = 1;
  const names = allNames();
  while (names.has(`s${i}`)) i++;
  return {
    name: `s${i}`,
    ports: 4,
    delay: 0.0,
    bandwidth: null,
    x: x ?? (220 + ((spec.switches || []).length % 4) * 200),
    y: y ?? 200,
  };
}

function nextCtrlDefaults(x = null, y = null) {
  let i = 0;
  const names = allNames();
  while (names.has(`c${i}`)) i++;
  return {
    name: `c${i}`,
    mode: "internal",
    idle_timeout: null,
    host: "127.0.0.1",
    port: 6653 + i,
    ports: 8,
    x: x ?? (380 + ((spec.controllers || []).length % 3) * 180),
    y: y ?? 70,
  };
}

function closePopovers() {
  if (ctxMenu) ctxMenu.style.display = "none";
  if (portPicker) portPicker.style.display = "none";
}

function updateHintBar() {
  const hint = $("hintBar");
  if (!hint) return;
  if (linkMode) {
    hint.innerHTML = "<b>Chế độ nối Link:</b> Chuột trái chọn thiết bị nguồn → Chọn cổng → Chọn thiết bị đích → Chọn cổng (Esc để hủy).";
    return;
  }
  if (selectedNode) {
    const node = findNode(selectedNode);
    if (node) {
      let desc = "";
      if (node.kind === "host") desc = `Host ${node.obj.name} (${node.obj.ip})`;
      else if (node.kind === "sw") {
        const parts = [`${node.obj.ports} cổng`];
        if (node.obj.delay) parts.push(`${+(node.obj.delay * 1000).toFixed(2)}ms`);
        if (node.obj.bandwidth) parts.push(`${node.obj.bandwidth}`);
        desc = `Switch ${node.obj.name} (${parts.join(" · ")})`;
      } else {
        desc = `Controller ${node.obj.name} (${node.obj.mode}, ${node.obj.ports || 8} cổng)`;
      }
      hint.innerHTML = `Đang chọn: <b>${escHtml(desc)}</b> | <b>Nhấn đúp</b> xem cấu hình | <b>Chuột phải</b> mở tính năng | <b>Delete</b> xóa.`;
      return;
    }
  }
  hint.innerHTML = "<b>Chuột trái:</b> Chọn / Kéo thả | <b>Cuộn chuột:</b> Thu phóng | <b>Kéo vùng trống:</b> Trượt màn hình | <b>Nhấn đúp:</b> Cấu hình | <b>Chuột phải:</b> Tính năng.";
}

function setLinkMode(onFlag) {
  linkMode = onFlag;
  if (!onFlag) linkDraft = null;
  const btn = $("btnLinkMode");
  if (btn) {
    btn.classList.toggle("btn-active", onFlag);
    btn.textContent = onFlag ? "✖ Hủy nối Link" : "🔗 Nối Link";
  }
  updateHintBar();
  render();
}

function appendDeviceIcon(parent, kind, x, y, color) {
  const g = svgEl("g", { transform: `translate(${x}, ${y})` });
  if (kind === "host") {
    g.appendChild(svgEl("rect", { x: 2, y: 3, width: 20, height: 14, rx: 2, fill: "none", stroke: color, "stroke-width": 2 }));
    g.appendChild(svgEl("path", { d: "M8 21h8M12 17v4", fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round" }));
  } else if (kind === "sw") {
    g.appendChild(svgEl("rect", { x: 2, y: 5, width: 20, height: 14, rx: 3, fill: "none", stroke: color, "stroke-width": 1.8 }));
    g.appendChild(svgEl("path", { d: "M7 10h10m-2.5-2 2.5 2-2.5 2M17 14H7m2.5-2-2.5 2 2.5 2", fill: "none", stroke: color, "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  } else {
    g.appendChild(svgEl("circle", { cx: 12, cy: 12, r: 3.2, fill: color }));
    g.appendChild(svgEl("path", { d: "M12 3v5.5m0 7V21M3 12h5.5m7 0H21M5.8 5.8l3.8 3.8m4.8 4.8 3.8 3.8M18.2 5.8l-3.8 3.8m-4.8 4.8-3.8 3.8", stroke: color, "stroke-width": 1.8, "stroke-linecap": "round" }));
  }
  parent.appendChild(g);
}

function updateSelectionVisual() {
  const ring = $("selRing");
  if (!ring) return;
  const node = selectedNode ? findNode(selectedNode) : null;
  if (!node) {
    ring.style.display = "none";
  } else {
    const w = node.kind === "ctrl" ? 162 : node.kind === "host" ? 136 : 132;
    const h = 64;
    ring.setAttribute("x", node.obj.x - w / 2);
    ring.setAttribute("y", node.obj.y - h / 2);
    ring.setAttribute("width", w);
    ring.setAttribute("height", h);
    ring.style.display = "block";
  }
  updateHintBar();
}

// =============================================================================
// HÀM VẼ SVG CHÍNH
// =============================================================================
function render() {
  if (!svg) return;
  svg.innerHTML = "";
  const hosts = spec.hosts || [];
  const switches = spec.switches || [];
  const controllers = spec.controllers || [];

  const isEmpty = !(hosts.length || switches.length || controllers.length);
  if ($("emptyState")) $("emptyState").style.display = isEmpty ? "flex" : "none";

  const portBg = cssVar("--port-bg") || "#ffffff";
  const linkColor = cssVar("--link-line") || "#64748b";
  const textColor = cssVar("--text") || "#0f172a";
  const bgCanvas = cssVar("--bg-canvas") || "#f8fafc";

  const defs = svgEl("defs");
  defs.innerHTML = `<filter id="nodeShadow" x="-15%" y="-15%" width="130%" height="140%">
    <feDropShadow dx="0" dy="3" stdDeviation="3.5" flood-color="#0f172a" flood-opacity="0.18"/>
  </filter>`;
  svg.appendChild(defs);

  const viewport = svgEl("g", { id: "viewport" });
  svg.appendChild(viewport);

  const selRing = svgEl("rect", {
    id: "selRing", x: 0, y: 0, width: 120, height: 64, rx: 14,
    fill: "none", stroke: "#0ea5e9", "stroke-width": 2.6, "stroke-dasharray": "6,3",
    style: "display: none; pointer-events: none;",
  });
  viewport.appendChild(selRing);

  if (linkMode && linkDraft) {
    const src = findNode(linkDraft.a);
    if (src) {
      viewport.appendChild(svgEl("line", {
        x1: src.obj.x, y1: src.obj.y, x2: mousePos.x, y2: mousePos.y,
        stroke: linkDraft.isControl ? "#8b5cf6" : "#f97316",
        "stroke-width": 2.5 / zoom, "stroke-dasharray": "6,4",
      }));
    }
  }

  // 1. Control Links
  (spec.control_links || []).forEach((cl, idx) => {
    const c = findNode(cl.controller), s = findNode(cl.switch);
    if (!c || !s) return;
    const ax = c.obj.x, ay = c.obj.y, bx = s.obj.x, by = s.obj.y;
    const isOk = c.obj.mode !== "openflow" || !liveSnap?.controller?.switches || liveSnap.controller.switches[cl.switch];

    const grp = svgEl("g", { class: "link-group" });
    grp.appendChild(svgEl("line", { x1: ax, y1: ay, x2: bx, y2: by, stroke: "transparent", "stroke-width": 16 }));
    grp.appendChild(svgEl("line", { class: "link-line", x1: ax, y1: ay, x2: bx, y2: by, stroke: isOk ? "#8b5cf6" : "#ef4444", "stroke-width": 2.1, "stroke-dasharray": "6,4" }));

    const p1x = ax + (bx - ax) * 0.25, p1y = ay + (by - ay) * 0.25;
    const p2x = ax + (bx - ax) * 0.75, p2y = ay + (by - ay) * 0.75;
    grp.appendChild(svgEl("circle", { cx: p1x, cy: p1y, r: 10.5, fill: portBg, stroke: "#8b5cf6", "stroke-width": 1.4 }));
    grp.appendChild(svgEl("text", { x: p1x, y: p1y + 3.5, "font-size": 10, "font-weight": 700, "text-anchor": "middle", fill: "#8b5cf6" }, `p${cl.c_port}`));
    grp.appendChild(svgEl("circle", { cx: p2x, cy: p2y, r: 11, fill: portBg, stroke: "#8b5cf6", "stroke-width": 1.4 }));
    grp.appendChild(svgEl("text", { x: p2x, y: p2y + 3.5, "font-size": 9.5, "font-weight": 700, "text-anchor": "middle", fill: "#8b5cf6" }, "OF"));

    const mx = (ax + bx) / 2, my = (ay + by) / 2;
    const delWrap = svgEl("g", { class: "link-del-wrap link-del-btn" });
    delWrap.appendChild(svgEl("circle", { cx: mx, cy: my, r: 12, fill: "#ef4444", stroke: "#fff", "stroke-width": 2 }));
    delWrap.appendChild(svgEl("text", { x: mx, y: my + 4, "font-size": 12, "font-weight": 700, "text-anchor": "middle", fill: "#fff" }, "✕"));
    delWrap.addEventListener("click", async e => {
      e.stopPropagation();
      const sCopy = cloneSpec();
      sCopy.control_links.splice(idx, 1);
      await commitSpec(sCopy);
    });
    grp.appendChild(delWrap);
    viewport.appendChild(grp);
  });

  // 2. Data Links
  (spec.links || []).forEach((l, idx) => {
    const a = findNode(l.a), b = findNode(l.b);
    if (!a || !b) return;
    const ax = a.obj.x, ay = a.obj.y, bx = b.obj.x, by = b.obj.y;

    const grp = svgEl("g", { class: "link-group", style: "cursor: pointer;" });
    grp.addEventListener("click", e => {
      e.stopPropagation();
      if (!linkMode) openEditLink(idx);
    });

    grp.appendChild(svgEl("line", { x1: ax, y1: ay, x2: bx, y2: by, stroke: "transparent", "stroke-width": 16 }));
    grp.appendChild(svgEl("line", { class: "link-line", x1: ax, y1: ay, x2: bx, y2: by, stroke: linkColor, "stroke-width": 2.6 }));

    const p1x = ax + (bx - ax) * 0.25, p1y = ay + (by - ay) * 0.25;
    const p2x = ax + (bx - ax) * 0.75, p2y = ay + (by - ay) * 0.75;
    grp.appendChild(svgEl("circle", { cx: p1x, cy: p1y, r: 10.5, fill: portBg, stroke: "#3b82f6", "stroke-width": 1.4 }));
    grp.appendChild(svgEl("text", { x: p1x, y: p1y + 3.5, "font-size": 10, "font-weight": 700, "text-anchor": "middle", fill: "#3b82f6" }, `p${l.a_port}`));
    grp.appendChild(svgEl("circle", { cx: p2x, cy: p2y, r: 10.5, fill: portBg, stroke: "#3b82f6", "stroke-width": 1.4 }));
    grp.appendChild(svgEl("text", { x: p2x, y: p2y + 3.5, "font-size": 10, "font-weight": 700, "text-anchor": "middle", fill: "#3b82f6" }, `p${l.b_port}`));

    const mx = (ax + bx) / 2, my = (ay + by) / 2;
    const lblParts = [`${+(l.delay * 1000).toFixed(1)}ms`];
    if (l.bandwidth) lblParts.push(`${l.bandwidth}`);
    if (l.loss) lblParts.push(`${+(l.loss * 100).toFixed(1)}%`);

    grp.appendChild(svgEl("text", {
      x: mx, y: my - 9, "font-size": 10.5, "font-weight": 600, "text-anchor": "middle",
      fill: textColor, "paint-order": "stroke", stroke: bgCanvas, "stroke-width": 4,
    }, lblParts.join(" · ")));

    const delWrap = svgEl("g", { class: "link-del-wrap link-del-btn" });
    delWrap.appendChild(svgEl("circle", { cx: mx, cy: my, r: 12, fill: "#ef4444", stroke: "#fff", "stroke-width": 2 }));
    delWrap.appendChild(svgEl("text", { x: mx, y: my + 4, "font-size": 12, "font-weight": 700, "text-anchor": "middle", fill: "#fff" }, "✕"));
    delWrap.addEventListener("click", async e => {
      e.stopPropagation();
      const sCopy = cloneSpec();
      sCopy.links.splice(idx, 1);
      await commitSpec(sCopy);
    });
    grp.appendChild(delWrap);
    viewport.appendChild(grp);
  });

  // 3. Controllers
  const ctrlFill = cssVar("--ctrl-fill") || "#7c3aed";
  const ctrlStroke = cssVar("--ctrl-stroke") || "#5b21b6";
  for (const ctrl of controllers) {
    const usedC = Object.keys(usedPortsOf(ctrl.name)).length;
    drawNode("ctrl", ctrl, g => {
      const isSrc = linkDraft && linkDraft.a === ctrl.name;
      const modeTxt = ctrl.mode === "openflow" ? `OF :${ctrl.port}` : "Internal";
      g.appendChild(svgEl("rect", {
        x: -74, y: -26, width: 148, height: 52, rx: 12, filter: "url(#nodeShadow)",
        fill: ctrlFill, stroke: isSrc ? "#f97316" : ctrlStroke, "stroke-width": isSrc ? 3 : 1.6,
      }));
      appendDeviceIcon(g, "ctrl", -62, -12, "#ede9fe");
      g.appendChild(svgEl("text", { x: -30, y: -3, "font-size": 13.5, "font-weight": 700, fill: "#fff" }, ctrl.name));
      g.appendChild(svgEl("text", { x: -30, y: 14, "font-size": 10.5, fill: "#ede9fe" }, `${modeTxt} · ${usedC}/${ctrl.ports || 8}p`));
    });
  }

  // 4. Switches
  const swFill = cssVar("--sw-fill") || "#2563eb";
  const swStroke = cssVar("--sw-stroke") || "#1d4ed8";
  for (const sw of switches) {
    const usedCount = Object.keys(usedPortsOf(sw.name)).length;
    const hasCtrl = Boolean(switchCtrlLink(sw.name));
    const perfParts = [`${usedCount}/${sw.ports}p`];
    if (sw.delay) perfParts.push(`${+(sw.delay * 1000).toFixed(1)}ms`);
    if (sw.bandwidth) perfParts.push(`${sw.bandwidth}`);
    const subText = hasCtrl ? perfParts.join(" · ") : "Chưa nối Ctrl";

    drawNode("sw", sw, g => {
      const isSrc = linkDraft && linkDraft.a === sw.name;
      g.appendChild(svgEl("rect", {
        x: -60, y: -26, width: 120, height: 52, rx: 11, filter: "url(#nodeShadow)",
        fill: swFill, stroke: isSrc ? "#f97316" : (hasCtrl ? swStroke : "#ef4444"),
        "stroke-width": isSrc ? 3 : (hasCtrl ? 1.6 : 2.5),
      }));
      appendDeviceIcon(g, "sw", -50, -12, "#dbeafe");
      g.appendChild(svgEl("text", { x: -19, y: -3, "font-size": 13.5, "font-weight": 700, fill: "#fff" }, sw.name));
      g.appendChild(svgEl("text", { x: -19, y: 14, "font-size": 10, fill: hasCtrl ? "#dbeafe" : "#fecaca" }, subText));
    });
  }

  // 5. Hosts
  const hostFill = cssVar("--host-fill") || "#ffffff";
  const hostStroke = cssVar("--host-stroke") || "#0ea5e9";
  const hostText = cssVar("--host-text") || "#0f172a";
  for (const h of hosts) {
    drawNode("host", h, g => {
      const isSrc = linkDraft && linkDraft.a === h.name;
      g.appendChild(svgEl("rect", {
        x: -62, y: -26, width: 124, height: 52, rx: 11, filter: "url(#nodeShadow)",
        fill: hostFill, stroke: isSrc ? "#f97316" : hostStroke, "stroke-width": isSrc ? 3 : 1.8,
      }));
      appendDeviceIcon(g, "host", -51, -12, "#0ea5e9");
      g.appendChild(svgEl("text", { x: -20, y: -3, "font-size": 13.5, "font-weight": 700, fill: hostText }, h.name));
      g.appendChild(svgEl("text", { x: -20, y: 14, "font-size": 10.5, fill: cssVar("--text-muted") || "#64748b" }, h.ip || h.mac));
    });
  }

  // 6. Lớp hoạt họa gói tin
  viewport.appendChild(svgEl("g", { id: "packetLayer" }));

  updateViewportTransform();
  updateSelectionVisual();
}

function drawNode(kind, obj, buildInner) {
  const g = svgEl("g", { transform: `translate(${obj.x}, ${obj.y})`, style: "cursor: pointer;" });
  buildInner(g);

  g.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    e.stopPropagation();
    closePopovers();
    selectedNode = obj.name;
    updateSelectionVisual();
    dragState = {
      kind, name: obj.name, obj,
      startX: e.clientX, startY: e.clientY,
      origX: obj.x, origY: obj.y, moved: false,
    };
  });

  g.addEventListener("dblclick", e => {
    e.stopPropagation();
    closePopovers();
    if (linkMode) return;
    if (kind === "host") openEditHost(obj.name);
    else if (kind === "sw") openEditSwitch(obj.name);
    else openEditController(obj.name);
  });

  g.addEventListener("contextmenu", e => {
    e.preventDefault();
    e.stopPropagation();
    dragState = null;
    selectedNode = obj.name;
    updateSelectionVisual();
    const p = toScreenCoords(obj.x, obj.y);
    openDeviceMenu(kind, obj.name, p.x, p.y);
  });

  const vp = $("viewport");
  if (vp) vp.appendChild(g);
}

// =============================================================================
// SỰ KIỆN CHUỘT TRÊN STAGE (ZOOM, PAN, DRAG)
// =============================================================================
if (stage && svg) {
  stage.addEventListener("wheel", e => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    const factor = e.deltaY < 0 ? 1.15 : 0.87;
    const newZoom = Math.max(0.15, Math.min(3.0, zoom * factor));

    pan.x = mouseX - (mouseX - pan.x) * (newZoom / zoom);
    pan.y = mouseY - (mouseY - pan.y) * (newZoom / zoom);
    zoom = newZoom;

    updateViewportTransform();
  }, { passive: false });

  stage.addEventListener("mousedown", e => {
    if (e.button === 1 || (e.button === 0 && (e.target === svg || e.target === stage))) {
      isPanning = true;
      panStart = { x: e.clientX - pan.x, y: e.clientY - pan.y };
      stage.style.cursor = "grabbing";
      closePopovers();
      if (e.button === 0) {
        selectedNode = null;
        updateSelectionVisual();
      }
    }
  });

  stage.addEventListener("mousemove", e => {
    mousePos = toCanvasCoords(e.clientX, e.clientY);

    if (isPanning) {
      pan.x = e.clientX - panStart.x;
      pan.y = e.clientY - panStart.y;
      updateViewportTransform();
      return;
    }

    if (dragState) {
      const dx = (e.clientX - dragState.startX) / zoom;
      const dy = (e.clientY - dragState.startY) / zoom;
      if (Math.hypot(dx, dy) >= 4) dragState.moved = true;
      if (dragState.moved) {
        dragState.obj.x = dragState.origX + dx;
        dragState.obj.y = dragState.origY + dy;
        render();
      }
    } else if (linkMode && linkDraft) {
      render();
    }
  });

  stage.addEventListener("dragover", e => e.preventDefault());
  stage.addEventListener("drop", e => {
    e.preventDefault();
    const kind = e.dataTransfer.getData("text/plain");
    if (!kind) return;
    const p = toCanvasCoords(e.clientX, e.clientY);
    if (kind === "host") openAddHost(p.x, p.y);
    else if (kind === "sw") openAddSwitch(p.x, p.y);
    else if (kind === "ctrl") openAddController(p.x, p.y);
  });
}

window.addEventListener("mouseup", async e => {
  if (isPanning) {
    isPanning = false;
    if (stage) stage.style.cursor = "default";
  }

  if (!dragState) return;
  const ds = dragState;
  dragState = null;

  if (ds.moved) {
    try {
      await api("/api/topology/positions", "PATCH", { [ds.name]: { x: ds.obj.x, y: ds.obj.y } });
    } catch {}
    return;
  }

  if (linkMode) {
    const p = toScreenCoords(ds.obj.x, ds.obj.y);
    openPortPicker(ds.name, p.x, p.y);
  }
});

window.addEventListener("keydown", async e => {
  if (e.key === "Escape") {
    closePopovers();
    if (linkMode) setLinkMode(false);
    return;
  }
  if (e.key === "Delete" && selectedNode) {
    const anyModalOpen = ["editModalWrap", "hostConsoleWrap", "ctrlConsoleWrap"]
      .some(id => $(id)?.style.display === "flex");
    if (anyModalOpen || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const node = findNode(selectedNode);
    if (node && confirm(`Xóa thiết bị ${selectedNode}?`)) {
      await deleteDevice(node.kind, selectedNode);
    }
  }
});

// =============================================================================
// MENU CHỨC NĂNG VÀ CHỌN CỔNG
// =============================================================================
function showPopover(el, x, y) {
  if (!el || !stage) return;
  el.style.display = "block";
  const maxX = stage.clientWidth - el.offsetWidth - 10;
  const maxY = stage.clientHeight - el.offsetHeight - 10;
  el.style.left = Math.max(8, Math.min(maxX, x)) + "px";
  el.style.top = Math.max(8, Math.min(maxY, y)) + "px";
}

function openDeviceMenu(kind, name, px, py) {
  closePopovers();
  if (!ctxMenu) return;
  let html = `<div class="popover-title">${kind === "host" ? "Host" : kind === "sw" ? "Switch" : "Controller"}: ${escHtml(name)}</div>`;
  if (kind === "host") {
    html += `<button class="menu-item" onclick="openHostConsole('${escHtml(name)}')">💻 Mở Console (Ping)</button>`;
    html += `<button class="menu-item" onclick="openEditHost('${escHtml(name)}')">✏️ Xem / Sửa cấu hình</button>`;
    html += `<button class="menu-item" onclick="startLinkFrom('${escHtml(name)}', ${px}, ${py})">🔗 Nối Link từ Host</button>`;
    html += `<button class="menu-item danger" onclick="deleteDevice('host', '${escHtml(name)}')">🗑️ Xóa Host</button>`;
  } else if (kind === "sw") {
    html += `<button class="menu-item" onclick="openEditSwitch('${escHtml(name)}')">✏️ Xem / Sửa cấu hình & Cổng</button>`;
    html += `<button class="menu-item" onclick="startLinkFrom('${escHtml(name)}', ${px}, ${py})">🔗 Nối Link (Switch / Ctrl)</button>`;
    html += `<button class="menu-item danger" onclick="deleteDevice('sw', '${escHtml(name)}')">🗑️ Xóa Switch</button>`;
  } else {
    html += `<button class="menu-item" onclick="openCtrlConsole('${escHtml(name)}')">🖥️ Mở Console (Xem Logging)</button>`;
    html += `<button class="menu-item" onclick="openEditController('${escHtml(name)}')">⚙️ Xem / Sửa cấu hình & Cổng</button>`;
    html += `<button class="menu-item" onclick="startLinkFrom('${escHtml(name)}', ${px}, ${py})">🔗 Nối Link tới Switch</button>`;
    html += `<button class="menu-item danger" onclick="deleteDevice('ctrl', '${escHtml(name)}')">🗑️ Xóa Controller</button>`;
  }
  ctxMenu.innerHTML = html;
  showPopover(ctxMenu, px + 8, py + 8);
}

function startLinkFrom(name, px, py) {
  closePopovers();
  setLinkMode(true);
  linkDraft = null;
  openPortPicker(name, px, py);
}

function openPortPicker(nodeName, px, py) {
  closePopovers();
  if (!portPicker) return;
  if (linkDraft && linkDraft.a === nodeName) return showToast("Hãy chọn thiết bị đích khác với thiết bị nguồn!");
  const node = findNode(nodeName);
  if (!node) return;

  const isTarget = Boolean(linkDraft);
  if (isTarget) {
    if (linkDraft.kind === "ctrl") {
      if (node.kind !== "sw") return showToast("Controller chỉ nối tới Switch!");
    } else if (linkDraft.isControl) {
      if (node.kind !== "ctrl") return showToast("Cổng Control (OF) chỉ nối tới Controller!");
    } else {
      if (node.kind === "ctrl") {
        return showToast(linkDraft.kind === "host" ? "Host không thể nối trực tiếp tới Controller!" : "Hãy chọn 'Cổng Control (OF)' trên Switch để nối Controller!");
      }
    }
  }

  let html = `<div class="popover-title">${isTarget ? `Chọn cổng đích (${escHtml(nodeName)})` : `Chọn cổng nguồn (${escHtml(nodeName)})`}</div><div class="port-grid">`;
  if (node.kind === "ctrl") {
    const totalPorts = node.obj.ports || 8, used = usedPortsOf(nodeName);
    for (let p = 1; p <= totalPorts; p++) {
      const peer = used[p];
      html += `<button class="port-btn of" ${peer ? "disabled" : ""} onclick="selectPort('${escHtml(nodeName)}', ${p}, true)"><span>Cổng Control ${p}</span><span>${peer ? `Đã nối → ${escHtml(peer)}` : "Trống"}</span></button>`;
    }
  } else if (node.kind === "sw") {
    const curCl = switchCtrlLink(nodeName);
    const fromCtrl = isTarget && linkDraft.kind === "ctrl";
    const fromData = isTarget && linkDraft.kind !== "ctrl";

    if (!fromData) {
      html += `<button class="port-btn of" ${curCl ? "disabled" : ""} onclick="selectPort('${escHtml(nodeName)}', 0, true)"><span>Cổng Control (OF)</span><span>${curCl ? `Đã nối → ${escHtml(curCl.controller)}:${curCl.c_port}` : "Trống"}</span></button>`;
    }

    const totalPorts = node.obj.ports || 4, used = usedPortsOf(nodeName);
    for (let p = 1; p <= totalPorts; p++) {
      const peer = used[p];
      if (fromCtrl) {
        html += `<button class="port-btn" disabled><span>Cổng ${p}</span><span>${peer ? `Data → ${escHtml(peer)}` : "Cổng Data"}</span></button>`;
      } else {
        html += `<button class="port-btn" ${peer ? "disabled" : ""} onclick="selectPort('${escHtml(nodeName)}', ${p}, false)"><span>Cổng ${p}</span><span>${peer ? `Đã nối → ${escHtml(peer)}` : "Trống"}</span></button>`;
      }
    }
  } else {
    const used = usedPortsOf(nodeName), peer = used[1];
    html += `<button class="port-btn" ${peer ? "disabled" : ""} onclick="selectPort('${escHtml(nodeName)}', 1, false)"><span>Cổng 1</span><span>${peer ? `Đã nối → ${escHtml(peer)}` : "Trống"}</span></button>`;
  }
  html += `</div>`;

  if (isTarget && !linkDraft.isControl && node.kind !== "ctrl") {
    html += `<div class="link-params">
      <label>Độ trễ (ms)<input id="lpDelay" type="number" value="1" min="0.1" step="0.5"></label>
      <label>Băng thông<input id="lpBw" placeholder="VD: 10Mbps"></label>
      <label>Mất gói (%)<input id="lpLoss" type="number" value="0" min="0" max="100" step="1"></label>
      <label>Hàng đợi<input id="lpQueue" type="number" placeholder="VD: 10"></label>
    </div>`;
  }
  portPicker.innerHTML = html;
  showPopover(portPicker, px + 10, py + 10);
}

async function selectPort(nodeName, port, isControl) {
  const node = findNode(nodeName);
  if (!linkDraft) {
    linkDraft = { kind: node.kind, a: nodeName, a_port: port, isControl };
    closePopovers();
    render();
    return;
  }

  const s = cloneSpec();
  if (linkDraft.isControl || isControl) {
    let ctrlName, cPort, swName;
    if (linkDraft.kind === "ctrl") {
      ctrlName = linkDraft.a; cPort = linkDraft.a_port; swName = nodeName;
    } else {
      ctrlName = nodeName; cPort = port; swName = linkDraft.a;
    }
    s.control_links.push({ controller: ctrlName, c_port: cPort, switch: swName });
  } else {
    const delayMs = parseFloat($("lpDelay")?.value || "1");
    const bwRaw = $("lpBw")?.value.trim() || "";
    const lossPct = parseFloat($("lpLoss")?.value || "0");
    const qRaw = $("lpQueue")?.value.trim() || "";
    s.links.push({
      a: linkDraft.a, a_port: linkDraft.a_port, b: nodeName, b_port: port,
      delay: Math.max(0.0001, delayMs / 1000.0),
      bandwidth: bwRaw || null,
      loss: Math.max(0, Math.min(1, lossPct / 100.0)),
      queue_limit: qRaw ? parseInt(qRaw, 10) : null,
    });
  }

  closePopovers();
  linkDraft = null;
  try {
    await commitSpec(s);
    setLinkMode(false);
  } catch (err) {
    showToast("Lỗi: " + err.message);
  }
}

async function deleteDevice(kind, name) {
  closePopovers();
  const s = cloneSpec();
  if (kind === "host") {
    s.hosts = s.hosts.filter(h => h.name !== name);
    s.links = s.links.filter(l => l.a !== name && l.b !== name);
  } else if (kind === "sw") {
    s.switches = s.switches.filter(sw => sw.name !== name);
    s.links = s.links.filter(l => l.a !== name && l.b !== name);
    s.control_links = s.control_links.filter(cl => cl.switch !== name);
  } else {
    s.controllers = s.controllers.filter(c => c.name !== name);
    s.control_links = s.control_links.filter(cl => cl.controller !== name);
  }
  if (selectedNode === name) selectedNode = null;
  await commitSpec(s);
}

function buildPortSummaryHtml(kind, name, totalPorts) {
  const used = usedPortsOf(name);
  let badges = "";
  if (kind === "sw") {
    const cl = switchCtrlLink(name);
    badges += `<span class="p-badge ${cl ? "ctrl" : ""}">OF: ${cl ? `${escHtml(cl.controller)}:${cl.c_port}` : "Chưa nối"}</span>`;
  }
  for (let p = 1; p <= totalPorts; p++) {
    const peer = used[p];
    badges += `<span class="p-badge ${peer ? (kind === "ctrl" ? "ctrl" : "used") : ""}">p${p}: ${escHtml(peer) || "Trống"}</span>`;
  }
  return `<div class="port-summary"><div class="port-summary-title">Trạng thái cổng kết nối</div><div class="port-badges">${badges}</div></div>`;
}

// =============================================================================
// MODAL XEM / SỬA CẤU HÌNH THIẾT BỊ & LINK
// =============================================================================
let editOnSave = null;
function openEditModal(title, html, onSave) {
  if ($("editModalTitle")) $("editModalTitle").textContent = title;
  if ($("editModalBody")) $("editModalBody").innerHTML = html;
  editOnSave = onSave;
  if ($("editModalWrap")) $("editModalWrap").style.display = "flex";
}
function closeEditModal() {
  if ($("editModalWrap")) $("editModalWrap").style.display = "none";
  editOnSave = null;
}
setClick("editModalSave", async () => {
  if (!editOnSave) return;
  try {
    await editOnSave();
    closeEditModal();
  } catch (err) {
    showToast("Lỗi: " + err.message);
  }
});

function openAddHost(x = null, y = null) {
  const d = nextHostDefaults(x, y);
  openEditModal("Thêm Host mới", `
    <div class="field"><label>Tên Host</label><input id="fName" value="${escHtml(d.name)}"></div>
    <div class="field"><label>Địa chỉ IP</label><input id="fIp" value="${escHtml(d.ip)}"></div>
    <div class="field"><label>Địa chỉ MAC</label><input id="fMac" value="${escHtml(d.mac)}"></div>
    <div class="field"><label>Số cổng kết nối</label><input value="1 (Host mặc định dùng cổng 1)" disabled></div>
  `, async () => {
    const s = cloneSpec();
    s.hosts.push({
      name: $("fName").value.trim(),
      ip: $("fIp").value.trim(),
      mac: $("fMac").value.trim(),
      ports: 1, x: d.x, y: d.y
    });
    await commitSpec(s);
  });
}

function openEditHost(name) {
  closePopovers();
  const h = spec.hosts.find(x => x.name === name);
  if (!h) return;
  openEditModal(`Cấu hình Host: ${name}`, `
    <div class="field"><label>Tên Host</label><input id="fName" value="${escHtml(h.name)}"></div>
    <div class="field"><label>Địa chỉ IP</label><input id="fIp" value="${escHtml(h.ip)}"></div>
    <div class="field"><label>Địa chỉ MAC</label><input id="fMac" value="${escHtml(h.mac)}"></div>
    <div class="field"><label>Số cổng kết nối</label><input value="1 (Host dùng cổng 1 trong sdnsim)" disabled></div>
    ${buildPortSummaryHtml("host", name, 1)}
  `, async () => {
    const newName = $("fName").value.trim();
    const s = cloneSpec();
    const target = s.hosts.find(x => x.name === name);
    target.name = newName;
    target.ip = $("fIp").value.trim();
    target.mac = $("fMac").value.trim();
    s.links.forEach(l => { if (l.a === name) l.a = newName; if (l.b === name) l.b = newName; });
    if (selectedNode === name) selectedNode = newName;
    await commitSpec(s);
  });
}

function openAddSwitch(x = null, y = null) {
  const d = nextSwitchDefaults(x, y);
  openEditModal("Thêm Switch mới", `
    <div class="field"><label>Tên Switch</label><input id="fName" value="${escHtml(d.name)}"></div>
    <div class="field"><label>Số cổng dữ liệu (1–64)</label><input id="fPorts" type="number" min="1" max="64" value="${d.ports}"></div>
    <div class="field"><label>Độ trễ xử lý (ms)</label><input id="fSwDelay" type="number" min="0" step="0.5" value="0"></div>
    <div class="field"><label>Băng thông chuyển mạch (để trống = không giới hạn)</label><input id="fSwBw" placeholder="VD: 10Mbps, 500Kbps, 1Gbps"></div>
  `, async () => {
    const s = cloneSpec();
    const delayMs = parseFloat($("fSwDelay").value || "0");
    const bwRaw = $("fSwBw").value.trim();
    s.switches.push({
      name: $("fName").value.trim(),
      ports: parseInt($("fPorts").value, 10),
      delay: Math.max(0, delayMs / 1000.0),
      bandwidth: bwRaw || null,
      x: d.x, y: d.y,
    });
    await commitSpec(s);
  });
}

function openEditSwitch(name) {
  closePopovers();
  const sw = spec.switches.find(x => x.name === name);
  if (!sw) return;
  const curDelayMs = +(((sw.delay || 0) * 1000).toFixed(3));
  openEditModal(`Cấu hình Switch: ${name}`, `
    <div class="field"><label>Tên Switch</label><input id="fName" value="${escHtml(sw.name)}"></div>
    <div class="field"><label>Số cổng dữ liệu (1–64)</label><input id="fPorts" type="number" min="1" max="64" value="${sw.ports}"></div>
    <div class="field"><label>Độ trễ xử lý (ms)</label><input id="fSwDelay" type="number" min="0" step="0.5" value="${curDelayMs}"></div>
    <div class="field"><label>Băng thông chuyển mạch (để trống = không giới hạn)</label><input id="fSwBw" value="${escHtml(sw.bandwidth ?? "")}" placeholder="VD: 10Mbps, 500Kbps, 1Gbps"></div>
    ${buildPortSummaryHtml("sw", name, sw.ports)}
  `, async () => {
    const newName = $("fName").value.trim();
    const newPorts = parseInt($("fPorts").value, 10);
    const delayMs = parseFloat($("fSwDelay").value || "0");
    const bwRaw = $("fSwBw").value.trim();
    const s = cloneSpec();
    const target = s.switches.find(x => x.name === name);
    target.name = newName;
    target.ports = newPorts;
    target.delay = Math.max(0, delayMs / 1000.0);
    target.bandwidth = bwRaw || null;
    s.links.forEach(l => { if (l.a === name) l.a = newName; if (l.b === name) l.b = newName; });
    s.control_links.forEach(cl => { if (cl.switch === name) cl.switch = newName; });
    if (selectedNode === name) selectedNode = newName;
    await commitSpec(s);
  });
}

function openAddController(x = null, y = null) {
  const d = nextCtrlDefaults(x, y);
  openEditModal("Thêm Controller mới", `
    <div class="field"><label>Tên Controller</label><input id="fCtrlName" value="${escHtml(d.name)}"></div>
    <div class="field"><label>Chế độ</label>
      <select id="fCtrlMode">
        <option value="internal">Internal (LearningController Python)</option>
        <option value="openflow">OpenFlow 1.3 TCP (os-ken / bên ngoài)</option>
      </select>
    </div>
    <div class="field"><label>Số cổng điều khiển (1–64)</label><input id="fCtrlPorts" type="number" min="1" max="64" value="${d.ports}"></div>
    <div class="field"><label>idle_timeout (giây, để trống nếu không hết hạn)</label><input id="fCtrlIdle" type="number" step="0.5" min="0" value=""></div>
    <div class="field"><label>Địa chỉ OpenFlow Host</label><input id="fCtrlHost" value="${escHtml(d.host)}"></div>
    <div class="field"><label>Cổng OpenFlow TCP</label><input id="fCtrlPort" type="number" value="${d.port}"></div>
  `, async () => {
    const s = cloneSpec();
    const idleVal = $("fCtrlIdle").value.trim();
    s.controllers.push({
      name: $("fCtrlName").value.trim(),
      mode: $("fCtrlMode").value,
      ports: parseInt($("fCtrlPorts").value, 10),
      idle_timeout: idleVal ? parseFloat(idleVal) : null,
      host: $("fCtrlHost").value.trim(),
      port: parseInt($("fCtrlPort").value, 10),
      x: d.x, y: d.y,
    });
    await commitSpec(s);
  });
}

function openEditController(name) {
  closePopovers();
  const c = spec.controllers.find(x => x.name === name);
  if (!c) return;
  openEditModal(`Cấu hình Controller: ${name}`, `
    <div class="field"><label>Tên Controller</label><input id="fCtrlName" value="${escHtml(c.name)}"></div>
    <div class="field"><label>Chế độ</label>
      <select id="fCtrlMode">
        <option value="internal" ${c.mode === "internal" ? "selected" : ""}>Internal (LearningController Python)</option>
        <option value="openflow" ${c.mode === "openflow" ? "selected" : ""}>OpenFlow 1.3 TCP (os-ken / bên ngoài)</option>
      </select>
    </div>
    <div class="field"><label>Số cổng điều khiển (1–64)</label><input id="fCtrlPorts" type="number" min="1" max="64" value="${c.ports || 8}"></div>
    <div class="field"><label>idle_timeout (giây, để trống nếu không hết hạn)</label><input id="fCtrlIdle" type="number" step="0.5" min="0" value="${c.idle_timeout ?? ""}"></div>
    <div class="field"><label>Địa chỉ OpenFlow Host</label><input id="fCtrlHost" value="${escHtml(c.host)}"></div>
    <div class="field"><label>Cổng OpenFlow TCP</label><input id="fCtrlPort" type="number" value="${c.port}"></div>
    ${buildPortSummaryHtml("ctrl", name, c.ports || 8)}
  `, async () => {
    const newName = $("fCtrlName").value.trim();
    const s = cloneSpec();
    const target = s.controllers.find(x => x.name === name);
    const idleVal = $("fCtrlIdle").value.trim();
    target.name = newName;
    target.mode = $("fCtrlMode").value;
    target.ports = parseInt($("fCtrlPorts").value, 10);
    target.idle_timeout = idleVal ? parseFloat(idleVal) : null;
    target.host = $("fCtrlHost").value.trim();
    target.port = parseInt($("fCtrlPort").value, 10);
    s.control_links.forEach(cl => { if (cl.controller === name) cl.controller = newName; });
    if (selectedNode === name) selectedNode = newName;
    await commitSpec(s);
  });
}

function openEditLink(idx) {
  closePopovers();
  const l = spec.links[idx];
  if (!l) return;
  const curDelayMs = +(((l.delay || 0.001) * 1000).toFixed(3));
  const curLossPct = +(((l.loss || 0) * 100).toFixed(2));
  openEditModal(`Cấu hình Liên kết: ${l.a}:${l.a_port} — ${l.b}:${l.b_port}`, `
    <div class="field"><label>Độ trễ đường truyền (ms)</label><input id="fLDelay" type="number" min="0.1" step="0.5" value="${curDelayMs}"></div>
    <div class="field"><label>Băng thông đường truyền (để trống = không giới hạn)</label><input id="fLBw" value="${escHtml(l.bandwidth ?? "")}" placeholder="VD: 10Mbps, 500Kbps, 1Gbps"></div>
    <div class="field"><label>Tỷ lệ mất gói (%)</label><input id="fLLoss" type="number" min="0" max="100" step="1" value="${curLossPct}"></div>
    <div class="field"><label>Giới hạn hàng đợi (gói, để trống = không giới hạn)</label><input id="fLQueue" type="number" min="1" value="${l.queue_limit ?? ""}" placeholder="VD: 10"></div>
  `, async () => {
    const s = cloneSpec();
    const target = s.links[idx];
    const delayMs = parseFloat($("fLDelay").value || "1");
    const bwRaw = $("fLBw").value.trim();
    const lossPct = parseFloat($("fLLoss").value || "0");
    const qRaw = $("fLQueue").value.trim();
    target.delay = Math.max(0.0001, delayMs / 1000.0);
    target.bandwidth = bwRaw || null;
    target.loss = Math.max(0, Math.min(1, lossPct / 100.0));
    target.queue_limit = qRaw ? parseInt(qRaw, 10) : null;
    await commitSpec(s);
    showToast(`Đã cập nhật liên kết ${target.a}:${target.a_port} — ${target.b}:${target.b_port}`);
  });
}

// =============================================================================
// GÁN SỰ KIỆN CHO THANH CÔNG CỤ (AN TOÀN CHỐNG NULL)
// =============================================================================
setClick("chipHost", () => openAddHost());
setClick("chipSwitch", () => openAddSwitch());
setClick("chipCtrl", () => openAddController());
setClick("btnLinkMode", () => setLinkMode(!linkMode));

setClick("btnClear", async () => {
  if (!confirm("Xóa toàn bộ thiết bị và Link hiện tại?")) return;
  const s = cloneSpec();
  s.hosts = []; s.switches = []; s.links = []; s.controllers = []; s.control_links = [];
  selectedNode = null;
  await commitSpec(s);
  showToast("Đã xóa trắng topology.");
});

setClick("btnOpenWeb", () => {
  if (window.pywebview?.api?.open_url_in_browser) window.pywebview.api.open_url_in_browser(webUrl);
  else window.open(webUrl, "_blank");
});

setClick("btnOpen", async () => {
  if (window.pywebview?.api?.open_topology_dialog) {
    const picked = await window.pywebview.api.open_topology_dialog();
    if (!picked) return;
    try {
      const res = await api("/api/topology/load", "POST", { path: picked });
      spec = res.spec; selectedNode = null; render(); fitView();
      showToast("Đã mở topology: " + picked.split(/[\\/]/).pop());
    } catch (err) { showToast("Lỗi mở file: " + err.message); }
  } else if ($("browserFileOpen")) {
    $("browserFileOpen").click();
  }
});

on("browserFileOpen", "change", async e => {
  const file = e.target.files?.[0];
  e.target.value = "";
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    await commitSpec(parsed);
    fitView();
    showToast("Đã mở topology: " + file.name);
  } catch (err) { showToast("File JSON không hợp lệ: " + err.message); }
});

setClick("btnSave", async () => {
  if (window.pywebview?.api?.save_topology_dialog) {
    const savedPath = await window.pywebview.api.save_topology_dialog(JSON.stringify(spec, null, 2));
    if (savedPath) showToast("Đã lưu: " + savedPath.split(/[\\/]/).pop());
  } else {
    const blob = new Blob([JSON.stringify(spec, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "sdn_topology.json";
    a.click();
    URL.revokeObjectURL(a.href);
    showToast("Đã tải xuống sdn_topology.json");
  }
});

// Nút Zoom & Fit View
setClick("btnZoomIn", () => {
  if (!svg) return;
  const rect = svg.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const newZoom = Math.min(3.0, zoom * 1.2);
  pan.x = cx - (cx - pan.x) * (newZoom / zoom);
  pan.y = cy - (cy - pan.y) * (newZoom / zoom);
  zoom = newZoom;
  updateViewportTransform();
});

setClick("btnZoomOut", () => {
  if (!svg) return;
  const rect = svg.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const newZoom = Math.max(0.15, zoom * 0.83);
  pan.x = cx - (cx - pan.x) * (newZoom / zoom);
  pan.y = cy - (cy - pan.y) * (newZoom / zoom);
  zoom = newZoom;
  updateViewportTransform();
});

setClick("btnFitView", fitView);
setClick("zoomLevel", () => { zoom = 1.0; updateViewportTransform(); });

document.querySelectorAll(".palette-chip").forEach(chip => {
  chip.addEventListener("dragstart", e => e.dataTransfer.setData("text/plain", chip.dataset.kind));
});

// Wireshark
async function refreshCaptureStatus() {
  try {
    const st = await api("/api/capture/status");
    liveTzsp = st.live_tzsp;
    if ($("capCount")) $("capCount").textContent = st.count;
    const btn = $("btnTzsp");
    if (btn) {
      btn.classList.toggle("btn-active", liveTzsp);
      btn.textContent = liveTzsp ? "📡 Live: BẬT" : "📡 Wireshark Live";
    }
  } catch {}
}

setClick("btnTzsp", async () => {
  const res = await api("/api/capture/tzsp", "POST", { enabled: !liveTzsp });
  await refreshCaptureStatus();
  showToast(res.live_tzsp ? "Đã BẬT phát TZSP (UDP 37008) tới Wireshark!" : "Đã TẮT phát Live Wireshark.");
});

setClick("btnPcap", async () => {
  if (window.pywebview?.api?.save_pcap_dialog) {
    const saved = await window.pywebview.api.save_pcap_dialog();
    if (saved) showToast("Đã lưu file PCAP: " + saved.split(/[\\/]/).pop());
  } else {
    window.location.href = "/api/capture/pcap";
  }
});

setClick("btnCapClear", async () => {
  await api("/api/capture/clear", "POST");
  await refreshCaptureStatus();
  showToast("Đã xóa bộ đệm gói bắt.");
});

// Theme Sáng / Tối / Theo thiết bị
const sysMedia = window.matchMedia("(prefers-color-scheme: dark)");
function applyTheme(mode) {
  localStorage.setItem("sdnsim_theme", mode);
  const resolved = mode === "system" ? (sysMedia.matches ? "dark" : "light") : mode;
  document.documentElement.setAttribute("data-resolved-theme", resolved);
  document.querySelectorAll(".theme-btn").forEach(btn => {
    if (btn.dataset.mode) btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  render();
}
document.querySelectorAll(".theme-btn").forEach(btn => {
  if (btn.dataset.mode) btn.addEventListener("click", () => applyTheme(btn.dataset.mode));
});
sysMedia.addEventListener("change", () => {
  if ((localStorage.getItem("sdnsim_theme") || "system") === "system") applyTheme("system");
});

// =============================================================================
// HIỆU ỨNG GÓI TIN CHẠY TỪ TỪ
// =============================================================================
const PKT_META = {
  icmp_req:     { color: "#00f0ff", label: "ICMP Req", duration: 900 },
  icmp_rep:     { color: "#10b981", label: "ICMP Rep", duration: 900 },
  arp_req:      { color: "#f59e0b", label: "ARP Req",  duration: 950 },
  arp_rep:      { color: "#f59e0b", label: "ARP Rep",  duration: 950 },
  of_packet_in: { color: "#c084fc", label: "PacketIn", duration: 800 },
  of_packetout: { color: "#c084fc", label: "PacketOut",duration: 800 },
  of_flowmod:   { color: "#e879f9", label: "FlowMod",  duration: 800 },
};

let packetQueueDelay = 0;
function enqueuePacket(fromName, toName, kind) {
  const delay = packetQueueDelay;
  packetQueueDelay += 380;
  setTimeout(() => {
    animatePacket(fromName, toName, kind);
    packetQueueDelay = Math.max(0, packetQueueDelay - 380);
  }, delay);
}

function animatePacket(fromName, toName, kind) {
  const fromNode = findNode(fromName);
  const toNode = findNode(toName);
  if (!fromNode || !toNode) return;

  const layer = $("packetLayer");
  if (!layer) return;

  const meta = PKT_META[kind] || { color: "#38bdf8", label: kind, duration: 900 };
  const x1 = fromNode.obj.x, y1 = fromNode.obj.y;
  const x2 = toNode.obj.x, y2 = toNode.obj.y;

  const svgNS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(svgNS, "g");
  g.setAttribute("class", "pkt-dot");
  g.style.pointerEvents = "none";

  const glow = document.createElementNS(svgNS, "circle");
  glow.setAttribute("cx", "0"); glow.setAttribute("cy", "0"); glow.setAttribute("r", "10");
  glow.setAttribute("fill", meta.color); glow.setAttribute("opacity", "0.4");
  glow.style.filter = "drop-shadow(0 0 6px " + meta.color + ")";

  const core = document.createElementNS(svgNS, "circle");
  core.setAttribute("cx", "0"); core.setAttribute("cy", "0"); core.setAttribute("r", "5.5");
  core.setAttribute("fill", "#ffffff"); core.setAttribute("stroke", meta.color); core.setAttribute("stroke-width", "2.2");

  const label = document.createElementNS(svgNS, "text");
  label.setAttribute("x", "0"); label.setAttribute("y", "-13");
  label.setAttribute("text-anchor", "middle");
  label.setAttribute("font-size", "10"); label.setAttribute("font-weight", "700");
  label.setAttribute("fill", meta.color); label.setAttribute("paint-order", "stroke");
  label.setAttribute("stroke", "#0b1120"); label.setAttribute("stroke-width", "3");
  label.textContent = meta.label;

  g.appendChild(glow); g.appendChild(core); g.appendChild(label);
  layer.appendChild(g);

  const anim = g.animate([
    { transform: `translate(${x1}px, ${y1}px) scale(0.7)`, opacity: 0 },
    { transform: `translate(${x1 + (x2 - x1) * 0.1}px, ${y1 + (y2 - y1) * 0.1}px) scale(1.1)`, opacity: 1, offset: 0.1 },
    { transform: `translate(${x2 - (x2 - x1) * 0.05}px, ${y2 - (y2 - y1) * 0.05}px) scale(1)`, opacity: 1, offset: 0.95 },
    { transform: `translate(${x2}px, ${y2}px) scale(0.8)`, opacity: 0.8 }
  ], {
    duration: meta.duration,
    easing: "cubic-bezier(0.3, 0, 0.2, 1)",
  });

  anim.onfinish = () => {
    const ripple = document.createElementNS(svgNS, "circle");
    ripple.setAttribute("cx", x2); ripple.setAttribute("cy", y2);
    ripple.setAttribute("r", "8"); ripple.setAttribute("fill", "none");
    ripple.setAttribute("stroke", meta.color); ripple.setAttribute("stroke-width", "2");
    layer.appendChild(ripple);
    const ripAnim = ripple.animate([{ r: 8, opacity: 1 }, { r: 26, opacity: 0 }], { duration: 320, easing: "ease-out" });
    ripAnim.onfinish = () => ripple.remove();
    g.remove();
  };
}

// =============================================================================
// HOST CONSOLE VÀ CONTROLLER CONSOLE
// =============================================================================
function appendHostLine(hostName, text, cls = "") {
  (hostLocalHistory[hostName] ||= []).push({ text, cls });
  if (activeHostConsole === hostName) renderHostTerm();
}

function renderHostTerm() {
  const term = $("hcTerm");
  if (!term) return;
  const lines = hostLocalHistory[activeHostConsole] || [];
  term.innerHTML = lines.map(l => `<div class="term-line ${l.cls}">${escHtml(l.text)}</div>`).join("");
  term.scrollTop = term.scrollHeight;
}

function openHostConsole(hostName) {
  closePopovers();
  activeHostConsole = hostName;
  const h = spec.hosts.find(x => x.name === hostName);
  if ($("hostConsoleTitle")) $("hostConsoleTitle").textContent = `Host Console — ${hostName} (${h?.ip || ""} | ${h?.mac || ""})`;
  if ($("hcPrompt")) $("hcPrompt").textContent = `${hostName}#`;
  const sel = $("hcTarget");
  if (sel) {
    sel.innerHTML = "";
    for (const other of spec.hosts) {
      if (other.name !== hostName) sel.add(new Option(`${other.name} (${other.ip})`, other.name));
    }
  }
  if (!hostLocalHistory[hostName]) {
    hostLocalHistory[hostName] = [{ text: `Console của ${hostName}. Gõ 'help' để xem danh sách lệnh.`, cls: "lvl-OPENFLOW" }];
  }
  renderHostTerm();
  if ($("hostConsoleWrap")) $("hostConsoleWrap").style.display = "flex";
  if ($("hcInput")) $("hcInput").focus();
}

function closeHostConsole() {
  activeHostConsole = null;
  if ($("hostConsoleWrap")) $("hostConsoleWrap").style.display = "none";
}

async function runHostCmd(cmd) {
  if (!activeHostConsole || !cmd.trim()) return;
  const hName = activeHostConsole;
  appendHostLine(hName, `${hName}# ${cmd}`);
  try {
    const res = await api(`/api/host/${hName}/exec`, "POST", { command: cmd });
    if (res.clear) { hostLocalHistory[hName] = []; renderHostTerm(); return; }
    for (const ln of res.lines) appendHostLine(hName, ln, res.ok ? "" : "lvl-ERROR");
  } catch (err) {
    appendHostLine(hName, `Lỗi: ${err.message}`, "lvl-ERROR");
  }
}

on("hcInput", "keydown", e => {
  if (e.key === "Enter") { const v = e.target.value; e.target.value = ""; runHostCmd(v); }
});
setClick("hcQuickPing", () => {
  const target = $("hcTarget")?.value, count = $("hcCount")?.value || "1";
  if (!target) return showToast("Chưa có host đích nào khác!");
  runHostCmd(`ping ${target} -c ${count}`);
});
setClick("hcPingAll", () => runHostCmd("pingall"));
setClick("hcClear", () => runHostCmd("clear"));

function openCtrlConsole(ctrlName) {
  closePopovers();
  ccOpen = true;
  ccCtrlName = ctrlName;
  updateCtrlConsoleButtons();
  renderCtrlTerm();
  if ($("ctrlConsoleWrap")) $("ctrlConsoleWrap").style.display = "flex";
}

function closeCtrlConsole() {
  ccOpen = false;
  ccCtrlName = null;
  if ($("ctrlConsoleWrap")) $("ctrlConsoleWrap").style.display = "none";
}

function updateCtrlConsoleButtons() {
  const c = spec.controllers.find(x => x.name === ccCtrlName) || spec.controllers[0];
  const isOf = c && c.mode === "openflow";
  if ($("ctrlConsoleTitle")) {
    $("ctrlConsoleTitle").textContent = c ? `Controller Console — ${c.name} (${isOf ? `OpenFlow :${c.port}` : "Internal"})` : "Controller Console";
  }
  if ($("ccOskenStart")) $("ccOskenStart").style.display = isOf && !oskenRunning ? "inline-flex" : "none";
  if ($("ccOskenStop")) $("ccOskenStop").style.display = isOf && oskenRunning ? "inline-flex" : "none";
}

function renderCtrlTerm() {
  if (!ccOpen) return;
  const term = $("ccTerm");
  if (!term) return;
  const filter = ($("ccFilter")?.value || "").trim().toLowerCase();
  const items = allLogs.filter(e => e.category !== "host")
    .filter(e => !ccCtrlName || e.source === ccCtrlName || e.source === "system" || e.level === "ERROR")
    .filter(e => !filter || e.message.toLowerCase().includes(filter) || e.level.toLowerCase().includes(filter));
  term.innerHTML = items.map(e => {
    const t = e.sim_time != null ? `${e.sim_time.toFixed(3)}s` : e.ts;
    return `<div class="term-line lvl-${e.level}">[${t}] [${e.level}] ${escHtml(e.message)}</div>`;
  }).join("");
  term.scrollTop = term.scrollHeight;
}

on("ccFilter", "input", renderCtrlTerm);
setClick("ccPauseBtn", e => {
  ccPaused = !ccPaused;
  e.target.textContent = ccPaused ? "Tiếp tục" : "Tạm dừng";
});
setClick("ccClearBtn", async () => {
  await api("/api/logs/clear?category=controller", "POST");
  allLogs = allLogs.filter(e => e.category === "host");
  renderCtrlTerm();
});
setClick("ccOskenStart", async () => {
  try {
    const q = ccCtrlName ? `?name=${encodeURIComponent(ccCtrlName)}` : "";
    await api(`/api/controller/osken/start${q}`, "POST");
    oskenRunning = true;
    updateCtrlConsoleButtons();
  } catch (err) { showToast("Lỗi: " + err.message); }
});
setClick("ccOskenStop", async () => {
  await api("/api/controller/osken/stop", "POST");
  oskenRunning = false;
  updateCtrlConsoleButtons();
});

// =============================================================================
// KẾT NỐI WEBSOCKET & KHỞI CHẠY
// =============================================================================
function connectSnapWs() {
  const wsSnap = new WebSocket(`ws://${location.host}/ws`);
  wsSnap.onmessage = e => {
    try {
      liveSnap = JSON.parse(e.data);
      if ($("simTime")) $("simTime").textContent = (liveSnap.time || 0).toFixed(2) + " s";
      refreshCaptureStatus();
    } catch {}
  };
  wsSnap.onclose = () => setTimeout(connectSnapWs, 1000);
}

function connectLogsWs() {
  const wsLogs = new WebSocket(`ws://${location.host}/ws/logs`);
  wsLogs.onmessage = e => {
    try {
      const entry = JSON.parse(e.data);
      if (entry.category === "packet" && entry.target) {
        enqueuePacket(entry.source, entry.target, entry.level);
      } else if (entry.category === "host") {
        appendHostLine(entry.source, entry.message, `lvl-${entry.level}`);
      } else {
        allLogs.push(entry);
        if (allLogs.length > 1000) allLogs.shift();
        if (!ccPaused) renderCtrlTerm();
      }
    } catch {}
  };
  wsLogs.onclose = () => setTimeout(connectLogsWs, 1000);
}

applyTheme(localStorage.getItem("sdnsim_theme") || "system");
connectSnapWs();
connectLogsWs();
loadInitial();