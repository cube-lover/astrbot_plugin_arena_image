"""Manual reCAPTCHA escalation; no automatic challenge solving."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

_REQUIRED: dict[str, bool] = {}
_NAME = "arena_generation_verification.json"


def _memory_key(endpoint: str, directory: Path | None) -> str:
    return endpoint + "|" + str(directory.resolve() if directory else "")


def required(endpoint: str, directory: Path | None) -> bool:
    key = _memory_key(endpoint, directory)
    if key in _REQUIRED:
        return _REQUIRED[key]
    if directory:
        try:
            data = json.loads((directory / _NAME).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("endpoint") == _scope(endpoint):
                _REQUIRED[key] = data.get("required") is True
                return _REQUIRED[key]
        except (OSError, ValueError):
            pass
    return False


def _scope(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def set_required(endpoint: str, directory: Path | None, value: bool) -> None:
    _REQUIRED[_memory_key(endpoint, directory)] = bool(value)
    if directory:
        path = directory / _NAME
        tmp = path.with_suffix(".tmp")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({
                "endpoint": _scope(endpoint), "required": bool(value), "updated_at": time.time(),
            }), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError:
            logging.getLogger(__name__).warning("Manual verification state persistence failed; memory state retained")


def is_rejected(status: int, body: str) -> bool:
    if status != 403:
        return False
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("error") == "recaptcha validation failed"


# Only the currently loaded public bundle's literal render configuration is
# accepted. Never borrow the V3 key or execute downloaded bundle source.
DISCOVER_KEY_JS = r"""
(async () => {
  const sources = [...new Set([...document.scripts].map(s => s.src)
    .filter(s => s.startsWith(location.origin) && s.includes('/_next/')))];
  const keys = new Set();
  const deadline = Date.now()+40000;
  for (const src of sources.slice(0, 100)) {
    if(Date.now()>=deadline) break;
    try {
    const response = await fetch(src, {cache:'force-cache', credentials:'include',
      signal:AbortSignal.timeout(8000)});
    if (!response.ok) continue;
    const source = await response.text();
    if (!source.includes('useGetRecaptchaV2Token')) continue;
    const pattern = /(?:window\.)?grecaptcha\.enterprise\.render\([^,]{1,100},\s*\{\s*sitekey:\s*["']([A-Za-z0-9_-]{20,100})["']/g;
    for (const hit of source.matchAll(pattern)) keys.add(hit[1]);
    if(keys.size) break;
    } catch(e) {}
  }
  return keys.size === 1 ? [...keys][0] : '';
})()
"""

SNAPSHOT_JS = """
(() => {
  const state = window.__arenaManualVerification;
  if (!state) return {state:'absent', token_ready:false};
  const ready = state.state === 'ready' && !!state.token && Date.now() < state.expiresAt;
  if (state.state === 'ready' && !ready) {
    state.token = ''; state.state = 'expired';
  }
  return {state:state.state, token_ready:ready,
    expires_in:ready ? Math.floor((state.expiresAt-Date.now())/1000) : 0};
})()
"""

TAKE_JS = """
(() => {
  const state = window.__arenaManualVerification;
  if(!state || state.state !== 'ready' || Date.now() >= state.expiresAt) {
    if(state) {state.token=''; if(state.state==='ready') state.state='expired';}
    return '';
  }
  const token=state.token; state.token=''; state.state='consumed';
  document.getElementById('arena-plugin-manual-verification')?.remove();
  return token;
})()
"""


def render_js(sitekey: str) -> str:
    return r"""
(async () => {
  const prior = window.__arenaManualVerification;
  if (prior && prior.state==='ready' && Date.now()<prior.expiresAt) return {state:'ready'};
  if (prior && prior.state==='waiting' &&
      document.getElementById('arena-plugin-manual-verification')) return {state:'waiting'};
  document.getElementById('arena-plugin-manual-verification')?.remove();
  if(!window.grecaptcha?.enterprise?.render) return {state:'unavailable'};
  const box=document.createElement('section'); box.id='arena-plugin-manual-verification';
  Object.assign(box.style,{position:'fixed',top:'24px',left:'50%',transform:'translateX(-50%)',
    zIndex:'2147483000',background:'#fff',color:'#111',padding:'20px',border:'1px solid #666',
    borderRadius:'4px',maxWidth:'90vw',boxShadow:'0 2px 12px #0004'});
  const title=document.createElement('p'); title.textContent='Arena 画图人工验证';
  const note=document.createElement('p'); note.style.maxWidth='340px';
  note.textContent='请手动完成下方 Google 验证，随后两分钟内重新发送原画图命令。';
  const holder=document.createElement('div');
  const close=document.createElement('button'); close.textContent='取消';
  const state={state:'waiting',token:'',expiresAt:0}; window.__arenaManualVerification=state;
  const update=(status)=>{if(window.__arenaManualVerification!==state)return;
    state.state=status; state.token='';};
  close.onclick=()=>{update('cancelled');box.remove();};
  box.append(title,note,holder,close);document.body.append(box);
  return await new Promise(resolve=>{
    const deadline=setTimeout(()=>{update('error');note.textContent='验证组件加载超时，请重新运行 /竞技场验证。';
      resolve({state:'error'});},15000);
    window.grecaptcha.enterprise.ready(()=>{
      if(state.state!=='waiting'){clearTimeout(deadline);resolve({state:state.state});return;}
      try {
        window.grecaptcha.enterprise.render(holder,{
          sitekey:SITEKEY,theme:'light',
          callback:(token)=>{
            if(window.__arenaManualVerification!==state || state.state!=='waiting')return;
            if(typeof token!=='string'||!token)return;
            state.token=token;state.expiresAt=Date.now()+110000;state.state='ready';
            note.textContent='人工验证已完成。请尽快重发原画图命令；上游接受请求后才确认恢复。';
          },
          'expired-callback':()=>{update('expired');note.textContent='验证结果已过期，请重新运行 /竞技场验证。';},
          'error-callback':()=>{update('error');note.textContent='验证组件出错，请重新运行 /竞技场验证。';}
        });
        clearTimeout(deadline);resolve({state:state.state});
      }catch(e){clearTimeout(deadline);update('error');resolve({state:'error'});}
    });
  });
})()
""".replace("SITEKEY", json.dumps(sitekey))


async def start(page: Any) -> dict[str, Any]:
    key = await page.evaluate(DISCOVER_KEY_JS, timeout=120)
    if not isinstance(key, str) or not key:
        return {"state": "unavailable"}
    result = await page.evaluate(render_js(key), timeout=25)
    return result if isinstance(result, dict) else {"state": "unavailable"}


async def snapshot(page: Any) -> dict[str, Any]:
    data = await page.evaluate(SNAPSHOT_JS, timeout=15)
    return data if isinstance(data, dict) else {"state": "unavailable", "token_ready": False}


async def take(page: Any) -> str:
    value = await page.evaluate(TAKE_JS, timeout=15)
    return value if isinstance(value, str) else ""
