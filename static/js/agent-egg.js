// Agent Easter Egg — a small agent portrait hiding at the very bottom of the
// page, just peeking above the edge. Only shows itself in full on hover.
// Click it for $100, once per 24h. Self-contained: include with a single
// <script src="/static/js/agent-egg.js"></script>, no other setup.
(function () {
    const AGENT_IMG = '/static/images/agents/5207.webp';

    function injectStyles() {
        const style = document.createElement('style');
        style.textContent = `
            #agentEggMount {
                position: relative; width: 100%; height: 18px;
                overflow: hidden; margin-top: 6px; pointer-events: none;
            }
            #agentEggWidget {
                position: absolute; bottom: -32px; right: 24px; z-index: 5;
                cursor: pointer; width: 48px; height: 48px;
                pointer-events: auto;
                transition: transform .25s ease;
            }
            #agentEggWidget:hover { transform: translateY(-34px); }
            #agentEggWidget.claimed { opacity: 0.4; }
            #agentEggWidget img {
                width: 100%; height: 100%; border-radius: 50%; object-fit: cover;
                box-shadow: 0 2px 12px rgba(0,0,0,0.6);
                border: 2px solid rgba(255,215,0,0.5);
            }
            #agentEggToast {
                position: fixed; bottom: 16px; right: 16px; z-index: 9999;
                background: #1a1a2e; border: 1px solid #ffd700; color: #ffd700;
                padding: 10px 14px; border-radius: 8px; font-size: 13px;
                font-family: 'Orbitron', 'Segoe UI', sans-serif;
                box-shadow: 0 4px 20px rgba(0,0,0,0.5); max-width: 220px;
                opacity: 0; transition: opacity .3s;
            }
            #agentEggToast.show { opacity: 1; }
        `;
        document.head.appendChild(style);
    }

    function showToast(msg) {
        let toast = document.getElementById('agentEggToast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'agentEggToast';
            document.body.appendChild(toast);
        }
        toast.textContent = msg;
        toast.classList.add('show');
        clearTimeout(toast._hideTimer);
        toast._hideTimer = setTimeout(() => toast.classList.remove('show'), 3500);
    }

    async function claim(widget) {
        try {
            const res = await fetch('/api/agent-egg/claim', { method: 'POST', credentials: 'include' });
            if (res.status === 401) {
                showToast('Log in to claim this — $100 is waiting!');
                return;
            }
            const data = await res.json();
            if (!res.ok) {
                showToast(data.detail || data.error || 'Already claimed today — come back tomorrow!');
                widget.classList.add('claimed');
                return;
            }
            showToast(`Secret Agent found! +$${data.reward} & ${data.tickets} tickets — don't blow it all in one place!`);
            widget.classList.add('claimed');
        } catch (e) {
            showToast('Something went wrong — try again later.');
        }
    }

    function init() {
        injectStyles();
        const mount = document.createElement('div');
        mount.id = 'agentEggMount';
        const widget = document.createElement('div');
        widget.id = 'agentEggWidget';
        widget.title = 'Psst...';
        widget.innerHTML = `<img src="${AGENT_IMG}" alt="" onerror="this.parentElement.style.display='none'">`;
        widget.addEventListener('click', () => claim(widget));
        mount.appendChild(widget);
        document.body.appendChild(mount);

        fetch('/api/agent-egg/status', { credentials: 'include' })
            .then(res => res.ok ? res.json() : null)
            .then(data => { if (data && data.claimed_today) widget.classList.add('claimed'); })
            .catch(() => {});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
