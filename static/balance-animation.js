(function(){'use strict';var _anim=null;function _ease(t){return 1-Math.pow(1-t,3)}
function animateBalance(elId,fText,tText,isWin,dur){
if(_anim){cancelAnimationFrame(_anim);_anim=null}
var el=typeof elId==='string'?document.getElementById(elId):elId;if(!el)return
dur=dur||800;var fromD=[],toD=[],nonD=[];var dI=0
for(var i=0;i<fText.length;i++){if(/[0-9]/.test(fText[i])){fromD[dI++]=parseInt(fText[i])}}
dI=0
for(var i=0;i<tText.length;i++){
if(/[0-9]/.test(tText[i])){toD[dI++]=parseInt(tText[i]);nonD.push(null)}
else{nonD.push(tText[i])}
}
var nD=toD.length;if(nD===0){el.textContent=tText;return}
var bg=el.style.background,clip=el.style.webkitBackgroundClip,fill=el.style.webkitTextFillColor,anim=el.style.animation
if(!isWin){
el.style.background='none';el.style.webkitBackgroundClip='unset';el.style.webkitTextFillColor='#ff4444';el.style.animation='none'
}
var st=performance.now()
function tk(now){
var p=Math.min((now-st)/dur,1),e=_ease(p),out=[],dI=0
for(var i=0;i<tText.length;i++){
if(nonD[i]===null){
var fD=fromD[dI]||0,tD=toD[dI]
out.push(Math.round(fD+(tD-fD)*e))
dI++
}else{out.push(nonD[i])}
}
el.textContent=out.join('')
if(p<1){_anim=requestAnimationFrame(tk)}else{
el.textContent=tText
el.style.background=bg||'';el.style.webkitBackgroundClip=clip||'';el.style.webkitTextFillColor=fill||'';el.style.animation=anim||''
_anim=null
}
}
_anim=requestAnimationFrame(tk)
}
window.animateBalance=animateBalance
})()
