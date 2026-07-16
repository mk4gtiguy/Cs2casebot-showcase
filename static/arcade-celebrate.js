(function(){'use strict';
// Self-contained, no dependency on dashboard.js's spawnConfetti (not reliably
// available on standalone game pages) -- modeled after big-win.js's own
// self-contained coin-burst pattern, just smaller/shorter/ticket-themed since
// arcade high scores are a lower-stakes, more-frequent event than a cash
// big-win.
var toastEl,hideTimer,gen=0;

function ensureDom(){
  if(toastEl) return;
  toastEl=document.createElement('div');
  toastEl.className='arcade-celebrate-toast';
  toastEl.id='arcadeCelebrateToast';
  document.body.appendChild(toastEl);
}

function spawnTicketBurst(){
  var container=document.createElement('div');
  container.className='arcade-celebrate-burst';
  document.body.appendChild(container);
  var centerX=window.innerWidth/2,centerY=window.innerHeight/2-60;
  for(var i=0;i<24;i++){
    var t=document.createElement('div');
    t.className='arcade-celebrate-ticket';
    t.textContent='🎫';
    var angle=Math.random()*Math.PI*2,dist=100+Math.random()*220;
    t.style.left=centerX+'px';
    t.style.top=centerY+'px';
    t.style.setProperty('--bx',Math.cos(angle)*dist+'px');
    t.style.setProperty('--by',Math.sin(angle)*dist+'px');
    t.style.fontSize=(14+Math.random()*14)+'px';
    t.style.animationDuration=(0.9+Math.random()*0.6)+'s';
    container.appendChild(t);
  }
  setTimeout(function(){container.remove();},1800);
}

// Call when a submit response's is_new_best is true. ticketsWon is optional
// (some tiers award 0 tickets even on a new best, e.g. a slow-but-improving
// reaction time) -- shown only if positive. `title`/`icon` let a pass/fail
// game like Bomb Defuse (no numeric "best" to compare, see LEADERBOARD_
// DIRECTION in ticket_games.py) reuse the same toast/burst for a plain
// "you won" celebration instead of a personal-best one.
function celebrateHighScore(ticketsWon,title,icon){
  ensureDom();
  clearTimeout(hideTimer);
  var myGen=++gen;
  var desc=ticketsWon>0?('+'+ticketsWon+' 🎟️ tickets'):'Keep pushing for tickets!';
  toastEl.innerHTML='<div class="arcade-celebrate-icon">'+(icon||'🏅')+'</div>'+
    '<div class="arcade-celebrate-info">'+
      '<div class="arcade-celebrate-title">'+(title||'NEW PERSONAL BEST!')+'</div>'+
      '<div class="arcade-celebrate-desc">'+desc+'</div>'+
    '</div>';
  toastEl.classList.remove('fade-out');
  toastEl.classList.add('active');
  spawnTicketBurst();
  hideTimer=setTimeout(function(){
    if(myGen!==gen) return;
    toastEl.classList.remove('active');
    toastEl.classList.add('fade-out');
    setTimeout(function(){
      if(myGen!==gen) return;
      toastEl.classList.remove('fade-out');
    },400);
  },1500);
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

// Fetches and renders a top-10 + "your best" leaderboard for one of the 4
// scored arcade games (reaction/aim/float/memory -- Bomb Defuse has no
// leaderboard, it's pass/fail). formatScore(score) turns the raw numeric
// score into this game's display string (e.g. "312ms", "14 hits").
async function renderArcadeLeaderboard(containerEl,gameType,formatScore){
  try{
    const res=await fetch('/api/ticket-games/'+gameType+'/leaderboard',{credentials:'include'});
    if(!res.ok){containerEl.innerHTML='';return;}
    const data=await res.json();
    const rows=(data.leaderboard||[]).map(function(r,i){
      return '<div class="arcade-lb-row"><span class="arcade-lb-rank">#'+(i+1)+'</span>'+
        '<span class="arcade-lb-name">'+escapeHtml(r.username)+'</span>'+
        '<span class="arcade-lb-score">'+formatScore(r.score)+'</span></div>';
    }).join('');
    const yourBest=data.your_best!=null
      ? '<div class="arcade-lb-yours">Your best: '+formatScore(data.your_best)+'</div>' : '';
    containerEl.innerHTML='<div class="arcade-lb-title">🏆 TOP 10</div>'+
      (rows||'<div class="arcade-lb-empty">No scores yet — be the first!</div>')+yourBest;
  }catch(e){containerEl.innerHTML='';}
}

window.celebrateHighScore=celebrateHighScore;
window.renderArcadeLeaderboard=renderArcadeLeaderboard;
})();
