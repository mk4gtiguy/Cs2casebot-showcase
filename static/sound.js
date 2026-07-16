// ── CS2CaseBot Procedural Sound Effects, Ambient, Haptic & Particles ────
// Uses Web Audio API + Vibration API — no external files needed.
// ────────────────────────────────────────────────────────────────────────

const Sound = (() => {
  let ctx = null;
  let enabled = true;
  let hapticEnabled = true;
  let _volume = parseFloat(localStorage.getItem('soundVolume')) || 0.3;
  let _ambientGain = null;
  let _ambientNodes = [];
  let _ambientRunning = false;
  let _particleFrame = null;
  let _particleRunning = false;

  function _saveVol() { localStorage.setItem('soundVolume', String(_volume)); }

  function _ctx() {
    if (!ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return null;
      ctx = new C();
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }

  function _osc(type, freq, dur, vol) {
    const c = _ctx();
    if (!c || !enabled) return;
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = type;
    o.frequency.value = freq;
    const v = vol != null ? vol : _volume;
    g.gain.setValueAtTime(v, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
    o.connect(g);
    g.connect(c.destination);
    o.start();
    o.stop(c.currentTime + dur);
  }

  function _noise(dur, vol) {
    const c = _ctx();
    if (!c || !enabled) return;
    const buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    const src = c.createBufferSource();
    src.buffer = buf;
    const g = c.createGain();
    const v = vol != null ? vol : _volume;
    g.gain.setValueAtTime(v, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
    src.connect(g);
    g.connect(c.destination);
    src.start();
  }

  function _chime(freq, dur, vol) {
    const c = _ctx();
    if (!c || !enabled) return;
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = 'sine';
    o.frequency.value = freq;
    const v = vol != null ? vol : _volume;
    g.gain.setValueAtTime(v, c.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + (dur || 0.3));
    const o2 = c.createOscillator();
    const g2 = c.createGain();
    o2.type = 'sine';
    o2.frequency.value = freq * 2.01;
    g2.gain.setValueAtTime(v * 0.3, c.currentTime);
    g2.gain.exponentialRampToValueAtTime(0.001, c.currentTime + (dur || 0.3));
    o.connect(g); g.connect(c.destination);
    o2.connect(g2); g2.connect(c.destination);
    o.start(); o2.start();
    o.stop(c.currentTime + (dur || 0.3));
    o2.stop(c.currentTime + (dur || 0.3));
  }

  // ── Haptic ────────────────────────────────────────────────
  function _vibrate(pattern) {
    if (!hapticEnabled) return;
    try { navigator.vibrate(pattern); } catch (_) {}
  }

  const Haptic = {
    set enabled(v) { hapticEnabled = v; },
    get enabled() { return hapticEnabled; },
    click()    { _vibrate(10); },
    win()      { _vibrate([30, 40, 30, 40, 60]); },
    bigWin()   { _vibrate([50, 60, 50, 60, 50, 80, 100]); },
    jackpot()  { _vibrate([100, 80, 100, 80, 100, 120, 150]); },
    loss()     { _vibrate([100, 50, 80, 50, 60]); },
    coinflip() { _vibrate([5, 10, 5, 10, 5, 10, 5, 10, 5, 20]); },
    spin()     { _vibrate(30); },
    reelStop() { _vibrate(15); },
    card()     { _vibrate(8); },
    chip()     { _vibrate(12); },
    cashout()  { _vibrate([20, 30, 40]); },
    explosion(){ _vibrate([100, 50, 80]); },
    reveal()   { _vibrate(10); },
    tick()     { _vibrate(5); },
    error()    { _vibrate([20, 30, 20]); },
    levelup()  { _vibrate([20, 30, 20, 30, 40]); },
    beep()     { _vibrate(15); },
    whoosh()   { _vibrate(20); },
    roulette() { _vibrate(40); },
    dice()     { _vibrate([5, 10, 5, 10, 5, 10]); },
    climbTick(){ _vibrate(3); },
    crash()    { _vibrate([60, 40, 60]); },
    shoot()    { _vibrate([20, 10]); },
    alert()    { _vibrate([30, 30, 30]); },
    pop()      { _vibrate(8); },
    step()     { _vibrate(10); },
    custom(p)  { _vibrate(p); },
    disable()  { hapticEnabled = false; },
    enable()   { hapticEnabled = true; },
    toggle()   { hapticEnabled = !hapticEnabled; return hapticEnabled; },
  };

  // ── Ambient Casino Loop ─────────────────────────────────
  function _createBiquad(type, freq, Q) {
    const c = _ctx(); if (!c) return null;
    const f = c.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    if (Q != null) f.Q.value = Q;
    return f;
  }

  function _ambientHum() {
    const c = _ctx(); if (!c) return null;
    const buf = c.createBuffer(1, c.sampleRate * 4, c.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    const src = c.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    const bp = _createBiquad('lowpass', 160, 0.5);
    const hp = _createBiquad('highpass', 50);
    const g = c.createGain();
    g.gain.value = 0;
    src.connect(bp); bp.connect(hp); hp.connect(g); g.connect(_ambientGain);
    _ambientNodes.push(src, bp, hp, g);
    return { src, gain: g, target: 0.045 };
  }

  function _ambientClicks() {
    const c = _ctx(); if (!c) return null;
    const g = c.createGain();
    g.gain.value = 0;
    g.connect(_ambientGain);
    _ambientNodes.push(g);
    let timeout = null;
    function schedule() {
      if (!_ambientRunning) return;
      const delay = 800 + Math.random() * 3000;
      timeout = setTimeout(() => {
        if (!_ambientRunning) return;
        const buf = c.createBuffer(1, c.sampleRate * 0.06, c.sampleRate);
        const d = buf.getChannelData(0);
        for (let i = 0; i < d.length; i++) {
          const t = i / c.sampleRate;
          d[i] = (Math.random() - 0.5) * Math.exp(-t * 80) * Math.sin(t * 8000);
        }
        const src = c.createBufferSource();
        src.buffer = buf;
        const clickG = c.createGain();
        clickG.gain.setValueAtTime(0.15 * _volume, c.currentTime);
        clickG.gain.exponentialRampToValueAtTime(0.001, c.currentTime + 0.06);
        src.connect(clickG); clickG.connect(g);
        src.start();
        schedule();
      }, delay);
    }
    schedule();
    return { stop: () => { if (timeout) clearTimeout(timeout); } };
  }

  function _ambientSlotHum() {
    const c = _ctx(); if (!c) return null;
    const g = c.createGain();
    g.gain.value = 0;
    g.connect(_ambientGain);
    _ambientNodes.push(g);
    let timeout = null;
    function schedule() {
      if (!_ambientRunning) return;
      const delay = 4000 + Math.random() * 8000;
      timeout = setTimeout(() => {
        if (!_ambientRunning) return;
        const dur = 0.8 + Math.random() * 1.5;
        const buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
        const d = buf.getChannelData(0);
        for (let i = 0; i < d.length; i++) {
          const t = i / c.sampleRate;
          d[i] = (Math.random() - 0.5) * Math.sin(t * (200 + Math.sin(t * 20) * 100));
          d[i] *= Math.max(0, 1 - t / dur) * 0.5;
        }
        const src = c.createBufferSource();
        src.buffer = buf;
        const slotG = c.createGain();
        const bp = _createBiquad('bandpass', 800, 1);
        slotG.gain.setValueAtTime(0.08 * _volume, c.currentTime);
        slotG.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
        src.connect(bp); bp.connect(slotG); slotG.connect(g);
        src.start();
        schedule();
      }, delay);
    }
    schedule();
    return { stop: () => { if (timeout) clearTimeout(timeout); } };
  }

  function _ambientChatter() {
    const c = _ctx(); if (!c) return null;
    const g = c.createGain();
    g.gain.value = 0;
    g.connect(_ambientGain);
    _ambientNodes.push(g);
    let timeout = null;
    function schedule() {
      if (!_ambientRunning) return;
      const delay = 2000 + Math.random() * 5000;
      timeout = setTimeout(() => {
        if (!_ambientRunning) return;
        const dur = 0.3 + Math.random() * 0.6;
        const buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
        const d = buf.getChannelData(0);
        for (let i = 0; i < d.length; i++) {
          const t = i / c.sampleRate;
          d[i] = Math.random() - 0.5;
          d[i] *= Math.sin(t * (300 + Math.sin(t * 40) * 150)) * 0.6;
          d[i] += (Math.random() - 0.5) * 0.4;
          d[i] *= Math.max(0, 1 - t / dur);
          d[i] *= 0.3;
        }
        const src = c.createBufferSource();
        src.buffer = buf;
        const talkG = c.createGain();
        const hp = _createBiquad('highpass', 400);
        const bp = _createBiquad('bandpass', 1200, 2);
        talkG.gain.setValueAtTime(0.04 * _volume, c.currentTime);
        talkG.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
        src.connect(hp); hp.connect(bp); bp.connect(talkG); talkG.connect(g);
        src.start();
        schedule();
      }, delay);
    }
    schedule();
    return { stop: () => { if (timeout) clearTimeout(timeout); } };
  }

  const Ambient = {
    get running() { return _ambientRunning; },

    start() {
      if (_ambientRunning) return;
      const c = _ctx(); if (!c) return;
      _ambientGain = c.createGain();
      _ambientGain.gain.value = 0.35;
      _ambientGain.connect(c.destination);
      _ambientNodes = [_ambientGain];
      _ambientRunning = true;
      _ambientHum();
      _ambientClicks();
      _ambientSlotHum();
      _ambientChatter();
    },

    stop() {
      _ambientRunning = false;
      _ambientNodes.forEach(n => {
        try { if (n.disconnect) n.disconnect(); } catch (_) {}
      });
      _ambientNodes = [];
      _ambientGain = null;
    },

    toggle() {
      if (_ambientRunning) { this.stop(); return false; }
      this.start(); return true;
    }
  };

  // ── Ambient Particles ────────────────────────────────────
  function _makeContainer() {
    let c = document.getElementById('ambient-particles');
    if (c) return c;
    c = document.createElement('div');
    c.id = 'ambient-particles';
    c.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:1;overflow:hidden;';
    document.body.appendChild(c);
    return c;
  }

  function _spawnParticle(container) {
    const el = document.createElement('div');
    const size = 2 + Math.random() * 3;
    const x = Math.random() * 100;
    const dur = 4 + Math.random() * 6;
    const drift = (Math.random() - 0.5) * 40;
    const opacity = 0.2 + Math.random() * 0.4;
    el.style.cssText =
      'position:absolute;left:'+x+'%;bottom:-10px;width:'+size+'px;height:'+size+'px;'+
      'border-radius:50%;background:rgba(255,215,0,'+opacity+');'+
      'box-shadow:0 0 '+(size*2)+'px rgba(255,215,0,'+(opacity*0.5)+');'+
      'animation:particle-float '+dur+'s linear forwards;'+
      '--drift:'+drift+'px;';
    container.appendChild(el);
    setTimeout(() => { try { el.remove(); } catch(_) {} }, dur * 1000 + 100);
  }

  const Particles = {
    get running() { return _particleRunning; },

    start() {
      if (_particleRunning) return;
      _particleRunning = true;
      const style = document.createElement('style');
      style.id = 'particle-anim-style';
      style.textContent =
        '@keyframes particle-float{0%{transform:translateY(0) translateX(0) scale(1);opacity:1}'+
        '100%{transform:translateY(-110vh) translateX(var(--drift)) scale(0);opacity:0}}';
      document.head.appendChild(style);
      const container = _makeContainer();
      let count = 0;
      function tick() {
        if (!_particleRunning) return;
        _spawnParticle(container);
        count++;
        const interval = count % 3 === 0 ? 1200 : 800 + Math.random() * 600;
        _particleFrame = setTimeout(tick, interval);
      }
      tick();
    },

    stop() {
      _particleRunning = false;
      if (_particleFrame) { clearTimeout(_particleFrame); _particleFrame = null; }
      const c = document.getElementById('ambient-particles');
      if (c) c.innerHTML = '';
      const s = document.getElementById('particle-anim-style');
      if (s) s.remove();
    },

    toggle() {
      if (_particleRunning) { this.stop(); return false; }
      this.start(); return true;
    }
  };

  // ── Public API ────────────────────────────────────────────
  return {
    Haptic,
    Ambient,
    Particles,

    /** Current volume level (0-1) */
    get volume() { return _volume; },
    set volume(v) {
      _volume = Math.max(0, Math.min(1, v));
      _saveVol();
    },

    /** Enable/disable all audio */
    set enabled(v) { enabled = v; },
    get enabled() { return enabled; },

    toggle() { enabled = !enabled; return enabled; },

    click()  { _osc('square', 800, 0.06, _volume * 0.3); Haptic.click(); },
    win()    { [523, 659, 784, 1047].forEach((f, i) => setTimeout(() => _chime(f, 0.25, _volume * 0.8), i * 100)); Haptic.win(); },
    bigWin() { [523, 659, 784, 1047, 784, 1047, 1319, 1568].forEach((f, i) => setTimeout(() => _chime(f, 0.3, _volume), i * 120)); Haptic.bigWin(); },
    jackpot(){ [523, 659, 784, 1047, 1319, 1568, 2093].forEach((f, i) => setTimeout(() => _chime(f, 0.4, _volume), i * 150)); setTimeout(() => _noise(0.5, _volume * 0.3), 1050); Haptic.jackpot(); },
    loss()   { [400, 350, 300, 200].forEach((f, i) => setTimeout(() => _osc('sine', f, 0.2, _volume * 0.6), i * 120)); Haptic.loss(); },

    coinflip() {
      const c = _ctx(); if (!c || !enabled) return;
      const dur = 0.8, buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) { const t = i / c.sampleRate; d[i] = (Math.random() - 0.5) * Math.sin(t * 2000 * (1 + t * 3)); }
      const src = c.createBufferSource(); src.buffer = buf;
      const g = c.createGain(); g.gain.setValueAtTime(_volume * 0.4, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
      src.connect(g); g.connect(c.destination); src.start(); Haptic.coinflip();
    },

    spin() {
      const c = _ctx(); if (!c || !enabled) return;
      const dur = 0.4, buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) { const t = i / c.sampleRate; d[i] = (Math.random() - 0.5) * Math.sin(t * 3000 * (1 + t * 5)); }
      const src = c.createBufferSource(); src.buffer = buf;
      const g = c.createGain(); g.gain.setValueAtTime(_volume * 0.3, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
      src.connect(g); g.connect(c.destination); src.start(); Haptic.spin();
    },

    reelStop() { _osc('triangle', 120, 0.08, _volume * 0.5); Haptic.reelStop(); },
    card()     { _noise(0.08, _volume * 0.2); _osc('triangle', 600, 0.06, _volume * 0.3); Haptic.card(); },
    chip()     { _chime(1200, 0.08, _volume * 0.3); Haptic.chip(); },
    cashout()  { [800, 1000, 1200].forEach((f, i) => setTimeout(() => _chime(f, 0.15, _volume * 0.6), i * 80)); Haptic.cashout(); },
    explosion(){ _noise(0.4, _volume * 0.6); _osc('sawtooth', 80, 0.3, _volume * 0.5); Haptic.explosion(); },
    reveal()   { _chime(880, 0.1, _volume * 0.3); Haptic.reveal(); },
    tick()     { _osc('sine', 1000, 0.04, _volume * 0.2); Haptic.tick(); },
    error()    { _osc('square', 200, 0.15, _volume * 0.4); Haptic.error(); },
    levelup()  { [523, 659, 784, 1047, 1319].forEach((f, i) => setTimeout(() => _chime(f, 0.2, _volume * 0.7), i * 80)); Haptic.levelup(); },
    beep()     { _osc('sine', 880, 0.1, _volume * 0.3); Haptic.beep(); },
    whoosh() {
      const c = _ctx(); if (!c || !enabled) return;
      const dur = 0.3, buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) { const t = i / c.sampleRate; d[i] = (Math.random() - 0.5) * Math.sin(t * (200 + t * 3000)); }
      const src = c.createBufferSource(); src.buffer = buf;
      const g = c.createGain(); g.gain.setValueAtTime(_volume * 0.2, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
      src.connect(g); g.connect(c.destination); src.start(); Haptic.whoosh();
    },
    roulette() {
      const c = _ctx(); if (!c || !enabled) return;
      const dur = 1.5, buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) { const t = i / c.sampleRate; d[i] = (Math.random() - 0.5) * Math.sin(t * (800 + t * 2000)); d[i] *= Math.max(0, 1 - t / dur); }
      const src = c.createBufferSource(); src.buffer = buf;
      const g = c.createGain(); g.gain.setValueAtTime(_volume * 0.3, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
      src.connect(g); g.connect(c.destination); src.start(); Haptic.roulette();
    },
    dice() {
      const c = _ctx(); if (!c || !enabled) return;
      const dur = 0.4, buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) { const t = i / c.sampleRate; d[i] = (Math.random() - 0.5) * Math.sin(t * 1500 * (1 + Math.sin(t * 30))); }
      const src = c.createBufferSource(); src.buffer = buf;
      const g = c.createGain(); g.gain.setValueAtTime(_volume * 0.25, c.currentTime); g.gain.exponentialRampToValueAtTime(0.001, c.currentTime + dur);
      src.connect(g); g.connect(c.destination); src.start(); Haptic.dice();
    },
    climbTick() { _osc('sine', 600 + Math.random() * 400, 0.03, _volume * 0.15); Haptic.climbTick(); },
    crash()    { _noise(0.6, _volume * 0.5); _osc('sawtooth', 60, 0.5, _volume * 0.4); Haptic.crash(); },
    shoot()    { _noise(0.15, _volume * 0.4); _osc('square', 150, 0.08, _volume * 0.3); Haptic.shoot(); },
    alert()    { [880, 660].forEach((f, i) => setTimeout(() => _osc('square', f, 0.12, _volume * 0.4), i * 150)); Haptic.alert(); },
    pop()      { _osc('sine', 1400, 0.06, _volume * 0.3); Haptic.pop(); },
    step()     { _osc('triangle', 300, 0.05, _volume * 0.2); Haptic.step(); },
    vipHost()  {
      [523, 659, 784, 1047].forEach((f, i) => {
        setTimeout(() => _osc('sine', f, 0.25, _volume * 0.5), i * 150);
        setTimeout(() => _osc('triangle', f * 0.5, 0.3, _volume * 0.2), i * 150 + 50);
      });
      setTimeout(() => _osc('sine', 1319, 0.5, _volume * 0.4), 600);
      Haptic.alert();
    },
  };
})();
