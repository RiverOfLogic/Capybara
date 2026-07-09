// 根组件 <App>：持有状态、连接后端、把协议消息声明式地映射为状态更新。
// 组件树只描述“当前状态该渲染成什么”，所有变化经 reducer 走单向数据流。

import { createElement, useReducer, useEffect, useState, useRef } from 'react';
import { Box, useApp } from 'ink';
import htm from 'htm';

import { connectBackend } from './protocol.js';
import { Header, Transcript, Activity, ConfirmDialog, PromptBox, HintBar } from './components.js';
import { t } from './i18n.js';
import { COMMANDS, findCommand } from './commands.js';

const html = htm.bind(createElement);

let _seq = 0;
const nextId = () => ++_seq;

// /help 文本：由命令注册表 + i18n 生成，双语随 lang 切换，命令增删无需另改。
function helpText(lang) {
  const lines = [t('helpTitle', lang)];
  for (const c of COMMANDS) lines.push(`  ${c.name.padEnd(11)} ${t(c.descKey, lang)}`);
  return lines.join('\n');
}

const initial = {
  ready: false,
  model: '',
  workspace: '',
  lang: 'zh',         // 界面语言（/lang 切换）；不影响 agent 回答语言
  usage: null,        // 上下文 token 用量 {used, window, percent}
  messages: [],       // 永久转录 {id, role, ...}
  activity: null,     // 闪现区当前状态 {labelKey|labelText, vars?, detail?}
  streaming: '',      // 本轮流式累积的答案
  running: false,     // 是否有一轮在进行
  pendingConfirm: null,
};

function reducer(state, action) {
  switch (action.type) {
    case 'ready':
      return { ...state, ready: true, model: action.model, workspace: action.workspace };
    case 'push':
      return { ...state, messages: [...state.messages, { id: nextId(), role: action.role, text: action.text }] };
    case 'tool':
      // 工具调用留一条紧凑常驻记录（滚动区可回看），补偿闪现区一闪而过读不到
      return {
        ...state,
        messages: [...state.messages, { id: nextId(), role: 'tool', ok: action.ok, text: action.text }],
      };
    case 'notice':
      // 后端命令结果 / 本轮 diff：作为一条常驻消息进滚动区（diff 格式由组件彩色渲染）
      return {
        ...state,
        messages: [...state.messages, {
          id: nextId(), role: 'notice', format: action.format, title: action.title, text: action.text,
        }],
      };
    case 'clearTranscript':
      return { ...state, messages: [] };
    case 'setLang':
      return { ...state, lang: action.lang };
    case 'usage':
      return { ...state, usage: action.usage };
    case 'turnStart':
      return { ...state, running: true, activity: { labelKey: 'thinking' }, streaming: '' };
    case 'activity':
      return { ...state, activity: action.payload };
    case 'chunk':
      return { ...state, streaming: state.streaming + action.text };
    case 'finish':
      return {
        ...state,
        messages: [...state.messages, { id: nextId(), role: 'agent', text: action.text || state.streaming }],
        streaming: '',
        activity: null,
      };
    case 'turnDone': {
      // agent_finish 已把答案落库并清空 streaming；这里兜底处理异常提前结束的情况
      const messages = state.streaming
        ? [...state.messages, { id: nextId(), role: 'agent', text: state.streaming }]
        : state.messages;
      return { ...state, running: false, activity: null, streaming: '', messages };
    }
    case 'confirm':
      return { ...state, pendingConfirm: action.payload };
    case 'confirmDone':
      return { ...state, pendingConfirm: null };
    default:
      return state;
  }
}

// 把工具名 + 参数/结果压成一行紧凑预览。
function toolLine(mark, name, detail) {
  const preview = detail ? String(detail).replace(/\s+/g, ' ').slice(0, 100) : '';
  return `${mark} ${name}${preview ? '  ' + preview : ''}`;
}

// 声明式 dispatch 表：一个 arun_stream 事件 → 一个或多个 reducer action。
// activity 用 labelKey(+vars) 交给组件按当前 lang 本地化；含数据的（工具名）用 labelText 原样。
const EVENT_TO_ACTION = {
  step_start: (d) => ({ type: 'activity', payload: { labelKey: 'stepThinking', vars: { step: d.step } } }),
  llm_start: () => ({ type: 'activity', payload: { labelKey: 'thinking' } }),
  tool_call: (d) => ({ type: 'activity', payload: { labelKey: 'calling', vars: { name: d.name }, detail: d.arguments } }),
  tool_result: (d) => [
    { type: 'tool', ok: true, text: toolLine('⏺', d.name, d.content) },
    { type: 'activity', payload: { labelText: `${d.name} ✓` } },
  ],
  tool_error: (d) => [
    { type: 'tool', ok: false, text: toolLine('✗', d.name, d.error) },
    { type: 'activity', payload: { labelText: `${d.name} ✗` } },
  ],
  llm_chunk: (d) => ({ type: 'chunk', text: d.text || '' }),
  agent_finish: (d) => ({ type: 'finish', text: d.result || '' }),
  agent_error: (d) => ({ type: 'push', role: 'system', text: `❌ ${d.error_type}: ${d.error}` }),
};

function dispatchMessage(msg, dispatch) {
  if (msg.type === 'ready') {
    dispatch({ type: 'ready', model: msg.model, workspace: msg.workspace });
  } else if (msg.type === 'agent_event') {
    const make = EVENT_TO_ACTION[msg.event.type];
    if (make) {
      const result = make(msg.event.data || {});
      for (const action of Array.isArray(result) ? result : [result]) dispatch(action);
    }
  } else if (msg.type === 'confirm_request') {
    dispatch({ type: 'confirm', payload: { id: msg.id, tool: msg.tool, preview: msg.preview } });
  } else if (msg.type === 'notice') {
    dispatch({ type: 'notice', format: msg.format, title: msg.title, text: msg.text });
  } else if (msg.type === 'usage') {
    dispatch({ type: 'usage', usage: { used: msg.used, window: msg.window, percent: msg.percent } });
  } else if (msg.type === 'turn_done') {
    dispatch({ type: 'turnDone' });
  } else if (msg.type === 'error') {
    dispatch({ type: 'push', role: 'system', text: '⚠ ' + msg.message });
  }
}

export function App(opts) {
  const [state, dispatch] = useReducer(reducer, initial);
  const [input, setInput] = useState('');
  const backendRef = useRef(null);
  const { exit } = useApp();

  useEffect(() => {
    const backend = connectBackend(opts, {
      onMessage: (msg) => dispatchMessage(msg, dispatch),
      onExit: () => exit(),
    });
    backendRef.current = backend;
    return () => backend.shutdown();
  }, []);

  const submit = (raw) => {
    const text = raw.trim();
    setInput('');
    if (!text) return;

    const cmd = findCommand(text);
    if (cmd) {
      dispatch({ type: 'push', role: 'cmd', text });  // 回显用户敲的命令
      if (cmd.kind === 'backend') {
        backendRef.current?.send({ type: 'command', name: cmd.backend });
        return;
      }
      switch (cmd.name) {
        case '/help':
          dispatch({ type: 'push', role: 'system', text: helpText(state.lang) });
          return;
        case '/clear':
          dispatch({ type: 'clearTranscript' });
          return;
        case '/reset':
          backendRef.current?.send({ type: 'reset' });
          dispatch({ type: 'clearTranscript' });
          dispatch({ type: 'push', role: 'system', text: t('historyCleared', state.lang) });
          return;
        case '/lang': {
          const arg = text.split(/\s+/)[1];
          const next = arg === 'zh' || arg === '中' ? 'zh'
            : arg === 'en' || arg === '英' ? 'en'
            : (state.lang === 'zh' ? 'en' : 'zh');
          dispatch({ type: 'setLang', lang: next });
          dispatch({ type: 'push', role: 'system', text: t('langSwitched', next) });
          return;
        }
        case '/exit':
          backendRef.current?.shutdown();
          exit();
          return;
      }
      return;
    }

    if (text.startsWith('/')) {
      dispatch({ type: 'push', role: 'system', text: t('unknownCommand', state.lang, { cmd: text }) });
      return;
    }

    dispatch({ type: 'push', role: 'user', text });
    dispatch({ type: 'turnStart' });
    backendRef.current?.send({ type: 'user_message', text });
  };

  const respondConfirm = (approved) => {
    const c = state.pendingConfirm;
    if (c) backendRef.current?.send({ type: 'confirm_response', id: c.id, approved });
    dispatch({ type: 'confirmDone' });
  };

  // 同一时刻只挂一个捕获输入的组件：有待确认→对话框；否则 turn 空闲→输入框；turn 中→都不挂
  const idle = !state.pendingConfirm && !state.running;
  let footer = null;
  if (state.pendingConfirm) {
    footer = html`<${ConfirmDialog} confirm=${state.pendingConfirm} onRespond=${respondConfirm} lang=${state.lang} />`;
  } else if (!state.running) {
    footer = html`<${PromptBox} value=${input} onChange=${setInput} onSubmit=${submit} ready=${state.ready} lang=${state.lang} />`;
  }

  return html`
    <${Box} flexDirection="column">
      <${Header} ready=${state.ready} model=${state.model} workspace=${state.workspace} usage=${state.usage} lang=${state.lang} />
      <${Transcript} messages=${state.messages} lang=${state.lang} />
      <${Activity} activity=${state.activity} streaming=${state.streaming} running=${state.running} lang=${state.lang} />
      ${footer}
      ${idle ? html`<${HintBar} lang=${state.lang} />` : null}
    <//>`;
}
