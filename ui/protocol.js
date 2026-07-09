// 后端连接层：把 agents.tui_backend 作为子进程拉起，按行收发 JSON 协议。
//
// 用启动器传来的同一个 Python 解释器（--python）拉起后端，保证后端跑在装了依赖的
// 那个环境里。后端 stdout 只承载协议 JSON（每行一个）；stderr 忽略（后端已把自己的
// 杂散输出重定向到 stderr，避免污染协议流）。

import { spawn } from 'node:child_process';
import readline from 'node:readline';

export function connectBackend({ python, workspace, config }, handlers) {
  const argv = ['-m', 'agents.tui_backend', '--workspace', workspace];
  if (config) argv.push('--config', config);

  const child = spawn(python || 'python', argv, {
    // stdin/stdout 走管道传协议；stderr 忽略（保持 UI 干净，调试时可临时改成 'inherit'）
    stdio: ['pipe', 'pipe', 'ignore'],
  });

  const rl = readline.createInterface({ input: child.stdout });
  rl.on('line', (raw) => {
    const line = raw.trim();
    if (!line) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return; // 非协议行直接丢弃
    }
    handlers.onMessage?.(msg);
  });

  child.on('exit', (code) => handlers.onExit?.(code));
  child.on('error', (err) => handlers.onError?.(err));

  return {
    send(obj) {
      try {
        child.stdin.write(JSON.stringify(obj) + '\n');
      } catch {
        /* 后端已退出，忽略 */
      }
    },
    shutdown() {
      try {
        child.stdin.write(JSON.stringify({ type: 'shutdown' }) + '\n');
      } catch {
        /* ignore */
      }
      try {
        child.stdin.end();
      } catch {
        /* ignore */
      }
    },
    child,
  };
}
