#!/usr/bin/env node
/**
 * DSH remote-access proxy.
 *
 * PURPOSE: make the DSH Web UI (loopback:3080) reachable over a private network
 * such as Tailscale, while supplying the two browser conveniences the UI needs
 * (crypto.randomUUID, navigator.clipboard).
 *
 * SECURITY RULES (mandatory in this file):
 *
 *  1. `window.__DSH_TRANSPORT__ = { ownsHost: true }` is NEVER injected.
 *     That flag is a security signal upstream: client-side it forces
 *     `isLoopback = true` (dsh-client-connection/lib/client.js). It grants a
 *     remote browser "I am on the host machine" privileges, so settings writes
 *     go to the host's own settings document and the settings-document editor
 *     that upstream deliberately disables becomes available. Remote clients
 *     are NOT given that authority.
 *
 *  2. `Host` and `Origin` headers are normalized to loopback (${TARGET_HOST}:${targetPort}) so upstream
 *     DSH accepts reverse-proxied requests behind authentication, preserving the
 *     client's host in `X-Forwarded-Host`. Sec-Fetch-Site is not modified.
 *
 *  3. The proxy REQUIRES authentication. When the `DSH_PROXY_TOKEN` environment
 *     variable is set, every request must carry `Authorization: Bearer <token>`
 *     or `?token=<token>`. With no token the proxy binds to loopback only and
 *     offers no remote access (the safe default).
 *
 *  3b. DSH itself also authenticates the browser (`dsh web` prints a one-shot
 *     launch token, exchanged for an authority-bound cookie). The proxy mints
 *     that cookie SERVER-SIDE: a request carrying the proxy token in `?token=`
 *     makes the proxy read the current launch token from the service journal,
 *     exchange it with DSH, and hand the resulting cookie to the browser via a
 *     303 to `./`. The DSH launch token is never put in a URL the browser sees,
 *     and it is never forwarded to the client.
 *
 *  3c. The proxy's own credentials are never forwarded upstream: the
 *     `authorization` header and the `dsh_proxy_token` cookie are removed, and
 *     a `referer` that carries the token has the parameter stripped.
 *
 *  4. The bind address is chosen with `DSH_PROXY_BIND`; the default is
 *     127.0.0.1. For Tailscale: DSH_PROXY_BIND=$(tailscale ip -4)
 *
 *  5. Request URLs (a query string may carry a token) are NOT written to the
 *     journal.
 *
 * Environment variables:
 *   DSH_WEB_URL        target address       (default http://127.0.0.1:3080)
 *   DSH_PROXY_PORT     port to listen on    (default 3000)
 *   DSH_PROXY_BIND     address to bind      (default 127.0.0.1)
 *   DSH_PROXY_TOKEN    shared secret        (empty disables remote access)
 */

import http from 'node:http';
import net from 'node:net';
import { execFile } from 'node:child_process';

const PROXY_PORT = Number(process.env.DSH_PROXY_PORT || 3000);
const BIND_ADDR = process.env.DSH_PROXY_BIND || '127.0.0.1';
const PROXY_TOKEN = (process.env.DSH_PROXY_TOKEN || '').trim();
const WEB_URL = new URL(process.env.DSH_WEB_URL || 'http://127.0.0.1:3080');
const TARGET_PORT = Number(WEB_URL.port || 80);
const TARGET_HOST = WEB_URL.hostname;
const DSH_SERVICE = process.env.DSH_SERVICE || 'dsh-web.service';
/** How long to wait for upstream response HEADERS (never for a streaming body). */
const UPSTREAM_HEADER_TIMEOUT_MS = Number(process.env.DSH_PROXY_UPSTREAM_TIMEOUT_MS || 30000);

/** Remote access is enabled only with a token; otherwise bind to loopback. */
const REMOTE_ENABLED = PROXY_TOKEN.length > 0;

if (!REMOTE_ENABLED && BIND_ADDR !== '127.0.0.1' && BIND_ADDR !== '::1') {
  console.error(
    'DSH proxy: refusing to bind beyond loopback without DSH_PROXY_TOKEN.\n' +
    '  For remote access: export DSH_PROXY_TOKEN="$(head -c 32 /dev/urandom | base64)"\n' +
    '  Binding to: 127.0.0.1'
  );
}

const LISTEN_ADDR = REMOTE_ENABLED ? BIND_ADDR : '127.0.0.1';

/**
 * Client convenience script.
 *
 * Fills in missing browser APIs only. It carries no security flag
 * (see rule 1 at the top of this file).
 */
const POLYFILL_SCRIPT = `<script>
(function() {
  if (typeof window === 'undefined') return;
  if (!window.crypto) window.crypto = {};
  if (!window.crypto.randomUUID) {
    window.crypto.randomUUID = function() {
      if (typeof window.crypto.getRandomValues === 'function') {
        return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, function(c) {
          return (c ^ (window.crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16);
        });
      }
      throw new Error('crypto.getRandomValues is unavailable; randomUUID cannot be polyfilled safely');
    };
  }
  if (typeof navigator !== 'undefined' && !navigator.clipboard) {
    navigator.clipboard = {
      writeText: function(text) {
        return new Promise(function(resolve, reject) {
          var textArea = document.createElement('textarea');
          textArea.value = text;
          textArea.style.position = 'fixed';
          textArea.style.opacity = '0';
          document.body.appendChild(textArea);
          textArea.focus();
          textArea.select();
          try { document.execCommand('copy'); resolve(); }
          catch (err) { reject(err); }
          document.body.removeChild(textArea);
        });
      }
    };
  }
})();
</script>`;

/**
 * Constant-time string comparison (prevents timing leaks).
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

const COOKIE_NAME = 'dsh_proxy_token';

/**
 * Parses a cookie value by name from request headers.
 * @param {http.IncomingMessage} req
 * @param {string} name
 * @returns {string|null}
 */
function getCookie(req, name) {
  const cookieHeader = req.headers['cookie'];
  if (!cookieHeader) return null;
  const cookies = cookieHeader.split(';');
  for (const c of cookies) {
    const [k, ...v] = c.trim().split('=');
    if (k === name) {
      // A malformed percent-escape must not take the process down: this runs
      // BEFORE authentication, so any peer could otherwise crash the proxy with
      // one request (uncaught URIError -> Restart=always crash loop).
      try {
        return decodeURIComponent(v.join('='));
      } catch {
        return null;
      }
    }
  }
  return null;
}

/**
 * Strips the proxy's own cookie from a Cookie header, keeping every other one.
 *
 * @param {string} headerValue - the raw Cookie header.
 * @returns {string|null} the remaining cookies, or null when nothing is left.
 */
function stripProxyCookie(headerValue) {
  const kept = String(headerValue)
    .split(';')
    .map((c) => c.trim())
    .filter((c) => c !== '' && c.split('=')[0] !== COOKIE_NAME);
  return kept.length > 0 ? kept.join('; ') : null;
}

/**
 * Removes the `token` query parameter from a URL (used for `Referer`).
 *
 * @param {string} value - the header value.
 * @returns {string} the value without the token parameter.
 */
function stripTokenFromUrl(value) {
  try {
    const url = new URL(String(value));
    if (url.searchParams.has('token')) {
      url.searchParams.delete('token');
      return url.href;
    }
  } catch {
    /* not a URL: leave it alone */
  }
  return String(value);
}

/**
 * Strips the ?token= parameter from the URL before forwarding upstream.
 * Upstream DSH uses processLaunchToken which causes 401 collisions if an external
 * proxy token is forwarded.
 * @param {string} rawUrl
 * @returns {string}
 */
function sanitizePath(rawUrl) {
  try {
    const url = new URL(rawUrl || '/', 'http://localhost');
    if (url.searchParams.has('token')) {
      url.searchParams.delete('token');
      const qs = url.searchParams.toString();
      return url.pathname + (qs ? `?${qs}` : '') + url.hash;
    }
  } catch {
    /* malformed URL */
  }
  return rawUrl || '/';
}

/**
 * Appends Set-Cookie header for proxy token authentication.
 * @param {Record<string, any>} headers
 * @param {string} token
 */
function attachSetCookie(headers, token) {
  const cookieVal = `${COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax`;
  const existing = headers['set-cookie'];
  if (!existing) {
    headers['set-cookie'] = [cookieVal];
  } else if (Array.isArray(existing)) {
    headers['set-cookie'] = [...existing, cookieVal];
  } else {
    headers['set-cookie'] = [existing, cookieVal];
  }
}

/**
 * Checks whether the request carries the valid shared secret and whether a cookie should be set.
 * @param {http.IncomingMessage} req
 * @returns {{ authorized: boolean, setCookie: boolean }}
 */
function checkAuth(req) {
  if (!REMOTE_ENABLED) return { authorized: true, setCookie: false };

  // 1. Authorization: Bearer <token>
  const header = req.headers['authorization'] || '';
  const m = /^Bearer\s+(.+)$/i.exec(String(header));
  if (m && safeEqual(m[1].trim(), PROXY_TOKEN)) {
    return { authorized: true, setCookie: false };
  }

  // 2. Cookie: dsh_proxy_token=<token>
  const cookieToken = getCookie(req, COOKIE_NAME);
  if (cookieToken && safeEqual(cookieToken, PROXY_TOKEN)) {
    return { authorized: true, setCookie: false };
  }

  // 3. Query: ?token=<token> -> issue Set-Cookie on verification
  try {
    const url = new URL(req.url || '/', 'http://localhost');
    const q = url.searchParams.get('token');
    if (q && safeEqual(q, PROXY_TOKEN)) {
      return { authorized: true, setCookie: true };
    }
    // A token generated with plain base64 (or any token containing `+`) is
    // decoded by URLSearchParams as a space. Compare the RAW value too, so both
    // an encoded and an unencoded link work.
    const raw = rawQueryValue(req.url, 'token');
    if (raw && safeEqual(raw, PROXY_TOKEN)) {
      return { authorized: true, setCookie: true };
    }
  } catch {
    /* malformed URL: treat as unauthorized */
  }

  return { authorized: false, setCookie: false };
}

/**
 * Reads a query parameter without percent/plus decoding.
 *
 * @param {string} rawUrl - the request URL.
 * @param {string} name - the parameter name.
 * @returns {string|null} the raw value.
 */
function rawQueryValue(rawUrl, name) {
  const qIndex = String(rawUrl || '').indexOf('?');
  if (qIndex < 0) return null;
  const query = String(rawUrl).slice(qIndex + 1).split('#')[0];
  for (const pair of query.split('&')) {
    const eq = pair.indexOf('=');
    if (eq < 0) continue;
    if (pair.slice(0, eq) === name) return pair.slice(eq + 1);
  }
  return null;
}

/**
 * Backwards-compatible check.
 * @param {http.IncomingMessage} req
 * @returns {boolean}
 */
function authorized(req) {
  return checkAuth(req).authorized;
}

/**
 * Resolve the target DSH Web port.
 *
 * The DSH Web target is pinned by `DSH_WEB_URL`, so that port is used directly.
 * @returns {number|null} the target port, or null when DSH_WEB_URL names none.
 */
let cachedTargetPort = null;
let lastCheck = 0;

function getTargetPort() {
  const now = Date.now();
  if (cachedTargetPort && now - lastCheck < 3000) return cachedTargetPort;
  if (TARGET_PORT) {
    cachedTargetPort = TARGET_PORT;
    lastCheck = now;
    return TARGET_PORT;
  }
  return null;
}

/** Shown when DSH Web is not reachable (service stopped or not yet up). */
const DSH_DOWN_PAGE = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>DeepSeek Harness</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: sans-serif; text-align: center; padding: 40px; background: #0f172a; color: #f8fafc;">
  <h2>DeepSeek Harness is not running</h2>
  <p>The DSH Web service is not currently reachable on the host machine.</p>
  <p>Please start the service and refresh this page.</p>
</body>
</html>`;

/**
 * DSH session bootstrap.
 *
 * DSH authenticates the browser separately from this proxy: `dsh web` prints a
 * one-shot launch token, and only a request carrying it (`GET /?token=<token>`)
 * mints the authority-bound session cookie. The proxy therefore exchanges that
 * token on the client's behalf — the launch token never reaches the browser and
 * never appears in a URL the user can copy.
 */
let cachedLaunchToken = null;
let cachedLaunchTokenAt = 0;
const LAUNCH_TOKEN_TTL_MS = 30000;

/**
 * Launch-token candidates from the service journal, newest first.
 *
 * `journalctl -g` behaves differently across versions and ties us to upstream's
 * log text, so raw records are fetched and filtered here (same approach as the
 * tray and the Chrome fallback).
 *
 * @returns {Promise<string[]>} candidate tokens.
 */
function launchTokenCandidates() {
  return new Promise((resolve) => {
    execFile(
      'journalctl',
      ['--user', '-u', DSH_SERVICE, '--no-pager', '-n', '200', '--output=cat'],
      { timeout: 8000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout) => {
        if (err || !stdout) {
          resolve([]);
          return;
        }
        const found = [];
        for (const line of String(stdout).split('\n')) {
          if (!line.includes('dsh web:') || !line.includes('http')) continue;
          const re = /[?&]token=([A-Za-z0-9_.-]+)/g;
          let m;
          while ((m = re.exec(line)) !== null) {
            if (!found.includes(m[1])) found.push(m[1]);
          }
        }
        resolve(found.reverse());
      }
    );
  });
}

/**
 * Asks DSH to exchange a launch token for its session cookie.
 *
 * @param {string} launchToken - candidate launch token.
 * @param {number} targetPort - upstream DSH port.
 * @returns {Promise<string[]|null>} the Set-Cookie values, or null.
 */
function requestSessionCookies(launchToken, targetPort) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const req = http.request(
      {
        host: TARGET_HOST,
        port: targetPort,
        path: `/?token=${encodeURIComponent(launchToken)}`,
        method: 'GET',
        headers: { host: `${TARGET_HOST}:${targetPort}` },
      },
      (upstream) => {
        const cookies = upstream.headers['set-cookie'];
        upstream.resume();
        if (upstream.statusCode === 303 && Array.isArray(cookies) && cookies.length > 0) {
          done(cookies);
        } else {
          done(null);
        }
      }
    );
    req.setTimeout(5000, () => req.destroy());
    req.on('error', () => done(null));
    req.end();
  });
}

/**
 * Mints a DSH session cookie for the requesting browser.
 *
 * @param {number} targetPort - upstream DSH port.
 * @returns {Promise<string[]|null>} Set-Cookie values, or null when DSH's launch
 *   token could not be found or DSH did not accept it.
 */
async function bootstrapSession(targetPort) {
  const now = Date.now();
  if (cachedLaunchToken && now - cachedLaunchTokenAt < LAUNCH_TOKEN_TTL_MS) {
    const cookies = await requestSessionCookies(cachedLaunchToken, targetPort);
    if (cookies) return cookies;
    cachedLaunchToken = null;
  }
  for (const candidate of await launchTokenCandidates()) {
    const cookies = await requestSessionCookies(candidate, targetPort);
    if (cookies) {
      cachedLaunchToken = candidate;
      cachedLaunchTokenAt = Date.now();
      return cookies;
    }
  }
  return null;
}

/**
 * Is this the application-root request that starts a browser session?
 *
 * @param {http.IncomingMessage} req
 * @returns {boolean}
 */
function isBootstrapRequest(req) {
  if (req.method !== 'GET') return false;
  const path = String(req.url || '/').split('?')[0].split('#')[0];
  return path === '/' || path === '';
}

const server = http.createServer((req, res) => {
  // 1) Authentication: with remote access on, the shared secret is required.
  const { authorized: isAuth, setCookie } = checkAuth(req);
  if (!isAuth) {
    res.writeHead(401, withSecurityHeaders({
      'Content-Type': 'text/plain; charset=utf-8',
      'WWW-Authenticate': 'Bearer realm="dsh-proxy"',
    }));
    res.end('DSH proxy: unauthorized. Supply the DSH_PROXY_TOKEN value.');
    return;
  }

  const targetPort = getTargetPort();
  if (!targetPort) {
    const errHeaders = withSecurityHeaders({ 'Content-Type': 'text/html; charset=utf-8' });
    if (setCookie) attachSetCookie(errHeaders, PROXY_TOKEN);
    res.writeHead(503, errHeaders);
    res.end(DSH_DOWN_PAGE);
    return;
  }

  // 1b) Session bootstrap: the client proved the proxy token in `?token=`, so
  //     exchange DSH's launch token for its session cookie and hand the cookie
  //     to the browser. Without this a first-time remote browser is locked out:
  //     DSH requires its own token or cookie, and the proxy token is neither.
  if (setCookie && isBootstrapRequest(req)) {
    bootstrapSession(targetPort)
      .then((cookies) => {
        if (res.headersSent) return;
        if (!cookies) {
          // DSH's launch token could not be found: proxy the request anyway so
          // the browser sees DSH's own message instead of a proxy error.
          console.error('[PROXY] session bootstrap: no usable DSH launch token; proxying the request as-is');
          forward(req, res, targetPort, setCookie);
          return;
        }
        const headers = withSecurityHeaders({
          Location: './',
          'Cache-Control': 'no-store',
          'Referrer-Policy': 'no-referrer',
        });
        headers['set-cookie'] = [...cookies];
        attachSetCookie(headers, PROXY_TOKEN);
        res.writeHead(303, headers);
        res.end();
      })
      .catch((err) => {
        console.error(`[PROXY] session bootstrap failed: ${err.message}`);
        if (!res.headersSent) forward(req, res, targetPort, setCookie);
      });
    return;
  }

  forward(req, res, targetPort, setCookie);
});

/**
 * Builds the header set sent upstream.
 *
 * Host is normalized to loopback so DSH's trust fence accepts the reverse
 * proxy; the client's host is preserved in `X-Forwarded-Host`. The proxy's own
 * credentials (bearer token, proxy cookie, token-bearing referer) are removed:
 * upstream has no use for them, and forwarding a secret to another process is a
 * needless leak.
 *
 * @param {http.IncomingMessage} req
 * @param {number} targetPort
 * @returns {Record<string, any>} headers for the upstream request.
 */
function buildUpstreamHeaders(req, targetPort) {
  const headers = { ...req.headers };
  if (req.headers['host']) {
    headers['x-forwarded-host'] = req.headers['host'];
  }
  headers['host'] = `${TARGET_HOST}:${targetPort}`;

  if (headers['origin']) {
    headers['origin'] = `http://${TARGET_HOST}:${targetPort}`;
  }

  if (headers['authorization']) {
    const m = /^Bearer\s+(.+)$/i.exec(String(headers['authorization']));
    if (m && safeEqual(m[1].trim(), PROXY_TOKEN)) delete headers['authorization'];
  }
  if (headers['cookie']) {
    const kept = stripProxyCookie(String(headers['cookie']));
    if (kept) headers['cookie'] = kept;
    else delete headers['cookie'];
  }
  if (headers['referer']) headers['referer'] = stripTokenFromUrl(headers['referer']);
  return headers;
}

/**
 * Proxies one request upstream.
 *
 * Split out of the server callback so the session-bootstrap path can reuse it.
 *
 * @param {http.IncomingMessage} req
 * @param {http.ServerResponse} res
 * @param {number} targetPort
 * @param {boolean} setCookie - attach the proxy cookie to the response.
 */
function forward(req, res, targetPort, setCookie) {
  // 2) Headers: Normalize Host and Origin headers to loopback so upstream DSH accepts the
  //    reverse-proxied request behind the authentication layer, and preserve original
  //    host in X-Forwarded-Host.
  const headers = buildUpstreamHeaders(req, targetPort);

  // Sanitize path so upstream does not collide with processLaunchToken
  const upstreamPath = sanitizePath(req.url);

  const isPotentialHtml = req.method === 'GET'
    && (upstreamPath === '/' || upstreamPath.startsWith('/?') || upstreamPath.endsWith('.html')
        || (headers['accept'] && headers['accept'].includes('text/html')));
  if (isPotentialHtml) {
    // Compression is disabled so the injection stays clean.
    delete headers['accept-encoding'];
  }

  // Upstream must at least start answering. This is a HEADERS timeout only and
  // is cleared as soon as the response arrives, so a long-lived SSE stream with
  // quiet periods is never cut off.
  let headerTimedOut = false;
  const headerTimer = setTimeout(() => {
    headerTimedOut = true;
    proxyReq.destroy(new Error('upstream did not respond in time'));
  }, UPSTREAM_HEADER_TIMEOUT_MS);

  const proxyReq = http.request(
    {
      host: TARGET_HOST,
      port: targetPort,
      path: upstreamPath,
      method: req.method,
      headers: headers,
    },
    (proxyRes) => {
      clearTimeout(headerTimer);
      // An upstream response stream that errors mid-flight would otherwise throw
      // an unhandled 'error' event and take the whole proxy process down.
      proxyRes.on('error', (err) => {
        console.error(`[PROXY] upstream response error: ${err.message}`);
        if (!res.headersSent) {
          res.writeHead(502, withSecurityHeaders({ 'Content-Type': 'text/plain; charset=utf-8' }));
        }
        res.end();
      });
      const contentType = proxyRes.headers['content-type'] || '';
      if (isPotentialHtml && contentType.includes('text/html')) {
        const chunks = [];
        let total = 0;
        // Memory guard: stream very large responses instead of buffering.
        const MAX_BUFFER = 4 * 1024 * 1024;
        let aborted = false;

        const onData = (chunk) => {
          total += chunk.length;
          if (total > MAX_BUFFER) {
            aborted = true;
            proxyRes.removeAllListeners('data');
            const responseHeaders = withSecurityHeaders({ ...proxyRes.headers });
            if (setCookie) attachSetCookie(responseHeaders, PROXY_TOKEN);
            res.writeHead(proxyRes.statusCode, responseHeaders);
            if (chunks.length > 0) {
              res.write(Buffer.concat(chunks));
            }
            res.write(chunk);
            proxyRes.pipe(res);
            return;
          }
          chunks.push(chunk);
        };

        proxyRes.on('data', onData);
        proxyRes.on('end', () => {
          if (aborted) return;
          let html = Buffer.concat(chunks).toString('utf-8');
          html = injectPolyfill(html);

          const responseHeaders = withSecurityHeaders({ ...proxyRes.headers });
          delete responseHeaders['transfer-encoding'];
          delete responseHeaders['etag'];
          responseHeaders['content-type'] = 'text/html; charset=utf-8';
          responseHeaders['content-length'] = Buffer.byteLength(html);
          if (setCookie) attachSetCookie(responseHeaders, PROXY_TOKEN);
          res.writeHead(proxyRes.statusCode, responseHeaders);
          res.end(html);
        });
      } else {
        const responseHeaders = withSecurityHeaders({ ...proxyRes.headers });
        if (setCookie) attachSetCookie(responseHeaders, PROXY_TOKEN);
        res.writeHead(proxyRes.statusCode, responseHeaders);
        proxyRes.pipe(res);
      }
    }
  );

  proxyReq.on('error', (err) => {
    clearTimeout(headerTimer);
    if (res.headersSent) return;
    if (headerTimedOut) {
      res.writeHead(504, withSecurityHeaders({ 'Content-Type': 'text/plain; charset=utf-8' }));
      res.end('Proxy error: the DSH Web service did not start responding in time.');
      return;
    }
    if (err.code === 'ECONNREFUSED') {
      // The service is down: show the readable page instead of a bare 502 line.
      const errHeaders = withSecurityHeaders({ 'Content-Type': 'text/html; charset=utf-8' });
      if (setCookie) attachSetCookie(errHeaders, PROXY_TOKEN);
      res.writeHead(503, errHeaders);
      res.end(DSH_DOWN_PAGE);
      return;
    }
    res.writeHead(502, withSecurityHeaders({ 'Content-Type': 'text/plain; charset=utf-8' }));
    res.end(`Proxy error: ${err.message}`);
  });

  req.pipe(proxyReq);
}

/**
 * Headers added to every response the proxy writes.
 *
 * DSH itself sends neither, and the session cookie is SameSite=Lax, so a
 * cross-site frame only ever shows the 401 page — but the tokenless
 * loopback-only mode is worth the extra guard.
 *
 * @param {object} headers - response headers to extend in place.
 * @returns {object} the same object.
 */
function withSecurityHeaders(headers) {
  headers['x-frame-options'] = 'SAMEORIGIN';
  headers['x-content-type-options'] = 'nosniff';
  return headers;
}

/**
 * Insert the polyfill script into the HTML `<head>`.
 *
 * An earlier version only looked for a bare `<head>` and its `</head>`
 * fallback appended the script AFTER `</head>` — that is, outside `<head>`
 * and after the module scripts. Any variant such as `<head lang="en">`
 * would have left the polyfill ineffective. Here a regex preserves the tag
 * attributes and inserts right after the opening tag.
 *
 * @param {string} html
 * @returns {string}
 */
function injectPolyfill(html) {
  const m = /<head(\s[^>]*)?>/i.exec(html);
  if (m) {
    const at = m.index + m[0].length;
    return html.slice(0, at) + POLYFILL_SCRIPT + html.slice(at);
  }
  if (/<\/head>/i.test(html)) {
    return html.replace(/<\/head>/i, `${POLYFILL_SCRIPT}</head>`);
  }
  return POLYFILL_SCRIPT + html;
}

server.on('upgrade', (req, socket, head) => {
  // WebSocket upgrades go through authentication too.
  if (!authorized(req)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }

  const targetPort = getTargetPort();
  const upstreamPath = sanitizePath(req.url);
  // The query string (which may carry a token) is not written to the journal.
  const safePath = String(upstreamPath).split('?')[0];
  if (!targetPort) {
    socket.destroy();
    return;
  }

  const targetSocket = net.connect(targetPort, TARGET_HOST, () => {
    const headers = buildUpstreamHeaders(req, targetPort);

    let rawReq = `${req.method} ${upstreamPath} HTTP/${req.httpVersion}\r\n`;
    for (const [key, value] of Object.entries(headers)) {
      if (Array.isArray(value)) {
        for (const v of value) rawReq += `${key}: ${v}\r\n`;
      } else {
        rawReq += `${key}: ${value}\r\n`;
      }
    }
    rawReq += '\r\n';

    targetSocket.write(rawReq);
    if (head && head.length > 0) targetSocket.write(head);

    targetSocket.pipe(socket);
    socket.pipe(targetSocket);
  });

  targetSocket.on('error', (err) => {
    console.error(`[UPGRADE] target socket error (${safePath}):`, err.message);
    socket.destroy();
  });

  socket.on('error', () => {
    targetSocket.destroy();
  });
});

// Slow-client (slowloris) protection.
server.headersTimeout = 20000;
server.requestTimeout = 120000;

server.on('error', (err) => {
  // Without this an EADDRINUSE becomes an uncaught exception and systemd
  // restarts the unit every three seconds without ever saying why.
  if (err.code === 'EADDRINUSE') {
    console.error(`DSH proxy: ${LISTEN_ADDR}:${PROXY_PORT} is already in use. ` +
      'Stop the other listener or change DSH_PROXY_PORT.');
  } else {
    console.error(`DSH proxy: server error: ${err.message}`);
  }
  process.exit(1);
});

server.listen(PROXY_PORT, LISTEN_ADDR, () => {
  const scope = REMOTE_ENABLED
    ? `remote access ENABLED, protected by shared secret (${LISTEN_ADDR}:${PROXY_PORT})`
    : `loopback only (${LISTEN_ADDR}:${PROXY_PORT}), remote access disabled`;
  console.log(`DSH proxy running: ${scope} -> ${WEB_URL.origin}`);
});
