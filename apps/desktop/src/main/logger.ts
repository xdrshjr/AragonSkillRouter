/**
 * 日志（spec §2.10 / §5.3）。
 *
 * - 每个子进程一路日志 + 一路 `main.log`；
 * - 行前缀 `[YYYY-MM-DDTHH:mm:ss.SSSZ] `，子进程行再带 `[<name>] `；
 * - 单文件 10 MB，超出后轮转为 `<name>.1.log`（最多保留 5 个）；
 * - `main.log` 的键固定为 `state=` / `error=` / `pid=` / `exit=` / `restart=`，便于 grep。
 */
import fs from 'node:fs';
import path from 'node:path';

import { LOG_MAX_BYTES, LOG_MAX_ROTATIONS } from '../shared/constants';

export type LogName = 'main' | 'api' | 'worker' | 'gateway' | 'web';

export interface LoggerOptions {
  dir: string;
  verbose?: boolean;
}

function stamp(): string {
  return `[${new Date().toISOString()}]`;
}

class RotatingFile {
  private readonly file: string;
  private written = 0;

  constructor(dir: string, name: string) {
    this.file = path.join(dir, `${name}.log`);
    fs.mkdirSync(dir, { recursive: true });
    try {
      this.written = fs.statSync(this.file).size;
    } catch {
      this.written = 0;
    }
  }

  write(line: string): void {
    const buf = Buffer.from(line, 'utf8');
    this.rotateIfNeeded(buf.length);
    try {
      fs.appendFileSync(this.file, buf);
      this.written += buf.length;
    } catch {
      /* 日志写失败不能拖垮应用 */
    }
  }

  private rotateIfNeeded(incoming: number): void {
    if (this.written + incoming <= LOG_MAX_BYTES) return;
    for (let i = LOG_MAX_ROTATIONS - 1; i >= 1; i -= 1) {
      const from = i === 1 ? this.file : `${this.file}.${i - 1}`;
      const to = `${this.file}.${i}`;
      if (fs.existsSync(from)) {
        try {
          fs.rmSync(to, { force: true });
          fs.renameSync(from, to);
        } catch {
          /* ignore */
        }
      }
    }
    try {
      fs.rmSync(this.file, { force: true });
    } catch {
      /* ignore */
    }
    this.written = 0;
  }
}

export class Logger {
  private readonly streams = new Map<LogName, RotatingFile>();
  private readonly verbose: boolean;

  constructor(private readonly opts: LoggerOptions) {
    this.verbose = opts.verbose ?? false;
  }

  /** 主进程结构化日志：`state=` / `error=` / `pid=` / `exit=` / `restart=`。 */
  main(message: string): void {
    this.stream('main').write(`${stamp()} [main] ${message}\n`);
  }

  /** 主进程调试行（只有 `--verbose` 才落盘）。 */
  debug(message: string): void {
    if (!this.verbose) return;
    this.main(`level=debug ${message}`);
  }

  /** 子进程输出（stdout/stderr 逐行转发）。 */
  child(name: LogName, stream: 'stdout' | 'stderr', text: string): void {
    const lines = text.split(/\r?\n/).filter((l) => l.length > 0);
    const target = this.stream(name);
    for (const line of lines) {
      target.write(`${stamp()} [${name}]${stream === 'stderr' ? ' ERR' : ''} ${line}\n`);
    }
  }

  /** 取某个日志文件的尾部若干行（错误窗口用）。 */
  tail(name: LogName, lines = 40): string {
    const file = path.join(this.opts.dir, `${name}.log`);
    try {
      const content = fs.readFileSync(file, 'utf8').split(/\r?\n/).filter((l) => l.length > 0);
      return content.slice(-lines).join('\n');
    } catch {
      return '';
    }
  }

  dir(): string {
    return this.opts.dir;
  }

  private stream(name: LogName): RotatingFile {
    let s = this.streams.get(name);
    if (s === undefined) {
      s = new RotatingFile(this.opts.dir, name);
      this.streams.set(name, s);
    }
    return s;
  }
}
