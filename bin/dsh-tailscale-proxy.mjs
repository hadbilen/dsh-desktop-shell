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
 *  2. `Host`, `Origin` and `Sec-Fetch-Site` headers are NEVER rewritten.
 *     The upstream trust fence (dsh-client-connection/lib/index.js) treats a
 *     request as trusted only when Host is loopback AND sec-fetch-site is not
 *     cross-site AND Origin equals Host. "Fixing" those three disables the
 *     fence entirely. Use `--trusted-host <address>` on the DSH side instead.
 *
 *  3. The proxy REQUIRES authentication. When the `DSH_PROXY_TOKEN` environment
 *     variable is set, every request must carry `Authorization: Bearer <token>`
 *     or `?token=<token>`. With no token the proxy binds to loopback only and
 *     offers no remote access (the safe default).
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

const PROXY_PORT = Number(process.env.DSH_PROXY_PORT || 3000);
const BIND_ADDR = process.env.DSH_PROXY_BIND || '127.0.0.1';
const PROXY_TOKEN = (process.env.DSH_PROXY_TOKEN || '').trim();
const WEB_URL = new URL(process.env.DSH_WEB_URL || 'http://127.0.0.1:3080');
const TARGET_PORT = Number(WEB_URL.port || 80);
const TARGET_HOST = WEB_URL.hostname;

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
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
        var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
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

/**
 * Checks whether the request carries the valid shared secret.
 * @param {http.IncomingMessage} req
 * @returns {boolean}
 */
function authorized(req) {
  if (!REMOTE_ENABLED) return true; // listening on loopback only
  const header = req.headers['authorization'] || '';
  const m = /^Bearer\s+(.+)$/i.exec(String(header));
  if (m && safeEqual(m[1].trim(), PROXY_TOKEN)) return true;
  try {
    const url = new URL(req.url || '/', 'http://localhost');
    const q = url.searchParams.get('token');
    if (q && safeEqual(q, PROXY_TOKEN)) return true;
  } catch {
    /* malformed URL: treat as unauthorized */
  }
  return false;
}

/**
 * Resolve the target DSH Web port.
 *
 * The DSH Web target is pinned by `DSH_WEB_URL`, so that port is used
 * directly; the port scan is only a fallback.
 * @returns {number|null}
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

const server = http.createServer((req, res) => {
  // 1) Authentication: with remote access on, the shared secret is required.
  if (!authorized(req)) {
    res.writeHead(401, {
      'Content-Type': 'text/plain; charset=utf-8',
      'WWW-Authenticate': 'Bearer realm="dsh-proxy"',
    });
    res.end('DSH proxy: unauthorized. Supply the DSH_PROXY_TOKEN value.');
    return;
  }

  const targetPort = getTargetPort();
  if (!targetPort) {
    res.writeHead(503, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>DeepSeek Harness</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: sans-serif; text-align: center; padding: 40px; background: #0f172a; color: #f8fafc;">
  <h2>DeepSeek Harness is not running</h2>
  <p>The DSH Web service is not currently reachable on the host machine.</p>
  <p>Please start the service and refresh this page.</p>
</body>
</html>`);
    return;
  }

  // 2) Headers are passed through verbatim. Host/Origin/Sec-Fetch-Site are
  //    not rewritten: the upstream trust fence relies on those values.
  const headers = { ...req.headers };
  // DSH listens on loopback only, so the connection target is fixed; the
  // Host header keeps the value the client saw and the fence evaluates it.

  const isPotentialHtml = req.method === 'GET'
    && (req.url === '/' || req.url.startsWith('/?') || req.url.endsWith('.html')
        || (headers['accept'] && headers['accept'].includes('text/html')));
  if (isPotentialHtml) {
    // Compression is disabled so the injection stays clean.
    delete headers['accept-encoding'];
  }

  const proxyReq = http.request(
    {
      host: TARGET_HOST,
      port: targetPort,
      path: req.url,
      method: req.method,
      headers: headers,
    },
    (proxyRes) => {
      const contentType = proxyRes.headers['content-type'] || '';
      if (isPotentialHtml && contentType.includes('text/html')) {
        const chunks = [];
        let total = 0;
        // Memory guard: stream very large responses instead of buffering.
        const MAX_BUFFER = 4 * 1024 * 1024;
        let aborted = false;
        proxyRes.on('data', (chunk) => {
          total += chunk.length;
          if (total > MAX_BUFFER) {
            if (!aborted) {
              aborted = true;
              res.writeHead(proxyRes.statusCode, proxyRes.headers);
              res.write(Buffer.concat(chunks));
              proxyRes.pipe(res);
            }
            res.write(chunk);
            return;
          }
          chunks.push(chunk);
        });
        proxyRes.on('end', () => {
          if (aborted) { res.end(); return; }
          let html = Buffer.concat(chunks).toString('utf-8');
          html = injectPolyfill(html);

          const responseHeaders = { ...proxyRes.headers };
          delete responseHeaders['transfer-encoding'];
          delete responseHeaders['etag'];
          responseHeaders['content-type'] = 'text/html; charset=utf-8';
          responseHeaders['content-length'] = Buffer.byteLength(html);
          res.writeHead(proxyRes.statusCode, responseHeaders);
          res.end(html);
        });
      } else {
        res.writeHead(proxyRes.statusCode, proxyRes.headers);
        proxyRes.pipe(res);
      }
    }
  );

  proxyReq.on('error', (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end(`Proxy error: ${err.message}`);
    }
  });

  req.pipe(proxyReq);
});

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
  // The query string (which may carry a token) is not written to the journal.
  const safePath = String(req.url || '/').split('?')[0];
  if (!targetPort) {
    socket.destroy();
    return;
  }

  const targetSocket = net.connect(targetPort, TARGET_HOST, () => {
    // Headers are passed through verbatim (see rule 2 on the HTTP path).
    const headers = { ...req.headers };

    let rawReq = `${req.method} ${req.url} HTTP/${req.httpVersion}\r\n`;
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

server.listen(PROXY_PORT, LISTEN_ADDR, () => {
  const scope = REMOTE_ENABLED
    ? `remote access ENABLED, protected by shared secret (${LISTEN_ADDR}:${PROXY_PORT})`
    : `loopback only (${LISTEN_ADDR}:${PROXY_PORT}), remote access disabled`;
  console.log(`DSH proxy running: ${scope} -> ${WEB_URL.origin}`);
});
