(function(){'use strict';
var overlay,labelEl,numberEl,coinContainer;
var hideTimer1,hideTimer2,gen=0;
var pendingResolve=null;

function ensureDom(){
  if(overlay) return;
  overlay=document.createElement('div');
  overlay.className='big-win-overlay';
  overlay.id='bigWinOverlay';
  labelEl=document.createElement('div');
  labelEl.className='big-win-label';
  labelEl.id='bigWinLabel';
  numberEl=document.createElement('div');
  numberEl.className='big-win-number';
  numberEl.id='bigWinNumber';
  overlay.appendChild(labelEl);
  overlay.appendChild(numberEl);
  coinContainer=document.createElement('div');
  coinContainer.className='big-win-coin-container';
  coinContainer.id='bigWinCoinContainer';
  document.body.appendChild(overlay);
  document.body.appendChild(coinContainer);
}

// Returns a Promise that resolves once the celebration's auto-hide sequence
// completes, so callers (autoplay in particular) can await it instead of
// firing the next round underneath a still-visible overlay.
function showBigWin(value,multiplier){
  ensureDom();

  // Cancel any in-flight hide timers / count-up loop from a previous call so
  // overlapping wins (e.g. rapid autoplay) can't stomp this call's display
  // state after the fact. Resolve the superseded call's promise immediately
  // so an earlier await doesn't hang forever.
  clearTimeout(hideTimer1);
  clearTimeout(hideTimer2);
  if(pendingResolve){var prevResolve=pendingResolve;pendingResolve=null;prevResolve();}
  var myGen=++gen;

  var labelText,labelClass;
  if(multiplier>=50){labelText='💰 JACKPOT!';labelClass='jackpot';}
  else if(multiplier>=20){labelText='🔥 MEGA WIN!';labelClass='mega';}
  else{labelText='⭐ BIG WIN!';labelClass='big';}

  labelEl.textContent=labelText;
  labelEl.className='big-win-label '+labelClass;
  numberEl.textContent='$0.00';

  overlay.style.display='';
  overlay.classList.remove('fade-out');
  overlay.classList.add('active');

  var shakeClass=multiplier>=50?'win-shake-heavy':'win-shake';
  document.body.classList.add(shakeClass);
  setTimeout(function(){document.body.classList.remove(shakeClass);},600);

  var coins=['🪙','💰','💎','⭐','🎉','👑'];
  var centerX=window.innerWidth/2;
  var centerY=window.innerHeight/2-80;
  for(var i=0;i<80;i++){
    var coin=document.createElement('div');
    coin.className='big-win-coin';
    coin.textContent=coins[Math.floor(Math.random()*coins.length)];
    var angle=Math.random()*Math.PI*2;
    var dist=200+Math.random()*500;
    coin.style.left=centerX+'px';
    coin.style.top=centerY+'px';
    coin.style.setProperty('--bx',Math.cos(angle)*dist+'px');
    coin.style.setProperty('--by',Math.sin(angle)*dist+'px');
    coin.style.setProperty('--br',(Math.random()*720-360)+'deg');
    coin.style.fontSize=(16+Math.random()*24)+'px';
    coin.style.animationDuration=(1.5+Math.random()*1.5)+'s';
    coin.style.animationDelay=(Math.random()*0.8)+'s';
    coinContainer.appendChild(coin);
  }

  var duration=Math.min(2000,600+value*0.3);
  var startTime=performance.now();
  function countUp(now){
    var elapsed=now-startTime;
    var progress=Math.min(elapsed/duration,1);
    var eased=1-Math.pow(1-progress,3);
    var current=value*eased;
    if(myGen!==gen) return;
    numberEl.textContent='$'+current.toFixed(2);
    if(progress<1) requestAnimationFrame(countUp);
    else numberEl.textContent='$'+value.toFixed(2);
  }
  requestAnimationFrame(countUp);

  return new Promise(function(resolve){
    pendingResolve=resolve;
    hideTimer1=setTimeout(function(){
      if(myGen!==gen) return;
      overlay.classList.remove('active');
      overlay.classList.add('fade-out');
      hideTimer2=setTimeout(function(){
        if(myGen!==gen) return;
        overlay.classList.remove('fade-out');
        overlay.style.display='none';
        coinContainer.innerHTML='';
        if(pendingResolve===resolve) pendingResolve=null;
        resolve();
      },1500);
    },3500);
  });
}

// Call after any wager game resolves a win: bet and win are in dollars.
// Only fires the celebration once the payout multiplier hits 10x, matching
// the case-opening overlay's threshold. Returns a Promise<boolean> so
// autoplay-enabled games can await it and pause until the celebration ends,
// instead of firing the next round underneath it.
function maybeBigWin(bet,win){
  if(!bet||bet<=0||!win) return Promise.resolve(false);
  var multiplier=win/bet;
  if(multiplier<10) return Promise.resolve(false);
  return showBigWin(win,multiplier).then(function(){ return true; });
}

window.showBigWin=showBigWin;
window.maybeBigWin=maybeBigWin;
})();
