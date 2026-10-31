// The meeting room (templates/meeting_room.html; meet.py + /ws/meet/<id> on the server). The lobby checks your camera + microphone; then
// the call: one RTCPeerConnection per other person (whoever joins later makes the offer, the server only relays it), a tile each, screen
// sharing (it takes your camera's place), a raised hand, the chat, live captions (your own speech, cut into clips at pauses, goes to
// Whisper on the server; everyone's captions come back, with English added when it's another language), the AI's live notes and, for
// hosts, recording: every tile + every voice drawn into one video, streamed to the server in pieces. Whatever the server or other people
// send is shown as text only, never as HTML.
(() => {
  "use strict";
  const root = document.querySelector("[data-meet]");
  if (!root) return;
  const $ = (s) => root.querySelector(s), $$ = (s) => [...root.querySelectorAll(s)];
  const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x; };
  let clock;
  try { clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", timeZone: root.dataset.tz || "UTC" }); } catch { clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }); }
  const hhmm = (iso) => { try { return iso ? clock.format(new Date(iso)) : ""; } catch { return ""; } };
  const initials = (n) => { const s = String(n || "?"), p = s.split(/[\W_]+/).filter(Boolean); return (p.length > 1 ? p[0][0] + p[1][0] : s.slice(0, 2)).toUpperCase(); };
  const toast = (text, kind) => {  // the site's toast look (app.css); text only
    let box = document.querySelector(".hx-toasts");
    if (!box) { box = el("div", "hx-toasts"); box.setAttribute("aria-live", "polite"); document.body.append(box); }
    const t = el("div", kind === "error" ? "alert hx-alert hx-toast hx-flash-error" : "alert hx-alert hx-toast hx-flash-info"), msg = el("span", "hx-msg");
    t.setAttribute("role", "alert");
    msg.append(el("b", null, kind === "error" ? "[err]" : "[info]"), " ", String(text));
    t.append(el("i", kind === "error" ? "fa-solid fa-triangle-exclamation" : "fa-solid fa-circle-info"), msg);
    box.append(t);
    setTimeout(() => t.remove(), kind === "error" ? 9000 : 6000);
  };
  // ---- your camera, microphone and screen ----
  const VIDEO = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 24, max: 30 } }, AUDIO = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const media = { mic: null, cam: null, screen: null, hasCam: false };
  const st = { mic: true, cam: true, screen: false, hand: false, cc: true };
  const out = () => (st.screen && media.screen) || (st.cam && media.cam) || null;  // the picture others get
  const actx = window.AudioContext ? new AudioContext() : null;  // levels (who's talking) + your caption clips
  addEventListener("pointerdown", () => actx?.resume().catch(() => {}), { once: true, capture: true });  // browsers start it paused until the first click (the lobby's level meter)
  const level = (track) => {  // a track's loudness, 0..1, on demand
    if (!actx || !track) return () => 0;
    const an = actx.createAnalyser(), buf = new Float32Array(512);
    an.fftSize = 512; actx.createMediaStreamSource(new MediaStream([track])).connect(an);
    return () => { an.getFloatTimeDomainData(buf); let s = 0; for (const x of buf) s += x * x; return Math.sqrt(s / buf.length); };
  };
  const preview = $("[data-preview]"), devices = $("[data-devices]"), joinBtn = $("[data-join]");
  const setup = async () => {
    const say = (text) => { devices.textContent = text; };
    if (!navigator.mediaDevices?.getUserMedia) { say("// this page can't use a camera or microphone here (browsers allow them on https or localhost only): you can still join to watch, listen and chat"); joinBtn.disabled = false; return sync(); }
    let stream = null;
    for (const c of [{ audio: AUDIO, video: VIDEO }, { audio: AUDIO }, { video: VIDEO }]) { try { stream = await navigator.mediaDevices.getUserMedia(c); break; } catch {} }
    media.mic = stream?.getAudioTracks()[0] || null; media.cam = stream?.getVideoTracks()[0] || null; media.hasCam = !!media.cam;
    st.mic = !!media.mic; st.cam = !!media.cam;
    say(media.mic && media.cam ? "// camera and microphone ready: switch either off before you join if you like" : media.mic ? "// microphone ready; no camera (or it's blocked): others see your initials"
      : media.cam ? "// camera ready; no microphone (or it's blocked): you can use the chat" : "// no camera or microphone (or the browser blocked them: check the address bar): you can still join to watch, listen and chat");
    joinBtn.disabled = false; joinBtn.focus();
    const loud = level(media.mic), bar = $("[data-level]");
    setInterval(() => { if (bar) bar.style.width = `${Math.min(100, Math.round((st.mic ? loud() : 0) * 400))}%`; }, 120);
    sync();
  };
  // ---- the call: socket, tiles, connections ----
  let sock = null, me = null, myCard = null, host = root.dataset.host === "yes", ice = [], inCall = false, over = false, tries = 0, ends = null, ai = { captions: false, english: false };
  const peers = new Map();  // peer id -> {id, card, tile, v (video), stream, pc, audio / video (senders), offerer, chain, pending, loud}
  let self = null, pinned = null;
  const send = (m) => { if (sock?.readyState === WebSocket.OPEN) sock.send(JSON.stringify(m)); };
  const sendBytes = (kind, buf) => {  // 1 = a speech clip, 2 = a piece of the recording
    if (sock?.readyState !== WebSocket.OPEN || (kind === 1 && sock.bufferedAmount > 4e6)) return;  // a slow link: a caption clip is skipped rather than queued
    const b = new Uint8Array(buf.byteLength + 1); b[0] = kind; b.set(new Uint8Array(buf), 1); sock.send(b);
  };
  const sendState = () => { if (myCard) Object.assign(myCard, { mic: st.mic, cam: !!(st.cam && media.cam), screen: st.screen, hand: st.hand }); if (self) paint(self); send({ t: "state", mic: st.mic, cam: !!(st.cam && media.cam), screen: st.screen, hand: st.hand }); };
  const grid = $("[data-grid]"), strip = $("[data-strip]"), stage = $("[data-stage]");
  const tile = (card, mine) => {
    const t = el("figure", mine ? "hx-meet-tile hx-meet-self" : "hx-meet-tile"), v = el("video"), cap = el("figcaption", "hx-meet-tag");
    v.playsInline = true; v.autoplay = true; v.muted = !!mine;
    const name = el("b", null, card.name), real = el("small", "hx-chat-real");
    name.dataset.chatUser = String(card.user); real.dataset.chatReal = String(card.user); real.title = "name on their identity proof";
    cap.append(el("i", "fa-solid fa-microphone-slash hx-meet-mute-icon"), name, " ", real);
    if (mine) cap.append(el("small", null, " (you)"));
    t.append(v, el("div", "hx-meet-avatar", initials(card.name)), cap, el("span", "hx-meet-hand", "✋"), el("span", "hx-meet-net", "connecting…"));
    t.tabIndex = 0; t.title = "click to pin this tile big (again to unpin)";
    t.addEventListener("click", () => { pinned = pinned === card.peer ? null : card.peer; layout(); });
    return { t, v };
  };
  const paint = (p) => {
    const c = p.card, t = p.tile;
    t.classList.toggle("hx-meet-nocam", !(c.cam || c.screen));
    t.classList.toggle("hx-meet-muted", !c.mic);
    t.classList.toggle("hx-meet-handup", !!c.hand);
    t.classList.toggle("hx-meet-screen", !!c.screen);
    t.classList.toggle("hx-meet-pinned", pinned === c.peer);
    t.querySelector("b").textContent = c.name;
    t.querySelector(".hx-chat-real").textContent = c.real ? c.real + (c.verified ? " ✓" : "") : "";
  };
  const layout = () => {  // a grid of everyone; when someone shares a screen (or a tile is pinned): that one big, the rest in a strip
    const all = [self, ...peers.values()].filter(Boolean);
    const focus = all.length > 1 ? (all.find((p) => p.card.peer === pinned) || [...peers.values()].find((p) => p.card.screen)) : null;
    stage.classList.toggle("hx-meet-spotlight", !!focus);
    const cols = focus ? 1 : all.length <= 1 ? 1 : all.length <= 4 ? 2 : all.length <= 9 ? 3 : 4;
    if (focus) { grid.replaceChildren(focus.tile); strip.replaceChildren(...all.filter((p) => p !== focus).map((p) => p.tile)); }
    else { grid.replaceChildren(...all.map((p) => p.tile)); strip.replaceChildren(); }
    grid.style.setProperty("--cols", String(cols)); grid.style.setProperty("--rows", String(focus ? 1 : Math.ceil(all.length / cols)));  // tiles stay 16:9 inside (app.css)
    all.forEach(paint);
    $("[data-count]").textContent = String(all.length);
    people();
  };
  let shown;
  const showSelf = (force) => {  // your own picture (lobby preview + your tile): only re-attached when it changes, so it never flickers
    const v = out();
    if (self) paint(self);
    if (v === shown && !force) return;
    shown = v;
    const s = new MediaStream(v ? [v] : []);
    if (self) self.v.srcObject = s;
    preview.srcObject = s;
    $("[data-preview-off]").hidden = !!v;
  };
  const add = (card) => { const { t, v } = tile(card, false); const p = { id: card.peer, card, tile: t, v, stream: new MediaStream(), pending: [], chain: Promise.resolve() }; v.srcObject = p.stream; peers.set(card.peer, p); return p; };
  const drop = (id) => { const p = peers.get(id); if (!p) return; try { p.pc?.close(); } catch {} p.tile.remove(); peers.delete(id); if (pinned === id) pinned = null; };
  const link = (p, offerer) => {
    const pc = new RTCPeerConnection({ iceServers: ice });
    p.pc = pc; p.offerer = offerer;
    pc.onicecandidate = (e) => { if (e.candidate) send({ t: "signal", to: p.id, data: { candidate: e.candidate.toJSON() } }); };
    pc.ontrack = (e) => {  // a fresh stream each time: a <video> doesn't pick up tracks added to the stream it already plays
      p.stream = new MediaStream([...p.stream.getTracks().filter((x) => x.kind !== e.track.kind), e.track]);
      p.v.srcObject = p.stream;
      p.v.play().catch(() => {});
      if (e.track.kind === "audio") p.loud = level(e.track);
    };
    pc.onconnectionstatechange = () => {
      p.tile.dataset.net = pc.connectionState;
      if (pc.connectionState === "failed" && p.offerer) pc.restartIce();
    };
    if (!offerer) return;
    p.audio = pc.addTransceiver("audio", { direction: "sendrecv" }).sender;
    p.video = pc.addTransceiver("video", { direction: "sendrecv" }).sender;
    p.audio.replaceTrack(media.mic).catch(() => {}); p.video.replaceTrack(out()).catch(() => {});
    pc.onnegotiationneeded = async () => { try { await pc.setLocalDescription(await pc.createOffer()); send({ t: "signal", to: p.id, data: { description: pc.localDescription } }); } catch {} };
  };
  const signal = async (p, data) => {  // one at a time per person (p.chain)
    if (!p.pc) link(p, false);
    const pc = p.pc;
    if (data.description) {
      await pc.setRemoteDescription(data.description);
      if (data.description.type === "offer") {
        for (const t of pc.getTransceivers()) {  // answer on the offer's own audio + video lines: no renegotiation, ever (toggles + screen share swap tracks)
          const kind = t.receiver.track.kind;
          if (kind === "audio" && !p.audio) { p.audio = t.sender; t.direction = "sendrecv"; await t.sender.replaceTrack(media.mic); }
          if (kind === "video" && !p.video) { p.video = t.sender; t.direction = "sendrecv"; await t.sender.replaceTrack(out()); }
        }
        await pc.setLocalDescription(await pc.createAnswer());
        send({ t: "signal", to: p.id, data: { description: pc.localDescription } });
      }
      for (const c of p.pending.splice(0)) await pc.addIceCandidate(c).catch(() => {});
    } else if (data.candidate) {
      if (pc.remoteDescription) await pc.addIceCandidate(data.candidate).catch(() => {}); else p.pending.push(data.candidate);
    }
  };
  const swapVideo = () => { for (const p of peers.values()) p.video?.replaceTrack(out()).catch(() => {}); showSelf(); };
  // ---- the buttons ----
  const ICONS = { mic: ["fa-microphone", "fa-microphone-slash"], cam: ["fa-video", "fa-video-slash"] };
  const sync = () => {
    $$('[data-act="mic"]').forEach((b) => { b.setAttribute("aria-pressed", String(st.mic)); b.disabled = !media.mic; b.querySelector("i").className = `fa-solid ${ICONS.mic[st.mic ? 0 : 1]}`; b.classList.toggle("hx-meet-off", !st.mic); });
    $$('[data-act="cam"]').forEach((b) => { const on = st.cam && !!media.cam; b.setAttribute("aria-pressed", String(on)); b.disabled = !media.hasCam; b.querySelector("i").className = `fa-solid ${ICONS.cam[on ? 0 : 1]}`; b.classList.toggle("hx-meet-off", !on); });
    const press = (act, on) => $$(`[data-act="${act}"]`).forEach((b) => { b.setAttribute("aria-pressed", String(on)); b.classList.toggle("hx-meet-on", on); });
    press("share", st.screen); press("hand", st.hand); press("cc", st.cc); press("rec", !!rec.r || recOn.mine);
    const r = $('[data-act="rec"] span'); if (r) r.textContent = rec.r ? "stop rec" : "record";
    root.classList.toggle("hx-meet-nocc", !st.cc);
    showSelf();
  };
  const setMic = (on) => { if (!media.mic) return; st.mic = on; media.mic.enabled = on; sync(); sendState(); };
  const setCam = async (on) => {
    if (on && !media.cam && media.hasCam) { try { media.cam = (await navigator.mediaDevices.getUserMedia({ video: VIDEO })).getVideoTracks()[0]; } catch { toast("Couldn't turn the camera back on: is another app using it?", "error"); on = false; } }
    if (!on && media.cam) { media.cam.stop(); media.cam = null; }  // off = the camera light goes off too
    st.cam = on && !!media.cam; swapVideo(); sync(); sendState();
  };
  const unshare = () => { media.screen?.stop(); media.screen = null; st.screen = false; swapVideo(); sync(); sendState(); layout(); };
  const share = async () => {
    if (st.screen) return unshare();
    if (!navigator.mediaDevices?.getDisplayMedia) return toast("This browser can't share its screen.", "error");
    try {
      media.screen = (await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 15, max: 30 } }, audio: false })).getVideoTracks()[0];
      media.screen.contentHint = "detail"; media.screen.addEventListener("ended", unshare);
      st.screen = true; swapVideo(); sync(); sendState(); layout();
      toast("You're sharing your screen: everyone sees it big. Press share again (or the browser's stop button) to stop.");
    } catch {}
  };
  // ---- the panel: chat, captions, notes, people ----
  const panel = $("[data-panel]"), unread = $("[data-unread]");
  let tab = "chat", unseen = 0;
  const open = (name) => {
    if (!panel.hidden && tab === name) { panel.hidden = true; root.classList.remove("hx-meet-paneled"); }
    else { tab = name; panel.hidden = false; root.classList.add("hx-meet-paneled"); }
    $$("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    $$("[data-pane]").forEach((x) => { x.hidden = x.dataset.pane !== tab; });
    ["chat", "notes", "people"].forEach((a) => $$(`[data-act="${a}"]`).forEach((b) => b.setAttribute("aria-pressed", String(!panel.hidden && tab === a))));
    if (!panel.hidden && tab === "chat") { unseen = 0; unread.hidden = true; setTimeout(() => $("[data-say-input]")?.focus(), 50); }
    const feed = $(`[data-feed="${tab}"]`); if (feed) feed.scrollTop = feed.scrollHeight;
  };
  const who = (x) => {  // current username (+ the name on their identity proof): kept live by {"t": "name"}
    const s = el("span", "hx-chat-who"), b = el("b", null, x.user && x.user === myCard?.user ? "you" : x.name);
    s.append(b);
    if (x.user && x.user !== myCard?.user) { b.dataset.chatUser = String(x.user); const r = el("small", "hx-chat-real", x.real ? x.real + (x.verified ? " ✓" : "") : ""); r.dataset.chatReal = String(x.user); s.append(" ", r); }
    if (x.gone) s.append(el("small", null, " (deleted account)"));
    s.append(el("time", null, hhmm(x.at)));
    return s;
  };
  const said = (x) => {  // a caption's text: the English, else what was said + its language
    const p = el("p", "hx-meet-said");
    p.append(el("span", null, x.en || x.text));
    if (x.lang && x.lang !== "en") p.append(el("small", "hx-meet-lang", x.en ? ` · ${x.language}: ${x.text}` : ` · ${x.language}${ai.english ? ", translating…" : ""}`));
    return p;
  };
  const caps = $("[data-captions]");
  const line = (x, live) => {
    const feed = $(`[data-feed="${x.kind === "chat" ? "chat" : "captions"}"]`);
    feed.querySelector("[data-empty]")?.remove();
    const li = el("li", x.kind === "chat" ? "hx-meet-msg" : "hx-meet-msg hx-meet-msg-cc");
    li.dataset.line = String(x.id);
    li.append(who(x), x.kind === "chat" ? el("p", "hx-meet-said", x.text) : said(x));
    const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    feed.append(li);
    while (feed.children.length > 400) feed.firstElementChild.remove();
    if (stick) feed.scrollTop = feed.scrollHeight;
    if (!live) return;
    if (x.kind === "chat" && x.user !== myCard?.user && (panel.hidden || tab !== "chat")) { unseen++; unread.textContent = String(unseen); unread.hidden = false; toast(`${x.name}: ${x.text.slice(0, 120)}`); }
    if (x.kind === "speech") {  // the overlay: the newest two captions, each for 9 s
      const c = el("p", "hx-meet-cap");
      c.dataset.line = String(x.id);
      c.append(el("b", null, `${x.user === myCard?.user ? "you" : x.name}: `), ...said(x).childNodes);
      caps.append(c);
      while (caps.children.length > 2) caps.firstElementChild.remove();
      setTimeout(() => c.remove(), 9000);
    }
  };
  const english = (m) => $$(`[data-line="${CSS.escape(String(m.id))}"]`).forEach((x) => {
    const span = x.querySelector(".hx-meet-said > span, .hx-meet-cap > span"), small = x.querySelector(".hx-meet-lang");
    if (span) { const was = span.textContent; span.textContent = m.en; if (small) small.textContent = ` · ${small.textContent.replace(/^ · /, "").split(",")[0]}: ${was}`; }
  });
  const notes = (n, at) => {
    const box = $("[data-notes]"), note = $("[data-notes-note]");
    box.replaceChildren();
    if (!n) { box.append(el("p", "hx-hint", ai.english ? "// the local AI writes notes as the meeting goes: every couple of minutes once people talk or write" : "// no AI notes: the local AI is offline")); note.textContent = ""; return; }
    if (n.summary) box.append(el("p", "hx-meet-sum", n.summary));
    const list = (title, icon, items, make) => {
      if (!items?.length) return;
      const h = el("h3"), ul = el("ul");
      h.append(el("i", `fa-solid ${icon}`), ` ${title}`); items.forEach((it) => ul.append(make(it))); box.append(h, ul);
    };
    list("key points", "fa-list-ul", n.points, (t) => el("li", null, t));
    list("decisions", "fa-gavel", n.decisions, (t) => el("li", null, t));
    list("action items", "fa-list-check", n.actions, (a) => { const li = el("li"); li.append(el("b", null, `${a.who} `), a.what); return li; });
    note.textContent = `// updated ${hhmm(at)} by the local AI from the captions + chat: check them`;
  };
  const people = () => {
    const ul = $("[data-people]"), all = [self, ...peers.values()].filter(Boolean);
    ul.replaceChildren(...all.sort((a, b) => (b.card.hand - a.card.hand) || a.card.name.localeCompare(b.card.name)).map((p) => {
      const c = p.card, li = el("li"), name = el("b", null, c.name + (p === self ? " (you)" : "")), icons = el("span", "hx-meet-pstate");
      name.dataset.chatUser = String(c.user);
      if (c.hand) icons.append(el("span", null, "✋"));
      icons.append(el("i", `fa-solid ${c.mic ? "fa-microphone" : "fa-microphone-slash"}`), el("i", `fa-solid ${c.screen ? "fa-display" : c.cam ? "fa-video" : "fa-video-slash"}`));
      li.append(el("span", "hx-meet-avatar", initials(c.name)), name, " ", el("small", "hx-chat-real", c.real ? c.real + (c.verified ? " ✓" : "") : ""), c.host ? el("small", "hx-meet-host", "host") : "", icons);
      return li;
    }));
  };
  // ---- your captions: the mic is recorded in clips cut at pauses (or every 10 s); clips with speech go to the server (Whisper) ----
  let ccTimer = 0;
  const captions = () => {
    if (ccTimer || !media.mic || !ai.captions || !window.MediaRecorder) return;
    const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm", "audio/mp4"].find((t) => MediaRecorder.isTypeSupported(t)) || "", loud = level(media.mic);
    let r = null, floor = 0.006;
    const begin = () => {
      const parts = [], x = new MediaRecorder(new MediaStream([media.mic]), type ? { mimeType: type, audioBitsPerSecond: 32000 } : undefined);
      x.voiced = 0; x.began = performance.now(); x.quiet = 0;
      x.ondataavailable = (e) => { if (e.data.size) parts.push(e.data); };
      x.onstop = async () => { if (x.voiced >= 300 && inCall) sendBytes(1, await new Blob(parts, { type: x.mimeType }).arrayBuffer()); };
      x.start(); r = x;
    };
    begin();
    ccTimer = setInterval(() => {
      if (!inCall) { clearInterval(ccTimer); ccTimer = 0; if (r?.state === "recording") r.stop(); return; }
      const v = loud(), talking = st.mic && v > Math.max(0.012, floor * 2.5), now = performance.now();
      if (!talking) floor = floor * 0.98 + v * 0.02;
      if (talking) { r.voiced += 50; r.quiet = 0; } else if (r.voiced && !r.quiet) r.quiet = now;
      if ((r.voiced && r.quiet && now - r.quiet > 650) || now - r.began > 10000 || (!r.voiced && now - r.began > 4000)) { r.stop(); begin(); }  // cut at a pause: no word is split
    }, 50);
  };
  // ---- recording (hosts): every tile + every voice into one 1280x720 video, sent in 4-second pieces (in order) ----
  const rec = { r: null, draw: 0, mix: 0, ac: null, q: Promise.resolve() }, recOn = { mine: false };
  const fit = (g, v, x, y, w, h, contain) => {
    const vw = v.videoWidth, vh = v.videoHeight;
    if (!vw || !vh || v.readyState < 2) return false;
    const s = contain ? Math.min(w / vw, h / vh) : Math.max(w / vw, h / vh), dw = vw * s, dh = vh * s;
    g.save(); g.beginPath(); g.rect(x, y, w, h); g.clip(); g.drawImage(v, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh); g.restore();
    return true;
  };
  const startRec = () => {
    if (rec.r) return;
    const type = window.MediaRecorder && ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"].find((t) => MediaRecorder.isTypeSupported(t));
    if (!type) { toast("This browser can't record the call (it needs WebM): try Chrome, Edge or Firefox.", "error"); send({ t: "rec", on: false }); return; }
    const canvas = el("canvas"), g = canvas.getContext("2d"), W = 1280, H = 720, title = document.querySelector(".hx-meet-what h1")?.textContent || "";
    canvas.width = W; canvas.height = H;
    rec.ac = new AudioContext(); rec.ac.resume().catch(() => {});  // started from a message, not a click: make sure it runs (else the recording is silent)
    const dest = rec.ac.createMediaStreamDestination(), mixed = new Set();
    const mix = () => [media.mic, ...[...peers.values()].flatMap((p) => p.stream.getAudioTracks())].filter(Boolean).forEach((t) => {
      if (mixed.has(t.id)) return;
      mixed.add(t.id); rec.ac.createMediaStreamSource(new MediaStream([t])).connect(dest);
    });
    mix(); rec.mix = setInterval(mix, 1500);
    const cell = (p, x, y, w, h) => {
      g.fillStyle = "#0d1210"; g.fillRect(x, y, w, h);
      if (!((p.card.cam || p.card.screen) && fit(g, p.v, x, y, w, h, p.card.screen))) {
        g.fillStyle = "#39ff88"; g.font = `700 ${Math.round(h / 5)}px monospace`; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(initials(p.card.name), x + w / 2, y + h / 2);
      }
      g.fillStyle = "rgba(0,0,0,.6)"; g.fillRect(x, y + h - 28, w, 28);
      g.fillStyle = "#d7e2dc"; g.font = "14px monospace"; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(`${p.card.name}${p.card.mic ? "" : " (muted)"}${p.card.hand ? " ✋" : ""}`, x + 10, y + h - 14);
    };
    rec.draw = setInterval(() => {
      const all = [self, ...peers.values()].filter(Boolean), focus = all.find((p) => p.card.screen && p !== self) || all.find((p) => p.card.screen);
      g.fillStyle = "#07090a"; g.fillRect(0, 0, W, H);
      if (focus && all.length > 1) {
        cell(focus, 8, 40, 960, H - 48);
        all.filter((p) => p !== focus).slice(0, 5).forEach((p, i) => cell(p, 976, 40 + i * 136, 296, 128));
      } else {
        const cols = all.length <= 1 ? 1 : all.length <= 4 ? 2 : all.length <= 9 ? 3 : 4, rows = Math.ceil(all.length / cols), w = (W - 8 * (cols + 1)) / cols, h = (H - 40 - 8 * rows) / rows;
        all.forEach((p, i) => cell(p, 8 + (i % cols) * (w + 8), 40 + Math.floor(i / cols) * (h + 8), w, h));
      }
      g.fillStyle = "#ff5f6d"; g.font = "700 16px monospace"; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText("● REC", 12, 20);
      g.fillStyle = "#d7e2dc"; g.font = "14px monospace"; g.fillText(`${title} · ${hhmm(new Date().toISOString())}`, 90, 20);
    }, 1000 / 15);
    const r = new MediaRecorder(new MediaStream([...canvas.captureStream(15).getVideoTracks(), ...dest.stream.getAudioTracks()]), { mimeType: type, videoBitsPerSecond: 1500000, audioBitsPerSecond: 96000 });
    r.ondataavailable = (e) => { if (e.data.size) rec.q = rec.q.then(async () => sendBytes(2, await e.data.arrayBuffer())); };  // in order, whatever arrayBuffer() takes
    r.start(4000); rec.r = r; sync();
    if (document.visibilityState !== "visible") toast("Keep this tab in front while you record: browsers slow down hidden tabs.");
  };
  const stopRec = (tell) => {  // tell: say so to the server once the last piece is sent (stopped here), not when the server stopped it
    const r = rec.r;
    if (!r) { if (tell) send({ t: "rec", on: false }); return; }
    rec.r = null;
    r.onstop = () => { clearInterval(rec.draw); clearInterval(rec.mix); rec.ac?.close().catch(() => {}); rec.q = rec.q.then(() => { if (tell) send({ t: "rec", on: false }); }); sync(); };
    r.stop();
  };
  // ---- messages from the server ----
  const ended = (title, text, again) => {
    over = true; inCall = false;
    stopRec(false);
    peers.forEach((_, id) => drop(id));
    [media.mic, media.cam, media.screen].forEach((t) => t?.stop());
    $("[data-over-title]").textContent = title; $("[data-over-text]").textContent = text; $("[data-rejoin]").hidden = !again;
    $("[data-over]").hidden = false;
    try { sock?.close(1000); } catch {}
  };
  const handle = (m) => {
    switch (m.t) {
      case "welcome": {
        me = m.you; myCard = m.me; host = !!m.host; ice = Array.isArray(m.ice) ? m.ice : []; ends = m.ends ? new Date(m.ends) : null; ai = m.ai || ai; tries = 0;
        peers.forEach((_, id) => drop(id));
        $$("[data-feed]").forEach((f) => f.querySelectorAll("li:not([data-empty])").forEach((x) => x.remove()));
        if (!self) { const { t, v } = tile(myCard, true); self = { id: me, card: myCard, tile: t, v }; } else { self.id = me; self.card = myCard; }
        self.loud = level(media.mic); showSelf(true);
        (m.peers || []).forEach((c) => link(add(c), true));  // you're the newcomer: you call everyone already here
        (m.lines || []).forEach((x) => line(x, false));
        notes(m.notes, m.notes_at); recState(m.rec || { on: false });
        $("[data-cc-note]").textContent = ai.captions ? `// Whisper on this server writes what's said${ai.english ? "; the local AI adds English when it's another language" : " (no English for other languages: the local AI is offline)"}` : `// captions are off: ${m.ai?.captions_detail || "the captions service is down"}`;
        $("[data-cc-chip] span").textContent = ai.captions ? "captions" : "no captions";
        inCall = true;
        $("[data-lobby]").hidden = true; stage.hidden = false; $("[data-bar]").hidden = false; root.classList.add("hx-meet-incall");
        sendState(); layout(); sync(); captions();
        if (!m.peers?.length) toast("You're the first one here: the others have been told the meeting has started.");
        break;
      }
      case "join": add(m); layout(); toast(`${m.name} joined.`); break;  // they call you (they're the newcomer)
      case "leave": { const p = peers.get(m.peer); if (p) { drop(m.peer); layout(); toast(`${p.card.name} left.`); } break; }
      case "state": { const p = peers.get(m.peer); if (!p) break; const raised = m.hand && !p.card.hand, shared = m.screen && !p.card.screen; Object.assign(p.card, m); layout(); if (raised) toast(`✋ ${m.name} raised their hand.`); if (shared) toast(`${m.name} is sharing their screen.`); break; }
      case "signal": { const p = peers.get(m.from); if (p && m.data) p.chain = p.chain.then(() => signal(p, m.data)).catch(() => {}); break; }
      case "line": line(m, true); break;
      case "english": english(m); break;
      case "notes": notes(m.notes, m.at); if (m.final) toast("The AI's final notes are in."); break;
      case "rec": recState(m); break;
      case "error": toast(m.detail || "Something went wrong.", "error"); break;
      case "refused": { const e = $("[data-refused]"); e.textContent = m.detail || "You can't join this meeting."; e.hidden = false; joinBtn.disabled = true; over = true; break; }
      case "replaced": ended("You joined from somewhere else", "This meeting is now open in another tab or on another device, so this one has left it.", true); break;
      case "ended": ended("The meeting has ended", `${m.by || "A host"} ended it for everyone. Its recording, captions, chat and the AI's notes are on the meeting's page.`, false); break;
      case "chat": toast(`New message from ${m.from}${m.title && m.title !== m.from ? ` in ${m.title}` : ""}: it's under Chats.`); break;  // the presence hub's nudge
      case "name": $$(`[data-chat-user="${CSS.escape(String(m.user))}"]`).forEach((x) => { x.textContent = String(m.username || ""); }); for (const p of [self, ...peers.values()]) if (p && p.card.user === m.user) { p.card.name = m.username; p.card.real = m.real; p.card.verified = m.verified; paint(p); } break;
    }
  };
  const recState = (m) => {
    const chip = $("[data-rec-chip]");
    chip.hidden = !m.on; $("[data-rec-by]").textContent = m.on ? `REC · ${m.by}` : "REC";
    recOn.mine = !!(m.on && m.peer === me);
    if (m.on && m.peer === me) startRec();
    else if (!m.on && rec.r) stopRec(false);
    if (inCall && m.on) toast(`● ${m.by} is recording this call.`);
    if (inCall && !m.on && m.why) toast(`Recording stopped: ${m.why}.`);
    sync();
  };
  const connect = () => {
    sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${root.dataset.ws}`);
    sock.binaryType = "arraybuffer";
    sock.onmessage = (e) => { let m = null; try { m = JSON.parse(e.data); } catch { return; } if (m && typeof m === "object") handle(m); };
    sock.onclose = (e) => {
      if (over) return;
      if (e.code === 4401) return ended("You've been signed out", "Log in again to rejoin.", false);
      if (e.code === 4403) return ended("You've left the meeting", "You're not in this company any more, or this page isn't allowed to connect.", false);
      if (e.code === 4404 || e.code === 4429) { if (inCall) ended("You've left the meeting", e.code === 4429 ? "Too much was sent too fast." : "The meeting can't be joined right now.", true); return; }
      if (!inCall) { $("[data-refused]").textContent = "Couldn't connect: try again in a moment."; $("[data-refused]").hidden = false; joinBtn.disabled = false; return; }
      if (++tries > 6) return ended("Connection lost", "The connection to the server dropped and didn't come back.", true);
      toast("Connection lost: reconnecting…", "error");
      peers.forEach((_, id) => drop(id)); layout();
      setTimeout(connect, 1000 * tries);
    };
  };
  // ---- wiring ----
  joinBtn.addEventListener("click", () => { joinBtn.disabled = true; actx?.resume().catch(() => {}); $("[data-refused]").hidden = true; connect(); });
  const armed = new WeakMap();
  const act = (a, b) => {
    if (a === "mic") setMic(!st.mic);
    else if (a === "cam") setCam(!(st.cam && media.cam));
    else if (a === "share") share();
    else if (a === "hand") { st.hand = !st.hand; sync(); sendState(); layout(); }
    else if (a === "cc") { st.cc = !st.cc; if (!st.cc) caps.replaceChildren(); sync(); }
    else if (a === "rec") { if (rec.r) stopRec(true); else if (recOn.mine) send({ t: "rec", on: false }); else send({ t: "rec", on: true }); }
    else if (a === "chat" || a === "notes" || a === "people") open(a);
    else if (a === "leave") { over = true; inCall = false; stopRec(true); setTimeout(() => { try { sock?.close(1000); } catch {} location.href = root.dataset.back; }, rec.r ? 800 : 50); }
    else if (a === "end" && b) {  // twice, so it's never by accident
      if (!armed.get(b)) { armed.set(b, true); b.querySelector("span").textContent = "sure? click again"; setTimeout(() => { armed.delete(b); b.querySelector("span").textContent = "end for all"; }, 3500); return; }
      stopRec(true); setTimeout(() => send({ t: "end" }), rec.r ? 800 : 0);
    }
  };
  root.addEventListener("click", (e) => { const b = e.target.closest("[data-act]"); if (b && !b.disabled) act(b.dataset.act, b); const t = e.target.closest("[data-tab]"); if (t) open(t.dataset.tab); if (e.target.closest("[data-panel-close]")) open(tab); });
  $("[data-say]").addEventListener("submit", (e) => { e.preventDefault(); const i = $("[data-say-input]"), text = i.value.trim(); if (!text || !inCall) return; send({ t: "chat", text }); i.value = ""; });
  document.addEventListener("keydown", (e) => {
    if (!inCall || e.ctrlKey || e.metaKey || e.altKey || e.target.closest?.("input, textarea, select, [contenteditable]")) return;
    const k = { m: "mic", v: "cam", s: "share", h: "hand", c: "cc", t: "chat" }[e.key.toLowerCase()];
    if (k) { e.preventDefault(); act(k); }
  });
  addEventListener("beforeunload", (e) => { if (rec.r) { e.preventDefault(); e.returnValue = ""; } });
  setInterval(() => {  // who's talking (a ring on their tile), and the time left in the slot
    for (const p of [self, ...peers.values()]) if (p?.loud) p.tile.classList.toggle("hx-meet-talk", p.card.mic && p.loud() > 0.04);
    if (ends) { const left = Math.round((ends - Date.now()) / 60000); $("[data-left]").textContent = left > 0 ? `${left} min left` : left === 0 ? "ending now" : `${-left} min over`; }
  }, 250);
  setup();
})();
