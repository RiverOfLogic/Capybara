// 双语文案单一来源。t(key, lang, vars?) → 取 STRINGS[key][lang] 并做 {var} 插值。
//
// 只本地化 **UI chrome**（标签、提示、命令描述、确认框、告示标题）。Agent 的回答、
// diff 内容、技能列表、代码智能状态等“数据”不进此表——它们跟随对话语言 / 由后端产出，
// 与项目约定「面向人类的动态内容保持原样」一致。

export const LANGS = ['zh', 'en'];

export const STRINGS = {
  connecting:        { zh: '连接后端中…',        en: 'Connecting to backend…' },
  labelModel:        { zh: '模型',               en: 'model' },
  labelWorkspace:    { zh: '工作区',             en: 'workspace' },
  labelContext:      { zh: '上下文',             en: 'context' },
  moreAbove:         { zh: '↑ 还有 {n} 项',      en: '↑ {n} more' },
  moreBelow:         { zh: '↓ 还有 {n} 项',      en: '↓ {n} more' },
  thinking:          { zh: '思考中…',            en: 'Thinking…' },
  stepThinking:      { zh: '步骤 {step} · 思考中…', en: 'Step {step} · thinking…' },
  calling:           { zh: '调用 {name}',        en: 'Calling {name}' },
  roleYou:           { zh: '你',                 en: 'You' },
  roleSystem:        { zh: '系统',               en: 'System' },
  promptPlaceholder: { zh: '问点什么，或输入 / 查看命令', en: 'Ask anything, or type / for commands' },
  promptWaiting:     { zh: '等待后端就绪…',      en: 'Waiting for backend…' },

  confirmTitle:      { zh: '确认执行 {tool}？',  en: 'Run {tool}?' },
  confirmApprove:    { zh: '批准',               en: 'approve' },
  confirmReject:     { zh: '拒绝',               en: 'reject' },
  confirmPress:      { zh: '按',                 en: 'Press' },

  hintBar:           { zh: 'Tab 补全 · /help 帮助 · /lang 中/EN · Ctrl+C 退出',
                       en: 'Tab complete · /help · /lang zh/EN · Ctrl+C exit' },
  suggestHint:       { zh: '↑↓ 选择 · Tab 补全 · Enter 执行',
                       en: '↑↓ select · Tab complete · Enter run' },
  helpTitle:         { zh: '可用命令：',         en: 'Commands:' },

  screenCleared:     { zh: '已清屏（对话记忆保留）。', en: 'Screen cleared (memory kept).' },
  historyCleared:    { zh: '已清空对话历史。',   en: 'Conversation history cleared.' },
  langSwitched:      { zh: '界面语言已切换为「中文」。', en: 'UI language switched to English.' },
  backendExited:     { zh: '后端已退出。',       en: 'Backend exited.' },
  unknownCommand:    { zh: '未知命令：{cmd}（输入 /help 查看）', en: 'Unknown command: {cmd} (type /help)' },

  diffOmitted:       { zh: '… 省略 {n} 行',      en: '… {n} lines omitted' },
  noticeTurnDiff:    { zh: '本轮改动',           en: 'Changes this turn' },
  noticeDiffManual:  { zh: '改动 diff',          en: 'Last changes' },

  // 命令描述（供 /help 文本 + 斜杠自动补全共用）
  cmdHelp:      { zh: '显示帮助',             en: 'Show help' },
  cmdClear:     { zh: '清屏（对话记忆保留）', en: 'Clear screen (keep memory)' },
  cmdReset:     { zh: '清空对话历史',         en: 'Clear conversation history' },
  cmdCompact:   { zh: '主动压缩对话历史',     en: 'Compact conversation history' },
  cmdSkills:    { zh: '列出技能库',           en: 'List skills' },
  cmdDiff:      { zh: '重看上一轮改动',       en: 'Show last changes' },
  cmdCodeintel: { zh: '代码智能就绪状态',     en: 'Code-intel status' },
  cmdTrace:     { zh: '导出 trace 报告',      en: 'Export trace report' },
  cmdLang:      { zh: '切换界面语言 中/英',   en: 'Toggle UI language zh/en' },
  cmdExit:      { zh: '退出',                 en: 'Exit' },
};

// 后端 diff notice 的 title 是稳定 key（turn_diff / diff_manual），在此映射到 i18n key。
const NOTICE_TITLE_KEY = { turn_diff: 'noticeTurnDiff', diff_manual: 'noticeDiffManual' };

export function t(key, lang = 'zh', vars = null) {
  const entry = STRINGS[key];
  let s = entry ? (entry[lang] ?? entry.zh ?? key) : key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      s = s.split('{' + k + '}').join(String(v));
    }
  }
  return s;
}

// 把后端 notice 的 title（可能是稳定 key，也可能是原始文本）解析成展示文案。
export function noticeTitle(rawTitle, lang = 'zh') {
  if (!rawTitle) return '';
  const key = NOTICE_TITLE_KEY[rawTitle];
  return key ? t(key, lang) : rawTitle;
}
