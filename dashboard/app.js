// Proof of Reach dashboard. Vanilla JS; every string from the API goes through textContent.
(function () {
  "use strict";
  var TIERS = [
    ["acknowledged", "acknowledged"], ["read", "read only"], ["delivered", "delivered only"],
    ["sent", "sent only"], ["unreachable", "unreachable"], ["not_started", "not started"]
  ];
  var CHANNEL_NAMES = { rcs: "RCS", whatsapp: "WhatsApp", email: "Email" };
  var STATE_TEXT = { acknowledged: "acknowledged", read: "read only", delivered: "delivered only", sent: "sent only", unreachable: "unreachable", none: "not started", not_started: "not started" };
  var cfg = null, started = false, seen = {}, reportTimer = null, lastAck = 0;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fmt(n) { return Number(n).toLocaleString("en-US"); }

  function banner(msg) {
    var b = $("banner");
    if (msg) { b.textContent = msg; b.hidden = false; } else { b.hidden = true; }
  }

  function api(path, opts) {
    var ctl = new AbortController();
    var t = setTimeout(function () { ctl.abort(); }, 8000);
    var url = cfg.apiUrl.replace(/\/$/, "") + path + (path.indexOf("?") < 0 ? "?" : "&") + "notice=" + encodeURIComponent(cfg.noticeId);
    return fetch(url, Object.assign({ signal: ctl.signal, cache: "no-store" }, opts || {}))
      .then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); })
      .finally(function () { clearTimeout(t); });
  }

  function renderBar(d) {
    var bar = $("bar"), legend = $("legend");
    clear(bar); clear(legend);
    TIERS.forEach(function (t) {
      var n = d.counts[t[0]] || 0;
      if (d.total && n) {
        var seg = el("div", "seg " + t[0]);
        seg.style.width = (100 * n / d.total) + "%";
        seg.title = t[1] + ": " + fmt(n);
        bar.appendChild(seg);
      }
      var li = el("li");
      var sw = el("span", "swatch " + t[0]);
      li.appendChild(sw);
      li.appendChild(el("span", null, t[1] + " " + fmt(n)));
      legend.appendChild(li);
    });
  }

  function renderChannels(d) {
    var root = $("channels"); clear(root);
    ["rcs", "whatsapp", "email"].forEach(function (ch) {
      var c = d.channels[ch] || { sent: 0, delivered: 0, read: 0, acknowledged: 0 };
      var row = el("div", "chan");
      var head = el("div", "chan-name");
      head.appendChild(el("span", null, CHANNEL_NAMES[ch]));
      head.appendChild(el("span", null, fmt(c.sent) + " sent"));
      row.appendChild(head);
      var cells = el("div", "chan-bars");
      [["sent", "sent", ""], ["delivered", "delivered", ""], ["read", "read receipt", "read"], ["acknowledged", "acknowledged", "ack"]].forEach(function (k) {
        var cell = el("div", "chan-cell " + k[2]);
        cell.appendChild(el("b", null, fmt(c[k[0]])));
        cell.appendChild(document.createTextNode(k[1]));
        var m = el("div", "meter"), i = el("i");
        i.style.width = (c.sent ? 100 * c[k[0]] / c.sent : 0) + "%";
        m.appendChild(i); cell.appendChild(m); cells.appendChild(cell);
      });
      row.appendChild(cells);
      root.appendChild(row);
    });
  }

  function clock(iso) {
    var d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { hour12: false });
  }

  function renderLive(d) {
    var root = $("live"); clear(root);
    if (!d.live || !d.live.length) { root.appendChild(el("p", "hint", "No live devices configured. Every recipient is replayed.")); return; }
    d.live.forEach(function (dev) {
      var card = el("div", "device");
      var head = el("div", "device-head");
      head.appendChild(el("span", "device-name", dev.label));
      head.appendChild(el("span", "state " + dev.state, STATE_TEXT[dev.state] || dev.state));
      card.appendChild(head);
      var latestRaw = null, ul = el("ul", "events");
      dev.history.slice().reverse().forEach(function (h) {
        var li = el("li");
        var key = dev.recipient_id + ":" + (h.event_id || h.event);
        if (!seen[key]) { seen[key] = true; if (started) li.className = "slide"; }
        li.appendChild(el("span", "when", clock(h.at)));
        var what = el("span", "what");
        var label = h.event + " · " + h.channel + " · " + h.state + (h.counted === false ? " (logged, not counted)" : "");
        what.appendChild(el("b", null, label));
        if (h.provider_message_id) what.appendChild(el("small", null, "message id " + h.provider_message_id + (h.event_id ? " · event " + h.event_id.slice(0, 8) : "")));
        li.appendChild(what); ul.appendChild(li);
        if (!latestRaw && h.raw) latestRaw = h;
      });
      card.appendChild(ul);
      if (latestRaw) {
        var det = el("details", "raw"); det.open = true;
        det.appendChild(el("summary", null, "raw event (" + latestRaw.event + ")"));
        det.appendChild(el("pre", "json", JSON.stringify(latestRaw.raw, null, 2)));
        card.appendChild(det);
      }
      if (dev.unreachable_reason) card.appendChild(el("p", "hint", dev.unreachable_reason));
      root.appendChild(card);
    });
  }

  function renderState(d) {
    if (d.status === "no_notice") {
      $("notice-title").textContent = "No notice seeded";
      $("status-chip").textContent = "empty"; return;
    }
    var defaults = Object.keys(d.text_sources || {}).filter(function (k) { return d.text_sources[k] === "default"; }).length;
    $("notice-title").textContent = (d.title || d.notice_id) + (defaults ? " · default text for " + defaults + " of " + Object.keys(d.text_sources).length + " messages" : "");
    var chip = $("status-chip");
    chip.className = "chip " + d.status;
    chip.textContent = d.status === "running" ? "running · stage " + Math.min(d.stage || 1, 3) + " of 3" : d.status;
    $("start-btn").hidden = d.status !== "ready";
    var empty = d.status === "ready";
    $("empty").hidden = !empty;
    $("bar-label").textContent = empty ? "" : d.label;
    $("bar-label").hidden = empty;
    var ack = d.counts.acknowledged || 0, countEl = $("bar-count");
    countEl.textContent = empty ? "" : fmt(ack) + " of " + fmt(d.total) + " acknowledged";
    if (started && ack > lastAck) { countEl.classList.remove("pulse"); void countEl.offsetWidth; countEl.classList.add("pulse"); }
    lastAck = ack;
    renderBar(d); renderChannels(d); renderLive(d);
    $("footer-text").textContent = d.footer || "";
    if (!reportTimer && d.status !== "ready") { loadReport(); reportTimer = setInterval(loadReport, 5000); }
    if (d.status === "ready" && reportTimer) { clearInterval(reportTimer); reportTimer = null; clearReport(); }
    started = true;
  }

  function clearReport() {
    ["rep-headline", "rep-breakdown", "rep-last", "rep-more"].forEach(function (id) { $(id).textContent = ""; });
    clear($("rep-exceptions").tBodies[0]); clear($("rep-reasons")); clear($("rep-themes"));
  }

  function renderReport(r) {
    if (r.status === "no_notice") return;
    $("rep-headline").textContent = r.headline;
    $("rep-breakdown").textContent = r.breakdown;
    $("rep-last").textContent = r.last_line;
    $("report-hint").textContent = r.closed ? "final" : "updating";
    var tb = $("rep-exceptions").tBodies[0]; clear(tb);
    r.exceptions.slice(0, 12).forEach(function (row) {
      var tr = el("tr");
      tr.appendChild(el("td", null, row.recipient_id));
      tr.appendChild(el("td", null, STATE_TEXT[row.state] || row.state));
      tr.appendChild(el("td", null, row.reason));
      tb.appendChild(tr);
    });
    $("rep-more").textContent = r.exceptions.length > 12 ? "and " + fmt(r.exceptions.length - 12) + " more in the ledger" : "";
    var reasons = $("rep-reasons"); clear(reasons);
    r.reasons.slice(0, 5).forEach(function (x) { reasons.appendChild(el("li", null, x.reason + ": " + fmt(x.count))); });
    if (!r.reasons.length) reasons.appendChild(el("li", null, "none yet"));
    var themes = $("rep-themes"); clear(themes);
    (r.themes || []).forEach(function (t) { themes.appendChild(el("li", null, t.label + " (" + (t.replies || []).length + ")")); });
    if (!(r.themes || []).length) themes.appendChild(el("li", null, r.reply_count ? "summary pending" : "no replies"));
    if (r.unmatched_events) {
      $("rep-more").textContent += (r.exceptions.length > 12 ? " · " : "") + fmt(r.unmatched_events) + " events could not be matched to a recipient";
    }
  }

  function loadReport() {
    api("/report").then(function (res) { renderReport(res.body); }).catch(function () { /* the state poll shows the banner */ });
  }

  function poll() {
    api("/state").then(function (res) {
      if (res.status >= 300 || !res.body.status) throw new Error("bad response " + res.status);
      banner("");
      renderState(res.body);
    }).catch(function () {
      banner("Cannot reach the ledger. Showing the last known state; retrying.");
    });
  }

  function onStart() {
    var btn = $("start-btn");
    btn.disabled = true;
    api("/start", { method: "POST", headers: { "x-start-token": cfg.startToken || "" } }).then(function (res) {
      if (res.status === 403) banner("Start was refused: the dashboard config has no valid start token. Run scripts/deploy.sh.");
      else if (res.status === 409) banner("This notice was already started. Reset it with seed/seed.py --reset.");
      else if (res.status >= 300) banner("Start failed: " + (res.body.error || res.status));
      else { banner(""); poll(); }
    }).catch(function () { banner("Start request failed; check the connection."); })
      .finally(function () { btn.disabled = false; });
  }

  fetch("config.json", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (c) {
    cfg = c;
    $("start-btn").addEventListener("click", onStart);
    poll();
    setInterval(poll, 2000);
  }).catch(function () { banner("config.json missing: run scripts/deploy.sh to publish the dashboard config."); });
})();
