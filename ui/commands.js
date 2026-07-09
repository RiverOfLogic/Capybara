// 斜杠命令注册表：单一来源，同时驱动 /help 文本与输入时的自动补全弹层，避免两处漂移。
//
//   kind='client'  → 前端直接处理（/help /clear /reset /lang /exit）。
//   kind='backend' → 发 {type:'command', name:<backend>} 给 Python 后端，结果以 notice 返回。
//
// descKey 指向 i18n.js 的文案 key（双语）。

export const COMMANDS = [
  { name: '/help',      kind: 'client',  descKey: 'cmdHelp' },
  { name: '/clear',     kind: 'client',  descKey: 'cmdClear' },
  { name: '/reset',     kind: 'client',  descKey: 'cmdReset' },
  { name: '/compact',   kind: 'backend', backend: 'compact',   descKey: 'cmdCompact' },
  { name: '/skills',    kind: 'backend', backend: 'skills',    descKey: 'cmdSkills' },
  { name: '/diff',      kind: 'backend', backend: 'diff',      descKey: 'cmdDiff' },
  { name: '/codeintel', kind: 'backend', backend: 'codeintel', descKey: 'cmdCodeintel' },
  { name: '/trace',     kind: 'backend', backend: 'trace',     descKey: 'cmdTrace' },
  { name: '/lang',      kind: 'client',  descKey: 'cmdLang' },
  { name: '/exit',      kind: 'client',  descKey: 'cmdExit' },
];

// 输入以 '/' 开头时，按前缀（不区分大小写）匹配命令，供自动补全弹层用。
// 输入里已包含空格（如 "/lang en"）说明命令已敲全、正在带参数 → 不再弹提示。
export function matchCommands(input) {
  const raw = input || '';
  if (!raw.startsWith('/') || /\s/.test(raw.trim())) return [];
  const q = raw.trim().toLowerCase();
  return COMMANDS.filter((c) => c.name.startsWith(q));
}

// 提交时精确查找命令（取第一个空格前的词）。/quit 视作 /exit 的同义词。
export function findCommand(input) {
  const word = (input || '').trim().split(/\s+/)[0].toLowerCase();
  if (word === '/quit') return COMMANDS.find((c) => c.name === '/exit') || null;
  return COMMANDS.find((c) => c.name === word) || null;
}
