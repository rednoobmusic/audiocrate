import{H as r,m as i,e as s,P as a,c as m}from"./index-Bel9FM1A.js";import"./vendor-Dl0FI7Wi.js";/*!
 * (C) Ionic http://ionicframework.com - MIT License
 */const p=()=>{const e=window;e.addEventListener("statusTap",()=>{r(()=>{const o=document.elementFromPoint(e.innerWidth/2,e.innerHeight/2);if(!o)return;const t=i(o);t&&new Promise(n=>s(t,n)).then(()=>{a(async()=>{t.style.setProperty("--overflow","hidden"),await m(t,300),t.style.removeProperty("--overflow")})})})})};export{p as startStatusTap};
