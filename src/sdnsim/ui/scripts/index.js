const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const row = cells => "<tr>" + cells.map(c => `<td>${esc(c)}</td>`).join("") + "</tr>";
const head = cols => "<tr>" + cols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr>";

let lastSnap = null;
const sysMedia = window.matchMedia("(prefers-color-scheme: dark)");

function applyTheme(mode) {
  localStorage.setItem("sdnsim_theme", mode);
  const resolved = mode === "system" ? (sysMedia.matches ? "dark" : "light") : mode;
  document.documentElement.setAttribute("data-resolved-theme", resolved);
  document.querySelectorAll(".theme-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  if (lastSnap) drawTopo(lastSnap);
}

document.querySelectorAll(".theme-btn").forEach(btn => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.mode));
});
sysMedia.addEventListener("change", () => {
  if ((localStorage.getItem("sdnsim_theme") || "system") === "system") applyTheme("system");
});

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fillSelects(hosts) {
  const names = Object.keys(hosts || {});
  const curSrc = $("src")?.value, curDst = $("dst")?.value;
  const existing = Array.from($("src")?.options || []).map(o => o.value);
  if (JSON.stringify(existing) === JSON.stringify(names)) return;
  if ($("src") && $("dst")) {
    $("src").innerHTML = ""; $("dst").innerHTML = "";
    for (const name of names) { $("src").add(new Option(name, name)); $("dst").add(new Option(name, name)); }
    if (names.includes(curSrc)) $("src").value = curSrc;
    if (names.includes(curDst)) $("dst").value = curDst;
    else if ($("dst").options.length > 1) $("dst").selectedIndex = 1;
  }
}

let prev = {};

function computePos(s) {
  const pos = {};
  if (s.layout) {
    for (const c of (s.layout.controllers || [])) pos[c.name] = { x: c.x, y: c.y, kind: "ctrl", data: c };
    for (const [n, info] of Object.entries(s.layout.switches || {})) {
      if (s.switches[n]) pos[n] = { x: info.x, y: info.y, kind: "sw", ports: info.ports, delay: info.delay, bandwidth: info.bandwidth };
    }
    for (const [n, info] of Object.entries(s.layout.hosts || {})) {
      if (s.hosts[n]) pos[n] = { x: info.x, y: info.y, kind: "host", ports: info.ports };
    }
    return pos;
  }
  Object.keys(s.switches || {}).forEach((n, i) => pos[n] = { x: 140 + i * 230, y: 150, kind: "sw", ports: 4 });
  const attach = {};
  for (const l of (s.links || [])) {
    for (const [h, o] of [[l.a, l.b], [l.b, l.a]]) if (s.hosts[h] && s.switches[o]) (attach[o] ||= []).push(h);
  }
  for (const [sw, hs] of Object.entries(attach)) {
    hs.forEach((h, j) => pos[h] = { x: pos[sw].x + (j - (hs.length - 1) / 2) * 120, y: 280, kind: "host", ports: 1 });
  }
  Object.keys(s.hosts || {}).forEach((h, idx) => { if (!pos[h]) pos[h] = { x: 120 + idx * 120, y: 280, kind: "host", ports: 1 }; });
  return pos;
}

function iconSvg(kind, x, y, color) {
  if (kind === "host") {
    return `<g transform="translate(${x},${y})"><rect x="2" y="3" width="20" height="14" rx="2" fill="none" stroke="${color}" stroke-width="2"/><path d="M8 21h8M12 17v4" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"/></g>`;
  }
  if (kind === "sw") {
    return `<g transform="translate(${x},${y})"><rect x="2" y="5" width="20" height="14" rx="3" fill="none" stroke="${color}" stroke-width="1.8"/><path d="M7 10h10m-2.5-2 2.5 2-2.5 2M17 14H7m2.5-2-2.5 2 2.5 2" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g>`;
  }
  return `<g transform="translate(${x},${y})"><circle cx="12" cy="12" r="3.2" fill="${color}"/><path d="M12 3v5.5m0 7V21M3 12h5.5m7 0H21M5.8 5.8l3.8 3.8m4.8 4.8 3.8 3.8M18.2 5.8l-3.8 3.8m-4.8 4.8-3.8 3.8" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/></g>`;
}

function linkPackets(s, l) {
  if (l.a_to_b && l.b_to_a) return (l.a_to_b.sent_packets || 0) + (l.b_to_a.sent_packets || 0);
  for (const [n, p] of [[l.a, l.a_port], [l.b, l.b_port]]) {
    const c = s.switches[n] && s.switches[n].ports[p];
    if (c) return c.rx_packets + c.tx_packets;
  }
  return 0;
}

function drawTopo(s) {
  const pos = computePos(s);
  if (!Object.keys(pos).length) {
    $("topo").innerHTML = `<div class="empty-msg">Chưa có topology — hãy tạo trên Desktop Studio hoặc mở file JSON.</div>`;
    return;
  }
  let maxX = 840, maxY = 420;
  for (const p of Object.values(pos)) { maxX = Math.max(maxX, p.x + 130); maxY = Math.max(maxY, p.y + 80); }

  const portBg = cssVar("--port-bg") || "#ffffff";
  const linkColor = cssVar("--link-line") || "#64748b";
  const bgSub = cssVar("--bg-elevated") || "#f8fafc";
  const textColor = cssVar("--text") || "#0f172a";
  const ctrlFill = cssVar("--ctrl-fill") || "#7c3aed";
  const swFill = cssVar("--sw-fill") || "#2563eb";
  const hostFill = cssVar("--host-fill") || "#ffffff";
  const hostStroke = cssVar("--host-stroke") || "#0ea5e9";
  const textMuted = cssVar("--text-muted") || "#64748b";

  let g = `<svg width="${maxX}" height="${maxY}" viewBox="0 0 ${maxX} ${maxY}">`;

  for (const cl of (s.layout?.control_links || [])) {
    const cp = pos[cl.controller], sp = pos[cl.switch];
    if (!cp || !sp) continue;
    const isOk = cp.data?.mode !== "openflow" || (s.controller?.switches && s.controller.switches[cl.switch]);
    g += `<line x1="${cp.x}" y1="${cp.y}" x2="${sp.x}" y2="${sp.y}" stroke="${isOk ? "#8b5cf6" : "#ef4444"}" stroke-width="2" stroke-dasharray="5,5"/>`;
    const cx = cp.x + (sp.x - cp.x) * 0.25, cy = cp.y + (sp.y - cp.y) * 0.25;
    const sx = cp.x + (sp.x - cp.x) * 0.75, sy = cp.y + (sp.y - cp.y) * 0.75;
    g += `<circle cx="${cx}" cy="${cy}" r="10.5" fill="${portBg}" stroke="#8b5cf6" stroke-width="1.3"/><text x="${cx}" y="${cy + 3.5}" font-size="10" font-weight="700" text-anchor="middle" fill="#8b5cf6">p${cl.c_port}</text>`;
    g += `<circle cx="${sx}" cy="${sy}" r="11" fill="${portBg}" stroke="#8b5cf6" stroke-width="1.3"/><text x="${sx}" y="${sy + 3.5}" font-size="9.5" font-weight="700" text-anchor="middle" fill="#8b5cf6">OF</text>`;
  }

  for (const l of (s.links || [])) {
    const a = pos[l.a], b = pos[l.b];
    if (!a || !b) continue;
    const key = `${l.a}:${l.a_port}-${l.b}:${l.b_port}`;
    const pk = linkPackets(s, l);
    const active = pk > (prev[key] ?? pk);
    prev[key] = pk;
    const w = 2.4 + Math.min(5, Math.log2(1 + pk) / 2);

    g += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${active ? "#f97316" : linkColor}" stroke-width="${w}"/>`;
    const ax = a.x + (b.x - a.x) * 0.25, ay = a.y + (b.y - a.y) * 0.25;
    const bx = a.x + (b.x - a.x) * 0.75, by = a.y + (b.y - a.y) * 0.75;
    g += `<circle cx="${ax}" cy="${ay}" r="10.5" fill="${portBg}" stroke="#3b82f6" stroke-width="1.3"/><text x="${ax}" y="${ay + 3.5}" font-size="10" font-weight="700" text-anchor="middle" fill="#3b82f6">p${l.a_port}</text>`;
    g += `<circle cx="${bx}" cy="${by}" r="10.5" fill="${portBg}" stroke="#3b82f6" stroke-width="1.3"/><text x="${bx}" y="${by + 3.5}" font-size="10" font-weight="700" text-anchor="middle" fill="#3b82f6">p${l.b_port}</text>`;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2 - 8;
    g += `<text x="${mx}" y="${my}" font-size="11" font-weight="700" text-anchor="middle" fill="${textColor}" paint-order="stroke" stroke="${bgSub}" stroke-width="4">${pk} pkt</text>`;
  }

  for (const [n, p] of Object.entries(pos)) {
    if (p.kind === "ctrl") {
      const modeTxt = p.data?.mode === "openflow" ? `OF :${p.data.port}` : "Internal";
      g += `<rect x="${p.x - 74}" y="${p.y - 26}" width="148" height="52" rx="12" fill="${ctrlFill}"/>`;
      g += iconSvg("ctrl", p.x - 62, p.y - 12, "#ede9fe");
      g += `<text x="${p.x - 30}" y="${p.y - 3}" font-size="13.5" font-weight="700" fill="#fff">${esc(n)}</text>`;
      g += `<text x="${p.x - 30}" y="${p.y + 14}" font-size="10.5" fill="#ede9fe">${esc(modeTxt)} · ${p.data?.ports || 8}p</text>`;
    } else if (p.kind === "sw") {
      const swParts = [`${p.ports || 4}p`];
      if (p.delay) swParts.push(`${+(p.delay * 1000).toFixed(1)}ms`);
      if (p.bandwidth) swParts.push(`${p.bandwidth}`);
      g += `<rect x="${p.x - 60}" y="${p.y - 26}" width="120" height="52" rx="11" fill="${swFill}"/>`;
      g += iconSvg("sw", p.x - 50, p.y - 12, "#dbeafe");
      g += `<text x="${p.x - 19}" y="${p.y - 3}" font-size="13.5" font-weight="700" fill="#fff">${esc(n)}</text>`;
      g += `<text x="${p.x - 19}" y="${p.y + 14}" font-size="10" fill="#dbeafe">${esc(swParts.join(" · "))}</text>`;
    } else {
      g += `<rect x="${p.x - 62}" y="${p.y - 26}" width="124" height="52" rx="11" fill="${hostFill}" stroke="${hostStroke}" stroke-width="1.8"/>`;
      g += iconSvg("host", p.x - 51, p.y - 12, "#0ea5e9");
      g += `<text x="${p.x - 20}" y="${p.y - 3}" font-size="13.5" font-weight="700" fill="${textColor}">${esc(n)}</text>`;
      g += `<text x="${p.x - 20}" y="${p.y + 14}" font-size="10.5" fill="${textMuted}">${esc(s.hosts[n]?.ip || "")}</text>`;
    }
  }

  g += `<g id="packetLayer"></g>`;
  $("topo").innerHTML = g + "</svg>";
}

function idleText(f, now) {
  if (f.idle_timeout == null) return "không hết hạn";
  const left = Math.max(0, f.idle_timeout - (now - f.last_hit));
  return `${f.idle_timeout}s (còn ${left.toFixed(1)}s)`;
}

function fmtBw(b) {
  if (b == null) return "không giới hạn";
  if (typeof b === "string") return b;
  if (b >= 1e9) return `${+(b / 1e9).toFixed(3)} Gb/s`;
  if (b >= 1e6) return `${+(b / 1e6).toFixed(3)} Mb/s`;
  if (b >= 1e3) return `${+(b / 1e3).toFixed(3)} kb/s`;
  return `${b} b/s`;
}

function dirText(d) {
  if (!d) return "-";
  let t = `${d.sent_packets} gói, mất ${d.dropped_loss}, tràn ${d.dropped_queue}`;
  if (d.queued) t += `, đợi ${d.queued}`;
  return t;
}

function render(s) {
  lastSnap = s;
  const ctl = s.controller || { mode: "internal" };
  $("ctl").textContent = ctl.mode === "openflow"
    ? "OpenFlow " + Object.entries(ctl.switches || {}).map(([n, ok]) => n + (ok ? " ✔" : " ✘")).join(" ")
    : "nội bộ";
  $("time").textContent = (s.time || 0).toFixed(2) + " s";
  $("pin").textContent = s.packet_in_total ?? 0;
  fillSelects(s.hosts);
  drawTopo(s);

  const hostEntries = Object.entries(s.hosts || {});
  let h = `<h3 class="card-title">Hosts (${hostEntries.length})</h3>`;
  if (!hostEntries.length) h += `<p style="color:var(--text-muted);font-size:13px;">Chưa có Host nào.</p>`;
  else {
    h += `<div class="table-wrap"><table class="td-left">` + head(["Tên", "IP", "MAC", "Số ping", "RTT gần nhất (ms)"]);
    for (const [name, x] of hostEntries) {
      const last = x.rtts.length ? (x.rtts[x.rtts.length - 1] * 1000).toFixed(2) : "-";
      h += row([name, x.ip, x.mac, x.rtts.length, last]);
    }
    h += `</table></div>`;
  }
  $("hostsCard").innerHTML = h;

  const links = s.links || [];
  let lk = `<h3 class="card-title">Liên kết (${links.length})</h3>`;
  if (!links.length) lk += `<p style="color:var(--text-muted);font-size:13px;">Chưa có đường nối nào.</p>`;
  else {
    lk += `<div class="table-wrap"><table class="td-left">` + head([
      "Liên kết", "Độ trễ", "Băng thông", "Mất gói", "Hàng đợi", "Chiều a → b", "Chiều b → a"
    ]);
    for (const l of links) {
      lk += row([
        `${l.a}:${l.a_port} — ${l.b}:${l.b_port}`,
        `${+(l.delay * 1000).toFixed(2)} ms`,
        fmtBw(l.bandwidth),
        `${+((l.loss || 0) * 100).toFixed(1)}%`,
        l.queue_limit != null ? `${l.queue_limit} gói` : "không giới hạn",
        dirText(l.a_to_b),
        dirText(l.b_to_a),
      ]);
    }
    lk += `</table></div>`;
  }
  $("linksCard").innerHTML = lk;

  let sw = "";
  for (const [name, x] of Object.entries(s.switches || {})) {
    const swDelayMs = +(((x.delay || 0) * 1000).toFixed(2));
    const swBwTxt = fmtBw(x.bandwidth);
    const dropPill = x.dropped_packets ? `<span class="pill" style="color:#ef4444;">rớt gói: <b>${x.dropped_packets}</b></span>` : "";

    const portRows = Object.entries(x.ports || {})
      .map(([p, c]) => row([p, c.rx_packets, c.rx_bytes, c.tx_packets, c.tx_bytes]))
      .join("") || `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);">Chưa có lưu lượng cổng</td></tr>`;

    const flowRows = (x.flows || []).map(f => {
      const matchTxt = Object.entries(f.match || {}).map(([k, v]) => `${k}=${v}`).join(", ") || "(mọi gói)";
      return row([matchTxt, f.out_port, f.priority, f.packets, f.bytes, idleText(f, s.time)]);
    }).join("") || `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">Chưa có flow nào</td></tr>`;

    sw += `
      <div class="card">
        <div class="card-head">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="badge-sw">Switch ${esc(name)}</span>
            <span class="pill">độ trễ: <b>${swDelayMs} ms</b></span>
            <span class="pill">băng thông: <b>${esc(swBwTxt)}</b></span>
            ${dropPill}
          </div>
          <span class="pill">table-miss: <b>${x.table_misses}</b></span>
        </div>
        <div class="sw-grid">
          <div class="sw-pane">
            <h4>Thống kê cổng</h4>
            <div class="table-wrap"><table class="td-center">${head(["Cổng", "rx gói", "rx byte", "tx gói", "tx byte"])}${portRows}</table></div>
          </div>
          <div class="sw-pane">
            <h4>Bảng Flow (${(x.flows || []).length})</h4>
            <div class="table-wrap"><table class="td-left">${head(["match", "cổng ra", "ưu tiên", "gói", "byte", "idle_timeout"])}${flowRows}</table></div>
          </div>
        </div>
      </div>`;
  }
  $("switches").innerHTML = sw;
}

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
  if (!lastSnap) return;
  const pos = computePos(lastSnap);
  const fromP = pos[fromName], toP = pos[toName];
  if (!fromP || !toP) return;

  const layer = document.getElementById("packetLayer");
  if (!layer) return;

  const meta = PKT_META[kind] || { color: "#38bdf8", label: kind, duration: 900 };
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
  core.setAttribute("fill", "#fff"); core.setAttribute("stroke", meta.color); core.setAttribute("stroke-width", "2.2");

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
    { transform: `translate(${fromP.x}px, ${fromP.y}px) scale(0.7)`, opacity: 0 },
    { transform: `translate(${fromP.x + (toP.x - fromP.x) * 0.1}px, ${fromP.y + (toP.y - fromP.y) * 0.1}px) scale(1.1)`, opacity: 1, offset: 0.1 },
    { transform: `translate(${toP.x - (toP.x - fromP.x) * 0.05}px, ${toP.y - (toP.y - fromP.y) * 0.05}px) scale(1)`, opacity: 1, offset: 0.95 },
    { transform: `translate(${toP.x}px, ${toP.y}px) scale(0.8)`, opacity: 0.8 }
  ], {
    duration: meta.duration,
    easing: "cubic-bezier(0.3, 0, 0.2, 1)",
  });

  anim.onfinish = () => {
    const ripple = document.createElementNS(svgNS, "circle");
    ripple.setAttribute("cx", toP.x); ripple.setAttribute("cy", toP.y);
    ripple.setAttribute("r", "8"); ripple.setAttribute("fill", "none");
    ripple.setAttribute("stroke", meta.color); ripple.setAttribute("stroke-width", "2");
    layer.appendChild(ripple);
    const ripAnim = ripple.animate([{ r: 8, opacity: 1 }, { r: 26, opacity: 0 }], { duration: 320, easing: "ease-out" });
    ripAnim.onfinish = () => ripple.remove();
    g.remove();
  };
}

// =============================================================================
// KẾT NỐI WEBSOCKET ĐỒNG BỘ
// =============================================================================
function connectWs() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $("status").textContent = "đã kết nối"; };
  ws.onerror = () => { $("status").textContent = "lỗi kết nối"; };
  ws.onclose = () => { $("status").textContent = "mất kết nối"; setTimeout(connectWs, 1000); };
  ws.onmessage = e => {
    try {
      render(JSON.parse(e.data));
    } catch (err) {
      console.error("Lỗi parse snapshot:", err);
    }
  };
}

function connectLogsWs() {
  const ws = new WebSocket(`ws://${location.host}/ws/logs`);
  ws.onmessage = e => {
    try {
      const entry = JSON.parse(e.data);
      if (entry.category === "packet" && entry.target) {
        enqueuePacket(entry.source, entry.target, entry.level);
      }
    } catch {}
  };
  ws.onclose = () => setTimeout(connectLogsWs, 1500);
}

$("go").onclick = () => {
  if (!$("src")?.value || !$("dst")?.value) return;
  fetch("/ping", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ src: $("src").value, dst: $("dst").value }),
  });
};

applyTheme(localStorage.getItem("sdnsim_theme") || "system");
fetch("/snapshot").then(r => r.json()).then(render).catch(err => console.error("Lỗi fetch snapshot:", err));
connectWs();
connectLogsWs();