/* 听雨 FM · 最后一夜 —— 前端主逻辑
 * 职责:场景呈现(画面/雨幕/音效)、旁白流式打字机、选择与自由行动、结局页与成就。
 * 降级原则:任何 AI 环节失败,都回落到服务端给的预写文本,游戏永远能继续。 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const NARRATION_FIRST_EVENT_TIMEOUT = 18000; // 首个事件超时 → 降级预写文本

  // ---------------------------------------------------------------- 存储
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem("ffg." + key); return v === null ? fallback : JSON.parse(v); }
      catch (_) { return fallback; }
    },
    set(key, value) { try { localStorage.setItem("ffg." + key, JSON.stringify(value)); } catch (_) { /* 隐私模式忽略 */ } },
    remove(key) { try { localStorage.removeItem("ffg." + key); } catch (_) { /* ignore */ } },
  };

  // ---------------------------------------------------------------- API
  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function api(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const res = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new ApiError(res.status, data.detail || res.statusText);
      return data;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(0, "网络异常,请检查服务是否在运行");
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------------------------------------------------------------- 打字机
  class Typewriter {
    constructor(el) { this.el = el; this.timer = null; this.reset(); }

    reset(onDone) {
      clearInterval(this.timer); this.timer = null;
      this.buffer = ""; this.shown = 0; this.ended = false; this.finished = false;
      this.onDone = onDone || null;
      this.el.replaceChildren();
    }
    push(text) { this.buffer += text; this._run(); }
    set(text) { this.buffer = text; this.shown = 0; this._run(); }
    end() { this.ended = true; this._run(); }
    flush() { this.shown = this.buffer.length; this._render(); if (this.ended) this._finish(); }

    _run() { if (!this.timer && !this.finished) this.timer = setInterval(() => this._tick(), 24); }
    _tick() {
      const backlog = this.buffer.length - this.shown;
      if (backlog > 0) {
        this.shown += backlog > 160 ? 5 : backlog > 60 ? 2 : 1;
        this._render();
      } else if (this.ended) {
        this._finish();
      }
    }
    _render() {
      const text = this.buffer.slice(0, this.shown);
      const paragraphs = text.split(/\n+/).filter((p) => p.length);
      const nodes = paragraphs.map((p) => { const el = document.createElement("p"); el.textContent = p; return el; });
      if (!this.finished && nodes.length) nodes[nodes.length - 1].classList.add("caret");
      this.el.replaceChildren(...nodes);
      this.el.parentElement.scrollTop = this.el.parentElement.scrollHeight;
    }
    _finish() {
      if (this.finished) return;
      this.finished = true;
      clearInterval(this.timer); this.timer = null;
      this._render();
      const cb = this.onDone; this.onDone = null;
      if (cb) cb();
    }
  }

  // ---------------------------------------------------------------- 雨幕画布
  class Rain {
    constructor(canvas) {
      this.canvas = canvas; this.ctx = canvas.getContext("2d");
      this.intensity = 0.4; this.target = 0.4;
      this.max = window.innerWidth < 700 ? 140 : 280;
      this.drops = Array.from({ length: this.max }, () => this._drop(true));
      this._resize(); window.addEventListener("resize", () => this._resize());
      if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) requestAnimationFrame(() => this._loop());
    }
    setIntensity(v) { this.target = v; }
    _resize() { this.canvas.width = window.innerWidth; this.canvas.height = window.innerHeight; }
    _drop(randomY) {
      return { x: Math.random() * 1.2 * window.innerWidth, y: randomY ? Math.random() * window.innerHeight : -20,
        len: 10 + Math.random() * 22, speed: 14 + Math.random() * 16, alpha: 0.12 + Math.random() * 0.22 };
    }
    _loop() {
      const { ctx, canvas } = this;
      this.intensity += (this.target - this.intensity) * 0.03;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.lineWidth = 1;
      const count = Math.floor(this.max * this.intensity);
      for (let i = 0; i < count; i++) {
        const d = this.drops[i];
        ctx.strokeStyle = `rgba(185, 222, 232, ${d.alpha})`;
        ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(d.x - d.len * 0.18, d.y + d.len); ctx.stroke();
        d.y += d.speed; d.x -= d.speed * 0.18;
        if (d.y > canvas.height || d.x < -20) this.drops[i] = this._drop(false);
      }
      requestAnimationFrame(() => this._loop());
    }
  }

  const RAIN_BY_MOOD = { rain_heavy: 1, tension: 0.75, rain_indoor: 0.45, quiet: 0.25, dawn: 0.06 };

  // ---------------------------------------------------------------- 状态与元素
  const G = {
    sid: null, scene: null, meta: null,
    token: 0,          // 场景令牌:异步回调只处理当前场景
    es: null,          // 当前 EventSource
    watchdog: null,
    busy: false, choicesShown: false,
    voice: store.get("voice", false),
  };
  const narrationTW = new Typewriter($("narration"));
  const epilogueTW = new Typewriter($("epilogueText"));
  const rain = new Rain($("rain"));
  const flashEl = $("flash");
  const ambience = new window.Ambience(() => {
    flashEl.classList.remove("on"); void flashEl.offsetWidth; flashEl.classList.add("on");
  });
  ambience.muted = store.get("sound", true) === false;

  // ---------------------------------------------------------------- 小工具
  let toastTimer = null;
  function toast(message, kind = "error", ms = 3800) {
    const el = $("toast");
    el.textContent = message; el.className = kind === "info" ? "info" : "";
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }
  const show = (el, on = true) => el.classList.toggle("hidden", !on);

  function closeStream() {
    clearTimeout(G.watchdog);
    if (G.es) { G.es.close(); G.es = null; }
  }

  function speak(text) {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    if (!G.voice || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN"; u.rate = 0.95; u.pitch = 0.95;
    const voice = window.speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith("zh"));
    if (voice) u.voice = voice;
    window.speechSynthesis.speak(u);
  }

  function setBackground(url) {
    const a = $("bgA"), b = $("bgB");
    const next = a.classList.contains("show") ? b : a, prev = next === a ? b : a;
    const apply = (ok) => {
      next.classList.toggle("fallback", !ok);
      next.style.backgroundImage = ok ? `url("${url}")` : "";
      next.style.animation = "none"; void next.offsetHeight; next.style.animation = "";
      next.classList.add("show"); prev.classList.remove("show");
    };
    if (!url) { apply(false); return; }
    const img = new Image();
    img.onload = () => apply(true);
    img.onerror = () => apply(false);
    img.src = url;
  }

  // ---------------------------------------------------------------- 场景呈现
  function showScene(scene) {
    const token = ++G.token;
    G.scene = scene; G.busy = false; G.choicesShown = false;
    closeStream();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();

    $("time").textContent = scene.time;
    $("place").textContent = scene.place;
    $("sceneTitle").textContent = scene.title;
    setBackground(scene.image);
    ambience.setMood(scene.mood);
    rain.setIntensity(RAIN_BY_MOOD[scene.mood] ?? 0.4);

    $("choices").replaceChildren();
    show($("act"), false); show($("reaction"), false); show($("notice"), false);
    show($("loading"), true);
    $("panel").scrollTop = 0;

    narrationTW.reset(() => onNarrationDone(token));
    startNarration(token, scene);
  }

  function startNarration(token, scene) {
    const authored = scene.paragraphs.join("\n\n");
    let gotFirst = false;

    const degrade = (message) => {
      if (token !== G.token) return;
      closeStream(); show($("loading"), false);
      if (message) notice(message);
      narrationTW.set(authored); narrationTW.end();
    };

    const es = new EventSource(`/api/narrate/${G.sid}`);
    G.es = es;
    G.watchdog = setTimeout(() => { if (!gotFirst) degrade("AI 旁白响应过慢,已切换为预写版本"); }, NARRATION_FIRST_EVENT_TIMEOUT);

    const parse = (e) => { gotFirst = true; clearTimeout(G.watchdog); show($("loading"), false); return JSON.parse(e.data); };
    es.addEventListener("delta", (e) => { if (token === G.token) narrationTW.push(parse(e).text); });
    es.addEventListener("replace", (e) => {
      if (token !== G.token) return;
      const d = parse(e);
      narrationTW.set(d.paragraphs.join("\n\n"));
      if (d.reason && d.reason !== "ai_disabled") notice("AI 旁白暂时不可用,已使用预写版本");
    });
    es.addEventListener("done", (e) => {
      if (token !== G.token) return;
      const d = parse(e);
      closeStream(); narrationTW.end();
      $("sceneTitle").textContent = `${scene.title}  ·  ${d.source === "ai" ? "AI 旁白" : "预写旁白"}`;
    });
    es.onerror = () => { if (G.es === es) degrade("旁白连接中断,已切换为预写版本"); };
  }

  function notice(message) { const el = $("notice"); el.textContent = message; show(el, true); }

  function onNarrationDone(token) {
    if (token !== G.token) return;
    show($("loading"), false);
    renderChoices(G.scene);
    speak(narrationTW.buffer);
  }

  // ---------------------------------------------------------------- 选择
  function renderChoices(scene) {
    G.choicesShown = true;
    const box = $("choices");
    box.replaceChildren();

    if (scene.ending) {
      box.append(makeChoiceButton(0, { label: "落幕 · 查看这一夜的结局", locked: false }, () => openEnding(scene)));
      return;
    }
    scene.choices.forEach((c, i) => {
      box.append(makeChoiceButton(i, c, () => choose(c.id)));
    });
    show($("act"), Boolean(G.meta && G.meta.ai_enabled));
  }

  function makeChoiceButton(index, choice, onClick) {
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "choice" + (choice.locked ? " locked" : "");
    btn.style.animationDelay = `${index * 0.12}s`;
    const key = document.createElement("span"); key.className = "key"; key.textContent = String(index + 1);
    const label = document.createElement("span");
    label.textContent = choice.label;
    if (choice.locked) {
      btn.disabled = true;
      const lock = document.createElement("span"); lock.className = "lock"; lock.textContent = `🔒 ${choice.lock_hint}`;
      label.append(lock);
    } else {
      btn.addEventListener("click", onClick);
    }
    btn.append(key, label);
    return btn;
  }

  async function choose(choiceId) {
    if (G.busy) return;
    G.busy = true;
    document.querySelectorAll(".choice").forEach((b) => { b.disabled = true; });
    try {
      const res = await api("/api/choose", { session_id: G.sid, choice_id: choiceId });
      showScene(res.scene);
    } catch (err) { handleActionError(err); }
  }

  function handleActionError(err) {
    if (err.status === 404) {
      toast("这个夜晚的存档已失效(服务可能重启过),请重新开始。");
      store.remove("sid"); backToTitle();
      return;
    }
    G.busy = false;
    document.querySelectorAll(".choice:not(.locked)").forEach((b) => { b.disabled = false; });
    toast(err.message || "出了点问题,请重试");
  }

  document.addEventListener("keydown", (e) => {
    if (!G.choicesShown || G.busy || e.target.tagName === "INPUT") return;
    const n = Number(e.key);
    if (!Number.isInteger(n) || n < 1) return;
    const btn = document.querySelectorAll(".choice")[n - 1];
    if (btn && !btn.disabled) btn.click();
  });

  // ---------------------------------------------------------------- 自由行动(AI 导演)
  $("act").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("actInput"), text = input.value.trim();
    if (!text || G.busy) return;
    G.busy = true;
    const button = $("act").querySelector("button"); button.disabled = true;
    const reaction = $("reaction"); reaction.textContent = "导演正在理解你的意图……"; show(reaction, true);
    try {
      const res = await api("/api/act", { session_id: G.sid, text });
      input.value = "";
      reaction.textContent = res.reply;
      if (res.matched && res.scene) {
        document.querySelectorAll(".choice").forEach((b) => { b.disabled = true; });
        setTimeout(() => showScene(res.scene), 1700);
      } else {
        G.busy = false;
      }
    } catch (err) {
      show(reaction, false);
      if (err.status === 429) { toast("慢一点,让故事喘口气。", "info", 2000); G.busy = false; }
      else if (err.status === 409) { G.busy = false; }
      else handleActionError(err);
    } finally {
      button.disabled = false;
    }
  });

  // ---------------------------------------------------------------- 结局
  const KIND_LABEL = { good: "好结局", ok: "普通结局", bad: "坏结局" };

  function openEnding(scene) {
    const end = scene.ending;
    const unlocked = new Set(store.get("endings", []));
    unlocked.add(end.id); store.set("endings", [...unlocked]);

    $("endingKind").textContent = KIND_LABEL[end.kind] || "结局";
    $("endingKind").className = `eyebrow kind-${end.kind}`;
    $("endingName").textContent = end.name;
    $("endingSubtitle").textContent = end.subtitle;
    $("recapList").replaceChildren(...scene.recap.map((m) => { const li = document.createElement("li"); li.textContent = m; return li; }));
    renderGallery(end.id);
    show($("ending"), true);
    startEpilogue();
  }

  function renderGallery(currentId) {
    const unlocked = new Set(store.get("endings", []));
    const slots = G.meta.endings.map((e) => {
      const el = document.createElement("span");
      const got = unlocked.has(e.id);
      el.className = "slot" + (got ? " got" : "") + (e.id === currentId ? " now" : "");
      el.textContent = got ? `${KIND_LABEL[e.kind]} · ${e.name}` : "？？？ 未解锁";
      return el;
    });
    $("gallery").replaceChildren(...slots);
  }

  function startEpilogue() {
    const label = $("epilogueSource");
    label.textContent = "";
    epilogueTW.reset();
    const es = new EventSource(`/api/epilogue/${G.sid}`);
    const finish = (source) => { es.close(); epilogueTW.end(); label.textContent = source === "ai" ? "· AI 生成" : "· 预写"; };
    es.addEventListener("delta", (e) => epilogueTW.push(JSON.parse(e.data).text));
    es.addEventListener("replace", (e) => epilogueTW.set(JSON.parse(e.data).paragraphs.join("\n\n")));
    es.addEventListener("done", (e) => finish(JSON.parse(e.data).source));
    es.onerror = () => {
      es.close();
      if (!epilogueTW.buffer) epilogueTW.set("(后日谈暂时无法生成。)");
      epilogueTW.end();
    };
  }

  $("btnAgain").addEventListener("click", () => { show($("ending"), false); startNewGame(); });

  // ---------------------------------------------------------------- 标题页与流程
  function backToTitle() {
    closeStream(); G.token++;
    show($("ending"), false); show($("hud"), false); show($("panel"), false); show($("title"), true);
    refreshTitle();
  }

  async function beginAudio() {
    try { await ambience.start(); } catch (_) { /* 浏览器可能拒绝自动播放,用户再点音效按钮即可 */ }
  }

  async function startNewGame() {
    $("btnStart").disabled = true;
    await beginAudio();
    try {
      const res = await api("/api/session", {});
      G.sid = res.session_id; store.set("sid", G.sid);
      enterGame(res.scene);
    } catch (err) {
      toast(err.message);
    } finally {
      $("btnStart").disabled = false;
    }
  }

  async function continueGame() {
    await beginAudio();
    try {
      const res = await api(`/api/session/${store.get("sid", "")}`);
      G.sid = res.session_id; enterGame(res.scene);
    } catch (_) {
      store.remove("sid"); toast("上次的存档已失效,请重新开始。"); refreshTitle();
    }
  }

  function enterGame(scene) {
    show($("title"), false); show($("hud"), true); show($("panel"), true);
    showScene(scene);
  }

  async function refreshTitle() {
    try {
      G.meta = await api("/api/meta");
    } catch (err) {
      $("aiBadge").textContent = "服务未连接"; $("aiBadge").className = "badge warn"; return;
    }
    const m = G.meta;
    $("gameTitle").textContent = m.title;
    $("tagline").textContent = m.tagline;
    const ai = $("aiBadge");
    ai.textContent = m.ai_enabled ? "AI 在线 · 实时旁白 + 自由行动" : "离线模式 · 预写剧情(未配置 API Key)";
    ai.className = "badge " + (m.ai_enabled ? "ok" : "warn");
    $("durationBadge").textContent = `约 ${m.estimated_minutes} 分钟`;
    const got = store.get("endings", []).length;
    $("endingsBadge").textContent = `结局 ${got} / ${m.endings.length}`;
    $("btnStart").disabled = false;
    show($("btnContinue"), Boolean(store.get("sid", "")));
  }

  // ---------------------------------------------------------------- 顶栏按钮
  function syncToolButtons() {
    $("btnSound").setAttribute("aria-pressed", String(!ambience.muted));
    $("btnVoice").setAttribute("aria-pressed", String(G.voice));
  }
  $("btnSound").addEventListener("click", async () => {
    ambience.setMuted(!ambience.muted); store.set("sound", !ambience.muted);
    if (!ambience.muted) await beginAudio();
    syncToolButtons();
  });
  $("btnVoice").addEventListener("click", () => {
    G.voice = !G.voice; store.set("voice", G.voice); syncToolButtons();
    if (G.voice && G.choicesShown) speak(narrationTW.buffer); else speak("");
  });
  $("btnSkip").addEventListener("click", () => {
    if (!G.scene || G.choicesShown) return;
    if (G.es) {  // AI 仍在生成:放弃本次生成,直接展示预写全文
      closeStream(); show($("loading"), false);
      narrationTW.set(G.scene.paragraphs.join("\n\n")); narrationTW.end();
    }
    narrationTW.flush();
  });

  $("btnStart").addEventListener("click", startNewGame);
  $("btnContinue").addEventListener("click", continueGame);
  syncToolButtons();
  refreshTitle();
})();
