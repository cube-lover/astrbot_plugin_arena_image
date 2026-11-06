// ==UserScript==
// @name         Arena 中转（AstrBot 画图）
// @name:en      Arena Relay for AstrBot
// @namespace    astrbot.arena.relay
// @version      0.1.0
// @description  把自己浏览器里已登录的 arena.ai 借给 AstrBot 插件出图。只执行固定的几种指令，Cookie 不出本机。
// @author       astrbot_plugin_arena_image
// @match        https://arena.ai/*
// @run-at       document-idle
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM_cookie
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      __RELAY_HOST__
// @connect      localhost
// @connect      127.0.0.1
// ==/UserScript==
//
// 这个脚本不主动做任何事：它只在插件排队时取一条指令，做完推回去。
// 指令只有六种（state / models / actions / recaptcha / reload / fetch），
// 没有「执行任意脚本」这一种，服务器也拿不到你的 Cookie。

(function () {
  'use strict';

  const CFG = {
    base: '__RELAY_BASE__',
    secret: '__RELAY_SECRET__',
    fingerprint: '__RELAY_FINGERPRINT__',
    protocol: __RELAY_PROTOCOL__,
    pollHoldMs: __RELAY_POLL_MS__,
    keepaliveMs: __RELAY_KEEPALIVE_MS__,
    pagePath: '__ARENA_PAGE_PATH__',
    version: '0.1.0',
    origin: 'https://arena.ai',
    authCookie: 'arena-auth-prod-v1',
  };

  const BASE_URL = (() => {
    try {
      return new URL(CFG.base);
    } catch (err) {
      return null;
    }
  })();

  // ---- 小工具 ---------------------------------------------------------------

  const enc = new TextEncoder();
  const nowSec = () => Math.floor(Date.now() / 1000);
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

  function hex(buffer) {
    return Array.from(new Uint8Array(buffer))
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
  }

  function randomHex(bytes) {
    const raw = new Uint8Array(bytes);
    crypto.getRandomValues(raw);
    return hex(raw);
  }

  function store(key, fallback) {
    try {
      const value = GM_getValue(key, fallback);
      return value === undefined ? fallback : value;
    } catch (err) {
      return fallback;
    }
  }

  function remember(key, value) {
    try {
      GM_setValue(key, value);
    } catch (err) {
      /* 隐私模式下写不进去也不影响本次运行 */
    }
  }

  const STATE = {
    browserId: store('browser_id', ''),
    label: store('label', ''),
    paused: !!store('paused', false),
    pageUrl: location.href,
    lastState: null,
    lastError: '',
    jobs: 0,
    fails: 0,
    relayOk: null,
    polling: false,
    busy: '',
    log: [],
  };
  if (!STATE.browserId) {
    STATE.browserId = randomHex(8);
    remember('browser_id', STATE.browserId);
  }
  if (!STATE.label) {
    const platform = (navigator.platform || 'PC').split(' ')[0];
    STATE.label = `${platform}-${STATE.browserId.slice(0, 4)}`;
    remember('label', STATE.label);
  }

  function note(text, kind) {
    const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    STATE.log.unshift({ at: stamp, text: String(text).slice(0, 200), kind: kind || '' });
    if (STATE.log.length > 12) STATE.log.length = 12;
    paint();
  }

  // ---- 签名 -----------------------------------------------------------------
  //
  // 每个请求都带 HMAC-SHA256(method.path.时间戳.随机数.sha256(body))，
  // 服务端只认 120 秒内、没用过的随机数，所以链接被别人抄走也没用。

  let keyPromise = null;
  function signingKey() {
    if (!keyPromise) {
      keyPromise = crypto.subtle.importKey(
        'raw',
        enc.encode(CFG.secret),
        { name: 'HMAC', hash: 'SHA-256' },
        false,
        ['sign']
      );
    }
    return keyPromise;
  }

  async function sha256Hex(text) {
    return hex(await crypto.subtle.digest('SHA-256', enc.encode(text || '')));
  }

  async function signRequest(method, path, ts, nonce, body) {
    const digest = await sha256Hex(body || '');
    const message = [String(method).toUpperCase(), path, ts, nonce, digest].join('\n');
    const key = await signingKey();
    return hex(await crypto.subtle.sign('HMAC', key, enc.encode(message)));
  }

  function endpoint(route) {
    if (!BASE_URL) return null;
    const base = BASE_URL.href.replace(/\/+$/, '');
    try {
      return new URL(`${base}/${route}`);
    } catch (err) {
      return null;
    }
  }

  const gmRequest =
    typeof GM_xmlhttpRequest === 'function'
      ? GM_xmlhttpRequest
      : typeof GM !== 'undefined' && GM && typeof GM.xmlHttpRequest === 'function'
      ? GM.xmlHttpRequest.bind(GM)
      : null;

  // 只有扩展环境的 GM_xmlhttpRequest 能从 https://arena.ai 连 http://服务器:端口：
  // 页面自己的 fetch 会被浏览器按「混合内容 / 私有网络」直接拦掉。
  async function call(route, payload, opts) {
    const options = opts || {};
    const target = endpoint(route);
    if (!target) {
      throw new Error('中转地址是空的：请在插件配置里填 relay_public_url，然后重新安装脚本');
    }
    if (!gmRequest) {
      throw new Error('脚本缺少 GM_xmlhttpRequest 权限，请用 Tampermonkey 安装');
    }
    const method = (options.method || 'POST').toUpperCase();
    const body = method === 'GET' ? '' : JSON.stringify(payload || {});
    const ts = String(nowSec());
    const nonce = randomHex(12);
    const sign = await signRequest(method, target.pathname, ts, nonce, body);
    return new Promise((resolve, reject) => {
      gmRequest({
        method: method,
        url: target.href,
        timeout: options.timeout || 45000,
        headers: {
          'Content-Type': 'application/json',
          'X-Arena-Ts': ts,
          'X-Arena-Nonce': nonce,
          'X-Arena-Sign': sign,
          'X-Arena-Browser': STATE.browserId,
        },
        data: method === 'GET' ? undefined : body,
        onload: (res) => {
          let json = null;
          try {
            json = res.responseText ? JSON.parse(res.responseText) : null;
          } catch (err) {
            json = null;
          }
          resolve({ status: res.status, json: json, text: res.responseText || '' });
        },
        onerror: () => reject(new Error('连不上中转服务（网络不通或端口没放开）')),
        ontimeout: () => reject(new Error('连中转服务超时')),
        onabort: () => reject(new Error('请求被取消')),
      });
    });
  }

  // ---- 登录判定：和插件 Python 端同一套规则 --------------------------------

  function b64Decode(text) {
    const cleaned = String(text || '').replace(/\s+/g, '');
    if (!cleaned) return '';
    const variants = [cleaned, cleaned.replace(/-/g, '+').replace(/_/g, '/')];
    for (const variant of variants) {
      const padded = variant + '='.repeat((4 - (variant.length % 4)) % 4);
      try {
        const raw = atob(padded);
        const bytes = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
        const text2 = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
        if (text2) return text2;
      } catch (err) {
        /* 换另一种字母表再试，和 Python 端 _base64_payload_variants 一致 */
      }
    }
    return '';
  }

  function parseJson(text) {
    try {
      const value = JSON.parse(text);
      return value && typeof value === 'object' ? value : null;
    } catch (err) {
      return null;
    }
  }

  function decodeEnvelope(token) {
    const text = String(token || '').trim();
    if (!text.startsWith('base64-')) return null;
    return parseJson(b64Decode(text.slice('base64-'.length)));
  }

  function decodeJwt(token) {
    const parts = String(token || '').trim().split('.');
    if (parts.length < 3 || !parts[1]) return null;
    return parseJson(b64Decode(parts[1]));
  }

  function intOf(value) {
    const parsed = parseInt(value, 10);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function expiryEpoch(token) {
    const envelope = decodeEnvelope(token);
    if (envelope) {
      const direct = intOf(envelope.expires_at);
      if (direct !== null) return direct;
      const inner = decodeJwt(envelope.access_token || '');
      if (inner) {
        const value = intOf(inner.exp);
        if (value !== null) return value;
      }
    }
    const jwt = decodeJwt(token);
    if (jwt) {
      const value = intOf(jwt.exp);
      if (value !== null) return value;
    }
    return null;
  }

  function isExpired(token, skew) {
    const expiry = expiryEpoch(token);
    if (expiry === null) return false;
    return nowSec() >= expiry - (skew === undefined ? 30 : skew);
  }

  function isPlausible(token) {
    const text = String(token || '').trim();
    if (!text) return false;
    if (text.startsWith('base64-')) {
      const envelope = decodeEnvelope(text);
      if (!envelope) return false;
      if (String(envelope.access_token || '').split('.').length < 3) return false;
      return !isExpired(text);
    }
    if (text.split('.').length >= 3) return text.length >= 100 && !isExpired(text);
    return false;
  }

  function flagTrue(value) {
    if (value === true) return true;
    if (typeof value === 'number') return value === 1;
    const text = String(value === null || value === undefined ? '' : value).trim().toLowerCase();
    return text === 'true' || text === '1' || text === 'yes';
  }

  function plainObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function isLoggedIn(token) {
    if (!isPlausible(token)) return false;
    const envelope = plainObject(decodeEnvelope(token));
    const access = String(envelope.access_token || '').trim();
    const jwt = plainObject((access ? decodeJwt(access) : null) || decodeJwt(token));
    const user = plainObject(envelope.user);
    const supabase = plainObject(envelope.supabaseUser);
    const appMeta = plainObject(jwt.app_metadata);
    const anonymous = [
      jwt.is_anonymous,
      envelope.is_anonymous,
      user.is_anonymous,
      user.isAnonymous,
      supabase.is_anonymous,
    ];
    if (anonymous.some(flagTrue)) return false;
    if (jwt.is_anonymous === false) return true;
    const marks = [
      jwt.email,
      user.email,
      supabase.email,
      user.emailProvider,
      appMeta.provider,
      appMeta.providers,
    ];
    for (const item of marks) {
      if (Array.isArray(item)) {
        if (item.some((part) => String(part === null || part === undefined ? '' : part).trim())) {
          return true;
        }
      } else if (String(item === null || item === undefined ? '' : item).trim()) {
        return true;
      }
    }
    return false;
  }

  function canRefresh(token) {
    const envelope = decodeEnvelope(token);
    if (!envelope) return false;
    if (!String(envelope.refresh_token || '').trim()) return false;
    const access = String(envelope.access_token || '').trim();
    if (access.split('.').length < 3) return false;
    const jwt = plainObject(decodeJwt(access));
    const user = plainObject(envelope.user);
    if (flagTrue(jwt.is_anonymous) || flagTrue(user.is_anonymous)) return false;
    if (String(jwt.role || '').trim().toLowerCase() === 'authenticated') return true;
    return !!String(jwt.email || user.email || '').trim();
  }

  function combineAuthCookie(list) {
    if (!Array.isArray(list)) return '';
    let whole = '';
    const chunks = new Map();
    for (const item of list) {
      if (!item || typeof item !== 'object') continue;
      const name = String(item.name || '').trim();
      const value = String(item.value || '');
      if (!value) continue;
      if (name === CFG.authCookie) {
        whole = value;
        continue;
      }
      const match = /^arena-auth-prod-v1\.(\d+)$/.exec(name);
      if (match) chunks.set(parseInt(match[1], 10), value);
    }
    let token = whole;
    if (!token && chunks.size) {
      // 数字序，不是字符串序：出现第 10 片时字符串序会把值拼错。
      token = Array.from(chunks.keys())
        .sort((a, b) => a - b)
        .map((index) => chunks.get(index))
        .join('');
    }
    if (token && !token.startsWith('base64-')) token = `base64-${token}`;
    return token;
  }

  const gmCookie =
    typeof GM_cookie !== 'undefined' && GM_cookie
      ? GM_cookie
      : typeof GM !== 'undefined' && GM && GM.cookie
      ? GM.cookie
      : null;

  // httpOnly 的 arena-auth-prod-v1.N 只有扩展的 cookie API 读得到，
  // document.cookie 看不见它。读不到时不硬判「没登录」——见 verbState()。
  function listCookiesOnce(query) {
    return new Promise((resolve) => {
      if (!gmCookie || typeof gmCookie.list !== 'function') {
        resolve(null);
        return;
      }
      let settled = false;
      const finish = (value) => {
        if (!settled) {
          settled = true;
          resolve(value);
        }
      };
      setTimeout(() => finish(null), 5000);
      try {
        gmCookie.list(query, (list, err) => finish(err ? null : list || []));
      } catch (err) {
        finish(null);
      }
    });
  }

  async function listCookies() {
    const direct = await listCookiesOnce({});
    if (direct && direct.length) return direct;
    const scoped = await listCookiesOnce({ domain: 'arena.ai' });
    if (scoped && scoped.length) return scoped;
    if (direct || scoped) return direct || scoped;
    return null;
  }

  const CHALLENGE_MARKERS = ['/login', '/signin', '/sign-in', '/auth/'];

  function urlIsChallenged(url) {
    const lowered = String(url || '').toLowerCase();
    return CHALLENGE_MARKERS.some((marker) => lowered.includes(marker));
  }

  // 访问令牌过期但 refresh_token 还在时，页面自己会换新的：
  // 唤一下可见性/焦点，再走一次同源 GET（Arena 的 SSR 中间件会顺手轮换 Cookie）。
  async function wakeSession() {
    try {
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new Event('focus'));
    } catch (err) {
      /* 事件派发失败不影响下面的请求 */
    }
    try {
      const res = await fetch(CFG.pagePath, { credentials: 'include', cache: 'no-store' });
      await res.text();
    } catch (err) {
      /* 网络抖动，交给下面的轮询判断 */
    }
  }

  async function readSession() {
    const cookies = await listCookies();
    if (cookies === null) return { token: '', cookies: null, readable: false };
    return { token: combineAuthCookie(cookies), cookies: cookies, readable: true };
  }

  function cookieValue(list, name) {
    if (!Array.isArray(list)) return '';
    for (const item of list) {
      if (item && typeof item === 'object' && String(item.name || '') === name) {
        return String(item.value || '').trim();
      }
    }
    return '';
  }

  async function verbState() {
    let read = await readSession();
    let refreshed = false;
    if (read.readable && read.token && isExpired(read.token) && canRefresh(read.token)) {
      await wakeSession();
      for (let round = 0; round < 8; round += 1) {
        await sleep(1000);
        const again = await readSession();
        if (again.token) {
          read = again;
          if (!isExpired(again.token)) {
            refreshed = true;
            break;
          }
        }
      }
    }
    if (!read.readable) {
      // 读不到 Cookie（多数是用了不支持 GM_cookie 的管理器）。这里不硬判「没登录」：
      // 你本人就在这个浏览器里登录着，真没登录的话 Arena 会用 401/403 说话，
      // 插件那边照样会提示去登录，比在这里一刀切更不容易冤枉人。
      const assumed = {
        has_arena_auth: true,
        has_cf_clearance: false,
        has_logged_in: true,
        session_logged_in: true,
        session_expired: false,
        session_refreshed: false,
        session_refreshable: false,
        session_source: 'assumed',
        session_expires_in: null,
        recaptcha_action: 'chat_submit',
        verified: !urlIsChallenged(location.href),
        page_url: location.href,
      };
      STATE.lastState = assumed;
      return { state: assumed, cookie_readable: false };
    }
    const token = read.token;
    const plausible = isPlausible(token);
    const expired = !!token && isExpired(token);
    const logged = !!token && isLoggedIn(token) && !expired;
    const expiry = token ? expiryEpoch(token) : null;
    const state = {
      has_arena_auth: !!token && plausible,
      has_cf_clearance: !!cookieValue(read.cookies, 'cf_clearance'),
      has_logged_in: logged,
      session_logged_in: logged,
      session_expired: expired,
      session_refreshed: refreshed,
      session_refreshable: !!token && expired && canRefresh(token),
      session_source: token ? 'userscript' : '',
      session_expires_in: expiry === null ? null : Math.max(0, expiry - nowSec()),
      recaptcha_action: logged ? 'chat_submit' : 'sign_up',
      verified: logged && !urlIsChallenged(location.href),
      page_url: location.href,
    };
    STATE.lastState = state;
    return { state: state, cookie_readable: true };
  }

  async function verbModels() {
    return await (async () => {
  try {
    const r = await fetch('/text/direct', {
      credentials: 'include', signal: AbortSignal.timeout(20000)
    });
    if (!r.ok) return {ok: false, status: r.status};
    const t = await r.text();
    // Decode Flight string chunks before parsing. The model array may span
    // several script tags and the following property changes between builds.
    const chunks = [];
    for (const script of t.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
      const push = script[1].match(/self\.__next_f\.push\((\[[\s\S]*\])\)\s*;?\s*$/);
      if (!push) continue;
      try {
        const frame = JSON.parse(push[1]);
        if (frame[0] === 1 && typeof frame[1] === 'string') chunks.push(frame[1]);
      } catch (_) {}
    }
    function arrayAt(source, start) {
      if (source[start] !== '[') return null;
      let depth = 0, quoted = false, escaped = false;
      for (let i = start; i < source.length; i++) {
        const ch = source[i];
        if (quoted) {
          if (escaped) escaped = false;
          else if (ch === '\\') escaped = true;
          else if (ch === '"') quoted = false;
        } else if (ch === '"') quoted = true;
        else if (ch === '[') depth++;
        else if (ch === ']' && --depth === 0) {
          try {
            const rows = JSON.parse(source.slice(start, i + 1));
            if (!Array.isArray(rows) || !rows.length ||
                !rows.every(row => row && typeof row === 'object' &&
                  !Array.isArray(row) && typeof row.id === 'string' &&
                  typeof row.publicName === 'string')) return null;
            return JSON.stringify(rows);
          } catch (_) { return null; }
        }
      }
      return null;
    }
    for (const source of [chunks.join(''), t]) {
      for (const marker of source.matchAll(/"initialModels"\s*:\s*/g)) {
        const json = arrayAt(source, marker.index + marker[0].length);
        if (json !== null) return {ok: true, status: r.status, json};
      }
    }
    return {ok: false, status: r.status, len: t.length};
  } catch (e) {
    return {ok: false, status: 0, error: String(e)};
  }
})();
  }

  // Next.js 每次发版都会换 server action id，所以从当前页面真正加载的 JS 里现扫。
  async function verbActions(payload) {
    const pattern = String((payload && payload.pattern) || '');
    const found = {};
    let scanned = 0;
    if (!pattern) return { actions: found, scanned: 0, error: 'missing pattern' };
    const sources = [...new Set(Array.from(document.scripts).map((item) => item.src).filter(Boolean)
      .concat(performance.getEntriesByType('resource').map((item) => item.name)
        .filter((url) => url.includes('/_next/') && url.endsWith('.js'))))]
      .filter((url) => { try { return new URL(url, location.href).origin === location.origin; } catch (_) { return false; } })
      .slice(0, 120);
    for (const url of sources) {
      try {
        const text = await (await fetch(url)).text();
        scanned += 1;
        const regex = new RegExp(pattern, 'g');
        let match = regex.exec(text);
        while (match !== null) {
          found[match[2]] = match[1];
          match = regex.exec(text);
        }
      } catch (err) {
        /* 某个 chunk 拉不下来就跳过 */
      }
    }
    return { actions: found, scanned: scanned };
  }

  function siteKey() {
    try {
      const fromScripts = Array.from(document.scripts)
        .map((item) => item.src)
        .find((url) => /recaptcha\/(enterprise|api)\.js\?render=/.test(url || ''));
      if (fromScripts) return fromScripts.split('render=')[1].split('&')[0];
      const match = document.documentElement.innerHTML.match(
        /recaptcha\/(?:enterprise|api)\.js\?render=([0-9A-Za-z_-]{8,200})/
      );
      return match ? match[1] : '';
    } catch (err) {
      return '';
    }
  }

  // 必须用页面自己的 grecaptcha：token 绑定站点 key、浏览器指纹和当前访客 Cookie，
  // 换个环境铸出来的会被 Arena 判无效。
  function pageGrecaptcha() {
    try {
      if (typeof unsafeWindow !== 'undefined' && unsafeWindow && unsafeWindow.grecaptcha) {
        return unsafeWindow.grecaptcha;
      }
    } catch (err) {
      /* 沙箱不给访问就退回 window */
    }
    return window.grecaptcha;
  }

  async function verbRecaptcha(payload) {
    const action = String((payload && payload.action) || 'chat_submit');
    const key = siteKey();
    if (!key) return { token: '', error: 'sitekey not found' };
    const grecaptcha = pageGrecaptcha();
    if (!grecaptcha || !grecaptcha.enterprise) return { token: '', error: 'grecaptcha not ready' };
    const token = await new Promise((resolve) => {
      let done = false;
      const finish = (value) => {
        if (!done) {
          done = true;
          resolve(value || '');
        }
      };
      setTimeout(() => finish(''), 20000);
      try {
        grecaptcha.enterprise.ready(async () => {
          try {
            finish(await grecaptcha.enterprise.execute(key, { action: action }));
          } catch (err) {
            finish('');
          }
        });
      } catch (err) {
        finish('');
      }
    });
    return { token: token || '' };
  }

  // 这一条同源请求就是整个插件的关键：credentials:'include' 让 Chrome 自己带上
  // 当前的 auth Cookie、__cf_bm 和 Cloudflare clearance，服务器一个都不用看见。
  async function verbFetch(payload) {
    const path = String((payload && payload.path) || '');
    if (!path.startsWith('/') || path.startsWith('//')) {
      return { status: 0, headers: {}, text: 'FETCH_ERROR:只允许同源相对路径' };
    }
    const method = String((payload && payload.method) || 'POST').toUpperCase();
    const headers = Object.assign(
      { 'Content-Type': 'text/plain;charset=UTF-8', Accept: 'text/event-stream' },
      plainObject(payload && payload.headers)
    );
    const init = { method: method, headers: headers, credentials: 'include' };
    if (payload && payload.body !== null && payload.body !== undefined) {
      init.body = String(payload.body);
    }
    try {
      const res = await fetch(path, init);
      const text = await res.text();
      const out = {};
      res.headers.forEach((value, key) => {
        out[key] = value;
      });
      return { status: res.status, headers: out, text: text };
    } catch (err) {
      return { status: 0, headers: {}, text: `FETCH_ERROR:${String(err)}` };
    }
  }

  // 刷新会把脚本自己也一起重来，所以结果先推回去，再刷。
  // 队列在服务端，刷完重新连上还是同一个 browser_id，后面的指令不会丢。
  async function verbReload() {
    return { ok: true, reloading: true, __after: 'reload' };
  }

  const VERBS = {
    state: verbState,
    models: verbModels,
    actions: verbActions,
    recaptcha: verbRecaptcha,
    reload: verbReload,
    fetch: verbFetch,
  };

  // ---- 取活、干活、推回去 ---------------------------------------------------

  function baseFields() {
    return {
      browser_id: STATE.browserId,
      label: STATE.label,
      version: CFG.version,
      page_url: location.href,
      user_agent: navigator.userAgent,
      paused: !!STATE.paused,
    };
  }

  async function push(extra) {
    const payload = Object.assign(baseFields(), extra || {});
    const res = await call('push', payload, { timeout: 30000 });
    if (res.status !== 200) throw new Error(`推送结果失败：HTTP ${res.status}`);
    return res.json || {};
  }

  async function runJob(job) {
    const handler = VERBS[job.verb];
    const started = Date.now();
    let keeper = null;
    STATE.busy = job.verb;
    paint();
    try {
      if (!handler) {
        await push({
          job_id: job.job_id,
          error: `脚本不认识这条指令：${job.verb}（版本可能太旧）`,
          code: 'browser_verb_unsupported',
        });
        return;
      }
      // 出图能跑好几分钟，后台标签页的定时器会被压到一分钟一次，
      // 所以服务端的失联判定留了 150 秒，比这个心跳宽得多。
      keeper = setInterval(() => {
        push({ job_id: job.job_id, keepalive: true }).catch(() => {});
      }, Math.max(5000, job.keepalive_ms || CFG.keepaliveMs));
      const budget = Math.max(5000, job.timeout_ms || 60000);
      const result = await Promise.race([
        handler(job.payload || {}),
        sleep(budget).then(() => {
          throw new Error('页面执行超时');
        }),
      ]);
      const after = result && result.__after;
      if (result && result.__after) delete result.__after;
      await push({ job_id: job.job_id, result: result, state: STATE.lastState || undefined });
      STATE.jobs += 1;
      note(`${job.verb} 完成（${((Date.now() - started) / 1000).toFixed(1)}s）`, 'ok');
      if (after === 'reload') setTimeout(() => location.reload(), 300);
    } catch (err) {
      STATE.fails += 1;
      STATE.lastError = String((err && err.message) || err);
      note(`${job.verb} 失败：${STATE.lastError}`, 'bad');
      await push({
        job_id: job.job_id,
        error: STATE.lastError,
        code: 'browser_job_failed',
      }).catch(() => {});
    } finally {
      if (keeper) clearInterval(keeper);
      STATE.busy = '';
      paint();
    }
  }

  async function pollOnce() {
    const payload = Object.assign(baseFields(), {
      state: STATE.lastState || undefined,
      hold: Math.round(CFG.pollHoldMs / 1000),
    });
    const res = await call('poll', payload, { timeout: CFG.pollHoldMs + 15000 });
    if (res.status === 401) {
      throw new Error(`中转拒绝签名：${(res.json && res.json.error) || 'unauthorized'}`);
    }
    if (res.status === 409) {
      throw new Error((res.json && res.json.message) || '接入的浏览器数量超上限');
    }
    if (res.status !== 200 && res.status !== 204) {
      throw new Error(`中转返回 HTTP ${res.status}`);
    }
    STATE.relayOk = true;
    if (res.status === 204) return null;
    return (res.json && res.json.job) || null;
  }

  async function pollForever() {
    STATE.polling = true;
    paint();
    let backoff = 2000;
    for (;;) {
      try {
        const job = await pollOnce();
        backoff = 2000;
        if (STATE.paused) {
          // 暂停时也照样打个招呼，这样服务端知道「人在、只是不接单」。
          await sleep(15000);
          continue;
        }
        if (job) {
          note(`收到指令 ${job.verb}`);
          await runJob(job);
        }
      } catch (err) {
        STATE.relayOk = false;
        STATE.lastError = String((err && err.message) || err);
        note(`取活失败：${STATE.lastError}`, 'bad');
        await sleep(backoff);
        backoff = Math.min(60000, Math.round(backoff * 1.8));
      }
    }
  }

  // 同一台电脑可能开了好几个 arena 标签页，只让一个去取活：
  // Web Locks 在持有者的标签页关闭时会自动放锁，等着的那个立刻接棒。
  async function elect() {
    if (navigator.locks && typeof navigator.locks.request === 'function') {
      for (;;) {
        try {
          await navigator.locks.request(
            'astrbot-arena-relay',
            { mode: 'exclusive' },
            async () => {
              await pollForever();
            }
          );
        } catch (err) {
          await sleep(5000);
        }
      }
    }
    await pollForever();
  }

  // ---- 连通测试与自检 -------------------------------------------------------

  async function testRelay() {
    const res = await call('hello', null, { method: 'GET', timeout: 20000 });
    if (res.status !== 200) throw new Error(`中转返回 HTTP ${res.status}`);
    const info = res.json || {};
    if (!info.ok) {
      throw new Error(`能连上但密钥不对（${info.error || 'unauthorized'}），请重新安装脚本`);
    }
    if (info.protocol !== CFG.protocol) {
      note(`协议版本不一致：服务端 v${info.protocol}，脚本 v${CFG.protocol}，建议重装`, 'warn');
    }
    STATE.relayOk = true;
    paint();
    return info;
  }

  async function selfTest() {
    const lines = [];
    let pattern = '';
    const step = async (name, fn) => {
      const started = Date.now();
      try {
        const detail = await fn();
        const cost = ((Date.now() - started) / 1000).toFixed(1);
        lines.push(`✅ ${name}：${detail}（${cost}s）`);
        note(lines[lines.length - 1], 'ok');
        return true;
      } catch (err) {
        lines.push(`❌ ${name}：${String((err && err.message) || err)}`);
        note(lines[lines.length - 1], 'bad');
        return false;
      }
    };

    await step('连通中转', async () => {
      const info = await testRelay();
      pattern = String(info.action_pattern || '');
      return `协议 v${info.protocol}，密钥指纹 ${info.fingerprint}，在线浏览器 ${info.online}`;
    });

    await step('登录状态', async () => {
      const out = await verbState();
      const state = out.state || {};
      if (!out.cookie_readable) return '读不到 Cookie，按已登录处理（建议换 Tampermonkey）';
      if (state.has_logged_in) {
        const left = Math.round((state.session_expires_in || 0) / 60);
        return `已登录，令牌还有 ${left} 分钟`;
      }
      if (state.has_arena_auth) throw new Error('有会话但还是匿名，请在本页登录账号');
      throw new Error('没有 Arena 会话，请先在本页登录');
    });

    await step('模型列表', async () => {
      const out = await verbModels();
      if (!out.ok) throw new Error(`Arena 返回 HTTP ${out.status}`);
      let count = 0;
      try {
        const parsed = JSON.parse(out.json);
        count = Array.isArray(parsed) ? parsed.length : Object.keys(parsed || {}).length;
      } catch (err) {
        count = 0;
      }
      if (!count) throw new Error('抓到页面但解析不出模型，可能 Arena 改版了');
      return `${count} 个模型`;
    });

    await step('上传通道（图生图）', async () => {
      if (!pattern) throw new Error('中转没给出扫描规则，先修好“连通中转”这一步');
      const out = await verbActions({ pattern });
      const names = Object.keys(out.actions || {});
      const missing = ['generateUploadUrl', 'getSignedUrl'].filter((n) => names.indexOf(n) < 0);
      if (missing.length) {
        throw new Error(`缺 ${missing.join(' / ')}，图生图会用不了（扫了 ${out.scanned} 个 chunk）`);
      }
      return `${names.length} 个 server action，含上传与取图链接`;
    });

    await step('人机令牌', async () => {
      const action = (STATE.lastState && STATE.lastState.recaptcha_action) || 'chat_submit';
      const out = await verbRecaptcha({ action });
      if (!out.token) throw new Error(`铸不出来（${out.error || '无 grecaptcha'}），Arena 有时也接受空令牌，不一定是坏的`);
      return `action=${action}，${out.token.length} 字符`;
    });

    const failed = lines.filter((line) => line.charAt(0) === '❌').length;
    const summary = failed
      ? `自检 ${lines.length - failed}/${lines.length} 项通过，还有 ${failed} 项要处理`
      : `自检全部 ${lines.length} 项通过，可以回 QQ 发 /jjc 了`;
    lines.push(failed ? `⚠️ ${summary}` : `🎉 ${summary}`);
    note(summary, failed ? 'warn' : 'ok');
    await call(
      'report',
      Object.assign(baseFields(), { kind: 'selftest', message: lines.join(' | ') })
    ).catch(() => {});
    return lines;
  }

  // ---- 面板 -----------------------------------------------------------------
  //
  // 挂在 closed shadow root 里：Arena 自己的脚本和样式碰不到它，它也不会污染页面。

  const PANEL_CSS = [
    '.box{position:fixed;right:16px;bottom:16px;width:290px;',
    'font:12px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;',
    'background:rgba(18,20,26,.94);color:#e8ebf0;border:1px solid #2c3040;',
    'border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.55);overflow:hidden}',
    '.hd{display:flex;align-items:center;gap:6px;padding:7px 10px;background:#1b1f29;',
    'cursor:move;user-select:none}',
    '.dot{width:8px;height:8px;border-radius:50%;background:#7a8194;flex:none}',
    '.dot.ok{background:#3ddc84}.dot.bad{background:#ff5f56}.dot.warn{background:#ffbd2e}',
    '.ttl{flex:1;font-weight:600}',
    '.mini{cursor:pointer;color:#8b93a7;padding:0 3px;font-size:14px;line-height:1}',
    '.mini:hover{color:#fff}',
    '.bd{padding:7px 10px 9px}',
    '.row{display:flex;justify-content:space-between;gap:10px}',
    '.k{color:#8b93a7;flex:none}.v{text-align:right;word-break:break-all}',
    '.btns{display:flex;gap:6px;margin:8px 0 2px}',
    'button{flex:1;padding:5px 0;font:inherit;color:#e8ebf0;background:#262b38;',
    'border:1px solid #333a4b;border-radius:6px;cursor:pointer}',
    'button:hover{background:#303645}button:disabled{opacity:.45;cursor:default}',
    '.log{max-height:150px;overflow-y:auto;margin-top:7px;padding-top:6px;',
    'border-top:1px solid #262b38;font-size:11px;line-height:1.5}',
    '.log p{margin:0 0 2px;color:#9aa2b6}',
    '.log p.ok{color:#8fe3b0}.log p.bad{color:#ff9b93}.log p.warn{color:#ffd479}',
    '.log b{color:#5f6779;font-weight:400;margin-right:5px}',
    '.hide{display:none}',
  ].join('');

  const PANEL_HTML = [
    '<div class="box" id="box">',
    '<div class="hd" id="hd"><i class="dot" id="dot"></i>',
    '<span class="ttl">Arena 中转</span>',
    '<span class="mini" id="fold" title="折叠 / 展开">−</span></div>',
    '<div class="bd" id="bd">',
    '<div class="row"><span class="k">中转</span><span class="v" id="v-relay">检查中…</span></div>',
    '<div class="row"><span class="k">登录</span><span class="v" id="v-login">检查中…</span></div>',
    '<div class="row"><span class="k">接单</span><span class="v" id="v-work">待命</span></div>',
    '<div class="row"><span class="k">本机</span><span class="v" id="v-id">—</span></div>',
    '<div class="btns"><button id="b-ping">测试连通</button>',
    '<button id="b-self">自检</button><button id="b-pause">暂停接单</button></div>',
    '<div class="log" id="log"></div>',
    '</div></div>',
  ].join('');

  let shadow = null;
  let hostNode = null;
  const el = {};
  let folded = !!store('folded', false);
  let hidden = !!store('hidden', false);
  const IDS = ['box', 'hd', 'dot', 'fold', 'bd', 'v-relay', 'v-login', 'v-work', 'v-id',
    'b-ping', 'b-self', 'b-pause', 'log'];

  function buildPanel() {
    if (shadow || !document.body) return;
    hostNode = document.createElement('div');
    hostNode.id = 'astrbot-arena-relay';
    hostNode.style.cssText = 'all:initial;position:fixed;left:0;top:0;width:0;height:0;z-index:2147483000';
    shadow = hostNode.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = PANEL_CSS;
    const wrap = document.createElement('div');
    wrap.innerHTML = PANEL_HTML;
    shadow.appendChild(style);
    shadow.appendChild(wrap);
    document.body.appendChild(hostNode);
    IDS.forEach((id) => { el[id] = shadow.getElementById(id); });
    const spot = store('spot', null);
    if (spot && typeof spot.right === 'number') {
      el.box.style.right = `${Math.max(0, spot.right)}px`;
      el.box.style.bottom = `${Math.max(0, spot.bottom)}px`;
    }
    el.fold.addEventListener('click', () => {
      folded = !folded;
      remember('folded', folded);
      paint();
    });
    el['b-ping'].addEventListener('click', () => guard('b-ping', pingButton));
    el['b-self'].addEventListener('click', () => guard('b-self', selfTestButton));
    el['b-pause'].addEventListener('click', pauseButton);
    dragging();
  }

  function dragging() {
    let from = null;
    el.hd.addEventListener('mousedown', (ev) => {
      if (ev.target === el.fold) return;
      const box = el.box.getBoundingClientRect();
      from = { x: ev.clientX, y: ev.clientY, right: window.innerWidth - box.right, bottom: window.innerHeight - box.bottom };
      ev.preventDefault();
    });
    window.addEventListener('mousemove', (ev) => {
      if (!from) return;
      const right = Math.max(0, Math.min(window.innerWidth - 60, from.right - (ev.clientX - from.x)));
      const bottom = Math.max(0, Math.min(window.innerHeight - 40, from.bottom - (ev.clientY - from.y)));
      el.box.style.right = `${right}px`;
      el.box.style.bottom = `${bottom}px`;
    });
    window.addEventListener('mouseup', () => {
      if (!from) return;
      from = null;
      const box = el.box.getBoundingClientRect();
      remember('spot', {
        right: Math.round(window.innerWidth - box.right),
        bottom: Math.round(window.innerHeight - box.bottom),
      });
    });
  }

  function loginLine() {
    const s = STATE.lastState;
    if (!s) return ['检查中…', 'warn'];
    if (s.session_source === 'assumed') return ['按已登录处理', 'warn'];
    if (s.has_logged_in) {
      const left = s.session_expires_in === null ? null : Math.round(s.session_expires_in / 60);
      return [left === null ? '已登录' : `已登录 · 余 ${left} 分`, 'ok'];
    }
    if (s.session_expired) return [s.session_refreshable ? '令牌过期，可自动续' : '令牌过期，请重登', 'bad'];
    if (s.has_arena_auth) return ['匿名会话，请登录账号', 'bad'];
    return ['未登录', 'bad'];
  }

  function paint() {
    if (!shadow) return;
    hostNode.style.display = hidden ? 'none' : '';
    el.bd.className = folded ? 'bd hide' : 'bd';
    el.fold.textContent = folded ? '+' : '−';

    const relay = STATE.relayOk === true ? '已连接' : STATE.relayOk === false ? '连不上' : '检查中…';
    el['v-relay'].textContent = BASE_URL ? relay : '未配置地址';
    const [text, tone] = loginLine();
    el['v-login'].textContent = text;
    el['v-work'].textContent = STATE.paused
      ? '已暂停'
      : STATE.busy
        ? `执行 ${STATE.busy}`
        : STATE.polling
          ? '待命中'
          : '未取活';
    el['v-id'].textContent = `${STATE.label} · 成 ${STATE.jobs} 败 ${STATE.fails}`;
    el['b-pause'].textContent = STATE.paused ? '恢复接单' : '暂停接单';

    let dot = 'ok';
    if (STATE.relayOk === false || tone === 'bad' || !BASE_URL) dot = 'bad';
    else if (STATE.relayOk === null || STATE.paused || tone === 'warn') dot = 'warn';
    el.dot.className = `dot ${dot}`;

    el.log.textContent = '';
    STATE.log.forEach((item) => {
      const line = document.createElement('p');
      if (item.kind) line.className = item.kind;
      const stamp = document.createElement('b');
      stamp.textContent = item.at;
      line.appendChild(stamp);
      line.appendChild(document.createTextNode(item.text));
      el.log.appendChild(line);
    });
  }

  async function guard(id, fn) {
    const btn = el[id];
    if (!btn || btn.disabled) return;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '执行中…';
    try {
      await fn();
    } catch (err) {
      note(String((err && err.message) || err), 'bad');
    } finally {
      btn.disabled = false;
      btn.textContent = label;
      paint();
    }
  }

  async function pingButton() {
    note('正在测试与中转的连接…');
    try {
      const info = await testRelay();
      note(
        `中转正常：协议 v${info.protocol}，密钥指纹 ${info.fingerprint}，` +
          `已接入 ${info.browsers} 台（在线 ${info.online}）`,
        'ok'
      );
    } catch (err) {
      STATE.relayOk = false;
      note(`测试失败：${String((err && err.message) || err)}`, 'bad');
      note(`地址 ${BASE_URL || '(空)'}；确认插件在跑、端口已放通、地址是这台电脑能访问到的`, 'warn');
    }
  }

  async function selfTestButton() {
    note('开始自检，大约十几秒…');
    await selfTest();
  }

  function pauseButton() {
    STATE.paused = !STATE.paused;
    remember('paused', STATE.paused);
    note(STATE.paused ? '已暂停接单，QQ 那边会提示浏览器已暂停' : '已恢复接单', STATE.paused ? 'warn' : 'ok');
    call(
      'report',
      Object.assign(baseFields(), {
        kind: 'pause',
        message: STATE.paused ? '用户暂停接单' : '用户恢复接单',
      })
    ).catch(() => {});
    paint();
  }

  // ---- 菜单与启动 -----------------------------------------------------------

  function menu() {
    const add = typeof GM_registerMenuCommand === 'function' ? GM_registerMenuCommand : null;
    if (!add) return;
    add('显示 / 隐藏面板', () => {
      hidden = !hidden;
      remember('hidden', hidden);
      paint();
    });
    add('测试连通', () => guard('b-ping', pingButton));
    add('运行自检', () => guard('b-self', selfTestButton));
    add('暂停 / 恢复接单', pauseButton);
    add('重置本机编号', () => {
      STATE.browserId = randomHex(8);
      STATE.label = `${(navigator.platform || 'PC').split(' ')[0]}-${STATE.browserId.slice(0, 4)}`;
      remember('browser_id', STATE.browserId);
      remember('label', STATE.label);
      note(`已换新编号 ${STATE.label}，刷新页面后生效`, 'warn');
    });
  }

  function boot() {
    buildPanel();
    menu();
    paint();
    note(`脚本已加载 v${CFG.version}，编号 ${STATE.label}`);
    if (!BASE_URL) {
      note('中转地址为空：插件配置里的 relay_public_url 没填，填好后重新安装脚本', 'bad');
      paint();
      return;
    }
    verbState().then(() => paint()).catch(() => {});
    testRelay().then(
      (info) => note(`中转在线：协议 v${info.protocol}，密钥指纹 ${info.fingerprint}`, 'ok'),
      (err) => {
        STATE.relayOk = false;
        note(`连不上中转：${String((err && err.message) || err)}`, 'bad');
      }
    );
    elect();
    // 面板上的登录状态自己刷新，免得显示的是十分钟前的旧结论。
    setInterval(() => {
      verbState().then(() => paint()).catch(() => {});
    }, 120000);
  }

  if (document.body) {
    boot();
  } else {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  }





















})();
