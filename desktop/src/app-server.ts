/**
 * The single loopback origin the desktop window talks to.
 *
 * Why a shell server at all: the packaged app has two HTTP servers (FastAPI for
 * the API, the Next.js standalone server for the UI) on ports chosen at launch.
 * Loading the Next server's origin directly would make every `/api` call
 * cross-origin — the session cookie would be third-party and the CSRF
 * double-submit would weaken. So the shell publishes ONE origin and routes:
 *
 *   /api/...        → FastAPI backend       (cookies, CSRF, SSE, uploads)
 *   /__desktop/...  → shell metadata/health (token-protected)
 *   everything else → Next.js UI server     (HTML, chunks, RSC payloads)
 *
 * The backend port is only known at runtime, which is exactly why the proxy lives
 * here instead of in Next's build-time `rewrites()`.
 *
 * Security posture:
 *  * binds loopback only;
 *  * the backend/UI origins are fixed at launch, never taken from the request;
 *  * hop-by-hop headers are stripped in both directions;
 *  * API responses are `no-store` so the renderer cannot serve stale state;
 *  * SSE and large bodies are streamed and flushed per chunk, never buffered.
 */

import { createServer, IncomingMessage, Server, ServerResponse } from 'node:http';

import type { LogSink } from './supervisor';

const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  // `Expect: 100-continue` is a client-to-first-hop handshake. Forwarding it makes
  // undici wait for an interim response that never reaches the caller, and the request
  // fails with a 502. PowerShell's Invoke-RestMethod always sends it, so an external
  // agent using PowerShell would otherwise be unable to call anything through the
  // desktop app.
  'expect',
]);

export interface ShellInfo {
  appVersion: string;
  backendOrigin: string;
  sidecarPort: number;
  dataDir: string;
  logsDir: string;
  platform: string;
  arch: string;
  packaged: boolean;
  enginePackage: string;
  engineVersion: string;
  adapterVersion: string;
}

export interface AppServerOptions {
  shellToken: string;
  sink: LogSink;
  info: () => ShellInfo;
  health: () => Promise<unknown>;
}

export class AppServer {
  private server: Server | null = null;
  private port = 0;
  private backendOrigin = '';
  private webOrigin = '';
  private readonly options: AppServerOptions;

  constructor(options: AppServerOptions) {
    this.options = options;
    this.backendOrigin = options.info().backendOrigin;
  }

  setBackendOrigin(origin: string): void {
    this.backendOrigin = origin;
  }

  setWebOrigin(origin: string): void {
    this.webOrigin = origin;
  }

  get origin(): string {
    return `http://127.0.0.1:${this.port}`;
  }

  async listen(port: number): Promise<number> {
    this.server = createServer((req, res) => {
      void this.handle(req, res).catch((error: unknown) => {
        this.options.sink.error('shell request failed', { error: String(error), url: req.url });
        if (!res.headersSent) {
          res.writeHead(502, { 'content-type': 'application/json' });
          res.end(
            JSON.stringify({
              error: {
                code: 'shell_proxy_error',
                message: `The desktop shell could not reach the service behind this request: ${String(error)}`,
                actions: ['Check the status panel in Settings → Desktop, then restart the API.'],
              },
            }),
          );
        } else {
          res.end();
        }
      });
    });
    await new Promise<void>((resolve, reject) => {
      this.server!.once('error', reject);
      this.server!.listen(port, '127.0.0.1', () => resolve());
    });
    const address = this.server.address();
    this.port = address && typeof address === 'object' ? address.port : port;
    return this.port;
  }

  async close(): Promise<void> {
    if (!this.server) return;
    const server = this.server;
    this.server = null;
    await new Promise<void>((resolve) => {
      // `close()` alone waits for existing keep-alive sockets, which would hang a
      // quit while the UI (or a verification script) still holds a connection.
      // Drop them, then wait for the listener to report closed.
      const done = (): void => resolve();
      server.close(done);
      server.closeAllConnections?.();
      // Belt and braces: never block shutdown for more than a moment.
      setTimeout(done, 1500).unref?.();
    });
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');

    if (url.pathname.startsWith('/__desktop/')) {
      await this.handleShellRoute(req, res, url);
      return;
    }
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
      await this.proxy(req, res, url, this.backendOrigin, { noStore: true });
      return;
    }
    if (!this.webOrigin) {
      res.writeHead(503, { 'content-type': 'text/html; charset=utf-8' });
      res.end(
        '<!doctype html><meta charset="utf-8"><title>Skill Router</title>' +
          '<body style="font-family:system-ui;padding:40px;max-width:640px">' +
          '<h1>Starting…</h1><p>The interface server is not ready yet. This page refreshes automatically.</p>' +
          '<script>setTimeout(function(){location.reload()},1500)</script>',
      );
      return;
    }
    await this.proxy(req, res, url, this.webOrigin, { noStore: false });
  }

  private authorised(req: IncomingMessage): boolean {
    const header = req.headers['x-shell-token'];
    const value = Array.isArray(header) ? header[0] : header;
    return Boolean(value) && value === this.options.shellToken;
  }

  private async handleShellRoute(req: IncomingMessage, res: ServerResponse, url: URL): Promise<void> {
    // Navigation requests from the window cannot set a header, but they never ask
    // for shell routes; only the renderer's fetch does, and it sends the token.
    if (url.pathname === '/__desktop/ping') {
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify({ ok: true }));
      return;
    }
    if (!this.authorised(req)) {
      res.writeHead(401, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { code: 'shell_unauthorised', message: 'Missing shell token.' } }));
      return;
    }
    if (url.pathname === '/__desktop/info' && req.method === 'GET') {
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify(this.options.info()));
      return;
    }
    if (url.pathname === '/__desktop/health' && req.method === 'GET') {
      const report = await this.options.health();
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify(report));
      return;
    }
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { code: 'shell_route_unknown', message: 'Unknown shell route.' } }));
  }

  /** Stream a request to `target` and the response back, preserving headers. */
  private async proxy(
    req: IncomingMessage,
    res: ServerResponse,
    url: URL,
    targetOrigin: string,
    opts: { noStore: boolean },
  ): Promise<void> {
    const target = new URL(url.pathname + url.search, targetOrigin);
    const headers: Record<string, string> = {};
    for (const [name, value] of Object.entries(req.headers)) {
      if (!value || HOP_BY_HOP.has(name.toLowerCase())) continue;
      if (name.toLowerCase() === 'x-shell-token') continue;
      headers[name] = Array.isArray(value) ? value.join(', ') : value;
    }
    // Present the request as coming from this origin: the API validates Origin
    // against SR_TRUSTED_ORIGINS and the UI server accepts any host header.
    headers.host = new URL(targetOrigin).host;
    headers['x-forwarded-host'] = url.host;
    headers['x-forwarded-proto'] = 'http';
    headers['x-forwarded-for'] = '127.0.0.1';

    const hasBody = req.method !== 'GET' && req.method !== 'HEAD';
    const abort = new AbortController();
    req.on('aborted', () => abort.abort());
    res.on('close', () => abort.abort());

    const upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? (req as unknown as BodyInit) : undefined,
      duplex: hasBody ? 'half' : undefined,
      redirect: 'manual',
      signal: abort.signal,
    } as RequestInit & { duplex?: 'half' });

    const responseHeaders: Record<string, string | string[]> = {};
    upstream.headers.forEach((value, name) => {
      const lowered = name.toLowerCase();
      if (HOP_BY_HOP.has(lowered)) return;
      if (opts.noStore && lowered === 'cache-control') return;
      // `fetch` has already decoded the body (undici sends accept-encoding and
      // unzips), so forwarding `content-encoding`/`content-length` would describe
      // the *compressed* payload while we write plain bytes: the client then waits
      // for a gunzip that never completes. Let Node re-frame the body as chunked.
      if (lowered === 'content-encoding' || lowered === 'content-length') return;
      // `set-cookie` is the one header that must not be comma-joined: a browser (or
      // any client) parsing "sr_session=…, sr_csrf=…" as a single cookie breaks
      // sign-in. Undici exposes the individual values through getSetCookie().
      if (lowered === 'set-cookie') return;
      responseHeaders[name] = value;
    });
    if (opts.noStore) responseHeaders['cache-control'] = 'no-store';
    const setCookies = upstream.headers.getSetCookie?.() ?? [];
    if (setCookies.length) {
      responseHeaders['set-cookie'] = setCookies;
    }
    res.writeHead(upstream.status, responseHeaders);

    if (!upstream.body) {
      res.end();
      return;
    }
    const reader = upstream.body.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!res.write(Buffer.from(value))) {
          await new Promise((resolve) => res.once('drain', resolve));
        }
        // Flush immediately so SSE (agent progress, streaming completions) is not
        // held in a socket buffer until the next chunk arrives.
        const flushable = res as ServerResponse & { flush?: () => void };
        if (typeof flushable.flush === 'function') flushable.flush();
      }
    } catch (error) {
      this.options.sink.warn('proxy stream interrupted', { url: url.pathname, error: String(error) });
    } finally {
      void reader.cancel().catch(() => undefined);
      res.end();
    }
  }
}
