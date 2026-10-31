// Page transitions (SPA-router style), typewriter headings and the dark/light theme toggle. External file, so it works under the strict CSP.
(() => {
  const root = document.documentElement, body = document.body;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  // ---- transitions: pages animate in via app.css; here we animate out before leaving ----
  const LEAVE_MS = 180;
  const leave = () => body.classList.add("hx-leaving");
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || reduceMotion || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search)) return;  // other sites, same-page links
    e.preventDefault();
    leave();
    setTimeout(() => location.assign(url), LEAVE_MS);
  });
  // Forms: let the browser submit as normal. A button with data-busy shows a spinner + that text while the server works;
  // other forms play the exit animation while the next page loads
  document.addEventListener("submit", (e) => {
    if (e.defaultPrevented) return;
    if (e.target.hasAttribute?.("data-download")) { const f = e.target; setTimeout(() => f.reset(), 800); return; }  // a file comes back, the page stays: no exit animation; the password is cleared once sent
    if (Date.now() - (+e.target.dataset.sent || 0) < 3000) { e.preventDefault(); return; }  // a double click sends a form once (the server copes with two as well: this saves the second trip)
    e.target.dataset.sent = Date.now();
    remember(e.target);
    const busy = e.submitter?.hasAttribute("data-busy") ? e.submitter : e.target.querySelector("[data-busy]");  // submitter: buttons outside the form (form="...")
    if (busy) { busy.classList.add("hx-busy"); busy.setAttribute("aria-busy", "true"); }
    else if (!reduceMotion) leave();
  });
  // Coming back via the back/forward cache restores the old page as it was, so undo the leaving/busy state
  addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    body.classList.remove("hx-leaving");
    document.querySelectorAll(".hx-busy").forEach((b) => { b.classList.remove("hx-busy"); b.removeAttribute("aria-busy"); });
  });
  // ---- typewriter: <span data-type>text</span> types itself out; screen readers get the full text right away ----
  document.querySelectorAll("[data-type]").forEach((el) => {
    const text = el.textContent;
    const sr = Object.assign(document.createElement("span"), { className: "visually-hidden", textContent: text });
    el.after(sr);
    el.setAttribute("aria-hidden", "true");
    if (reduceMotion) return;
    el.textContent = "";
    let i = 0;
    const tick = () => { el.textContent = text.slice(0, ++i); if (i < text.length) setTimeout(tick, 45 + Math.random() * 55); };
    setTimeout(tick, 300);
  });
  // ---- home: scroll reveal, score meters and number counters ----
  const fmt = (el, v) => (el.dataset.prefix || "") + v.toLocaleString() + (el.dataset.suffix || "");
  const countUp = (el) => {
    const to = +el.dataset.countTo, t0 = performance.now(), dur = 1400;
    const step = (t) => { const p = Math.min((t - t0) / dur, 1); el.textContent = fmt(el, Math.round(to * (1 - (1 - p) ** 3))); if (p < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  };
  const reveal = (el) => {
    el.classList.add("hx-in");
    el.querySelectorAll("[data-pct]").forEach((bar) => bar.style.setProperty("--w", bar.dataset.pct + "%"));  // CSSOM, allowed by the CSP
    el.querySelectorAll("[data-count-to]").forEach((n) => (reduceMotion ? (n.textContent = fmt(n, +n.dataset.countTo)) : countUp(n)));
    el.addEventListener("transitionend", () => el.classList.remove("hx-reveal", "hx-in"), { once: true });  // back to the card's own hover transitions
  };
  const io = "IntersectionObserver" in window
    ? new IntersectionObserver((entries) => entries.forEach((e) => { if (e.isIntersecting) { io.unobserve(e.target); reveal(e.target); } }), { threshold: 0.2 })
    : null;
  document.querySelectorAll(".hx-reveal").forEach((el) => (io ? io.observe(el) : reveal(el)));
  // ---- home: looping terminal demo (lines appear one by one, commands type themselves) ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  document.querySelectorAll("[data-demo]").forEach(async (demo) => {
    const lines = [...demo.querySelectorAll(".hx-line")], typed = [...demo.querySelectorAll(".hx-t")];
    typed.forEach((t) => (t.dataset.text = t.textContent));
    if (reduceMotion) return lines.forEach((l) => l.classList.add("hx-in"));
    for (;;) {
      lines.forEach((l) => l.classList.remove("hx-in"));
      typed.forEach((t) => (t.textContent = ""));
      await sleep(400);
      for (const l of lines) {
        l.classList.add("hx-in");
        const t = l.querySelector(".hx-t");
        if (t) { for (let i = 1; i <= t.dataset.text.length; i++) { t.textContent = t.dataset.text.slice(0, i); await sleep(30 + Math.random() * 50); } await sleep(300); }
        else await sleep(450);
      }
      await sleep(5000);
    }
  });
  // ---- login/signup: show/hide password, caps-lock warning, password strength ----
  document.querySelectorAll(".hx-eye").forEach((btn) => btn.addEventListener("click", () => {
    const field = btn.closest(".hx-field"), input = field.querySelector("input"), show = input.type === "password";
    input.type = show ? "text" : "password";
    field.classList.toggle("hx-revealed", show);
    btn.setAttribute("aria-pressed", String(show));
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    input.focus();
  }));
  document.querySelectorAll('input[type="password"]').forEach((input) => {
    const caps = input.form?.querySelector("[data-caps]");
    const meter = input.autocomplete === "new-password" ? input.form?.querySelector("[data-strength]") : null;
    const checkCaps = (e) => { if (caps && e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); };
    input.addEventListener("keydown", checkCaps);
    input.addEventListener("keyup", checkCaps);
    input.addEventListener("blur", () => { if (caps) caps.hidden = true; });
    if (!meter) return;
    input.addEventListener("input", () => {  // rough score: length, mixed case, digits, symbols -> 0..4
      const v = input.value;
      const score = v ? Math.min(4, (v.length >= 8) + (v.length >= 12) + (/[a-z]/.test(v) && /[A-Z]/.test(v)) + /\d/.test(v) + /[^\w\s]/.test(v)) : 0;
      meter.dataset.level = score;
      meter.querySelector(".hx-bar span").style.setProperty("--w", score * 25 + "%");
      meter.querySelector("b").textContent = "entropy: " + ["--", "weak", "fair", "good", "strong"][score];
    });
  });
  // ---- flash messages (toasts) + tips: the x closes one (fades out, then it's removed; the empty container goes too). A toast also closes
  //      itself when its 10-second bar runs out (the bar pauses while hovered); with reduced motion, after a plain 10 s ----
  const closeAlert = (alert) => {
    if (!alert?.isConnected || alert.dataset.closing) return;
    alert.dataset.closing = "1";
    const box = alert.parentElement;
    const done = () => { alert.remove(); if (box && !box.querySelector('[role="alert"]')) box.remove(); };
    if (reduceMotion) return done();
    alert.classList.add("hx-flash-out");
    alert.addEventListener("animationend", (e) => e.target === alert && done());
    setTimeout(done, 400);  // in case the animation never runs (hidden tab etc.)
  };
  document.addEventListener("click", (e) => closeAlert(e.target.closest("[data-flash-close]")?.closest('[role="alert"]')));
  const arm = (t) => {  // a timer, not the bar's animation: it also runs in background tabs
    let left = 10000, since = 0, timer = 0;
    const run = () => { since = Date.now(); timer = setTimeout(() => closeAlert(t), left); };
    const hold = () => { if (timer) { clearTimeout(timer); timer = 0; left -= Date.now() - since; } };  // hovered / focused: paused (the bar pauses too, in CSS)
    t.addEventListener("mouseenter", hold); t.addEventListener("focusin", hold);
    t.addEventListener("mouseleave", () => timer || run()); t.addEventListener("focusout", () => timer || t.matches(":hover") || run());
    run();
  };
  document.querySelectorAll("[data-toast]").forEach(arm);
  const TOAST = { warn: ["alert hx-alert hx-toast hx-flash-warn", "fa-user-clock", "[idle]"], info: ["alert hx-alert hx-toast hx-flash-info", "fa-circle-info", "[info]"],
                  error: ["alert hx-alert hx-toast hx-flash-error", "fa-triangle-exclamation", "[err]"] };  // the same three looks as the server's flashes (base.html)
  const toast = (kind, text, sticky, link) => {  // a toast made here (same look as the server's); sticky = stays until closed or removed; link = {href, label} (our own paths only)
    let box = document.querySelector(".hx-toasts");
    if (!box) { box = document.createElement("div"); box.className = "hx-toasts"; box.setAttribute("aria-live", "polite"); document.body.append(box); }
    const [cls, icon, tag] = TOAST[kind], t = document.createElement("div");
    t.className = cls; t.setAttribute("role", "alert");
    t.innerHTML = `<i class="fa-solid ${icon}"></i><span class="hx-msg"><b></b> </span><button type="button" class="hx-close" data-flash-close aria-label="Close message"><i class="fa-solid fa-xmark"></i></button>`;
    t.querySelector("b").textContent = tag; t.querySelector(".hx-msg").append(text);  // text, never HTML
    if (link) { const a = document.createElement("a"); a.className = "hx-nudge-go"; a.href = link.href; a.textContent = link.label; t.querySelector(".hx-msg").append(" ", a); }
    if (!sticky) { t.dataset.toast = ""; t.insertAdjacentHTML("beforeend", '<span class="hx-toast-bar" aria-hidden="true"></span>'); arm(t); }
    box.append(t);
    return t;
  };
  // ---- a refused form comes back to its page with an [err] toast: what was typed goes back in. Kept in this tab until the next page, and never passwords, account or ID numbers, codes or files ----
  const PRIVATE = /password|account_number|account_confirm|^co_account(_confirm)?$|id_number|(^|_)code$|csrf|token|^confirm$/i;
  const fillable = (el) => el.name && !PRIVATE.test(el.name) && !["file", "password", "submit", "button", "reset"].includes(el.type) && (el.type !== "hidden" || "typed" in el.dataset) && !el.closest("[data-locked]");  // data-locked: shows what's saved, whatever was typed
  function remember(form) {
    if (form.method !== "post") return;
    const data = {}, shown = {};
    for (const el of form.elements) {
      if (!fillable(el) || el.disabled) continue;
      const vals = (data[el.name] ||= []);
      if (el.type === "checkbox" || el.type === "radio") { if (el.checked) vals.push(el.value); }
      else if (el.multiple) vals.push(...[...el.selectedOptions].map((o) => o.value));
      else vals.push(el.value);
      if (el.id && "typed" in el.dataset) shown[el.id] = document.getElementById(el.id + "-shown")?.textContent;  // the place picked on the map: its name, shown beside it
    }
    try { sessionStorage.setItem("hx-typed", JSON.stringify({ page: location.pathname, action: form.action, at: Date.now(), data, shown })); } catch {}
  }
  let typed = null;
  try { typed = JSON.parse(sessionStorage.getItem("hx-typed") || "null"); sessionStorage.removeItem("hx-typed"); } catch {}
  if (typed?.page === location.pathname && Date.now() - typed.at < 120000 && document.querySelector(".hx-flash-error")) {
    const form = [...document.forms].find((f) => f.action === typed.action), used = {};
    for (const el of form?.elements || []) {
      const vals = fillable(el) ? typed.data[el.name] : null;
      if (!vals) continue;
      if (el.type === "checkbox" || el.type === "radio") el.checked = vals.includes(el.value);
      else if (el.multiple) [...el.options].forEach((o) => (o.selected = vals.includes(o.value)));
      else { el.value = vals[used[el.name] || 0] ?? el.value; used[el.name] = (used[el.name] || 0) + 1; }
      const name = typed.shown?.[el.id], label = name && document.getElementById(el.id + "-shown");
      if (label) label.textContent = name;
    }
  }
  // ---- instruction bar: "/" focuses it (unless you're typing somewhere), example chips fill it ----
  const bars = [...document.querySelectorAll("[data-assist]")];
  const bar = () => bars.find((el) => el.offsetParent !== null) || bars[0];  // the visible one (sidebar or dashboard)
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || !bars.length || t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName)) return;
    e.preventDefault();
    bar().focus();
  });
  document.querySelectorAll("[data-fill]").forEach((chip) => chip.addEventListener("click", () => {
    const input = document.querySelector(".hx-assist-main [data-assist]") || bar();
    input.value = chip.dataset.fill;
    input.focus();
    const hole = input.value.indexOf("<code>");  // select the placeholder so pasting replaces it
    if (hole >= 0) input.setSelectionRange(hole, hole + 6);
  }));
  // ---- workplaces: live search / filter / sort of the company cards (cards carry data-name/-text/-tags/-members/-joined) ----
  const wpForm = document.querySelector("[data-wp-filter]"), wpGrid = document.querySelector("[data-wp-grid]");
  if (wpForm && wpGrid) {
    const cards = [...wpGrid.children], count = wpForm.querySelector("[data-wp-count]"), empty = document.querySelector("[data-wp-empty]");
    const by = (key, dir, num) => (a, b) => dir * (num ? a.dataset[key] - b.dataset[key] : a.dataset[key].localeCompare(b.dataset[key]));
    const sorts = { joined_desc: by("joined", -1), joined_asc: by("joined", 1), name_asc: by("name", 1), name_desc: by("name", -1), members_desc: by("members", -1, true), members_asc: by("members", 1, true) };
    const apply = () => {
      const f = Object.fromEntries(new FormData(wpForm)), words = (f.q || "").toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      cards.sort(sorts[f.sort] || sorts.joined_desc).forEach((c) => {
        const ok = words.every((w) => c.dataset.text.includes(w)) && (f.show === "all" || c.dataset.tags.split(" ").includes(f.show));
        c.hidden = !ok; shown += ok; wpGrid.append(c);  // re-append in sorted order
      });
      count.textContent = `${shown} of ${cards.length}`;
      empty.hidden = shown > 0;
    };
    wpForm.addEventListener("input", apply);
    wpForm.addEventListener("submit", (e) => { e.preventDefault(); apply(); });  // Enter just filters, no reload
    apply();
  }
  // ---- MDB modals: /workplaces#c-<id> opens that company's dialog, #edit-<id> its edit dialog (actions + the instruction bar link there) ----
  const openHash = () => {  // on load, and when a link changes the hash on this same page (the reminders link to #c- / #edit-)
    let m = null;  // null, never false: "?." below only short-circuits on null/undefined
    try { m = location.hash.length > 1 ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null; } catch {}  // malformed %-escape
    if (!m?.classList.contains("modal") || !window.mdb) return;
    document.querySelectorAll(".modal.show").forEach((o) => o !== m && mdb.Modal.getInstance(o)?.hide());
    mdb.Modal.getOrCreateInstance(m).show();
  };
  // a form submitted from inside a dialog: the page it lands on (the same one, success or error) opens that dialog again
  document.addEventListener("submit", (e) => {
    const modal = e.target.closest?.(".modal");
    if (modal?.id && !e.target.hasAttribute?.("data-no-reopen")) try { sessionStorage.setItem("hx-reopen", JSON.stringify({ id: modal.id, at: Date.now(), onError: e.target.hasAttribute("data-reopen-on-error") })); } catch {}
  }, true);
  let reopen = null;
  try { reopen = JSON.parse(sessionStorage.getItem("hx-reopen") || "null"); sessionStorage.removeItem("hx-reopen"); } catch {}
  if (reopen?.onError && !document.querySelector(".hx-flash-error")) reopen = null;  // data-reopen-on-error: only when it didn't work (e.g. a new meeting: done = no empty form again)
  const again = reopen && Date.now() - reopen.at < 60000 ? document.getElementById(reopen.id) : null;
  if (again?.classList.contains("modal") && window.mdb) mdb.Modal.getOrCreateInstance(again).show();
  else openHash();
  addEventListener("hashchange", openHash);
  // ---- company location: Geoapify picker ([data-place-picker]). Leaflet (served from /static) loads on first use only, with this page's
  //      CSP nonce. A search result or a click on the map gives a Geoapify place id; only that goes back to the server, which looks the
  //      place up with Geoapify again before saving it (so the page can't fake a location). Text from the API is only set as textContent ----
  const NONCE = document.currentScript?.nonce || "";  // only readable now, while this file first runs
  let leaflet = null;
  const loadLeaflet = (lib) => leaflet ||= new Promise((ok, fail) => {
    document.head.append(Object.assign(document.createElement("link"), { rel: "stylesheet", href: lib + "leaflet.css", nonce: NONCE }));
    document.head.append(Object.assign(document.createElement("script"), { src: lib + "leaflet.js", nonce: NONCE, onload: () => ok(window.L), onerror: () => { leaflet = null; fail(); } }));
  });
  const placeStatus = (box, text) => { const s = box?.querySelector("[data-place-status]"); if (s) s.textContent = text; };
  const usePlace = (box) => {
    if (!box?.dataset.pick) return false;
    document.getElementById(box.dataset.target).value = box.dataset.pick;
    document.getElementById(box.dataset.target + "-shown").textContent = box.dataset.addr;
    document.getElementById(box.dataset.target).closest("form")?.querySelector(".hx-loc")?.classList.remove("hx-loc-missing");
    return true;
  };
  const setupPicker = async (box) => {
    if (!box || box.dataset.ready || !box.dataset.key) return;
    box.dataset.ready = "1";
    let L;
    try { L = await loadLeaflet(box.dataset.lib); }
    catch { box.dataset.ready = ""; return placeStatus(box, "The map couldn't load: reload the page and try again."); }
    const el = box.querySelector("[data-place-map]"), input = box.querySelector("[data-place-q]"), list = box.querySelector("[data-place-results]");
    const start = box.dataset.lat ? [+box.dataset.lat, +box.dataset.lng] : null;
    const map = L.map(el, { center: start || [22, 79], zoom: start ? 16 : 4 });
    const tileUrl = () => `https://maps.geoapify.com/v1/tile/${root.dataset.mdbTheme === "dark" ? "dark-matter" : "osm-bright"}/{z}/{x}/{y}${L.Browser.retina ? "@2x" : ""}.png?apiKey=${encodeURIComponent(box.dataset.key)}`;
    const tiles = L.tileLayer(tileUrl(), { maxZoom: 20, referrerPolicy: "strict-origin-when-cross-origin",  // the origin is what a restricted key is checked against
      attribution: 'Powered by <a href="https://www.geoapify.com/" target="_blank" rel="noopener">Geoapify</a> | © <a href="https://openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors' }).addTo(map);
    map.attributionControl.setPrefix(false);
    new MutationObserver(() => tiles.setUrl(tileUrl())).observe(root, { attributes: true, attributeFilter: ["data-mdb-theme"] });  // follows the theme toggle
    new ResizeObserver(() => map.invalidateSize()).observe(el);  // it was created while its dialog / <details> was still hidden
    const pin = L.marker(start || [0, 0], { keyboard: false, icon: L.divIcon({ className: "hx-place-pin", html: '<i class="fa-solid fa-location-dot"></i>', iconSize: [28, 36], iconAnchor: [14, 34] }) });
    if (start) pin.addTo(map);
    const choose = (p) => {  // p = a Geoapify geocoding result
      box.dataset.pick = p.place_id; box.dataset.addr = p.formatted || "";
      pin.setLatLng([p.lat, p.lon]).addTo(map); map.setView([p.lat, p.lon], Math.max(map.getZoom(), 16));
      list.hidden = true;
      placeStatus(box, "picked: " + box.dataset.addr + ("confirm" in box.dataset ? " · press “use this location”" : " · saved with the company"));
      if (!("confirm" in box.dataset)) usePlace(box);  // inline (edit dialog): fill the form right away
    };
    const failed = (e) => e.name === "AbortError" || placeStatus(box, e.status === 401 || e.status === 403 ? "Geoapify rejected the API key: check it (and its allowed addresses) at myprojects.geoapify.com."
      : e.status === 429 ? "This Geoapify key has used up today's searches: try again later." : "The address search failed: try again.");
    let timer, ctrl, found = [];
    const ask = async (path, params) => {  // Geoapify geocoding (autocomplete / reverse): results with a place id and coordinates; throws the Response on an HTTP error
      ctrl?.abort(); ctrl = new AbortController();
      const r = await fetch(`https://api.geoapify.com/v1/geocode/${path}?${new URLSearchParams({ ...params, format: "json", lang: "en", apiKey: box.dataset.key })}`, { signal: ctrl.signal, credentials: "omit" });
      if (!r.ok) throw r;
      return ((await r.json()).results || []).filter((p) => p.place_id && Number.isFinite(p.lat) && Number.isFinite(p.lon));
    };
    const show = (results) => {
      found = results;
      list.replaceChildren(...results.map((p) => {
        const li = document.createElement("li"), b = document.createElement("button"), main = document.createElement("b"), sub = document.createElement("small");
        b.type = "button"; main.textContent = p.address_line1 || p.formatted; sub.textContent = p.address_line2 || "";
        b.append(main, sub); b.addEventListener("click", () => choose(p)); li.append(b);
        return li;
      }));
      list.hidden = !results.length;
      if (!results.length) placeStatus(box, "nothing found: try another spelling, or click the spot on the map");
    };
    input.addEventListener("input", () => {
      clearTimeout(timer); ctrl?.abort();
      const text = input.value.trim(), c = map.getCenter();
      if (text.length < 3) return (list.hidden = true);
      timer = setTimeout(() => ask("autocomplete", { text, limit: 6, ...(map.getZoom() > 5 && { bias: `proximity:${c.lng},${c.lat}` }) }).then(show, failed), 300);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); if (found[0] && !list.hidden) choose(found[0]); }  // picks the first result instead of submitting the form
      if (e.key === "ArrowDown" && !list.hidden) { e.preventDefault(); list.querySelector("button")?.focus(); }
      if (e.key === "Escape" && !list.hidden) { e.stopPropagation(); list.hidden = true; }  // closes the list, not the dialog
    });
    list.addEventListener("keydown", (e) => {  // arrow keys move through the results
      const items = [...list.querySelectorAll("button")], i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); (items[i + (e.key === "ArrowDown" ? 1 : -1)] || input).focus(); }
      if (e.key === "Escape") { e.stopPropagation(); list.hidden = true; input.focus(); }
    });
    map.on("click", (e) => {  // click a spot: the nearest address Geoapify knows there
      placeStatus(box, "looking up that spot…");
      ask("reverse", { lat: e.latlng.lat.toFixed(6), lon: e.latlng.lng.toFixed(6), limit: 1 })
        .then(([p]) => p ? choose(p) : placeStatus(box, "no address there: click closer to a street or a building"), failed);
    });
  };
  addEventListener("click", (e) => {  // window capture: runs before MDB's delegated handlers (they capture on document), so it can stop them
    const open = e.target.closest("[data-place-open]"), use = e.target.closest("[data-place-use]");
    if (open) setupPicker(document.querySelector(open.dataset.placeOpen));
    if (use && !usePlace(document.querySelector(use.dataset.placeUse))) {  // nothing picked yet: keep the dialog open
      e.preventDefault(); e.stopPropagation();
      placeStatus(document.querySelector(use.dataset.placeUse), "Search for the place and pick it from the list, or click it on the map first.");
    }
  }, true);
  document.querySelectorAll("[data-place-details]").forEach((d) => d.addEventListener("toggle", () => d.open && setupPicker(d.querySelector("[data-place-picker]"))));
  // ---- company form: a position's minimum-salary box is required while the position is ticked, and off while it isn't ----
  document.querySelectorAll("[data-salary-for]").forEach((box) => {
    const tick = box.form?.querySelector(`input[type="checkbox"][name="positions"][value="${box.dataset.salaryFor}"]`);
    if (!tick) return;  // always offered (Rookie) or held by someone: stays required
    const sync = () => { box.disabled = !tick.checked; box.required = tick.checked; };
    tick.addEventListener("change", sync);
    sync();
  });
  // ---- AI review running: poll its status and reload the page once it's done (no reload loop: stops at the first non-running answer) ----
  const aiPoll = document.querySelector("[data-ai-poll]");
  if (aiPoll) {
    const check = async () => {
      try {
        const r = await fetch(aiPoll.dataset.aiPoll, { headers: { Accept: "application/json" }, cache: "no-store" });
        if (r.ok && (await r.json()).status !== "running") return location.reload();
      } catch {}
      setTimeout(check, 5000);
    };
    setTimeout(check, 5000);
  }
  // ---- messages in any chat (the drawer, a chat's page, the Deminic AI): links made clickable (text only, never HTML), a YouTube link shows
  //      its video (privacy mode, nothing loads from YouTube but the thumbnail until it's played), other links a title card (/chats/preview/link,
  //      fetched by the server from public addresses only), pictures + files sent in the chat, and copy / forward on every message ----
  const mk = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = String(text); return x; };
  const fa = (name) => { const i = mk("i", `fa-solid ${name}`); i.setAttribute("aria-hidden", "true"); return i; };
  const profileLink = (id, name, chat) => { const a = mk("a", "hx-chat-link"); a.href = "/people/" + Number(id) + (chat ? "?chat=" + Number(chat) : ""); a.title = "Open their profile"; a.append(name); return a; };  // a name in a chat (routers/people.py decides what you may see)
  const KIND_ICON = { group: "fa-users", pgroup: "fa-people-group", direct: "fa-user-tie", personal: "fa-user", enquiry: "fa-envelope-open-text", ai: "fa-robot" };
  const csrfNow = () => document.querySelector("input[name=csrf_token]")?.value || "";
  const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/g;
  const ytId = (u) => {
    try {
      const x = new URL(u), id = x.hostname.replace(/^www\.|^m\./, "") === "youtu.be" ? x.pathname.slice(1) : /(^|\.)youtube\.com$/.test(x.hostname) ? x.searchParams.get("v") || (x.pathname.match(/^\/(?:shorts|embed|live)\/([\w-]+)/) || [])[1] : null;
      return /^[\w-]{11}$/.test(id || "") ? id : null;
    } catch { return null; }
  };
  const linkify = (p) => {  // the text's web addresses become links (rebuilt from the text: nothing in it is ever read as HTML)
    const text = p.textContent, urls = [], frag = document.createDocumentFragment();
    let at = 0, m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text))) {
      const url = m[0].replace(/[.,!?;:)\]]+$/, "");
      frag.append(text.slice(at, m.index));
      const a = mk("a", "", url); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; frag.append(a);
      at = m.index + url.length; urls.push(url);
    }
    if (urls.length) { frag.append(text.slice(at)); p.replaceChildren(frag); }
    return urls;
  };
  const linkCard = (li, url) => {
    const id = ytId(url);
    if (id) {  // a YouTube link: its thumbnail; the player (youtube-nocookie) only once it's clicked
      const card = mk("button", "hx-yt"); card.type = "button"; card.setAttribute("aria-label", "Play the YouTube video here");
      const img = mk("img"); img.src = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`; img.alt = ""; img.loading = "lazy";
      card.append(img, fa("fa-circle-play"), mk("small", "", "YouTube · play here"));
      card.addEventListener("click", () => {
        const f = document.createElement("iframe");
        f.src = `https://www.youtube-nocookie.com/embed/${id}?autoplay=1&rel=0`; f.title = "YouTube video"; f.className = "hx-yt-frame";
        f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen"; f.allowFullscreen = true; f.referrerPolicy = "strict-origin-when-cross-origin";
        card.replaceWith(f);
      });
      li.append(card);
      return;
    }
    fetch(`/chats/preview/link?url=${encodeURIComponent(url)}`, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null)).then((d) => {
        if (!d?.title) return;
        const a = mk("a", "hx-linkcard"); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow";
        a.append(mk("small", "", d.site), mk("b", "", d.title)); if (d.description) a.append(mk("span", "", d.description));
        li.append(a);
      }).catch(() => {});
  };
  const size = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const fileBox = (f) => {  // a picture shows; anything else is a download
    if (f.image) { const a = mk("a", "hx-chat-img"); a.href = f.url; a.target = "_blank"; a.rel = "noopener"; const img = mk("img"); img.src = f.url; img.alt = f.name; img.loading = "lazy"; a.append(img); return a; }
    const a = mk("a", "hx-chat-file"); a.href = f.url; a.setAttribute("download", ""); a.append(fa("fa-file-arrow-down"), mk("span", "", f.name), mk("small", "", size(f.size)));
    return a;
  };
  const forwardTo = async (kind, id) => {  // a small dialog: your chats; pick one and the message goes there ("forwarded from …")
    let chats = [];
    try { chats = (await (await fetch("/chats/drawer/list", { headers: { Accept: "application/json" }, credentials: "same-origin" })).json()).chats || []; } catch {}
    const shade = mk("div", "hx-fwd-shade"), box = mk("div", "hx-fwd"), list = mk("ul", "hx-fwd-list"), close = () => { shade.remove(); box.remove(); };
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true"); box.setAttribute("aria-label", "Forward the message to");
    chats.forEach((c) => {
      const b = mk("button", "hx-fwd-pick"), li = mk("li");
      b.type = "button"; b.append(fa(KIND_ICON[c.kind] || "fa-user"), mk("span", "", c.title), ...(c.company ? [mk("small", "", `company chat · ${c.company}`)] : []));
      b.addEventListener("click", async () => {
        const fd = new FormData(); fd.append("csrf_token", csrfNow()); fd.append(kind === "ai" ? "ai_id" : "message_id", String(id));
        b.disabled = true;
        try {
          const r = await fetch(`/chats/${c.id}/forward`, { method: "POST", body: fd, credentials: "same-origin", headers: { Accept: "application/json", "X-Requested-With": "fetch" } });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Couldn't forward it.");
          close();
          const t = toast("info", `Forwarded to ${c.title}.`, false, { href: `/chats/${c.id}#end`, label: "open it →" });
          if (document.querySelector("[data-drawer]")) t.querySelector(".hx-nudge-go").dataset.chatOpen = String(c.id);
        } catch (e) { toast("error", e.message); b.disabled = false; }
      });
      li.append(b); list.append(li);
    });
    if (!chats.length) list.append(mk("li", "hx-dw-none", "no chats to forward to yet"));
    const cancel = mk("button", "hx-btn hx-btn-sm hx-btn-ghost", "cancel"); cancel.type = "button"; cancel.addEventListener("click", close);
    shade.addEventListener("click", close);
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
    box.append(mk("p", "hx-fwd-head", "forward to…"), list, cancel);
    document.body.append(shade, box);
    (box.querySelector(".hx-fwd-pick") || cancel).focus();
  };
  const msgActions = (kind, id, text) => {  // copy (copyText, below: the clipboard API or the plain-http fallback), on every message; forward too where it can go
    const bar = mk("span", "hx-msg-acts"), copy = mk("button", "hx-msg-act");
    copy.type = "button"; copy.title = "copy"; copy.setAttribute("aria-label", "Copy this message"); copy.append(fa("fa-copy"));
    copy.addEventListener("click", async () => (await copyText(text)) ? toast("info", "Copied.") : toast("error", "Couldn't copy: select the text and copy it by hand."));
    bar.append(copy);
    if (id) {
      const fwd = mk("button", "hx-msg-act"); fwd.type = "button"; fwd.title = "forward"; fwd.setAttribute("aria-label", "Forward this message"); fwd.append(fa("fa-share"));
      fwd.addEventListener("click", () => forwardTo(kind, id)); bar.append(fwd);
    }
    return bar;
  };
  const finish = (li, kind, id, text) => {  // links + their previews + copy / forward (messages the server rendered get it on load)
    const p = li.querySelector("[data-msg-text]");
    if (p) linkify(p).slice(0, 2).forEach((u) => linkCard(li, u));
    li.append(msgActions(kind, id, text || ""));
  };
  const dress = (li, x, kind) => {  // a message's body from JSON: where it came from, its file, its text
    if (x.fwd) { const f = mk("small", "hx-chat-fwd"); f.append(fa("fa-share"), ` forwarded from ${x.fwd}`); li.append(f); }
    if (x.file) li.append(fileBox(x.file));
    if (x.text) { const p = mk("p", "", x.text); p.dataset.msgText = ""; li.append(p); }
    finish(li, kind, x.id, x.text || x.file?.name || "");
  };
  document.querySelectorAll("[data-chat] [data-msg]").forEach((li) => finish(li, "chat", li.dataset.msg, li.querySelector("[data-msg-text]")?.textContent || ""));  // a chat's page
  const sendFiles = async (chat, files, caption) => {  // files into a chat (the drawer, a chat's page): one message each, virus-scanned on the server, 5 at a time; true if any went
    const all = [...(files || [])];
    if (all.length > 5) toast("info", "5 files at a time: sending the first 5.");
    let sent = 0;
    for (const [i, f] of all.slice(0, 5).entries()) {
      const fd = new FormData(); fd.append("csrf_token", csrfNow()); fd.append("file", f, f.name || "pasted.png"); fd.append("body", i ? "" : caption);
      toast("info", `Sending ${f.name || "the picture"}…`);
      try {
        const r = await fetch(`/chats/${encodeURIComponent(chat)}/files`, { method: "POST", body: fd, credentials: "same-origin", headers: { Accept: "application/json", "X-Requested-With": "fetch" } });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "it couldn't be sent.");
        sent++;
      } catch (e) { toast("error", `${f.name || "That file"}: ${e.message}`); break; }
    }
    return sent > 0;
  };
  const dropFiles = (zone, ok, send) => {  // drag files onto a chat to send them (ok(): a chat you can write in is there); the innermost zone takes them
    let depth = 0;
    const has = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    zone.addEventListener("dragenter", (e) => { if (ok() && has(e)) { depth++; zone.classList.add("hx-drop"); } });
    zone.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; zone.classList.remove("hx-drop"); } });
    zone.addEventListener("dragover", (e) => { if (ok() && has(e)) e.preventDefault(); });
    zone.addEventListener("drop", (e) => {
      depth = 0; zone.classList.remove("hx-drop");
      if (e.defaultPrevented || !ok() || !e.dataTransfer?.files.length) return;
      e.preventDefault(); send([...e.dataTransfer.files]);
    });
  };
  const runCommand = (q) => {  // a Deminic AI answer's button: the command, through the instruction bar like typing it
    const f = mk("form"); f.method = "post"; f.action = "/assist"; f.hidden = true;
    [["csrf_token", csrfNow()], ["q", q]].forEach(([k, v]) => { const i = mk("input"); i.type = "hidden"; i.name = k; i.value = v; f.append(i); });
    document.body.append(f); f.submit();
  };
  // ---- the chat drawer (base.html [data-drawer], the button bottom right): the Deminic AI (pinned, private) + every chat of yours, newest first.
  //      Company chats are labelled "company chat · <company>" (its owner, co-owners, managers and HR can read them); personal ones aren't. A chat
  //      opens in place, messages + files go over fetch, and the presence socket's nudges ({"t": "chat"}, {"t": "ai"}) bring new ones in live ----
  const drawer = (() => {
    const box = document.querySelector("[data-drawer]"), fab = document.querySelector("[data-drawer-open]"), shade = document.querySelector("[data-drawer-shade]");
    if (!box || !fab || !shade) return null;
    const $ = (s) => box.querySelector(s), list = $("[data-dw-chats]"), people = $("[data-dw-people]"), msgs = $("[data-dw-msgs]"), form = $("[data-dw-form]"), input = $("[data-dw-input]"), filter = $("[data-dw-filter]");
    const aiRow = $("[data-dw-ai]"), attach = $("[data-dw-file]"), clear = $("[data-dw-clear]"), off = $("[data-dw-off]"), webBtn = $("[data-dw-web]"), APP = box.dataset.app || "Deminic";
    const sendBtn = form.querySelector("button[type=submit], button:not([type])");
    let aiPoll = 0, aiBusy = false, webOn = false, aiPic = null;  // aiPoll: while an answer is being written it's checked every few seconds too, in case a nudge is missed; aiBusy: it's answering; webOn: the web toggle; aiPic: a picture to show it
    const pending = $("[data-dw-pending]");
    const csrf = form.querySelector("[name=csrf_token]")?.value || "";
    let data = { chats: [], contacts: [], unread: 0 }, tab = "all", open = 0, last = 0, back = null, hiding = 0, reload = 0;  // open: a chat id, or "ai"
    const el = mk, icon = fa;
    const realName = (id, name, ok) => { const s = el("small", "hx-chat-real", name ? name + (ok ? " ✓" : "") : ""); s.dataset.chatReal = String(id); s.title = "name on their identity proof"; return s; };
    const user = (id, name) => { const b = el("b", "hx-dw-name", name); if (id) b.dataset.chatUser = String(id); return b; };  // kept live by {"t": "name"}
    const call = async (url, opts = {}) => {  // JSON both ways; a refusal's own words come back as the error
      const r = await fetch(url, { credentials: "same-origin", ...opts, headers: { Accept: "application/json", "X-Requested-With": "fetch" } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.status === 404 ? "That chat isn't there any more." : "That didn't work: try again in a moment.");
      return d;
    };
    const post = (url, fields) => { const fd = new FormData(); fd.append("csrf_token", csrf); Object.entries(fields).forEach(([k, v]) => fd.append(k, v)); return call(url, { method: "POST", body: fd }); };
    const setCount = (n) => {
      document.querySelectorAll("[data-chat-count]").forEach((b) => { b.textContent = String(n); b.hidden = !n; });
      fab.setAttribute("aria-label", n ? `Chats (${n} unread)` : "Chats");
    };
    const ping = () => { fab.classList.remove("hx-ping"); void fab.offsetWidth; fab.classList.add("hx-ping"); };  // restarts the ring animation
    // the list: one row per chat (company chats: the label first, then who), then the people you can start a chat with
    const row = (c, i) => {
      const li = el("li"), b = el("button", c.id === open ? "hx-dw-chat hx-active" : "hx-dw-chat");
      b.type = "button"; b.dataset.dwChat = String(c.id); li.style.animationDelay = `${Math.min(i, 14) * 22}ms`;
      if (c.unread) b.classList.add("hx-dw-unread");
      const ic = el("span", "hx-chat-ic"), main = el("span", "hx-dw-main"), top = el("span", "hx-dw-top"), meta = el("span", "hx-dw-meta");
      ic.setAttribute("aria-hidden", "true"); ic.append(icon(KIND_ICON[c.kind] || "fa-user"));
      if (c.company) { const t = el("span", "hx-dw-tag"); t.append(icon(c.kind === "enquiry" ? "fa-envelope-open-text" : "fa-building"), c.kind === "enquiry" ? ` enquiry · ${c.company}` : ` company chat · ${c.company}`); main.append(t); }
      top.append(user(c.other, c.title));
      if (c.other) top.append(" ", realName(c.other, c.real, c.verified));
      if (c.kind === "group" || c.kind === "pgroup") top.append(el("small", "hx-dw-count", ` · ${c.members} people`));
      main.append(top, el("small", "hx-dw-last", c.last ? `${c.last.who}: ${c.last.text}` : "no messages yet"));
      if (c.last) meta.append(el("small", "", c.last.at));
      if (c.unread) meta.append(el("b", "hx-new", `${c.unread} new`));
      b.append(ic, main, meta); li.append(b);
      return li;
    };
    const person = (p, i) => {
      const li = el("li", "hx-dw-person"), who = el("span", "hx-dw-main"), top = el("span", "hx-dw-top"), acts = el("span", "hx-dw-acts");
      li.style.animationDelay = `${Math.min(i, 14) * 22}ms`;
      top.append(profileLink(p.user, user(p.user, p.name)), " ", realName(p.user, p.real, p.verified));
      if (p.friend) top.append(" ", el("small", "hx-dw-friend", "friend"));
      who.append(top);
      const start = (kind, label, ic, title, company) => {
        const b = el("button", kind === "personal" ? "hx-btn hx-btn-sm" : "hx-btn hx-btn-sm hx-btn-ghost");
        b.type = "button"; b.dataset.dwStart = kind; b.dataset.user = String(p.user); b.title = title;
        if (company) b.dataset.company = company;
        b.append(icon(ic), el("span", "", label)); acts.append(b);
      };
      start("personal", "message", "fa-user-lock", "a personal chat: only the two of you can read it");
      p.companies.forEach((co) => start("direct", `via ${co.name}`, "fa-building", `a company chat in ${co.name}: its owner, co-owners, managers and HR can read it`, co.id));
      li.append(who, acts);
      return li;
    };
    const paint = () => {
      const q = filter.value.trim().toLowerCase(), has = (...s) => !q || s.some((x) => x && String(x).toLowerCase().includes(q));
      const chats = data.chats.filter((c) => (tab === "all" || (tab === "company") === Boolean(c.company)) && has(c.title, c.real, c.company, c.last?.text));
      list.replaceChildren(...(chats.length ? chats.map(row) : [el("li", "hx-dw-none", data.chats.length ? "no chats match that" : "no chats yet: start one below")]));
      const folks = data.contacts.filter((p) => has(p.name, p.real, ...p.companies.map((x) => x.name)));
      people.replaceChildren(...(folks.length ? folks.map(person) : [el("li", "hx-dw-none", data.contacts.length ? "nobody matches that" : "add friends, or join a company, to chat with people")]));
      aiRow.hidden = tab === "company" || (q && !has(`${APP} AI`, "ai", "assistant"));
    };
    const load = async () => {
      try { data = await call("/chats/drawer/list"); setCount(data.unread); paint(); }
      catch (e) { list.replaceChildren(el("li", "hx-dw-none", e.message)); }
    };
    const soon = () => { clearTimeout(reload); reload = setTimeout(load, 300); };
    // one chat: its newest messages, who can read it, the compose box (+ a file)
    const add = (items) => {
      items.forEach((x) => {
        if (x.id <= last) return;
        msgs.querySelector("[data-dw-empty]")?.remove();
        const li = el("li", x.mine ? "hx-chat-msg hx-chat-mine" : "hx-chat-msg"), who = el("span", "hx-chat-who");
        who.append(x.mine ? el("b", "", "you") : x.user ? profileLink(x.user, user(x.user, x.who), open) : user(x.user, x.who));
        if (!x.mine && x.user) who.append(" ", realName(x.user, x.real, x.verified));
        who.append(el("time", "", x.at)); li.append(who); dress(li, x, "chat"); msgs.append(li);
        last = x.id;
      });
      msgs.scrollTop = msgs.scrollHeight;
    };
    const pane = (on) => {  // the conversation side shows; the list's active row follows
      box.classList.add("hx-dw-talking");  // phones: the conversation takes the whole drawer
      $("[data-dw-none]").hidden = true; $("[data-dw-open]").hidden = false;
      list.querySelectorAll("[data-dw-chat]").forEach((b) => b.classList.toggle("hx-active", b.dataset.dwChat === String(on)));
      aiRow.classList.toggle("hx-active", on === "ai");
      box.classList.toggle("hx-dw-is-ai", on === "ai");
      attach.closest("label").hidden = clear.hidden = false; off.hidden = true; webBtn.hidden = on !== "ai"; unstage();
      attach.accept = on === "ai" ? "image/*" : ""; attach.multiple = on !== "ai";  // the AI: one picture to look at; a chat: up to 5 files of any kind
      attach.closest("label").title = on === "ai" ? `show ${APP} a picture: it looks at it with your question (or drop / paste one)` : "send files or pictures, up to 5 at once (virus-scanned; what's typed goes with the first). You can also drop them here or paste a screenshot";
    };
    const unstage = () => { aiPic = null; pending.hidden = true; pending.replaceChildren(); };
    const stage = (f) => {  // the AI chat: a picture waits above the box and goes with the next question
      if (!f || !["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"].includes(f.type)) { toast("error", `${APP} can look at pictures (PNG, JPEG, WebP or GIF): that file isn't one.`); return; }
      aiPic = f;
      const r = new FileReader();
      r.onload = () => {
        const img = el("img"), x = el("button", "hx-icon-btn"); img.src = String(r.result); img.alt = "";  // a data: URL (the CSP allows those for pictures)
        x.type = "button"; x.title = "don't send it"; x.setAttribute("aria-label", "Remove the picture"); x.append(icon("fa-xmark")); x.addEventListener("click", unstage);
        pending.replaceChildren(img, el("span", "", `${f.name || "picture"}: goes with your question`), x); pending.hidden = false;
      };
      r.readAsDataURL(f);
      input.focus({ preventScroll: true });
    };
    const openChat = async (id) => {
      open = Number(id); last = 0;
      pane(open); clear.hidden = true; $("[data-dw-full]").hidden = false;
      msgs.replaceChildren(el("li", "hx-chat-none", "loading…"));
      try {
        const d = await call(`/chats/${open}/recent`), c = d.chat;
        if (c.id !== open) return;  // another chat was picked meanwhile
        const title = $("[data-dw-title]");
        title.replaceChildren(c.other ? profileLink(c.other, user(c.other, c.title), c.id) : user(c.other, c.title));
        if (c.other) title.append(" ", realName(c.other, c.real, false));
        $("[data-dw-sub]").textContent = c.kind === "enquiry" ? `enquiry · ${c.company} · ${c.people.join(", ")}` : c.company ? `company chat · ${c.company} · ${c.kind === "group" ? `group of ${c.people.length}` : "one to one"}` : c.kind === "pgroup" ? `personal group · ${c.people.join(", ")}` : "personal chat · one to one";
        $("[data-dw-icon]").className = `fa-solid ${KIND_ICON[c.kind] || "fa-user"}`;
        $("[data-dw-readers]").textContent = `Who can read this: ${c.readers}.`;
        $("[data-dw-eye]").className = `fa-solid ${c.company ? "fa-eye" : "fa-user-lock"}`;
        box.classList.toggle("hx-dw-personal", !c.company);
        $("[data-dw-full]").href = `/chats/${c.id}#end`;
        form.hidden = !c.inside; $("[data-dw-ro]").hidden = c.inside; lock();
        msgs.replaceChildren(); add(d.messages);
        if (!d.messages.length) { const none = el("li", "hx-chat-none", "no messages yet: say hello"); none.dataset.dwEmpty = ""; msgs.append(none); }
        const r = data.chats.find((x) => x.id === open);
        if (r?.unread) { data.unread = Math.max(0, data.unread - r.unread); r.unread = 0; setCount(data.unread); paint(); }
        if (c.inside && matchMedia("(pointer: fine)").matches) input.focus({ preventScroll: true });
      } catch (e) { msgs.replaceChildren(el("li", "hx-chat-none", e.message)); }
    };
    // the Deminic AI: private (only you), the local model; it remembers this conversation, can turn what you say into a command, and searches
    // the web when asked to (the globe toggle, "search…") or when a question needs fresh facts: such an answer carries a small info label + its sources
    const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
    const webLabel = (w) => {  // "searched the web for …" (or why it couldn't): small, above the answer
      const s = el("small", w.error ? "hx-ai-web hx-ai-web-off" : "hx-ai-web"); s.dataset.aiWeb = "";
      s.append(icon(w.error ? "fa-circle-info" : "fa-globe"), w.error ? ` couldn't search the web for “${w.query}” (${w.error}): answered from what it knows` : ` searched the web for “${w.query}”${w.engine ? ` · ${w.engine}` : ""}`);
      return s;
    };
    const sourceList = (list) => {  // the results it read, numbered like its [1] [2]: links out (http(s) only)
      const ol = el("ol", "hx-ai-srcs"); ol.setAttribute("aria-label", "Sources");
      list.filter((s) => s.url.startsWith("https:") || s.url.startsWith("http:")).forEach((s, i) => { const a = el("a", "", `[${i + 1}] ${host(s.url)}`); a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer nofollow"; a.title = s.title; const li = el("li"); li.append(a); ol.append(li); });  // (no regex: rjsmin cut one with "//" in it)
      return ol;
    };
    const searching = (p, query) => { p.className = "hx-ai-think"; p.dataset.searching = "1"; p.replaceChildren(icon("fa-globe fa-spin"), ` searching the web for “${query}”…`); };
    const aiMessage = (x) => {
      const li = el("li", x.role === "me" ? "hx-chat-msg hx-chat-mine" : "hx-chat-msg hx-ai-msg"), who = el("span", "hx-chat-who");
      who.append(el("b", "", x.role === "me" ? "you" : `${APP} AI`), el("time", "", x.at)); li.append(who);
      if (x.model) who.append(el("small", "hx-ai-model", x.model));  // which local model wrote it
      if (x.image) { const a = el("a", "hx-chat-img"), img = el("img"); a.href = x.image; a.target = "_blank"; a.rel = "noopener"; img.src = x.image; img.alt = "the picture you showed it"; img.loading = "lazy"; a.append(img); li.append(a); }
      if (x.status === "thinking") {  // stream mode: what it has written so far + a cursor ({"t": "ai-part"} keeps it growing); nothing yet: a spinner (or "searching the web…")
        const p = el("p", x.text ? "hx-ai-stream" : "hx-ai-think", x.text || null); li.dataset.aiId = String(x.id); p.dataset.aiStream = String(x.text.length);
        if (x.web?.query && x.text) li.append(webLabel({ query: x.web.query }));
        if (!x.text) x.web?.searching ? searching(p, x.web.query) : p.append(icon("fa-spinner fa-spin"), " thinking… (the local AI, on this server)");
        li.append(p); return li;
      }
      if (x.status === "failed") { li.append(el("p", "hx-ai-think", "I couldn't answer just now: ask again in a moment.")); return li; }
      if (x.web) li.append(webLabel(x.web));
      dress(li, { id: x.role === "ai" ? x.id : 0, text: x.text }, "ai");
      const acts = li.querySelector(".hx-msg-acts");
      if (x.web?.sources?.length) li.insertBefore(sourceList(x.web.sources), acts);
      if (x.command) {
        const b = el("button", "hx-btn hx-btn-sm hx-ai-run"); b.type = "button"; b.title = "do it: the same as typing it in the bar";
        b.append(icon("fa-play"), el("span", "", x.command)); b.addEventListener("click", () => runCommand(x.command));
        li.insertBefore(b, acts);
      }
      return li;
    };
    const streamTo = (id, text, web) => {  // stream mode: the reply so far, in place (it only ever grows: a piece arriving late is dropped); web: the search words
      const li = msgs.querySelector(`[data-ai-id="${CSS.escape(String(id))}"]`), p = li?.querySelector("[data-ai-stream]");
      if (!p) return;
      if (!text) { if (web && !p.dataset.searching) searching(p, web); return; }
      if (text.length <= Number(p.dataset.aiStream)) return;
      if (web && !li.querySelector("[data-ai-web]")) li.insertBefore(webLabel({ query: web }), p);  // answered from the web: labelled from the first word
      const end = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;  // following along at the bottom: stay there
      p.className = "hx-ai-stream"; p.textContent = text; p.dataset.aiStream = String(text.length); delete p.dataset.searching;
      if (end) msgs.scrollTop = msgs.scrollHeight;
    };
    const lock = () => {  // the AI answers one question at a time: its send button waits meanwhile (typing the next one is fine)
      const ai = open === "ai";
      sendBtn.disabled = ai && aiBusy;
      input.placeholder = !ai ? "message… (Enter sends, Shift+Enter: new line; drop or paste files)" : aiBusy ? `${APP} is answering… (one question at a time)` : webOn ? `ask ${APP}: it searches the web… (Enter sends)` : `ask ${APP}… (Enter sends)`;
    };
    webBtn.addEventListener("click", () => {  // the web toggle: on = every question is searched on the web first (stays on until switched off)
      webOn = !webOn; webBtn.setAttribute("aria-pressed", String(webOn)); lock();
      toast("info", webOn ? `${APP} searches the web for your questions now (only the search words go to DuckDuckGo).` : `Web search off: ${APP} answers from what it knows (it still searches when you ask it to).`);
      input.focus({ preventScroll: true });
    });
    const loadAI = async () => {
      try {
        const d = await call("/deminic/messages");
        if (open !== "ai") return;
        aiBusy = d.messages.some((m) => m.status === "thinking"); lock();
        msgs.replaceChildren(...d.messages.map(aiMessage));
        if (!d.messages.length) {
          const hi = el("li", "hx-chat-none"); hi.dataset.dwEmpty = "";
          hi.append(icon("fa-robot"), ` Ask ${APP} anything about the app, your companies or what to do next. It's private: only you see this chat. Try: ${d.examples.map((e) => `“${e}”`).join(", ")}`);
          msgs.append(hi);
        }
        form.hidden = !d.ready; $("[data-dw-ro]").hidden = true; off.hidden = d.ready;
        off.querySelector("span").textContent = `The local AI is offline right now (${d.detail || "not running"}): try again in a moment.`;
        msgs.scrollTop = msgs.scrollHeight;
        clearTimeout(aiPoll);
        if (d.messages.some((m) => m.status === "thinking")) aiPoll = setTimeout(() => { if (open === "ai" && !box.hidden) loadAI(); }, 5000);
      } catch (e) { msgs.replaceChildren(el("li", "hx-chat-none", e.message)); }
    };
    const openAI = async () => {
      open = "ai"; last = 0;
      pane("ai"); $("[data-dw-full]").hidden = true;
      $("[data-dw-title]").replaceChildren(el("b", "hx-dw-name", `${APP} AI`), " ", el("small", "hx-dw-friend", "private"));
      $("[data-dw-sub]").textContent = "the local AI on this server · it remembers this conversation";
      $("[data-dw-icon]").className = "fa-solid fa-robot";
      $("[data-dw-readers]").textContent = "Who can read this: only you. No company can, and it's never in a change log. A web search sends only its search words (to DuckDuckGo).";
      $("[data-dw-eye]").className = "fa-solid fa-user-lock";
      box.classList.add("hx-dw-personal");
      lock();
      msgs.replaceChildren(el("li", "hx-chat-none", "loading…"));
      await loadAI();
      if (!form.hidden && matchMedia("(pointer: fine)").matches) input.focus({ preventScroll: true });
    };
    const pull = async () => { if (open === "ai") return loadAI(); if (!open) return; try { add((await call(`/chats/${open}/since/${last}`)).messages || []); } catch {} };
    const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; };  // CSSOM, allowed by the CSP
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!open || (!input.value.trim() && !(open === "ai" && aiPic))) return;
      if (open === "ai" && aiBusy) { toast("info", `${APP} is still answering: one question at a time.`); return; }
      sendBtn.disabled = true;
      try {
        if (open === "ai") {  // + the web toggle, + the picture waiting above the box
          const fd = new FormData(); fd.append("csrf_token", csrf); fd.append("text", input.value.trim()); fd.append("web", webOn ? "1" : "0");
          if (aiPic) fd.append("image", aiPic, aiPic.name || "picture.png");
          await call("/deminic/messages", { method: "POST", body: fd }); unstage();
        } else await call(`/chats/${open}/messages`, { method: "POST", body: new FormData(form) });
        input.value = ""; grow(); await pull(); if (open !== "ai") soon();
      } catch (err) { toast("error", err.message); }
      finally { lock(); input.focus({ preventScroll: true }); }
    });
    const writable = () => open && !form.hidden;  // a chat you can write in is open (any kind: company or personal, one to one or a group; or the AI)
    const sendHere = async (files) => {  // files into the open chat (picked, dropped, pasted), what's typed going with the first; the AI: a picture waits for the question
      if (!writable()) return;
      if (open === "ai") return stage(files[0]);
      if (await sendFiles(open, files, input.value.trim())) { input.value = ""; grow(); await pull(); soon(); }
    };
    attach.addEventListener("change", () => { const f = [...attach.files]; attach.value = ""; sendHere(f); });
    dropFiles($("[data-dw-open]"), writable, sendHere);
    input.addEventListener("paste", (e) => { const f = [...(e.clipboardData?.files || [])]; if (f.length && writable()) { e.preventDefault(); sendHere(f); } });  // a pasted screenshot
    clear.addEventListener("click", async () => {
      if (!confirm(`Clear your whole conversation with ${APP} AI? It forgets it too.`)) return;
      try { await post("/deminic/clear", {}); await loadAI(); } catch (err) { toast("error", err.message); }
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    input.addEventListener("input", grow);
    list.addEventListener("click", (e) => { const b = e.target.closest("[data-dw-chat]"); if (b) openChat(b.dataset.dwChat); });
    aiRow.addEventListener("click", openAI);
    people.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-dw-start]");
      if (!b) return;
      b.disabled = true;
      try {
        const personal = b.dataset.dwStart === "personal", d = await post(personal ? "/chats/personal" : "/chats/direct", personal ? { user_id: b.dataset.user } : { user_id: b.dataset.user, company_id: b.dataset.company });
        filter.value = ""; await load(); openChat(d.id);
      } catch (err) { toast("error", err.message); } finally { b.disabled = false; }
    });
    filter.addEventListener("input", paint);
    box.querySelectorAll("[data-dw-tab]").forEach((b) => b.addEventListener("click", () => {
      tab = b.dataset.dwTab; box.querySelectorAll("[data-dw-tab]").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); paint();
    }));
    $("[data-dw-back]").addEventListener("click", () => { box.classList.remove("hx-dw-talking"); (list.querySelector(".hx-active") || aiRow).focus(); });
    // open / close: a dialog (focus kept inside, Esc closes, focus goes back where it was)
    const isOpen = () => !box.hidden;
    const show = async () => {
      clearTimeout(hiding); back = document.activeElement;
      box.hidden = false; shade.hidden = false; void box.offsetWidth;  // laid out first, so the slide-in runs
      box.classList.add("hx-drawer-in"); document.body.classList.add("hx-drawer-on"); fab.setAttribute("aria-expanded", "true");
      filter.focus({ preventScroll: true });
      await load();
    };
    const hide = () => {
      box.classList.remove("hx-drawer-in"); document.body.classList.remove("hx-drawer-on"); fab.setAttribute("aria-expanded", "false");
      hiding = setTimeout(() => { box.hidden = true; shade.hidden = true; }, reduceMotion ? 0 : 380);
      (back?.isConnected && back !== document.body ? back : fab).focus({ preventScroll: true });
    };
    fab.addEventListener("click", () => (isOpen() ? hide() : show()));
    shade.addEventListener("click", hide);
    box.querySelectorAll("[data-drawer-close]").forEach((b) => b.addEventListener("click", hide));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); hide(); return; }
      if (e.key !== "Tab") return;
      const f = [...box.querySelectorAll('button:not([disabled]), a[href], input, textarea, [tabindex="0"]')].filter((x) => x.offsetParent !== null);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    });
    document.addEventListener("click", (e) => {  // a "read it" link in a toast opens that chat (or the AI) here (capture: before the page-leave handler)
      const a = e.target.closest("[data-chat-open], [data-ai-open]");
      if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault(); closeAlert(a.closest('[role="alert"]'));
      (isOpen() ? Promise.resolve() : show()).then(() => (a.hasAttribute("data-ai-open") ? openAI() : openChat(a.dataset.chatOpen)));
    }, true);
    document.addEventListener("hx-live", (e) => {  // stream mode: the reply so far (its chat open); the AI answered: shown if its chat is open, else a toast
      if (e.detail?.t === "ai-part") { if (isOpen() && open === "ai") streamTo(e.detail.id, String(e.detail.text || ""), String(e.detail.web || "")); return; }
      if (e.detail?.t !== "ai") return;
      if (isOpen() && open === "ai") return loadAI();
      ping();
      const t = toast("info", `${APP} AI answered you.`, false, { href: "/dashboard", label: "read it →" });
      t.querySelector(".hx-nudge-go").dataset.aiOpen = "";
    });
    if (document.body.hasAttribute("data-open-ai")) show().then(openAI);  // the instruction bar handed it something: its chat opens here
    return {
      shown: isOpen,
      take: (m) => {  // a nudge from the socket: true = shown here (the drawer is open), so no toast
        if (!isOpen()) { ping(); return false; }
        if (Number(m.chat) === open) pull().then(soon); else soon();
        return true;
      },
    };
  })();
  // ---- presence: one WebSocket per tab (/ws/presence, presence.py). "active" = this tab is visible and was used in the last 5 minutes
  //      (only that yes/no is sent: nothing typed or viewed). The server counts attendance from it (working hours only) and pushes
  //      everyone's status to company dashboards ([data-watch-company] + [data-presence-user] dots) ----
  const wsPath = body.dataset.presence;
  if (wsPath && "WebSocket" in window) {
    const IDLE_MS = 5 * 60 * 1000, says = { active: "online", idle: "idle", offline: "offline", untracked: "not monitored" };
    const watch = document.querySelector("[data-watch-company]")?.dataset.watchCompany;
    let sock = null, wait = 1000, lastUse = Date.now(), sent = null, moved = 0, osActive = null;  // osActive: the whole computer's state (null = not known)
    const tabActive = () => document.visibilityState === "visible" && Date.now() - lastUse < IDLE_MS;
    const active = () => tabActive() || osActive === true;  // in use here, or (with permission) anywhere on the computer: coding in VS Code counts
    const send = (msg) => sock?.readyState === WebSocket.OPEN && sock.send(JSON.stringify(msg));
    // idle in working hours where attendance is tracked (the server says so: never owners, co-owners or freelancers): a warning that stays
    // until they're back, "⚠ idle" in the tab title, then how long it lasted
    let idleSince = 0, idleAt = "", idleWarn = null;
    const title = document.title;
    const warnIdle = (names) => {
      idleSince = idleSince || Date.now(); idleAt = names.join(", ");
      document.title = `⚠ idle · ${title}`;
      if (idleWarn?.isConnected) return;
      idleWarn = toast("warn", `You're idle during working hours at ${idleAt}: this time isn't counted in your attendance. Move the mouse or press a key to carry on.`, true);
    };
    const back = () => {
      if (!idleSince) return;
      const mins = Math.round((Date.now() - idleSince) / 60000);
      document.title = title; idleSince = 0;
      if (idleWarn) { closeAlert(idleWarn); idleWarn = null; }
      if (mins >= 1 && document.visibilityState === "visible") toast("info", `Welcome back: you were idle for ${mins} min during working hours at ${idleAt}. That time isn't counted in your attendance.`);
    };
    const report = (force) => { const a = active(); if (a) back(); if (force || a !== sent) { sent = a; send({ t: "state", active: a, system: a && !tabActive() }); } };
    // work in other apps (Chrome / Edge: the Idle Detection API, only with the person's permission): the browser says whether anyone is using
    // the computer at all (keyboard / mouse anywhere, screen unlocked), never what they do. With it, an open tab of this site is enough
    const osButtons = [...document.querySelectorAll("[data-idle-os]")];
    const startOS = async () => {
      try {
        const d = new IdleDetector(), read = () => { osActive = d.userState === "active" && d.screenState === "unlocked"; report(); };
        d.addEventListener("change", read);
        await d.start({ threshold: IDLE_MS });
        read();
        osButtons.forEach((b) => { b.dataset.on = ""; b.querySelector("span").textContent = "work in other apps counts: on"; b.disabled = true; });
        return true;
      } catch { return false; }
    };
    if ("IdleDetector" in window) {
      navigator.permissions?.query({ name: "idle-detection" }).then((p) => { if (p.state === "granted") startOS(); }).catch(() => {});
      osButtons.forEach((b) => b.addEventListener("click", async () => {
        let ok = false;
        try { ok = (await IdleDetector.requestPermission()) === "granted" && await startOS(); } catch {}
        toast(ok ? "info" : "warn", ok ? "Done: while this site is open in a tab, working in any app (VS Code, documents…) counts as active. Only active / idle is known, never what you do." : "Not allowed: the browser didn't give permission (check the address bar's site settings).");
      }));
    } else osButtons.forEach((b) => { b.disabled = true; b.querySelector("span").textContent = "other apps: Chrome / Edge only"; b.title = "This browser can't tell whether you're using other apps: only time in this tab counts. Chrome and Edge can."; });
    // chats: a nudge {"t": "chat", chat, from, title, company} (never the text). On that chat's page the new messages are fetched (access-checked)
    // and appended as text; with the chat drawer open it shows them; anywhere else the counts go up and a toast says who wrote (and in which company)
    const chatList = document.querySelector("[data-chat]");
    const pullChat = async () => {
      try {
        const r = await fetch(`/chats/${encodeURIComponent(chatList.dataset.chat)}/since/${encodeURIComponent(chatList.dataset.last || "0")}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
        if (!r.ok) return;
        const { messages = [] } = await r.json();
        messages.forEach((x) => {
          chatList.querySelector("[data-chat-empty]")?.remove();
          const li = document.createElement("li"), who = document.createElement("span"), b = document.createElement("b"), at = document.createElement("time");
          li.className = x.mine ? "hx-chat-msg hx-chat-mine" : "hx-chat-msg"; who.className = "hx-chat-who";
          b.textContent = x.mine ? "you" : x.who; at.textContent = x.at;  // text only, never HTML
          who.append(!x.mine && x.user ? profileLink(x.user, b, chatList.dataset.chat) : b);
          if (!x.mine && x.user) {  // current username + the name on their identity proof, both kept live ({"t": "name"})
            b.dataset.chatUser = String(x.user);
            const real = document.createElement("small"); real.className = "hx-chat-real"; real.dataset.chatReal = String(x.user); real.title = "name on their identity proof";
            real.textContent = x.real ? x.real + (x.verified ? " ✓" : "") : ""; who.append(" ", real);
          }
          who.append(at); li.append(who); dress(li, x, "chat"); chatList.append(li);
          chatList.dataset.last = String(x.id);
        });
        if (messages.length) document.getElementById("end")?.scrollIntoView({ block: "end", behavior: reduceMotion ? "auto" : "smooth" });
      } catch {}
    };
    if (chatList) document.addEventListener("hx-chat-pull", () => pullChat());  // your own files, sent from this page: shown at once
    const onChat = (m) => {
      if (chatList && String(m.chat) === chatList.dataset.chat) { if (drawer?.shown()) drawer.take(m); return pullChat(); }  // (the drawer open on top: it hears it too)
      if (drawer?.take(m)) return;
      document.querySelectorAll("[data-chat-count]").forEach((b) => { b.textContent = String((parseInt(b.textContent, 10) || 0) + 1); b.hidden = false; });
      const group = m.title && m.title !== m.from ? ` in ${m.title}` : "", where = m.company ? ` (${m.kind === "enquiry" ? "enquiry" : "company chat"} · ${m.company}${group})` : group;
      const t = toast("info", `New message from ${m.from}${where}.`, false, { href: `/chats/${Number(m.chat)}#end`, label: "read it →" });
      if (drawer) t.querySelector(".hx-nudge-go").dataset.chatOpen = String(Number(m.chat));  // opens in the drawer instead
    };
    const show = (uid, status) => document.querySelectorAll(`[data-presence-user="${CSS.escape(String(uid))}"]`).forEach((el) => {
      el.dataset.status = status;
      const label = el.querySelector("span");
      if (label) label.textContent = says[status] || "offline";
    });
    const connect = () => {
      sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${wsPath}`);
      sock.onopen = () => { wait = 1000; report(true); if (watch) send({ t: "watch", company: watch }); };
      sock.onmessage = (e) => {
        let m = null;
        try { m = JSON.parse(e.data); } catch { return; }
        if (m?.t === "presence" && m.company === watch) show(m.user, m.status);
        if (m?.t === "snapshot" && m.company === watch) Object.entries(m.statuses || {}).forEach(([u, s]) => show(u, s));
        if (m?.t === "idle" && Array.isArray(m.companies) && m.companies.length) warnIdle(m.companies.map(String));
        if (m?.t === "active") back();
        document.dispatchEvent(new CustomEvent("hx-live", { detail: m }));  // the shop, the chat drawer… listen for theirs
        if (m?.t === "chat") onChat(m);
        if (m?.t === "meet" && Number.isInteger(m.id)) {  // a meeting you're invited to has started / starts in 10 minutes (meet.py): a toast with the way in
          const room = `/meetings/${m.id}/room`;
          if (location.pathname !== room) toast("info", m.live ? `${m.by} started “${m.title}” at ${m.company}.` : `“${m.title}” at ${m.company} starts in 10 minutes: the room is open.`, true, { href: room, label: "join the call →" });
        }
        if (m?.t === "name" && m.user) {  // a colleague renamed themselves or changed their identity proof: every place their name is shown
          const id = CSS.escape(String(m.user));
          document.querySelectorAll(`[data-chat-user="${id}"]`).forEach((el) => { el.textContent = String(m.username || ""); });
          document.querySelectorAll(`[data-chat-real="${id}"]`).forEach((el) => { el.textContent = m.real ? String(m.real) + (m.verified ? " ✓" : "") : ""; });
        }
      };
      sock.onclose = (e) => { sock = null; if (e.code !== 4401 && e.code !== 4403) { setTimeout(connect, wait); wait = Math.min(wait * 2, 60000); } };  // signed out / refused: stop
    };
    const used = () => { lastUse = Date.now(); if (sent === false) report(); };
    ["pointerdown", "keydown", "scroll", "touchstart", "wheel"].forEach((ev) => addEventListener(ev, used, { passive: true, capture: true }));
    addEventListener("pointermove", () => { if (Date.now() - moved > 15000) { moved = Date.now(); used(); } }, { passive: true });
    document.addEventListener("visibilitychange", () => report());
    setInterval(() => report(), 15000);  // turns idle by itself after 5 quiet minutes
    setInterval(() => report(true), 60000);  // heartbeat
    connect();
  }
  // ---- the AI company finder ([data-find]: the sidebar link's dialog + /find): an example fills the prompt; Enter searches, Shift+Enter is a new line ----
  const findSubmit = (form) => form.requestSubmit(form.querySelector("[data-busy]") || (form.id && document.querySelector(`[form="${CSS.escape(form.id)}"][data-busy]`)) || undefined);
  document.addEventListener("click", (e) => {
    const ex = e.target.closest("[data-find-example]"), box = ex?.closest("[data-find]")?.querySelector("[data-find-prompt]");
    if (box) { box.value = ex.dataset.findExample; box.focus(); }
    if (e.target.closest('[data-mdb-target="#ai-find"]')) setTimeout(() => document.querySelector("#ai-find [data-find-prompt]")?.focus(), 350);  // once the dialog is in
  });
  document.addEventListener("keydown", (e) => {
    const box = e.target.closest?.("[data-find-prompt], [data-chat-input]");  // the finder's prompt + a chat's message box: Enter sends, Shift+Enter is a new line
    if (box && e.key === "Enter" && !e.shiftKey && !e.isComposing && box.value.trim().length >= (box.hasAttribute("data-chat-input") ? 1 : 3)) { e.preventDefault(); findSubmit(box.form); }
  });
  if (document.querySelector("[data-chat]") && !location.hash) document.getElementById("end")?.scrollIntoView({ block: "end" });  // a chat opens at its newest message
  document.addEventListener("click", (e) => {  // the language menu closes when you click elsewhere (or press Esc)
    document.querySelectorAll("[data-lang-menu][open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelectorAll("[data-lang-menu][open]").forEach((d) => { d.open = false; d.querySelector("summary")?.focus(); }); });
  document.querySelectorAll("[data-account]").forEach((b) => b.addEventListener("click", () => {  // the login page's account chooser: an account fills the username, the password is next
    const user = document.querySelector("[data-login-user]"), pass = document.querySelector("[data-login-pass]");
    if (!user || !pass) return;
    user.value = b.dataset.account;
    document.querySelectorAll("[data-account]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    (b.dataset.account ? pass : user).focus();
  }));
  document.querySelectorAll("[data-expires-in]").forEach((el) => {  // forgot username / password: the code's countdown (from the seconds it had when the page was made: no clock skew)
    const end = Date.now() + Number(el.dataset.expiresIn) * 1000, row = el.closest("p");
    const tick = () => {
      const s = Math.max(0, Math.round((end - Date.now()) / 1000));
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      row?.classList.toggle("hx-late", s < 60);
      if (s) setTimeout(tick, 1000); else if (row && !row.querySelector("span")) row.append(Object.assign(document.createElement("span"), { textContent: ": it expired, send a new one" }));
    };
    tick();
  });
  const clip = document.querySelector("[data-chat-file]");  // a chat's page: files picked, dropped anywhere on it or pasted go at once (what's typed with the first)
  if (clip) {
    const say = document.querySelector("[data-chat-input]");
    const send = async (files) => { if (await sendFiles(clip.dataset.chatFile, files, say?.value.trim() || "")) { if (say) say.value = ""; document.dispatchEvent(new CustomEvent("hx-chat-pull")); } };
    clip.addEventListener("change", () => { const f = [...clip.files]; clip.value = ""; send(f); });
    dropFiles(document.body, () => !document.body.classList.contains("hx-drawer-on"), send);  // (the drawer open: its own chat takes them)
    say?.addEventListener("paste", (e) => { const f = [...(e.clipboardData?.files || [])]; if (f.length) { e.preventDefault(); send(f); } });
  }
  // ---- signup: the region + time zone picker starts on the browser's own time zone when it's one of ours ----
  const guess = document.querySelector("[data-zone-guess]");
  if (guess) try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone, match = [...guess.options].find((o) => o.dataset.tz === tz);
    if (match) guess.value = match.value;
  } catch {}
  // ---- giving tasks: "add another task" shows the next empty block (up to 5) ----
  document.addEventListener("click", (e) => {
    const more = e.target.closest("[data-task-more]"), form = more?.closest("form");
    if (!form) return;
    const next = form.querySelector("[data-task-block][hidden]");
    if (next) { next.hidden = false; next.querySelector("input")?.focus(); }
    if (!form.querySelector("[data-task-block][hidden]")) more.hidden = true;
  });
  // ---- creating a company: "do you sell products?" yes -> the shop's offer (open a shop + what it gives) ----
  const offer = document.querySelector("[data-shop-offer]");
  if (offer) { const sells = [...document.querySelectorAll("[data-sells]")], sync = () => { offer.hidden = !sells.find((r) => r.checked && r.value === "true"); }; sells.forEach((r) => r.addEventListener("change", sync)); sync(); }
  // ---- the shop (routers/shop.py, routers/seller.py): small helpers ----
  document.querySelectorAll("select[data-autosubmit], input[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form?.requestSubmit()));  // (inputs: the admin's switches)
  // ---- giving a task: "the AI picks" hides the people boxes (the server goes by the AI then) ----
  document.querySelectorAll("[data-ai-pick]").forEach((s) => s.addEventListener("change", () => {
    const box = s.closest("[data-task-block]")?.querySelector("[data-pick-people]");
    if (box) box.hidden = s.value !== "0";
  }));
  // ---- signup: is the username free? asked as you type (any capitals count as the same name); the server checks again on submit ----
  document.querySelectorAll("[data-username-check]").forEach((input) => {
    const note = input.form.querySelector("[data-username-note]");
    let timer = 0, asked = 0;
    const show = (ok, text, hold) => { note.hidden = false; note.textContent = text; note.classList.toggle("hx-ok", ok); note.classList.toggle("hx-err-msg", !ok); input.setCustomValidity(ok || hold ? "" : text); };  // hold: maybe your own earlier try, the server decides
    input.addEventListener("input", () => {
      clearTimeout(timer); input.setCustomValidity(""); note.hidden = true;
      const name = input.value.trim();
      if (!/^[A-Za-z0-9_]{3,32}$/.test(name)) return;
      timer = setTimeout(async () => {
        const mine = ++asked;
        try {
          const r = await fetch(`/register/username?name=${encodeURIComponent(name)}`, { headers: { Accept: "application/json" } });
          const j = await r.json();
          if (mine === asked && input.value.trim() === name) show(!!j.ok, j.why || "", !!j.hold);
        } catch {}  // offline: the server still checks when the form is sent
      }, 350);
    });
  });
  document.querySelectorAll("[data-w]").forEach((b) => { b.style.width = `${Math.max(0, Math.min(100, Number(b.dataset.w) || 0))}%`; });  // rating bars (CSSOM: no inline style attributes under the CSP)
  document.addEventListener("click", (e) => {  // [data-copy]: one click copies it (a tracking number, a delivery link)
    const c = e.target.closest("[data-copy]");
    if (!c) return;
    const value = c.hasAttribute("data-copy-url") ? location.origin + c.dataset.copy : c.dataset.copy;
    copyText(value).then((ok) => (ok ? toast("info", `Copied: ${value}`) : toast("error", "Couldn't copy: select it and copy it by hand.")));
  });
  const gallery = document.querySelector("[data-gallery]");
  if (gallery) gallery.addEventListener("click", (e) => {  // a thumbnail shows its photo / video big
    const b = e.target.closest("[data-gallery-pick]"), main = gallery.querySelector("[data-gallery-main]");
    if (!b || !main) return;
    const next = document.createElement(b.dataset.kind === "video" ? "video" : "img");
    next.src = b.dataset.galleryPick; next.dataset.galleryMain = "";
    if (b.dataset.kind === "video") { next.controls = true; next.preload = "metadata"; } else next.alt = main.alt || "";
    main.replaceWith(next);
    gallery.querySelectorAll("[data-gallery-pick]").forEach((x) => x.classList.toggle("hx-active", x === b));
  });
  const ship = document.querySelector("[data-ship-form]");
  if (ship) {  // "own delivery / another courier": who + their phone instead of a tracking number
    const pick = ship.querySelector("[data-courier]"), custom = ship.querySelector("[data-courier-custom]"), awb = ship.querySelector("[data-courier-awb] input");
    const sync = () => { const own = pick.value === "custom"; custom.hidden = !own; awb.required = !own; custom.querySelector("input").required = own; };
    pick.addEventListener("change", sync); sync();
  }
  // ---- the AI shopping assistant ([data-ask]): asked over fetch, answered live (the socket says {"t": "answer"}; polled too, in case) ----
  const ask = document.querySelector("[data-ask]"), mine = document.querySelector("[data-qa-mine]");
  const showAnswer = async (li) => {
    try {
      const r = await fetch(`/shop/questions/${encodeURIComponent(li.dataset.question)}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) return;
      const q = await r.json(), a = li.querySelector("[data-answer]");
      li.dataset.status = q.status;
      if (q.status === "pending") return;
      a.textContent = "";
      if (q.status === "failed") { a.textContent = "The assistant couldn't answer just now: ask again in a moment."; return; }
      const icon = document.createElement("i"); icon.className = "fa-solid fa-wand-magic-sparkles"; a.append(icon, " ", q.answer);
      if (q.status === "spam") return;
      let meta = li.querySelector("[data-meta]");
      if (!meta) { meta = document.createElement("small"); meta.className = "hx-qa-meta"; meta.dataset.meta = ""; a.after(meta); }
      meta.textContent = `AI answer · ${q.confidence} confidence${q.sources ? ` · from ${q.sources}` : ""}${q.ask_seller ? " · the seller has been asked too" : ""}`;
    } catch {}
  };
  if (mine) {
    setInterval(() => mine.querySelectorAll('[data-status="pending"]').forEach(showAnswer), 5000);
    document.addEventListener("hx-live", (e) => { if (e.detail?.t === "answer") { const li = mine.querySelector(`[data-question="${CSS.escape(String(e.detail.id))}"]`); if (li) showAnswer(li); } });
  }
  document.querySelectorAll("[data-ask-chip]").forEach((b) => b.addEventListener("click", () => { const i = ask?.querySelector("input[name=question]"); if (i) { i.value = b.dataset.askChip; i.focus(); } }));
  if (ask && mine) ask.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = ask.querySelector("input[name=question]"), btn = ask.querySelector("button"), text = input.value.trim();
    if (text.length < 4) return;
    btn.disabled = true;
    try {
      const r = await fetch(ask.action, { method: "POST", body: new FormData(ask), headers: { Accept: "application/json", "X-Requested-With": "fetch" }, credentials: "same-origin" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.id) { toast("error", data.detail || "Couldn't ask that right now."); return; }
      const li = document.createElement("li"), q = document.createElement("p"), a = document.createElement("p"), qi = document.createElement("i"), spin = document.createElement("i");
      li.className = "hx-qa-item"; li.dataset.question = String(data.id); li.dataset.status = data.status;
      q.className = "hx-qa-q"; qi.className = "fa-solid fa-user"; q.append(qi, " ", text);
      a.className = "hx-qa-a"; a.dataset.answer = ""; spin.className = "fa-solid fa-spinner fa-spin"; a.append(spin, " thinking… (the local AI reads the listing and the reviews)");
      li.append(q, a); mine.prepend(li); input.value = "";
      if (data.status !== "pending") showAnswer(li);
    } catch { toast("error", "Couldn't reach the shop: try again."); } finally { btn.disabled = false; }
  });
  // ---- a buyer's order page ([data-order]): the timeline follows the delivery live ----
  const orderPage = document.querySelector("[data-order]");
  const TRACK_BAD = { exception: "hx-track-exception", attempted: "hx-track-attempted", cancelled: "hx-track-cancelled" };  // whole names: the build renames each hx- class (system.obfuscate), so never "hx-track-" + status
  if (orderPage) document.addEventListener("hx-live", async (e) => {
    if (e.detail?.t !== "order" || String(e.detail.id) !== orderPage.dataset.order) return;
    try {
      const r = await fetch(`/shop/orders/${encodeURIComponent(orderPage.dataset.order)}/track`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) return;
      const t = await r.json(), list = orderPage.querySelector("[data-track]");
      orderPage.querySelector("[data-order-status]").textContent = t.label;
      if (!list) return;
      list.replaceChildren(...t.events.map((x) => {
        const li = document.createElement("li"), i = document.createElement("i"), d = document.createElement("div"), b = document.createElement("b"), tm = document.createElement("time");
        li.className = TRACK_BAD[x.status] || ""; i.className = `fa-solid ${x.icon}`; b.textContent = x.label; d.append(b);
        if (x.location) { const s = document.createElement("span"); s.textContent = ` · ${x.location}`; d.append(s); }
        if (x.note) { const p = document.createElement("p"); p.textContent = x.note; d.append(p); }
        tm.textContent = x.at + (x.by && x.source !== "system" ? ` · ${x.by}` : ""); d.append(tm); li.append(i, d);
        return li;
      }));
      toast("info", `Order update: ${t.label}.`);
    } catch {}
  });
  document.addEventListener("hx-live", (e) => {  // friends (routers/friends.py): a request or an acceptance, live: a toast + the sidebar count
    const m = e.detail;
    if (m?.t !== "friend") return;
    if (m.kind === "request") document.querySelectorAll("[data-friend-count]").forEach((b) => { b.textContent = String((parseInt(b.textContent, 10) || 0) + 1); b.hidden = false; });
    toast("info", m.kind === "request" ? `${m.from} sent you a friend request.` : `${m.from} accepted your friend request: you can chat privately now.`, false, { href: "/friends", label: m.kind === "request" ? "answer it →" : "friends →" });
  });
  document.addEventListener("hx-live", (e) => {  // elsewhere: a toast with the way in
    const m = e.detail;
    if (m?.t === "order" && !orderPage) toast("info", m.title || "An order of yours moved on.", false, { href: `/shop/orders/${Number(m.id)}`, label: "see it →" });
    if (m?.t === "shop") toast("info", m.title || "Shop update.", false, { href: location.pathname.match(/^\/companies\/[0-9a-f-]{36}\//) ? `${location.pathname.split("/shop")[0]}/shop/orders/${Number(m.id)}` : "/workplaces", label: "open it →" });
  });
  // ---- charts (Chart.js, vendored): the shop's dashboard ([data-shop-charts]) + a product's price history ([data-price-chart]). The validated
  //      categorical palette (dataviz reference: fixed slot order, light + dark steps), thin marks, hairline grid, a table twin for each ----
  const PAL = { light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"], dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"] };
  const INK = { light: { text: "#52514e", grid: "#e1e0d9", surface: "#fcfcfb" }, dark: { text: "#c3c2b7", grid: "#2c2c2a", surface: "#1a1a19" } };
  const mode = () => (document.documentElement.dataset.mdbTheme === "dark" ? "dark" : "light");
  const charts = [];
  const cash = (cur) => { try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }); } catch { return new Intl.NumberFormat(); } };
  const table = (el, head, rows) => {  // the chart's table view
    if (!el) return;
    const thead = document.createElement("thead"), tr = document.createElement("tr"), tbody = document.createElement("tbody");
    head.forEach((h) => { const th = document.createElement("th"); th.textContent = h; tr.append(th); });
    thead.append(tr);
    rows.forEach((r) => { const row = document.createElement("tr"); r.forEach((v) => { const td = document.createElement("td"); td.textContent = v; row.append(td); }); tbody.append(row); });
    el.replaceChildren(thead, tbody);
  };
  const draw = (canvas, config) => { const old = charts.findIndex((c) => c.canvas === canvas); if (old >= 0) { charts[old].destroy(); charts.splice(old, 1); } const c = new window.Chart(canvas, config); charts.push(c); return c; };
  const base = (ink) => ({ responsive: true, maintainAspectRatio: false, animation: { duration: reduceMotion ? 0 : 600 }, plugins: { legend: { labels: { color: ink.text, boxWidth: 10, boxHeight: 10, usePointStyle: true } }, tooltip: { intersect: false, mode: "index" } } });
  const donut = (canvas, labels, values, cur, money) => {
    const m = mode(), ink = INK[m], total = values.reduce((a, b) => a + b, 0), f = cash(cur);
    if (!total) { canvas.closest(".hx-chart-box").dataset.empty = "nothing yet"; return; }
    delete canvas.closest(".hx-chart-box").dataset.empty;
    draw(canvas, { type: "doughnut", data: { labels, datasets: [{ data: values, backgroundColor: PAL[m].slice(0, values.length), borderColor: ink.surface, borderWidth: 2, hoverOffset: 4 }] },
      options: { ...base(ink), cutout: "62%", plugins: { ...base(ink).plugins, legend: { position: "bottom", labels: { color: ink.text, boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money ? f.format(c.parsed) : c.parsed} (${Math.round((c.parsed * 100) / total)}%)` } } } } });
  };
  const shopCharts = document.querySelector("[data-shop-charts]");
  const loadShop = async (days) => {
    try {
      const r = await fetch(`${shopCharts.dataset.shopCharts}?days=${days}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) return;
      const d = await r.json(), m = mode(), ink = INK[m], f = cash(d.currency), q = (s) => shopCharts.querySelector(s);
      Object.entries(d.kpi).forEach(([k, v]) => { const el = q(`[data-kpi="${k}"]`); if (el) el.textContent = ["sales", "aov", "commission"].includes(k) ? f.format(v) : String(v); });
      shopCharts.querySelectorAll("[data-cur]").forEach((el) => { el.textContent = `(${d.currency}, tax included)`; });
      if (!window.Chart) return;
      draw(q('[data-chart="sales"]'), { type: "line", data: { labels: d.days, datasets: [{ label: "sales", data: d.sales, borderColor: PAL[m][0], backgroundColor: `${PAL[m][0]}1f`, fill: true, borderWidth: 2, pointRadius: 0, pointHitRadius: 12, tension: 0.25 }] },
        options: { ...base(ink), plugins: { ...base(ink).plugins, legend: { display: false }, tooltip: { intersect: false, mode: "index", callbacks: { label: (c) => ` ${f.format(c.parsed.y)}` } } },
          scales: { x: { grid: { display: false }, ticks: { color: ink.text, maxTicksLimit: 8 }, border: { color: ink.grid } }, y: { beginAtZero: true, grid: { color: ink.grid }, ticks: { color: ink.text, callback: (v) => f.format(v) }, border: { display: false } } } } });
      table(q('[data-table="sales"]'), ["day", `sales (${d.currency})`], d.days.map((x, i) => [x, f.format(d.sales[i])]));
      [["category", true], ["status", false], ["method", true]].forEach(([k, money]) => { donut(q(`[data-chart="${k}"]`), d[k][0], d[k][1], d.currency, money); table(q(`[data-table="${k}"]`), [k, money ? `sales (${d.currency})` : "orders"], d[k][0].map((x, i) => [x, money ? f.format(d[k][1][i]) : String(d[k][1][i])])); });
      const [names, vals] = d.top;
      draw(q('[data-chart="top"]'), { type: "bar", data: { labels: names, datasets: [{ label: "sales", data: vals, backgroundColor: PAL[m][0], borderRadius: 4, borderSkipped: "start", barThickness: 14 }] },
        options: { ...base(ink), indexAxis: "y", plugins: { ...base(ink).plugins, legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${f.format(c.parsed.x)}` } } },
          scales: { x: { beginAtZero: true, grid: { color: ink.grid }, ticks: { color: ink.text, callback: (v) => f.format(v), maxTicksLimit: 5 }, border: { display: false } }, y: { grid: { display: false }, ticks: { color: ink.text }, border: { color: ink.grid } } } } });
      table(q('[data-table="top"]'), ["product", `sales (${d.currency})`], names.map((x, i) => [x, f.format(vals[i])]));
    } catch {}
  };
  if (shopCharts) {
    let days = Number(shopCharts.dataset.days) || 30;
    loadShop(days);
    shopCharts.querySelectorAll("[data-days-pick]").forEach((b) => b.addEventListener("click", () => {  // one period control for every chart
      days = Number(b.dataset.daysPick);
      shopCharts.querySelectorAll("[data-days-pick]").forEach((x) => { x.classList.toggle("hx-btn-solid", x === b); x.setAttribute("aria-pressed", String(x === b)); });
      loadShop(days);
    }));
    new MutationObserver(() => loadShop(days)).observe(document.documentElement, { attributes: true, attributeFilter: ["data-mdb-theme"] });  // light <-> dark: redrawn with that mode's steps
  }
  const priceBox = document.querySelector("[data-price-chart]");
  const loadPrices = async () => {
    try {
      const r = await fetch(priceBox.dataset.priceChart, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok || !window.Chart) return;
      const d = await r.json(), m = mode(), ink = INK[m], f = cash(d.currency), canvas = priceBox.querySelector("canvas");
      if (!d.days.length) { canvas.closest(".hx-chart-box").dataset.empty = "the first prices are read within a day"; return; }
      draw(canvas, { type: "line", data: { labels: d.days, datasets: d.series.map((s, i) => ({ label: s.label, data: s.values, borderColor: PAL[m][i], backgroundColor: PAL[m][i], borderWidth: s.own ? 3 : 2, pointRadius: d.days.length < 3 ? 4 : 0, pointHitRadius: 12, spanGaps: true, tension: 0.2 })) },
        options: { ...base(ink), plugins: { ...base(ink).plugins, legend: { position: "bottom", labels: { color: ink.text, boxWidth: 10, boxHeight: 10, usePointStyle: true } }, tooltip: { intersect: false, mode: "index", callbacks: { label: (c) => ` ${c.dataset.label}: ${c.parsed.y == null ? "–" : f.format(c.parsed.y)}` } } },
          scales: { x: { grid: { display: false }, ticks: { color: ink.text, maxTicksLimit: 8 }, border: { color: ink.grid } }, y: { grid: { color: ink.grid }, ticks: { color: ink.text, callback: (v) => f.format(v) }, border: { display: false } } } } });
      table(priceBox.querySelector("[data-table]"), ["day", ...d.series.map((s) => s.label)], d.days.map((x, i) => [x, ...d.series.map((s) => (s.values[i] == null ? "–" : f.format(s.values[i])))]));
    } catch {}
  };
  if (priceBox) { loadPrices(); new MutationObserver(loadPrices).observe(document.documentElement, { attributes: true, attributeFilter: ["data-mdb-theme"] }); }
  // completion rings ([data-completion], completion.py: /profile + a company's dashboard): each group's finished items in the palette's fixed slots
  // (the checklist's swatches match: app.css .hx-sw-*) + "still to do" in the neutral grid ink. The checklist under it is its table
  const rings = [...document.querySelectorAll("[data-completion]")];
  const drawRings = () => rings.forEach((canvas) => {
    if (!window.Chart) return;
    let groups = [];
    try { groups = JSON.parse(canvas.dataset.completion || "[]"); } catch { return; }
    const m = mode(), ink = INK[m], left = Number(canvas.dataset.left) || 0;
    draw(canvas, { type: "doughnut", data: { labels: [...groups.map((g) => g.label), "still to do"], datasets: [{ data: [...groups.map((g) => g.done), left],
      backgroundColor: [...PAL[m].slice(0, groups.length), ink.grid], borderColor: ink.surface, borderWidth: 2, hoverOffset: 4 }] },
      options: { ...base(ink), cutout: "74%", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${c.label}: ${c.parsed} ${c.parsed === 1 ? "item" : "items"}` } } } } });
  });
  if (rings.length) { drawRings(); new MutationObserver(drawRings).observe(document.documentElement, { attributes: true, attributeFilter: ["data-mdb-theme"] }); }
  // ---- bank details in any country ([data-bank-country], banks.py): the bank's own code (IFSC, ABA routing number, BSB…) and its example,
  //      or no code where the IBAN is the account number; the account inputs ([data-bank-account]) ask for the number or the IBAN to match ----
  document.querySelectorAll("[data-bank-country]").forEach((pick) => {
    const form = pick.closest("form"), local = form?.querySelector("[data-bank-local]"), code = local?.querySelector("[data-bank-code]");
    if (!code) return;
    const needed = code.required, accounts = [...form.querySelectorAll("[data-bank-account]")];
    const sync = () => {
      const o = pick.selectedOptions[0], iban = o?.dataset.scheme === "iban", other = o?.value === "ZZ";
      if (!o) return;
      local.hidden = iban; code.disabled = iban; code.required = needed && !iban && !other;
      local.querySelector("[data-bank-label]").textContent = `${o.dataset.label}:`; local.querySelector("[data-bank-label-sr]").textContent = o.dataset.label;
      code.placeholder = o.dataset.example || "if the bank uses one";
      accounts.forEach((a, i) => { a.placeholder = i ? `confirm the ${iban ? "IBAN" : "account number"}` : a.dataset.keep || o.dataset.account; a.inputMode = iban || other ? "text" : "numeric"; });
      form.querySelectorAll("[data-bank-account-label]").forEach((l) => { l.textContent = iban ? "IBAN:" : "account no:"; });
    };
    pick.addEventListener("change", sync); sync();
  });
  // ---- a new meeting ([data-meet-form]): "everyone" covers each person; "schedule" needs a start time, "start now" doesn't ----
  const meetForm = document.querySelector("[data-meet-form]");
  if (meetForm) {
    const all = meetForm.querySelector("[data-meet-everyone]"), picks = [...meetForm.querySelectorAll("[data-meet-pick] input")], starts = meetForm.querySelector("[data-meet-starts]");
    const sync = () => picks.forEach((p) => { p.disabled = all.checked; p.closest("label")?.classList.toggle("hx-disabled", all.checked); });
    all?.addEventListener("change", sync); sync();
    document.querySelector("[data-meet-schedule]")?.addEventListener("click", () => { starts.required = true; });
    document.querySelector("[data-meet-now]")?.addEventListener("click", () => { starts.required = false; });
  }
  // ---- the tasks calendar ([data-cal]): month blocks from /tasks/calendar?month=YYYY-MM in a scrolling box of their own ([data-cal-scroll]): appended as you scroll down in it,
  //      prepended by the button at its top, forever. (Prepended by scrolling up, on the page itself, it pushed you back down faster than you could climb: 96 wheel notches up left
  //      you lower than you started. Now the box has a real top, where the wheel moves on to the page.) A task is a bar from the day it was given to its deadline (clipped to each
  //      week; 4 lanes, then "+n more"); each day shows how many tasks run through it and your region's national holiday. Built as text nodes only ----
  const cal = document.querySelector("[data-cal]");
  if (cal) {
    const months = cal.querySelector("[data-cal-months]"), scroller = cal.querySelector("[data-cal-scroll]"), endS = cal.querySelector("[data-cal-end]");
    const pad = (n) => String(n).padStart(2, "0"), iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, ym = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
    const day = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
    const plus = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n), month = (d, n) => new Date(d.getFullYear(), d.getMonth() + n, 1);
    const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x; };
    const now = new Date(), today = iso(now), thisMonth = month(now, 0), LANES = 4;
    const ATT = { full: ["hx-cal-att hx-cal-att-full", "✓"], part: ["hx-cal-att hx-cal-att-part", "◐"], absent: ["hx-cal-att hx-cal-att-absent", "✗"],  // attendance marks (whole class names: the build renames them)
                  leave: ["hx-cal-att hx-cal-att-leave", "☾"], today: ["hx-cal-att hx-cal-att-today", "●"] };
    const attMark = (a, many) => {  // one company's day: what it says + where it leads (that company's dashboard)
      const [cls, mark] = ATT[a.status] || ATT.part, what = a.status === "absent" ? "absent" : a.status === "leave" ? "on leave" : a.status === "today" ? `${a.worked} so far` : a.worked;
      const chip = el("a", cls, `${mark} ${many ? `${a.company.slice(0, 10)} · ` : ""}${what}`);
      chip.href = a.url; chip.title = `${a.company}: ${a.status === "absent" ? "absent (nothing worked on a working day)" : a.status === "leave" ? "approved leave" : `worked ${a.worked} of ${a.of}${a.status === "today" ? " so far today" : ""}`}`;
      return chip;
    };
    let earliest = null, latest = null, busyTop = false, busyEnd = false;
    const block = (data) => {
      const first = day(data.first), last = day(data.last), sec = el("section", "hx-cal-month");
      sec.dataset.month = data.month;
      sec.append(el("h3", "hx-cal-title", data.label));
      const dow = el("div", "hx-cal-dow");
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((w) => dow.append(el("span", null, w)));
      sec.append(dow);
      const tasks = (data.tasks || []).map((t) => ({ ...t, s: day(t.start), e: day(t.end) })), meets = (data.meetings || []).map((m) => ({ ...m, meet: true, s: day(m.start), e: day(m.end) }));
      const items = [...meets, ...tasks];  // meetings first: they're on one day at a set time
      for (let start = plus(first, -((first.getDay() + 6) % 7)); start <= last; start = plus(start, 7)) {
        const end = plus(start, 6), lo = start < first ? first : start, hi = end > last ? last : end, week = el("div", "hx-cal-week");
        const col = (d) => Math.round((d - start) / 864e5);
        const segs = items.filter((t) => t.s <= hi && t.e >= lo).map((t) => ({ t, a: col(t.s > lo ? t.s : lo), b: col(t.e < hi ? t.e : hi) }))
          .sort((x, y) => x.a - y.a || (y.b - y.a) - (x.b - x.a));
        const lanes = [], hidden = Array(7).fill(0);
        segs.forEach((g) => {
          let i = lanes.findIndex((busyTo) => busyTo < g.a);
          if (i === -1 && lanes.length < LANES) { lanes.push(-1); i = lanes.length - 1; }
          if (i === -1) { g.lane = -1; for (let k = g.a; k <= g.b; k++) hidden[k]++; return; }
          lanes[i] = g.b; g.lane = i;
        });
        const att = data.attendance || {}, marks = Math.max(0, ...Array.from({ length: 7 }, (_, i) => (att[iso(plus(start, i))] || []).length));  // attendance chips under the date
        week.style.gridTemplateRows = `${54 + marks * 19}px repeat(${Math.max(lanes.length, 1)}, 24px) 8px`;
        for (let i = 0; i < 7; i++) {
          const d = plus(start, i), key = iso(d), cell = el("div", "hx-cal-day");
          cell.style.gridColumn = String(i + 1); cell.style.gridRow = "1 / -1";
          if (d < first || d > last) { cell.classList.add("hx-cal-out"); week.append(cell); continue; }
          if (i >= 5) cell.classList.add("hx-cal-weekend");
          if (key === today) { cell.classList.add("hx-cal-today"); cell.dataset.today = ""; }
          const head = el("div", "hx-cal-daytop"), n = tasks.filter((t) => t.s <= d && t.e >= d).length, k = meets.filter((t) => t.s <= d && t.e >= d).length;
          head.append(el("b", null, String(d.getDate())));
          if (n || k) head.append(el("span", "hx-cal-count", [n && `${n} task${n === 1 ? "" : "s"}`, k && `${k} meet${k === 1 ? "" : "s"}`].filter(Boolean).join(" · ")));
          cell.append(head);
          (att[key] || []).forEach((a) => cell.append(attMark(a, data.tracked > 1)));
          const holiday = data.holidays?.[key];
          if (holiday) { cell.classList.add("hx-cal-holiday"); cell.append(el("small", "hx-cal-hol", holiday)); cell.title = `${holiday}: national holiday (${data.region})`; }
          if (hidden[i]) cell.append(el("small", "hx-cal-more", `+${hidden[i]} more`));
          week.append(cell);
        }
        segs.filter((g) => g.lane >= 0).forEach(({ t, a, b, lane }) => {
          const bar = el("a", "hx-cal-bar", t.meet ? `${t.time.split("–")[0]} ${t.title}` : t.title + (t.gave ? ` · ${t.progress} done` : ""));
          bar.href = t.url;
          bar.title = t.meet ? `video meeting: ${t.title} · ${t.company} · ${t.time}${t.status === "live" ? " · live now" : t.status === "ended" ? " · over" : ""}`
            : `${t.title} · ${t.company} · given ${t.start}, due ${t.due}${t.gave ? ` · ${t.progress} done` : ""}`;
          if (t.meet) bar.classList.add("hx-cal-meet");
          if (t.meet && t.status === "ended") bar.classList.add("hx-cal-meet-past");
          if (t.done) bar.classList.add("hx-cal-done"); else if (t.late) bar.classList.add("hx-cal-late");
          if (t.gave) bar.classList.add("hx-cal-gave");
          if (t.mine) bar.classList.add("hx-cal-mine");
          if (t.s < lo) bar.classList.add("hx-cal-from");  // runs on from before this week / after it: square edges
          if (t.e > hi) bar.classList.add("hx-cal-on");
          bar.style.gridColumn = `${a + 1} / ${b + 2}`; bar.style.gridRow = String(lane + 2);
          week.append(bar);
        });
        sec.append(week);
      }
      return sec;
    };
    const get = async (d) => {
      const r = await fetch(`/tasks/calendar?month=${ym(d)}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    };
    const near = (s) => { const r = s.getBoundingClientRect(), b = scroller.getBoundingClientRect(); return r.top < b.bottom + 500 && r.bottom > b.top - 500; };
    const later = async () => {  // the next month at the bottom
      if (busyEnd) return;
      busyEnd = true;
      try { const m = latest ? month(latest, 1) : thisMonth; months.append(block(await get(m))); latest = m; earliest = earliest || m; }
      catch { endS.textContent = "Couldn't load the next month: scroll again to retry."; }
      finally { busyEnd = false; }
      if (near(endS)) setTimeout(later, 50);  // still in view (short months, tall screen): keep going
    };
    const earlier = async () => {  // the month before, at the top: shown straight away, under the button
      if (busyTop || !earliest) return;
      busyTop = true;
      try { const m = month(earliest, -1); months.prepend(block(await get(m))); earliest = m; scroller.scrollTop = 0; }
      catch { toast("error", "Couldn't load that month: try again."); } finally { busyTop = false; }
    };
    const io = new IntersectionObserver((list) => list.forEach((x) => { if (x.isIntersecting) later(); }), { root: scroller, rootMargin: "500px 0px" });
    const goToday = (behavior) => {  // today's week, inside the box (scrollIntoView would move the page too)
      const d = months.querySelector("[data-today]");
      if (d) scroller.scrollTo({ top: Math.max(0, scroller.scrollTop + d.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 70), behavior });
    };
    later().then(() => { goToday("auto"); io.observe(endS); });
    cal.querySelector("[data-cal-earlier]")?.addEventListener("click", earlier);
    cal.querySelector("[data-cal-today]")?.addEventListener("click", () => goToday(reduceMotion ? "auto" : "smooth"));
    const hide = { mine: "hx-cal-hide-mine", gave: "hx-cal-hide-gave", meet: "hx-cal-hide-meet", att: "hx-cal-hide-att" };
    cal.querySelectorAll("[data-cal-filter]").forEach((box) => box.addEventListener("change", () => cal.classList.toggle(hide[box.dataset.calFilter], !box.checked)));
  }
  // ---- statements: /statements#pay-<id> (the "salary credited" notification) and …/dashboard#pay-<id> open that payment's details ----
  const openPayment = () => {
    let d = null;
    try { d = location.hash.length > 1 ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null; } catch {}
    if (d?.tagName !== "DETAILS") return;
    d.open = true;
    d.classList.add("hx-tx-focus");
    d.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
  };
  openPayment();
  addEventListener("hashchange", openPayment);
  // ---- recording a payout: live net = gross - TDS ([data-payout]); paid in any currency: the cycle's minimum + the clearance (in the salary currency) converted at data-rate ----
  document.querySelectorAll("[data-payout]").forEach((f) => {
    const gross = f.querySelector("[data-gross]"), tds = f.querySelector("[data-tds]"), out = f.querySelector("[data-net]"), tax = out.dataset.tax || "TDS";
    const noTax = f.querySelector("[data-no-tax-warn]"), pick = f.querySelector("[data-pay-currency]"), cycle = f.querySelector('select[name="cycle"]'), counted = f.querySelector("[data-counted]");
    const sc = f.dataset.salaryCurrency, cap = +f.dataset.max, num = (n) => n.toLocaleString("en-US"), ph = gross.placeholder;
    const sync = () => {
      const cur = pick ? pick.value : sc, k = (pick && +pick.selectedOptions[0].dataset.rate) || 1, opt = cycle?.selectedOptions[0], low = +(opt?.dataset.minimum || 0), sug = +(opt?.dataset.suggest || 0);  // k: 1 cur in the salary currency
      const lo = low ? Math.max(1, Math.ceil((low - 0.5) / k)) : 1, most = sug ? Math.max(1, Math.ceil((sug - 0.5) / k)) : Infinity;  // the server rounds half up: lo is the least that still counts as the minimum, most the first amount that reaches the salary
      const hi = Math.max(1, Math.min(Math.floor(cap / k), most));  // not over the clearance, nor the cycle's salary
      gross.min = lo; gross.max = hi; out.dataset.currency = cur;
      gross.placeholder = sug ? String(most) : ph;  // the cycle's calculated salary, in the currency picked
      f.querySelectorAll("[data-pay-cur]").forEach((s) => (s.textContent = cur));
      if (counted) {
        counted.hidden = cur === sc;
        counted.querySelector("[data-counted-amount]").textContent = gross.value === "" ? "–" : `${sc} ${num(Math.round((+gross.value || 0) * k))}`;
        counted.querySelector("[data-counted-rate]").textContent = `1 ${cur} = ${+k.toPrecision(6)} ${sc}`;
        counted.querySelector("[data-counted-range]").textContent = `${cur} ${num(lo)} – ${cur} ${num(hi)}`;
      }
      const g = +gross.value || 0, t = +tds.value || 0, bad = t > g && gross.value !== "";
      tds.setCustomValidity(bad ? `${tax} can't be more than the gross salary.` : "");
      out.textContent = gross.value === "" || tds.value === "" ? `net: type the gross and ${tax}` : bad ? `${tax} is more than the gross salary` : `net credited: ${out.dataset.currency} ${(g - t).toLocaleString("en-US")}`;
      out.classList.toggle("hx-net-bad", bad);
      if (noTax) noTax.hidden = !(t > 0 && "noTax" in tds.dataset);  // e.g. AED: no income tax, so a deduction is worth a second look
    };
    f.addEventListener("input", sync);
    sync();
  });
  // ---- company form: working hours span, at most data-max hours a day (the server checks it too) ----
  document.querySelectorAll("[data-hours]").forEach((box) => {
    const start = box.querySelector("[data-hours-start]"), end = box.querySelector("[data-hours-end]"), out = box.querySelector("[data-hours-span]"), max = +box.dataset.max * 60;
    const mins = (v) => { const [h, m] = v.split(":").map(Number); return h * 60 + m; };
    const sync = () => {
      if (!start.value || !end.value) return;
      const span = mins(end.value) - mins(start.value);
      const bad = span <= 0 ? "must end after they start" : span > max ? `can't be more than ${max / 60} hours` : span < 60 ? "must be at least 1 hour" : "";
      end.setCustomValidity(bad ? `Working hours ${bad}.` : "");
      out.textContent = bad ? `✗ ${bad}` : `${Math.floor(span / 60)}h ${String(span % 60).padStart(2, "0")}m a day`;
      out.classList.toggle("hx-hours-bad", !!bad);
    };
    box.addEventListener("input", sync);
    sync();
  });
  document.querySelectorAll("[data-print]").forEach((b) => b.addEventListener("click", () => print()));
  // ---- company form: the payout rules of the salary currency picked ([data-rules], one block per currency) ----
  document.querySelectorAll("[data-rules]").forEach((box) => {
    const pick = box.closest("form")?.querySelector('select[name="salary_currency"]');
    const show = () => box.querySelectorAll("[data-rules-for]").forEach((b) => (b.hidden = b.dataset.rulesFor !== pick.value));
    if (pick) { pick.addEventListener("change", show); show(); }
  });
  // ---- copy buttons: <button data-copy="text"> (company invite codes: workplaces + public search cards) ----
  const copyText = async (text) => {
    try { await navigator.clipboard.writeText(text); return true; } catch {}  // needs https or localhost
    const t = Object.assign(document.createElement("textarea"), { value: text, readOnly: true });  // fallback: plain http (e.g. a LAN address)
    t.style.cssText = "position:fixed;opacity:0;pointer-events:none";
    document.body.append(t); t.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch {}
    t.remove();
    return ok;
  };
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    if (!(await copyText(b.dataset.copy))) return;
    const icon = b.querySelector("i"), text = b.querySelector("span"), label = b.getAttribute("aria-label"), was = text?.textContent;
    icon.className = "fa-solid fa-check";
    if (text) text.textContent = "copied!";
    b.classList.add("hx-copied");
    b.setAttribute("aria-label", "Copied");
    setTimeout(() => { icon.className = "fa-solid fa-copy"; if (text) text.textContent = was; b.classList.remove("hx-copied"); b.setAttribute("aria-label", label); }, 1500);
  }));
  // ---- footer clock ----
  const clock = document.getElementById("hx-clock");
  if (clock) {
    const tick = () => (clock.textContent = new Date().toLocaleTimeString([], { hour12: false }));
    tick();
    setInterval(tick, 1000);
  }
  // ---- theme toggle (saved in localStorage; theme.js applies it on load). Icons swap via CSS. ----
  const btn = document.getElementById("hx-theme-toggle");
  const label = () => btn.setAttribute("aria-label", `Switch to ${root.dataset.mdbTheme === "dark" ? "light" : "dark"} theme`);
  if (btn) {
    label();
    btn.addEventListener("click", () => {
      const theme = root.dataset.mdbTheme === "dark" ? "light" : "dark";
      root.dataset.mdbTheme = theme;
      try { localStorage.setItem("theme", theme); } catch {}
      label();
      btn.classList.remove("hx-spinning");
      void btn.offsetWidth;  // restart the icon spin animation
      btn.classList.add("hx-spinning");
    });
  }
})();
