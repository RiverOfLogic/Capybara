#!/usr/bin/env node
// Capybara 交互式 TUI 前端入口。
//
// 由 Python 端 `capybara`（无参数）用 subprocess 拉起，透传 --python/--workspace/
// --config/--logo。挂载 Ink 之前，先把 logo.six 的原始字节写到 stdout —— 这样 Sixel
// 图形落在 Ink 渲染区之上的滚动区里，既能显示、又不会被 Ink 按字符宽度重排干扰。

import fs from 'node:fs';
import process from 'node:process';
import { render } from 'ink';
import { createElement } from 'react';
import { App } from './app.js';

function parseArgs(argv) {
  const out = { python: 'python', workspace: '.', config: '', logo: '' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--python') out.python = argv[++i];
    else if (a === '--workspace') out.workspace = argv[++i];
    else if (a === '--config') out.config = argv[++i];
    else if (a === '--logo') out.logo = argv[++i];
  }
  return out;
}

const opts = parseArgs(process.argv.slice(2));

// Sixel logo：挂载前一次性写入。不支持 Sixel 的终端会显示乱码但不影响后续 UI；
// 文件缺失 / 读失败静默跳过。
if (opts.logo) {
  try {
    process.stdout.write(fs.readFileSync(opts.logo));
    process.stdout.write('\n');
  } catch {
    /* ignore */
  }
}

render(createElement(App, opts));
