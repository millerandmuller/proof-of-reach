// Proof of Reach dashboard. Vanilla JS; every string from the API goes through textContent.
(function () {
  "use strict";
  var TIERS = [
    ["acknowledged", "acknowledged"], ["read", "read only"], ["delivered", "delivered only"],
    ["sent", "sent only"], ["unreachable", "unreachable"], ["not_started", "not started"]
  ];
  var CHANNEL_NAMES = { rcs: "RCS", whatsapp: "WhatsApp", email: "Email" };
  var STATE_TEXT = { acknowledged: "acknowledged", read: "read only", delivered: "delivered only", sent: "sent only", unreachable: "unreachable", none: "not started", not_started: "not started" };
  var cfg = null, started = false, seen = {}, reportTimer = null, lastAck = 0, liveIds = [];
  var OVERDUE_AFTER_S = 120; // a stage over 2,000 recipients takes about 40 s; past this the schedule itself is late
  var SWEEP_S = 4;           // must match the clock-sweep duration in styles.css
  var skewMs = 0, clocks = [], textsKey = null;

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

  // The API sends its own time with every state; counting against it keeps a viewer's wrong clock out of the countdown.
  function serverNow() { return Date.now() + skewMs; }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function mmss(seconds) { var s = Math.max(0, Math.ceil(seconds)); return Math.floor(s / 60) + ":" + pad2(s % 60); }
  function joinNames(channels) {
    var names = channels.map(function (ch) { return CHANNEL_NAMES[ch] || ch; });
    return names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names.join("");
  }

  function clockNote(c) {
    var tried = c.tried.length ? "No acknowledgment on " + joinNames(c.tried) + ". " : "";
    return tried + (c.verdict ? "No rung is left; when the window ends the rule records the verdict."
      : "When the window ends the rule sends " + joinNames([c.next_channel]) + ".");
  }

  // One tick of one device clock: counting down, sending (due time passed), or late (the stage was never recorded).
  function tickClock(k) {
    var c = k.c, due = c.due_at ? Date.parse(c.due_at) : NaN;
    var left = isNaN(due) ? NaN : (due - serverNow()) / 1000;
    var state = isNaN(left) || left <= 0 ? "sending" : "counting";
    if (state === "sending" && !isNaN(left) && -left > OVERDUE_AFTER_S) state = "late";
    k.root.className = "clock " + state;
    if (state === "counting") {
      k.num.textContent = mmss(left);
      k.fill.style.width = Math.min(100, Math.max(0, 100 * (1 - left / c.window_seconds))) + "%";
      k.due.textContent = "due " + clock(c.due_at) + " · window " + c.window_seconds + " s";
      k.note.textContent = clockNote(c);
      return;
    }
    k.fill.style.width = "";
    if (state === "late") {
      k.num.textContent = "+" + mmss(-left);
      k.due.textContent = "was due " + clock(c.due_at);
      k.note.textContent = "Stage " + c.next_stage + " has not been recorded yet. The scheduled run is late.";
      return;
    }
    k.num.textContent = "sending";
    k.due.textContent = isNaN(due) ? "" : "was due " + clock(c.due_at);
    k.note.textContent = "Stage " + c.next_stage + " is running over every recipient. This clock moves on when the stage is recorded.";
  }

  function tickClocks() { clocks.forEach(tickClock); }

  function buildClock(c) {
    var root = el("div", "clock");
    root.appendChild(el("p", "clock-label", (c.verdict ? "Verdict" : "Next rung · " + joinNames([c.next_channel])) + " · stage " + c.next_stage));
    var row = el("div", "clock-row"), num = el("span", "clock-num"), due = el("span", "clock-due");
    num.setAttribute("role", "timer");
    row.appendChild(num); row.appendChild(due); root.appendChild(row);
    var track = el("div", "clock-track"), fill = el("i");
    // phase from the wall clock, so the sweep does not restart when the card is rebuilt by the next poll
    fill.style.animationDelay = "-" + ((serverNow() / 1000) % SWEEP_S).toFixed(2) + "s";
    track.appendChild(fill); root.appendChild(track);
    var note = el("p", "clock-note"); root.appendChild(note);
    var k = { c: c, root: root, num: num, due: due, fill: fill, note: note };
    clocks.push(k); tickClock(k);
    return root;
  }

  function renderLive(d) {
    var root = $("live"); clear(root); clocks = [];
    if (!d.live || !d.live.length) { root.appendChild(el("p", "hint", "No live devices configured. Every recipient is replayed.")); return; }
    d.live.forEach(function (dev) {
      var card = el("div", "device");
      var head = el("div", "device-head");
      head.appendChild(el("span", "device-name", dev.label));
      head.appendChild(el("span", "state " + dev.state, STATE_TEXT[dev.state] || dev.state));
      card.appendChild(head);
      if (dev.clock) card.appendChild(buildClock(dev.clock));
      var latestRaw = null, ul = el("ul", "events");
      dev.history.slice().reverse().forEach(function (h) {
        var li = el("li");
        var key = dev.recipient_id + ":" + (h.event_id || h.event);
        if (!seen[key]) { seen[key] = true; if (started) li.className = "slide"; }
        li.appendChild(el("span", "when", clock(h.at)));
        var what = el("span", "what");
        var label = h.event + " · " + h.channel + " · " + h.state + (h.counted === false ? (h.is_bot ? " (bot-flagged, not counted)" : " (already at this tier)") : "");
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
      if (dev.feedback_status) card.appendChild(el("p", "hint", dev.feedback_status === "sent" ? "PutMessageFeedback: sent" : (dev.feedback_status.indexOf("transport:") === 0 ? "PutMessageFeedback: failed, " + dev.feedback_status.slice(10) + " (no AWS response)" : "PutMessageFeedback: failed, AWS returned " + dev.feedback_status + " (attempted once, not retried)")));
      root.appendChild(card);
    });
  }

  // The stamp is the validator's own result for this text: what it checked, the length against the limit, the verdict.
  function textBlock(label, t, headline) {
    var item = el("div", "text-item");
    item.appendChild(el("p", "text-seg", label));
    if (headline) item.appendChild(el("p", "text-title", headline));
    item.appendChild(el("p", "text-body", t.body));
    var failed = t.problems.length > 0;
    var stamp = el("p", "stamp" + (failed ? " fail" : ""));
    stamp.appendChild(el("span", "stamp-detail", (failed ? t.problems.join(", ") : "product, hazard, action, contact in order") + " · " + t.length + " of " + t.max + " characters"));
    stamp.appendChild(el("span", "stamp-verdict", failed ? "failed" : "passed"));
    item.appendChild(stamp);
    item.appendChild(el("p", "text-source", t.source === "agent" ? "written by the model on AgentCore Runtime, checked by code" : "stored default text, checked by code"));
    return item;
  }

  // Rebuilt only when the texts change (once, at Start), so an opened "more" row stays open across polls.
  function renderTexts(d) {
    var key = JSON.stringify(d.texts || null);
    if (key === textsKey) return;
    textsKey = key;
    var root = $("texts"); clear(root);
    $("texts-hint").textContent = d.texts ? "Written by the model, judged by code. A text that fails the check is replaced by the stored default." : "";
    if (!d.texts) {
      root.appendChild(el("p", "texts-empty", "The model writes the consumer and dealer texts when the notice starts. Each one is checked by code before anything is sent."));
      return;
    }
    [["consumer", "Consumer"], ["dealer", "Dealer"]].forEach(function (seg) {
      var t = d.texts[seg[0]], col = el("div", "text-col");
      col.appendChild(textBlock(seg[1] + " · RCS", t.rcs, t.rcs.title));
      var more = el("details", "text-more");
      more.appendChild(el("summary", null, "WhatsApp and email versions"));
      more.appendChild(textBlock(seg[1] + " · WhatsApp", t.whatsapp));
      more.appendChild(textBlock(seg[1] + " · Email", t.email, t.email.subject));
      col.appendChild(more);
      root.appendChild(col);
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
    liveIds = (d.live || []).map(function (dev) { return dev.recipient_id; });
    if (d.server_time && !isNaN(Date.parse(d.server_time))) skewMs = Date.parse(d.server_time) - Date.now();
    renderBar(d); renderChannels(d); renderTexts(d); renderLive(d);
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
    // live devices first (stable within each group), so a juror sees the phone they hold in the list
    var rows = r.exceptions.map(function (row, i) { return { row: row, i: i, live: liveIds.indexOf(row.recipient_id) >= 0 ? 0 : 1 }; })
      .sort(function (a, b) { return a.live - b.live || a.i - b.i; })
      .map(function (x) { return x.row; });
    var total = r.exception_total != null ? r.exception_total : rows.length; // the payload is capped; the total is not
    rows.slice(0, 12).forEach(function (row) {
      var tr = el("tr");
      tr.appendChild(el("td", null, row.recipient_id));
      tr.appendChild(el("td", null, STATE_TEXT[row.state] || row.state));
      tr.appendChild(el("td", null, row.reason));
      tb.appendChild(tr);
    });
    $("rep-more").textContent = total > 12 ? "and " + fmt(total - 12) + " more in the ledger" : "";
    var reasons = $("rep-reasons"); clear(reasons);
    r.reasons.slice(0, 5).forEach(function (x) { reasons.appendChild(el("li", null, x.reason + ": " + fmt(x.count))); });
    if (!r.reasons.length) reasons.appendChild(el("li", null, "none yet"));
    var themes = $("rep-themes"); clear(themes);
    (r.themes || []).forEach(function (t) { themes.appendChild(el("li", null, t.label + " (" + (t.replies || []).length + ")")); });
    if (!(r.themes || []).length) themes.appendChild(el("li", null, r.reply_count ? "summary pending" : "no replies"));
    if (r.unmatched_events) {
      $("rep-more").textContent += (total > 12 ? " · " : "") + fmt(r.unmatched_events) + " events could not be matched to a recipient";
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
    setInterval(tickClocks, 1000);
  }).catch(function () { banner("config.json missing: run scripts/deploy.sh to publish the dashboard config."); });
})();
