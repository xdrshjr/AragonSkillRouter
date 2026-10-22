/**
 * Assemble `desktop/resources/runtime/` — everything the packaged app runs.
 *
 *   runtime/backend/  PyInstaller one-folder build of the FastAPI backend
 *   runtime/sidecar/  compiled AragonAgent sidecar + its production node_modules
 *
 * Both steps are skipped with a loud, actionable message when a tool is missing,
 * because a half-built runtime produces a desktop app that starts and then fails
 * in a way the user cannot diagnose. The Electron main process also refuses to
 * start with a clear error when the runtime is incomplete (see supervisor.ts).
 */

import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopDir = path.resolve(here, '..');
const repoRoot = path.resolve(desktopDir, '..');
const runtimeDir = path.join(desktopDir, 'resources', 'runtime');
const buildDir = path.join(desktopDir, 'build');

const args = new Set(process.argv.slice(2));
const skipBackend = args.has('--skip-backend');
const skipSidecar = args.has('--skip-sidecar');
const skipInstaller = args.has('--skip-installer');

function fail(message) {
  console.error(`\n[build-backend] ${message}\n`);
  process.exit(1);
}

function run(command, cmdArgs, cwd, extraEnv = {}) {
  // Two different quoting rules are needed on Windows:
  //  * python.exe / .exe  → spawn directly, so a quoted `-c "…"` reaches argv
  //    intact (cmd.exe would split it and Python would see a syntax error);
  //  * npm / npx          → these are .cmd shims, and Node 22 refuses to spawn a
  //    .cmd without a shell (EINVAL). Their arguments are plain flags, so the
  //    shell adds no quoting risk here.
  const isShellShim = process.platform === 'win32' && (command === 'npm' || command === 'npx');
  const executable = isShellShim ? command : command;
  console.log(`[build-backend] ${executable} ${cmdArgs.join(' ')}  (cwd=${cwd})`);
  const result = spawnSync(executable, cmdArgs, {
    cwd,
    stdio: 'inherit',
    shell: isShellShim,
    env: { ...process.env, ...extraEnv },
  });
  if (result.error) {
    fail(`${command} could not be started: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`${command} ${cmdArgs.join(' ')} exited with ${result.status}`);
  }
}

function pythonBinary() {
  const candidates = [
    path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(repoRoot, '.venv', 'bin', 'python'),
    'python',
  ];
  for (const candidate of candidates) {
    if (candidate === 'python' || existsSync(candidate)) return candidate;
  }
  return 'python';
}

mkdirSync(runtimeDir, { recursive: true });

// ---------------------------------------------------------------- backend ----
if (!skipBackend) {
  const python = pythonBinary();
  const probe = spawnSync(python, ['-c', 'import PyInstaller, sys; print(PyInstaller.__version__)'], {
    cwd: repoRoot,
    encoding: 'utf8',
  });
  if (probe.status !== 0) {
    fail(
      `PyInstaller is not installed for ${python}.\n` +
        '  Install it into the packaging virtualenv:\n' +
        `    ${python} -m pip install -r backend/requirements-desktop.txt\n` +
        '  Or re-run with --skip-backend to reuse an existing resources/runtime/backend.',
    );
  }
  console.log(`[build-backend] PyInstaller ${probe.stdout.trim()}`);

  rmSync(path.join(runtimeDir, 'backend'), { recursive: true, force: true });
  run(
    python,
    [
      '-m',
      'PyInstaller',
      '--noconfirm',
      '--clean',
      '--distpath',
      path.join(buildDir, 'python'),
      '--workpath',
      path.join(buildDir, 'pyi-work'),
      'desktop_backend.spec',
    ],
    path.join(repoRoot, 'backend'),
  );

  const built = path.join(buildDir, 'python', 'skillrouter-backend');
  if (!existsSync(built)) fail(`PyInstaller reported success but ${built} does not exist`);
  cpSync(built, path.join(runtimeDir, 'backend'), { recursive: true });
  const exe = path.join(runtimeDir, 'backend', process.platform === 'win32' ? 'skillrouter-backend.exe' : 'skillrouter-backend');
  if (!existsSync(exe)) fail(`the bundled backend executable is missing at ${exe}`);
  console.log(`[build-backend] backend runtime ready (${Math.round(sizeOf(path.join(runtimeDir, 'backend')) / 1e6)} MB)`);
}

// ---------------------------------------------------------------- sidecar ----
if (!skipSidecar) {
  const sidecarDir = path.join(repoRoot, 'sidecar');
  if (!existsSync(path.join(sidecarDir, 'package.json'))) {
    fail(`no sidecar project at ${sidecarDir}`);
  }
  if (!existsSync(path.join(sidecarDir, 'node_modules'))) {
    console.log('[build-backend] installing sidecar dependencies');
    run('npm', ['ci', '--omit=dev', '--no-audit', '--no-fund'], sidecarDir);
  }
  console.log('[build-backend] compiling the sidecar');
  run('npm', ['run', 'build'], sidecarDir);

  const target = path.join(runtimeDir, 'sidecar');
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
  cpSync(path.join(sidecarDir, 'dist'), path.join(target, 'dist'), { recursive: true });
  cpSync(path.join(sidecarDir, 'package.json'), path.join(target, 'package.json'));
  if (existsSync(path.join(sidecarDir, 'package-lock.json'))) {
    cpSync(path.join(sidecarDir, 'package-lock.json'), path.join(target, 'package-lock.json'));
  }
  // Only production dependencies are shipped; the sidecar imports nothing else at
  // runtime (it is a plain node:http server around @aragon-agent/core).
  const modules = path.join(sidecarDir, 'node_modules');
  if (!existsSync(modules)) fail('sidecar node_modules is missing after npm ci');
  cpSync(modules, path.join(target, 'node_modules'), {
    recursive: true,
    filter: (source) => !/node_modules[\\/]\.cache/.test(source),
  });

  // A tiny launcher so Electron can start it with `node main.js` and the same
  // resolution rules the sidecar expects.
  writeFileSync(
    path.join(target, 'main.js'),
    [
      '// Generated by desktop/scripts/build-backend.mjs.',
      '// Electron runs this file as Node (ELECTRON_RUN_AS_NODE=1).',
      "import('./dist/main.js').catch((error) => {",
      '  console.error("[sidecar] fatal:", error);',
      '  process.exit(1);',
      '});',
      '',
    ].join('\n'),
    'utf8',
  );

  const version = JSON.parse(readFileSync(path.join(target, 'package.json'), 'utf8')).version ?? 'unknown';
  const engine = JSON.parse(
    readFileSync(path.join(modules, '@aragon-agent', 'core', 'package.json'), 'utf8'),
  ).version;
  writeFileSync(
    path.join(runtimeDir, 'runtime.json'),
    JSON.stringify(
      {
        builtAt: new Date().toISOString(),
        sidecar: { version, entry: 'sidecar/main.js' },
        engine: { package: '@aragon-agent/core', version: engine },
        backend: { entry: process.platform === 'win32' ? 'backend/skillrouter-backend.exe' : 'backend/skillrouter-backend' },
      },
      null,
      2,
    ),
    'utf8',
  );
  console.log(`[build-backend] sidecar runtime ready (engine @aragon-agent/core@${engine})`);
}

// -------------------------------------------------------------- installer ------
// Ship the connection CLI with the app. The Overview page tells people to run it, so
// the file has to exist on an installed machine — pointing at a repository path that
// only exists on the build box would be a command that cannot work.
if (!skipInstaller) {
  const installerDir = path.join(repoRoot, 'packages', 'installer');
  if (!existsSync(path.join(installerDir, 'bin', 'skill-router-connect.js'))) {
    fail(`no installer CLI at ${installerDir}`);
  }
  const target = path.join(runtimeDir, 'installer');
  rmSync(target, { recursive: true, force: true });
  mkdirSync(target, { recursive: true });
  cpSync(path.join(installerDir, 'bin'), path.join(target, 'bin'), { recursive: true });
  cpSync(path.join(installerDir, 'src'), path.join(target, 'src'), { recursive: true });
  cpSync(path.join(installerDir, 'package.json'), path.join(target, 'package.json'));
  const cli = path.join(target, 'bin', 'skill-router-connect.js');
  if (!existsSync(cli)) fail(`installer copy failed: ${cli} is missing`);
  const version = JSON.parse(readFileSync(path.join(target, 'package.json'), 'utf8')).version;
  console.log(`[build-backend] installer CLI ready (skill-router-connect ${version})`);
}

function sizeOf(target) {
  if (!existsSync(target)) return 0;
  const info = statSync(target);
  if (!info.isDirectory()) return info.size;
  let total = 0;
  for (const entry of readdirSync(target)) {
    total += sizeOf(path.join(target, entry));
  }
  return total;
}
