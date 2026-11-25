/* 环境音效引擎:用 WebAudio 实时合成雨声 / 雷声 / 低频氛围和弦。
 * 全程序化生成,不依赖任何音频素材文件(原创、无版权风险)。
 * 不同场景 mood 对应不同的"调音预设",切换时平滑过渡。 */
(function () {
  "use strict";

  // rain: 雨声音量; rainLP: 雨声低通截止(越低越像隔着窗); rumble: 低频轰鸣
  // pad: 氛围和弦音量; chord: 和弦频率; lfo/depth: 氛围脉动的速度与深度; thunder: 雷声间隔[min,max]秒
  const MOODS = {
    rain_heavy:  { rain: 0.20, rainLP: 6500, rumble: 0.30, pad: 0.020, chord: [55, 82.4, 110],    lfo: 0.12, depth: 0.004, thunder: [8, 16] },
    rain_indoor: { rain: 0.09, rainLP: 1700, rumble: 0.18, pad: 0.026, chord: [55, 82.4, 98],     lfo: 0.10, depth: 0.005, thunder: [14, 26] },
    tension:     { rain: 0.13, rainLP: 3200, rumble: 0.28, pad: 0.046, chord: [55, 58.3, 87.3],   lfo: 1.10, depth: 0.020, thunder: [10, 18] },
    quiet:       { rain: 0.06, rainLP: 1400, rumble: 0.10, pad: 0.032, chord: [110, 164.8, 220],  lfo: 0.08, depth: 0.005, thunder: null },
    dawn:        { rain: 0.025, rainLP: 1100, rumble: 0.04, pad: 0.042, chord: [220, 277.2, 329.6], lfo: 0.06, depth: 0.006, thunder: null },
  };

  class Ambience {
    constructor(onThunder) {
      this.ctx = null;
      this.muted = false;
      this.mood = "rain_indoor";
      this.onThunder = onThunder || function () {};
      this._thunderTimer = null;
    }

    async start() {
      if (!this.ctx) this._build();
      if (this.ctx.state === "suspended") await this.ctx.resume();
      this._apply(this.mood, 1.5);
      this._scheduleThunder();
    }

    _build() {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = (this.ctx = new AC());

      // 2 秒白噪声缓冲,循环播放作为雨声/轰鸣的声源
      const len = ctx.sampleRate * 2;
      const buf = ctx.createBuffer(1, len, ctx.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;
      this.noiseBuf = buf;
      const noise = () => {
        const s = ctx.createBufferSource();
        s.buffer = buf; s.loop = true; s.start();
        return s;
      };

      this.master = ctx.createGain();
      this.master.gain.value = this.muted ? 0 : 0.9;
      this.master.connect(ctx.destination);

      // 雨声:噪声 → 高通 → 低通 → 增益
      this.rainHP = ctx.createBiquadFilter(); this.rainHP.type = "highpass"; this.rainHP.frequency.value = 450;
      this.rainLP = ctx.createBiquadFilter(); this.rainLP.type = "lowpass"; this.rainLP.frequency.value = 3000;
      this.rainGain = ctx.createGain(); this.rainGain.gain.value = 0;
      noise().connect(this.rainHP); this.rainHP.connect(this.rainLP); this.rainLP.connect(this.rainGain); this.rainGain.connect(this.master);

      // 低频轰鸣:噪声 → 低通
      this.rumbleLP = ctx.createBiquadFilter(); this.rumbleLP.type = "lowpass"; this.rumbleLP.frequency.value = 150;
      this.rumbleGain = ctx.createGain(); this.rumbleGain.gain.value = 0;
      noise().connect(this.rumbleLP); this.rumbleLP.connect(this.rumbleGain); this.rumbleGain.connect(this.master);

      // 氛围和弦:3 个正弦波 + LFO 脉动
      this.padGain = ctx.createGain(); this.padGain.gain.value = 0;
      this.padGain.connect(this.master);
      this.oscs = [0, 1, 2].map((i) => {
        const o = ctx.createOscillator();
        o.type = i === 0 ? "sine" : "triangle";
        o.frequency.value = 55 * (i + 1);
        const g = ctx.createGain(); g.gain.value = 1 / (i + 1.4);
        o.connect(g); g.connect(this.padGain); o.start();
        return o;
      });
      this.lfo = ctx.createOscillator(); this.lfo.frequency.value = 0.1;
      this.lfoDepth = ctx.createGain(); this.lfoDepth.gain.value = 0.004;
      this.lfo.connect(this.lfoDepth); this.lfoDepth.connect(this.padGain.gain); this.lfo.start();
    }

    _apply(moodName, seconds) {
      const m = MOODS[moodName] || MOODS.rain_indoor;
      if (!this.ctx) return;
      const t = this.ctx.currentTime, k = seconds / 3;
      this.rainGain.gain.setTargetAtTime(m.rain, t, k);
      this.rainLP.frequency.setTargetAtTime(m.rainLP, t, k);
      this.rumbleGain.gain.setTargetAtTime(m.rumble, t, k);
      this.padGain.gain.setTargetAtTime(m.pad, t, k);
      m.chord.forEach((f, i) => this.oscs[i].frequency.setTargetAtTime(f, t, k));
      this.lfo.frequency.setTargetAtTime(m.lfo, t, k);
      this.lfoDepth.gain.setTargetAtTime(m.depth, t, k);
    }

    setMood(name) {
      this.mood = MOODS[name] ? name : "rain_indoor";
      this._apply(this.mood, 2.2);
      this._scheduleThunder();
    }

    setMuted(muted) {
      this.muted = muted;
      if (this.master) this.master.gain.setTargetAtTime(muted ? 0 : 0.9, this.ctx.currentTime, 0.15);
    }

    _scheduleThunder() {
      clearTimeout(this._thunderTimer);
      const range = (MOODS[this.mood] || {}).thunder;
      if (!range || !this.ctx) return;
      const wait = (range[0] + Math.random() * (range[1] - range[0])) * 1000;
      this._thunderTimer = setTimeout(() => { this.thunder(); this._scheduleThunder(); }, wait);
    }

    thunder() {
      if (!this.ctx || this.muted) { this.onThunder(); return; }
      const ctx = this.ctx, t = ctx.currentTime;
      const src = ctx.createBufferSource(); src.buffer = this.noiseBuf;
      const lp = ctx.createBiquadFilter(); lp.type = "lowpass";
      lp.frequency.setValueAtTime(520, t); lp.frequency.exponentialRampToValueAtTime(50, t + 3.6);
      const g = ctx.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.7, t + 0.12);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 4.2);
      src.connect(lp); lp.connect(g); g.connect(this.master);
      src.start(t); src.stop(t + 4.4);
      this.onThunder();
    }

    stop() {
      clearTimeout(this._thunderTimer);
      if (this.ctx) this.ctx.suspend();
    }
  }

  window.Ambience = Ambience;
})();
