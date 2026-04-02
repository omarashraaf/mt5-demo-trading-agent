import { app, dialog, shell } from 'electron';
import path from 'path';
import fs from 'fs';
import { spawn, ChildProcess } from 'child_process';
import http from 'http';
import type { Server as HttpServer } from 'http';

const isDev = !app.isPackaged;
let backendProcess: ChildProcess | null = null;
let webServer: HttpServer | null = null;

const API_HOST = '127.0.0.1';
const API_PORT = 8000;
const API_STATUS_PATH = '/api/status';
const WEB_HOST = '127.0.0.1';
const WEB_PORT = 5173;
const DESKTOP_WEB_URL = process.env.LINKTRADE_WEB_URL || `http://${WEB_HOST}:${WEB_PORT}/auth`;

function writeStartupLog(message: string): void {
  try {
    const line = `[${new Date().toISOString()}] ${message}\n`;
    const logPath = path.join(app.getPath('userData'), 'startup.log');
    fs.appendFileSync(logPath, line, 'utf8');
  } catch {
    // best effort only
  }
}

function backendRuntimeDir(): string {
  return path.join(app.getPath('userData'), 'backend-runtime');
}

function stableDbPath(): string {
  return path.join(app.getPath('userData'), 'trading_agent.db');
}

function prepareBackendRuntime(backendDir: string): string {
  const runtimeDir = backendRuntimeDir();
  if (!fs.existsSync(runtimeDir)) {
    fs.mkdirSync(runtimeDir, { recursive: true });
  }

  const envSource = path.join(backendDir, '.env');
  const envExampleSource = path.join(backendDir, '.env.example');
  const envTarget = path.join(runtimeDir, '.env');
  const envExampleTarget = path.join(runtimeDir, '.env.example');
  const userDataEnv = path.join(app.getPath('userData'), '.env');
  const cwdEnv = path.join(process.cwd(), '.env');

  // Prefer an existing runtime .env. If missing, seed from the first available source.
  const envSeedCandidates = [envSource, userDataEnv, cwdEnv];
  if (!fs.existsSync(envTarget)) {
    for (const candidate of envSeedCandidates) {
      if (fs.existsSync(candidate)) {
        fs.copyFileSync(candidate, envTarget);
        writeStartupLog(`Seeded runtime .env from ${candidate}`);
        break;
      }
    }
  }

  if (fs.existsSync(envExampleSource) && !fs.existsSync(envExampleTarget)) {
    fs.copyFileSync(envExampleSource, envExampleTarget);
  }

  return runtimeDir;
}

function copyIfMissing(source: string, target: string): boolean {
  try {
    if (!fs.existsSync(source) || fs.existsSync(target)) {
      return false;
    }
    fs.copyFileSync(source, target);
    return true;
  } catch {
    return false;
  }
}

function tryMigrateLegacyDb(backendDir: string, runtimeDir: string): string {
  const target = stableDbPath();
  if (fs.existsSync(target)) {
    return target;
  }

  const legacyCandidates = [
    path.join(runtimeDir, 'trading_agent.db'),
    path.join(backendDir, 'trading_agent.db'),
    path.join(path.dirname(backendDir), 'trading_agent.db'),
  ];

  const existingCandidates = legacyCandidates
    .filter((candidate) => fs.existsSync(candidate))
    .map((candidate) => ({ candidate, size: fs.statSync(candidate).size }))
    .filter((entry) => entry.size > 0)
    .sort((a, b) => b.size - a.size);

  for (const entry of existingCandidates) {
    if (copyIfMissing(entry.candidate, target)) {
      writeStartupLog(
        `Migrated legacy DB from ${entry.candidate} (${entry.size} bytes) to ${target}`,
      );
      return target;
    }
  }

  return target;
}

function backendBaseDir(): string {
  if (isDev) {
    return path.resolve(__dirname, '../../backend');
  }
  return path.join(process.resourcesPath, 'backend');
}

function checkBackendOnce(timeoutMs = 1500): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: API_HOST,
        port: API_PORT,
        path: API_STATUS_PATH,
        method: 'GET',
        timeout: timeoutMs,
      },
      (res) => {
        resolve((res.statusCode ?? 500) < 500);
        res.resume();
      },
    );
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
    req.end();
  });
}

async function waitForBackendReady(maxWaitMs = 30000): Promise<boolean> {
  const started = Date.now();
  while (Date.now() - started < maxWaitMs) {
    const ok = await checkBackendOnce();
    if (ok) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

function contentTypeFor(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.html') return 'text/html; charset=utf-8';
  if (ext === '.js') return 'application/javascript; charset=utf-8';
  if (ext === '.css') return 'text/css; charset=utf-8';
  if (ext === '.json') return 'application/json; charset=utf-8';
  if (ext === '.png') return 'image/png';
  if (ext === '.jpg' || ext === '.jpeg') return 'image/jpeg';
  if (ext === '.svg') return 'image/svg+xml';
  if (ext === '.ico') return 'image/x-icon';
  if (ext === '.woff') return 'font/woff';
  if (ext === '.woff2') return 'font/woff2';
  return 'application/octet-stream';
}

function distBaseDir(): string {
  if (isDev) {
    return path.resolve(__dirname, '../dist');
  }
  return path.join(app.getAppPath(), 'dist');
}

function safeResolve(base: string, reqPath: string): string {
  const stripped = reqPath.replace(/^\/+/, '');
  const candidate = path.resolve(base, stripped);
  if (!candidate.startsWith(path.resolve(base))) {
    return path.resolve(base, 'index.html');
  }
  return candidate;
}

function checkWebServerOnce(timeoutMs = 1000): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: WEB_HOST,
        port: WEB_PORT,
        path: '/',
        method: 'GET',
        timeout: timeoutMs,
      },
      (res) => {
        resolve((res.statusCode ?? 500) < 500);
        res.resume();
      },
    );
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
    req.on('error', () => resolve(false));
    req.end();
  });
}

async function waitForWebReady(maxWaitMs = 10000): Promise<boolean> {
  const started = Date.now();
  while (Date.now() - started < maxWaitMs) {
    const ok = await checkWebServerOnce();
    if (ok) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function ensureWebServerRunning(): Promise<void> {
  const already = await checkWebServerOnce();
  if (already) {
    writeStartupLog('Web server already running.');
    return;
  }

  const baseDir = distBaseDir();
  const indexPath = path.join(baseDir, 'index.html');
  if (!fs.existsSync(indexPath)) {
    throw new Error(`Frontend dist not found at ${indexPath}`);
  }

  webServer = http.createServer((req, res) => {
    const urlPath = decodeURIComponent((req.url || '/').split('?')[0] || '/');
    let filePath = safeResolve(baseDir, urlPath);

    if (fs.existsSync(filePath) && fs.statSync(filePath).isDirectory()) {
      filePath = path.join(filePath, 'index.html');
    }

    // SPA fallback
    if (!fs.existsSync(filePath) || fs.statSync(filePath).isDirectory()) {
      filePath = indexPath;
    }

    try {
      const data = fs.readFileSync(filePath);
      res.writeHead(200, { 'Content-Type': contentTypeFor(filePath), 'Cache-Control': 'no-store' });
      res.end(data);
    } catch {
      res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Local web server failed to read file.');
    }
  });

  await new Promise<void>((resolve, reject) => {
    if (!webServer) {
      reject(new Error('Web server not initialized'));
      return;
    }
    webServer.once('error', reject);
    webServer.listen(WEB_PORT, WEB_HOST, () => resolve());
  });
  writeStartupLog(`Web server started at http://${WEB_HOST}:${WEB_PORT}`);

  const ready = await waitForWebReady();
  if (!ready) {
    throw new Error('Local web server did not become ready in time.');
  }
}

type LaunchSpec = { command: string; args: string[]; shell?: boolean };

function launchSpecs(backendDir: string): LaunchSpec[] {
  const mainPy = path.join(backendDir, 'main.py');
  const bundledExe = path.join(backendDir, 'LinkTradeBackend.exe');
  const venvPython = path.join(backendDir, 'venv', 'Scripts', 'python.exe');
  const specs: LaunchSpec[] = [];

  if (fs.existsSync(bundledExe)) {
    specs.push({ command: bundledExe, args: [] });
  }

  // Development fallback (packaged build should rely on bundled backend EXE only).
  if (isDev) {
    if (fs.existsSync(venvPython) && fs.existsSync(mainPy)) {
      specs.push({ command: venvPython, args: [mainPy] });
    }
    if (fs.existsSync(mainPy)) {
      specs.push({ command: 'py', args: ['-3', mainPy], shell: true });
      specs.push({ command: 'python', args: [mainPy], shell: true });
    }
  }

  return specs;
}

async function ensureBackendRunning(): Promise<void> {
  const alreadyRunning = await checkBackendOnce(1000);
  if (alreadyRunning) {
    writeStartupLog('Backend already running.');
    return;
  }

  const backendDir = backendBaseDir();
  const runtimeDir = isDev ? backendDir : prepareBackendRuntime(backendDir);
  const resolvedDbPath = process.env.DB_PATH || tryMigrateLegacyDb(backendDir, runtimeDir);
  const specs = launchSpecs(backendDir);
  writeStartupLog(`Backend dir: ${backendDir}`);
  writeStartupLog(`Runtime dir: ${runtimeDir}`);
  writeStartupLog(`DB path: ${resolvedDbPath}`);
  if (!specs.length) {
    throw new Error(`No backend launch target found in ${backendDir}`);
  }

  let startedAny = false;
  let errors: string[] = [];

  for (const spec of specs) {
    try {
      backendProcess = spawn(spec.command, spec.args, {
        cwd: runtimeDir,
        shell: spec.shell ?? false,
        stdio: 'ignore',
        detached: false,
        env: {
          ...process.env,
          PYTHONUNBUFFERED: '1',
          DB_PATH: resolvedDbPath,
        },
      });
      startedAny = true;
      writeStartupLog(`Started backend with: ${spec.command} ${spec.args.join(' ')}`);

      const ready = await waitForBackendReady(30000);
      if (ready) {
        writeStartupLog('Backend became ready.');
        return;
      }

      backendProcess.kill();
      backendProcess = null;
      errors.push(`Backend did not become ready using: ${spec.command} ${spec.args.join(' ')}`);
    } catch (error) {
      errors.push(`Failed start with ${spec.command}: ${String(error)}`);
    }
  }

  if (startedAny) {
    throw new Error(errors.join('\n'));
  }
}

app.whenReady().then(async () => {
  try {
    await ensureBackendRunning();
    await ensureWebServerRunning();
  } catch (error) {
    dialog.showErrorBox(
      'Startup Failed',
      `LinkTrade desktop app could not start local services.\n\n${String(error)}`,
    );
    app.quit();
    return;
  }
  writeStartupLog(`Opening external URL: ${DESKTOP_WEB_URL}`);
  try {
    await shell.openExternal(DESKTOP_WEB_URL);
  } catch (error) {
    dialog.showErrorBox('Open Website Failed', `Could not open LinkTrade website.\n\n${String(error)}`);
  }
  // Keep process alive in background so local backend/web services continue running.
});

app.on('window-all-closed', () => {
  // Intentionally no-op: launcher runs headless (browser-only UX) and keeps local services alive.
});

app.on('before-quit', () => {
  if (webServer) {
    webServer.close();
    webServer = null;
  }
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill();
    backendProcess = null;
  }
});
