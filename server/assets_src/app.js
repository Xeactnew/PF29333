// Small helpers: charts drawn as SVG, clickable rows, confirm buttons, broker and server dropdowns.
(function () {
  // Time limit: show the date box only for "Until a date"
  document.querySelectorAll("select[data-limit]").forEach((sel) => {
    const box = sel.closest("form").querySelector("[data-limit-date]");
    const sync = () => { if (box) box.hidden = sel.value !== "date"; };
    sel.addEventListener("change", sync); sync();
  });

  // Sign-up: payment choices only for paid plans
  document.querySelectorAll("select[data-plan-select]").forEach((sel) => {
    const free = (sel.dataset.free || "").split(",");
    const sync = () => sel.closest("form").querySelectorAll("[data-paid-only]").forEach((el) => { el.hidden = free.includes(sel.value); });
    sel.addEventListener("change", sync); sync();
  });

  // Broker → server dropdowns; "isn't listed" opens a box to type the server
  document.querySelectorAll("[data-srvpick]").forEach((box) => {
    const data = JSON.parse(box.querySelector("[data-srv-data]").textContent || "{}");
    const brokerBox = box.querySelector("[data-srv-brokerbox]"), listBox = box.querySelector("[data-srv-listbox]");
    const typed = box.querySelector("[data-srv-typed]"), input = typed.querySelector("input");
    const bSel = box.querySelector("[data-srv-broker]"), sSel = box.querySelector("[data-srv-select]");
    const need = box.hasAttribute("data-required"), OTHER = "__other";
    const key = (x) => String(x).replace(/[\s_\-.]+/g, "").toLowerCase();
    const fill = (name) => {
      sSel.innerHTML = "";
      const add = (v, label) => { const o = document.createElement("option"); o.value = v; o.textContent = label; sSel.appendChild(o); };
      const list = data[name] || [];
      if (list.length !== 1) add("", "Choose your server");
      list.forEach((srv) => add(srv, srv));
      add(OTHER, "My server isn't listed");
    };
    const sync = () => {
      const b = bSel.value, known = !!b && b !== OTHER;
      const typing = b === OTHER || (known && sSel.value === OTHER);
      listBox.hidden = !known;
      typed.hidden = !typing;
      bSel.required = need;
      sSel.required = need && known && !typing;
      input.required = need && typing;
      if (known && !typing) input.value = sSel.value;
      else if (!typing) input.value = "";
    };
    // start from the server already saved (account page) or nothing
    const cur = input.value.trim();
    let found = "";
    if (cur) for (const name in data) if (data[name].some((x) => key(x) === key(cur))) { found = name; break; }
    brokerBox.hidden = false;
    if (found) { bSel.value = found; fill(found); sSel.value = data[found].find((x) => key(x) === key(cur)); }
    else if (cur) bSel.value = OTHER;
    bSel.addEventListener("change", () => {
      if (bSel.value && bSel.value !== OTHER) fill(bSel.value);
      if (bSel.value === OTHER) input.value = "";
      sync();
      (bSel.value === OTHER ? input : sSel).focus();
    });
    sSel.addEventListener("change", () => { if (sSel.value === OTHER) input.value = ""; sync(); if (sSel.value === OTHER) input.focus(); });
    sync();
  });

  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

  // Daily P/L bars: data-bars='[["2026-11-01", 12.5], ...]'
  document.querySelectorAll("svg[data-bars]").forEach((svg) => {
    const data = JSON.parse(svg.dataset.bars || "[]");
    const W = 600, H = 150, pad = 4;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const max = Math.max(1, ...data.map((d) => Math.abs(d[1])));
    const mid = H / 2, bw = W / Math.max(1, data.length);
    svg.appendChild(el("line", { x1: 0, x2: W, y1: mid, y2: mid, class: "zero" }));
    data.forEach(([day, v], i) => {
      const h = v === 0 ? 2 : Math.max(3, (Math.abs(v) / max) * (mid - pad));
      const r = el("rect", { x: i * bw + bw * 0.18, width: bw * 0.64, rx: 1.5,
        y: v >= 0 ? mid - h : mid, height: h, class: v > 0 ? "u" : v < 0 ? "d" : "e" });
      const t = el("title", {}); t.textContent = `${day}: ${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
      r.appendChild(t); svg.appendChild(r);
    });
  });

  // Balance / equity line: data-line='[["time", balance, equity], ...]'
  document.querySelectorAll("svg[data-line]").forEach((svg) => {
    const data = JSON.parse(svg.dataset.line || "[]");
    const W = 600, H = 200, pad = 8;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    if (data.length < 2) {
      const msg = document.createElement("p");
      msg.className = "muted chart-empty";
      msg.textContent = "The chart fills in once the account has been online for a while.";
      svg.replaceWith(msg); return;
    }
    const vals = data.flatMap((d) => [d[1], d[2]]);
    const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
    const xy = (i, v) => `${(i / (data.length - 1)) * W},${H - pad - ((v - lo) / span) * (H - 2 * pad)}`;
    svg.appendChild(el("polyline", { points: data.map((d, i) => xy(i, d[2])).join(" "), fill: "none",
      stroke: "#8b7bff", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
    svg.appendChild(el("polyline", { points: data.map((d, i) => xy(i, d[1])).join(" "), fill: "none",
      stroke: "#e7eaf5", "stroke-width": 1.2, "stroke-dasharray": "4 4", opacity: 0.6, "vector-effect": "non-scaling-stroke" }));
  });

  document.querySelectorAll("tr[data-href]").forEach((tr) => {
    tr.classList.add("link-row");
    tr.addEventListener("click", (e) => { if (!e.target.closest("a,button,form")) location.href = tr.dataset.href; });
  });

  // Times the customer picks (daily summary) are in their own time zone: send its offset with the form.
  document.querySelectorAll("input[data-tz]").forEach((i) => { i.value = String(new Date().getTimezoneOffset()); });

  document.querySelectorAll("[data-confirm]").forEach((b) => {
    b.addEventListener("click", (e) => { if (!confirm(b.dataset.confirm)) e.preventDefault(); });
  });

  // lot mode: show only the matching input
  const mode = document.querySelector("select[name=lot_mode]");
  if (mode) {
    const sync = () => document.querySelectorAll("[data-lot]").forEach((f) => {
      f.hidden = f.dataset.lot !== mode.value;
    });
    mode.addEventListener("change", sync); sync();
  }

  // billing: show paid-until only for monthly
  document.querySelectorAll("select[name=billing]").forEach((sel) => {
    const box = sel.form.querySelector("[data-monthly]");
    const sync = () => { if (box) box.hidden = sel.value !== "monthly"; };
    sel.addEventListener("change", sync); sync();
  });

  // ---- phone app ---------------------------------------------------------
  // Tables turn into cards on phones: give each cell its column name.
  document.querySelectorAll(".table-wrap table").forEach((table) => {
    const head = table.querySelector("tr:has(> th)") || table.rows[0];
    if (!head) return;
    const names = [...head.cells].map((c) => c.textContent.trim());
    [...table.rows].forEach((row) => {
      if (row === head) return;
      let col = 0;
      [...row.cells].forEach((cell) => {
        if (!cell.hasAttribute("colspan") && names[col] && !cell.dataset.label) cell.dataset.label = names[col];
        col += cell.colSpan || 1;
      });
    });
  });

  // "More" sheet in the admin tab bar
  document.querySelectorAll("[data-sheet]").forEach((btn) => {
    const sheet = document.getElementById(btn.dataset.sheet);
    if (!sheet) return;
    const set = (open) => { sheet.hidden = !open; btn.setAttribute("aria-expanded", String(open)); };
    btn.addEventListener("click", () => set(sheet.hidden));
    sheet.addEventListener("click", (e) => { if (e.target === sheet) set(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") set(false); });
  });

  const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  if (standalone) document.documentElement.classList.add("app");

  // Pull down to refresh: a home-screen app has no reload button.
  if (standalone && "ontouchstart" in window) {
    const ptr = document.createElement("div");
    ptr.className = "ptr"; ptr.innerHTML = "<i></i>";
    document.body.appendChild(ptr);
    let startY = null, pull = 0;
    window.addEventListener("touchstart", (e) => {
      startY = window.scrollY <= 0 && !document.querySelector(".sheet:not([hidden])") ? e.touches[0].clientY : null;
    }, { passive: true });
    window.addEventListener("touchmove", (e) => {
      if (startY === null) return;
      pull = Math.max(0, Math.min(120, e.touches[0].clientY - startY));
      ptr.style.opacity = pull > 10 ? "1" : "0";
      ptr.style.transform = `translateY(${pull * 0.6 - 20}px) rotate(${pull * 3}deg)`;
    }, { passive: true });
    window.addEventListener("touchend", () => {
      if (startY !== null && pull > 80) { ptr.classList.add("spin"); location.reload(); }
      else { ptr.style.opacity = "0"; ptr.style.transform = "translateY(-60px)"; }
      startY = null; pull = 0;
    });
  }

  // Signal group deck: swipe between groups on phones, dots + "2 of 4" follow along.
  document.querySelectorAll("[data-deck]").forEach((deck) => {
    const cards = Array.from(deck.children);
    const panel = deck.closest("section") || document;
    const dots = panel.querySelectorAll("[data-deck-dots] i");
    const count = panel.querySelector("[data-deck-count]");
    const total = count ? count.textContent : "";
    let shown = -1;
    const show = () => {
      if (deck.scrollWidth <= deck.clientWidth + 4) { if (count) count.textContent = total; return; }
      const mid = deck.scrollLeft + deck.clientWidth / 2;
      let best = 0, gap = Infinity;
      cards.forEach((c, i) => { const d = Math.abs(c.offsetLeft + c.offsetWidth / 2 - mid); if (d < gap) { gap = d; best = i; } });
      if (best === shown) return;
      shown = best;
      dots.forEach((d, i) => d.classList.toggle("on", i === best));
      if (count) count.textContent = (best + 1) + " of " + cards.length;
    };
    let raf = 0;
    deck.addEventListener("scroll", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(show); }, { passive: true });
    dots.forEach((d, i) => d.addEventListener("click", () => cards[i].scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" })));
    window.addEventListener("resize", show);
    // opening a card from the overview (#group-<key>) lands on that card
    const want = location.hash.startsWith("#g-") ? document.getElementById(location.hash.slice(1)) : null;
    if (want && deck.contains(want)) want.scrollIntoView({ inline: "center", block: "nearest" });
    show();
  });

  // Lets Android offer "Install app"; also shows a friendly page when there's no signal.
  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
})();
