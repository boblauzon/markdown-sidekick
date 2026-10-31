// Minimal CDP driver for the pywebview/WebView2 window.
// node cdp.mjs <port> <cmd> [args...]
//   eval <js>                      evaluate in the page, print JSON result
//   shot <out.png>                 screenshot the page
//   click <selector> [index] [btn] real mouse click at the element's centre (btn: left|middle|right)
//   key <key> [ctrl|shift|ctrl+shift]
//   drop <selector> <file>...      native-style file drag + drop onto the element
//   wait <js> [timeoutMs]          poll until the expression is truthy
import { writeFileSync } from 'node:fs';

const [port, cmd, ...args] = process.argv.slice(2);
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const page = targets.find((t) => t.type === 'page');
const ws = new WebSocket(page.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
};
await new Promise((r) => (ws.onopen = r));
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq;
  pending.set(id, (msg) => (msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
};
const centre = async (sel, i = 0) => {
  const r = await evaluate(`(() => { const el = document.querySelectorAll(${JSON.stringify(sel)})[${i}];
    if (!el) return null; el.scrollIntoView({block:'nearest'}); const b = el.getBoundingClientRect();
    return {x: b.x + b.width/2, y: b.y + b.height/2, disabled: !!el.disabled}; })()`);
  if (!r) throw new Error(`no element ${sel}[${i}]`);
  return r;
};
const mods = (m) => (m?.includes('ctrl') ? 2 : 0) | (m?.includes('shift') ? 8 : 0);

try {
  if (cmd === 'eval') console.log(JSON.stringify(await evaluate(args[0]), null, 1));
  else if (cmd === 'shot') {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(args[0], Buffer.from(r.data, 'base64'));
    console.log('saved', args[0]);
  } else if (cmd === 'click') {
    const { x, y, disabled } = await centre(args[0], Number(args[1] || 0));
    const button = args[2] || 'left'; // left | middle | right
    for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
      await send('Input.dispatchMouseEvent', { type, x, y, button, clickCount: 1 });
    console.log('clicked', args[0], disabled ? '(disabled)' : '');
  } else if (cmd === 'key') {
    const key = args[0], m = mods(args[1]);
    const code = key.length === 1 ? 'Key' + key.toUpperCase() : key;
    const vk = key.length === 1 ? key.toUpperCase().charCodeAt(0) : { Delete: 46, Escape: 27, ArrowDown: 40, ArrowUp: 38 }[key];
    await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, modifiers: m, windowsVirtualKeyCode: vk });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, modifiers: m, windowsVirtualKeyCode: vk });
    console.log('key', key, args[1] || '');
  } else if (cmd === 'drop') {
    const { x, y } = await centre(args[0]);
    const data = { items: [], files: args.slice(1), dragOperationsMask: 1 };
    for (const type of ['dragEnter', 'dragOver', 'drop'])
      await send('Input.dispatchDragEvent', { type, x, y, data });
    console.log('dropped', args.slice(1).length, 'file(s) on', args[0]);
  } else if (cmd === 'wait') {
    const end = Date.now() + Number(args[1] || 30000);
    let v;
    while (!(v = await evaluate(args[0]))) {
      if (Date.now() > end) throw new Error('timeout waiting for ' + args[0]);
      await new Promise((r) => setTimeout(r, 150));
    }
    console.log('ok', JSON.stringify(v));
  }
} catch (e) { console.error('ERROR', e.message); process.exitCode = 1; }
ws.close();
