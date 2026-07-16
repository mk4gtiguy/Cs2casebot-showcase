// ── CS2CaseBot Standardized Autoplay ─────────────────────────
// Include after sound.js. Usage:
//   const ap = new AutoPlay({ onRound: async () => { ... }, onStop: reason => { ... } });
//   ap.start(count, { stopOnLoss: true, stopOnWin: false, stopAtMultiplier: 0, stopOnBalanceChange: false });
//   ap.stop();
// ────────────────────────────────────────────────────────────

class AutoPlay {
  constructor(opts = {}) {
    this._running = false;
    this._remaining = 0;
    this._roundDelay = opts.roundDelay || 600;
    this._onRound = opts.onRound || (() => {});
    this._onStop = opts.onStop || (() => {});
    this._stopReasons = {};
    this._balance = opts.initialBalance || 0;
    this._origBalance = this._balance;
    this._el = null;
  }

  get running() { return this._running; }

  setBalance(b) {
    this._balance = b;
    if (this._origBalance === 0) this._origBalance = b;
  }

  start(count = 10, opts = {}) {
    if (this._running) return;
    this._running = true;
    this._remaining = count;
    this._origBalance = this._balance;
    this._stopReasons = {
      stopOnLoss: !!opts.stopOnLoss,
      stopOnWinMultiplier: opts.stopAtMultiplier || 0,
      stopOnBalanceIncrease: !!opts.stopOnBalanceInc,
      stopOnBalanceDecrease: !!opts.stopOnBalanceDec,
    };
    this._run();
  }

  stop(reason) {
    if (!this._running) return;
    this._running = false;
    this._remaining = 0;
    try { this._onStop(reason || 'manual'); } catch (_) {}
    this._updateUI();
  }

  async _run() {
    while (this._running && this._remaining > 0) {
      try {
        const preBalance = this._balance;
        const result = await this._onRound();
        if (!this._running) break;
        this._remaining--;
        this._updateUI();

        if (this._stopReasons.stopOnLoss && result && result.win === false) {
          this.stop('loss');
          return;
        }
        if (this._stopReasons.stopOnWinMultiplier > 0 && result && result.multiplier && result.multiplier >= this._stopReasons.stopOnWinMultiplier) {
          this.stop('target_mult');
          return;
        }
        if (this._stopReasons.stopOnBalanceIncrease && this._balance > preBalance) {
          this.stop('balance_up');
          return;
        }
        if (this._stopReasons.stopOnBalanceDecrease && this._balance < preBalance) {
          this.stop('balance_down');
          return;
        }

        if (this._remaining > 0) await this._delay(this._roundDelay);
      } catch (e) {
        this.stop('error');
        return;
      }
    }
    if (this._remaining <= 0) this.stop('complete');
  }

  bindUI(containerEl, opts = {}) {
    this._el = containerEl;
    const count = opts.defaultRounds || 10;
    const html =
      '<div class="autoplay-panel open" id="apPanel">' +
        '<div class="ap-row">' +
          '<label>ROUNDS</label>' +
          '<input type="number" id="apCount" value="' + count + '" min="1" max="999">' +
          '<button class="ap-btn" id="apStartBtn">▶ AUTO</button>' +
          '<button class="ap-btn" id="apStopBtn" style="display:none;">⏹ STOP</button>' +
        '</div>' +
        '<div class="ap-row">' +
          '<label><input type="checkbox" id="apStopLoss" checked> Stop on loss</label>' +
          '<label><input type="checkbox" id="apStopWinMulti"> Stop at</label>' +
          '<input type="number" id="apStopMult" value="2" min="1.1" step="0.1" style="width:50px;display:none;">' +
          '<label style="font-size:9px;">×</label>' +
        '</div>' +
        '<div class="ap-row">' +
          '<label><input type="checkbox" id="apStopBalUp"> Stop on win +</label>' +
          '<label><input type="checkbox" id="apStopBalDown"> Stop on loss -</label>' +
        '</div>' +
        '<div class="ap-status" id="apStatus">AUTO · <span id="apRemain">' + count + '</span> ROUNDS REMAINING</div>' +
      '</div>';
    containerEl.innerHTML = html;

    const apCount = containerEl.querySelector('#apCount');
    const apStartBtn = containerEl.querySelector('#apStartBtn');
    const apStopBtn = containerEl.querySelector('#apStopBtn');
    const apStatus = containerEl.querySelector('#apStatus');
    const apRemain = containerEl.querySelector('#apRemain');
    const apStopLoss = containerEl.querySelector('#apStopLoss');
    const apStopWinMulti = containerEl.querySelector('#apStopWinMulti');
    const apStopMult = containerEl.querySelector('#apStopMult');
    const apStopBalUp = containerEl.querySelector('#apStopBalUp');
    const apStopBalDown = containerEl.querySelector('#apStopBalDown');

    apStopWinMulti.addEventListener('change', () => {
      apStopMult.style.display = apStopWinMulti.checked ? '' : 'none';
    });

    const updateUI = () => {
      apStatus.classList.toggle('running', this._running);
      apStartBtn.style.display = this._running ? 'none' : '';
      apStopBtn.style.display = this._running ? '' : 'none';
      apRemain.textContent = this._remaining;
      apCount.disabled = this._running;
      apStopLoss.disabled = this._running;
      apStopWinMulti.disabled = this._running;
      apStopMult.disabled = this._running;
      apStopBalUp.disabled = this._running;
      apStopBalDown.disabled = this._running;
    };

    const origStop = this.stop.bind(this);
    this.stop = (reason) => {
      origStop(reason);
      updateUI();
    };

    apStartBtn.addEventListener('click', () => {
      const rounds = parseInt(apCount.value) || 10;
      this._balance = opts.getBalance ? opts.getBalance() : this._balance;
      this._origBalance = this._balance;
      this.start(rounds, {
        stopOnLoss: apStopLoss.checked,
        stopAtMultiplier: apStopWinMulti.checked ? (parseFloat(apStopMult.value) || 2) : 0,
        stopOnBalanceInc: apStopBalUp.checked,
        stopOnBalanceDec: apStopBalDown.checked,
      });
      updateUI();
    });

    apStopBtn.addEventListener('click', () => {
      this.stop('manual');
      updateUI();
    });

    this._updateUI = updateUI;
    updateUI();
  }

  _updateUI() {
    if (this._updateUI) this._updateUI();
  }

  _delay(ms) { return new Promise(r => setTimeout(r, ms)); }
}

// ── Auto-inject volume slider + ambient toggle into game page headers ──
(function(){
  function inject() {
    var hdr = document.querySelector('.hdr');
    if (!hdr || hdr.querySelector('.vol-wrap')) return;

    var bal = hdr.querySelector('.bal');
    if (!bal) return;

    var wrap = document.createElement('div');
    wrap.className = 'vol-wrap';

    var ambBtn = document.createElement('button');
    ambBtn.className = 'vol-toggle';
    ambBtn.id = 'ambToggle';
    ambBtn.textContent = '🎵';
    ambBtn.title = 'Casino ambient';
    ambBtn.onclick = function(){
      var active = Sound.Ambient.toggle();
      ambBtn.classList.toggle('active', active);
    };

    var slider = document.createElement('input');
    slider.type = 'range';
    slider.min = 0;
    slider.max = 100;
    slider.value = (Sound.volume * 100).toFixed(0);
    slider.title = 'Volume';
    slider.oninput = function(){ Sound.volume = this.value / 100; };

    var sndBtn = document.createElement('button');
    sndBtn.className = 'vol-toggle';
    sndBtn.id = 'sndToggleInjected';
    sndBtn.textContent = '🔊';
    sndBtn.title = 'Toggle sound';
    sndBtn.onclick = function(){
      var muted = Sound.toggle();
      sndBtn.textContent = muted ? '🔇' : '🔊';
      sndBtn.classList.toggle('muted', muted);
    };

    wrap.appendChild(ambBtn);
    wrap.appendChild(slider);
    wrap.appendChild(sndBtn);
    hdr.insertBefore(wrap, bal);
  }

  // Start ambient particles on all game pages (always-on backdrop)
  function startParticles() {
    if (Sound.Particles && !Sound.Particles.running) {
      Sound.Particles.start();
    }
  }

  // On first user interaction, offer to start ambient too
  function onFirstInteraction() {
    document.removeEventListener('click', onFirstInteraction);
    document.removeEventListener('touchstart', onFirstInteraction);
    startParticles();
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    inject();
    startParticles();
    document.addEventListener('click', onFirstInteraction, { once: true });
    document.addEventListener('touchstart', onFirstInteraction, { once: true });
  } else {
    document.addEventListener('DOMContentLoaded', function(){
      inject();
      startParticles();
      document.addEventListener('click', onFirstInteraction, { once: true });
      document.addEventListener('touchstart', onFirstInteraction, { once: true });
    });
  }
})();
