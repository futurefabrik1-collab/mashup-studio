// The pages themselves, in a real browser: node tests/pagecheck.mjs
//
// Starts a fixture Studio (tests/fixture_app.py: fake audio work, a small catalog, everything in a temp folder) and
// drives headless Google Chrome over the DevTools protocol, with real mouse and key events: without its key the page
// says so and a junk key cannot replace it, the Studio loads and navigates, a brief slider rewrites the brief and the
// live count follows, Audition's Radio and keys work, and in the editor a split cuts a block with a crossfade and a
// fade handle drags. No npm packages: Node 22+ and Chrome.
// Prints one line per check and exits 1 if any fails (or the page logs an error).
//   MF_PYTHON  the Python with the app's packages (default: the venv packaging/install.sh made; nothing is installed)
//   CHROME     the Chrome binary (default /Applications/Google Chrome.app/Contents/MacOS/Google Chrome)
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
let PY = process.env.MF_PYTHON || join(homedir(), 'Library/Application Support/Mashup Studio/venv/bin/python');
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const kids = [], dirs = [];
const cleanup = () => {                                           // the app on Ctrl-C removes its temp folder; Chrome just goes
  kids.forEach((k, i) => { try { k.kill(i ? 'SIGKILL' : 'SIGINT'); } catch {} });
  dirs.forEach((d) => { try { rmSync(d, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); } catch {} });   // Chrome may still write
};
process.on('exit', cleanup);
setTimeout(() => { console.log('FAIL  timed out after 4 minutes'); process.exit(1); }, 240000).unref();

// ---------- the fixture Studio ----------
const app = spawn(PY, ['-u', join(HERE, 'fixture_app.py')], { cwd: dirname(HERE), stdio: ['ignore', 'pipe', 'inherit'] });
kids.push(app);
const [url, run, mashup] = await new Promise((resolve, reject) => {
  let buf = '';
  app.stdout.on('data', (d) => { buf += d; const m = buf.match(/ready (\S+) (\S+) (\S+)/); if (m) resolve(m.slice(1)); });
  app.on('exit', (c) => reject(new Error(`fixture app exited (${c})`)));
});

// ---------- Chrome ----------
const profile = mkdtempSync(join(tmpdir(), 'mf-pagecheck-chrome-')); dirs.push(profile);
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, '--autoplay-policy=no-user-gesture-required', '--mute-audio', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
kids.push(chrome);
const wsUrl = await new Promise((resolve, reject) => {
  let buf = '';
  chrome.stderr.on('data', (d) => { buf += d; const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) resolve(m[1]); });
  chrome.on('exit', () => reject(new Error('Chrome exited before DevTools came up')));
});
const ws = new WebSocket(wsUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
let nextId = 1; const pending = new Map(); const errors = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const { resolve, reject } = pending.get(m.id); pending.delete(m.id); m.error ? reject(new Error(m.error.message)) : resolve(m.result); }
  else if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map((a) => a.value ?? a.description).join(' '));
  else if (m.method === 'Page.javascriptDialogOpening') send('Page.handleJavaScriptDialog', { accept: true }, m.sessionId);   // confirm, leave
};
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const id = nextId++; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params, sessionId }));
});
const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
const S = (m, p) => send(m, p, sessionId);
await S('Page.enable'); await S('Runtime.enable');
await S('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

const js = async (expr) => {                                       // an expression in the page (awaited), its value
  const { result, exceptionDetails } = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || exceptionDetails.text);
  return result.value;
};
const until = async (expr, ms = 15000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await js(expr)) return true; await sleep(100); } return false; };
const center = (sel) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null; e.scrollIntoView({block: "center"});
  const r = e.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()`);
const click = async (sel, dx = 0) => {                             // a real mouse click on an element (dx: right of its centre)
  const p = await center(sel); if (!p) throw new Error(`no ${sel}`);
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
    await S('Input.dispatchMouseEvent', { type, x: p.x + dx, y: p.y, button: 'left', clickCount: 1 });
};
const drag = async (sel, dx) => {                                  // press on an element, move dx pixels, release
  const p = await center(sel);
  await S('Input.dispatchMouseEvent', { type: 'mousePressed', x: p.x, y: p.y, button: 'left', clickCount: 1 });
  for (let i = 1; i <= 5; i++) await S('Input.dispatchMouseEvent', { type: 'mouseMoved', x: p.x + dx * i / 5, y: p.y, button: 'left', buttons: 1 });
  await S('Input.dispatchMouseEvent', { type: 'mouseReleased', x: p.x + dx, y: p.y, button: 'left', clickCount: 1 });
};
const KEYS = { ArrowRight: 39, ArrowLeft: 37, ' ': 32, End: 35 };
const key = async (k) => {                                         // a real key press
  const code = KEYS[k] || k.toUpperCase().charCodeAt(0);
  await S('Input.dispatchKeyEvent', { type: 'keyDown', key: k, windowsVirtualKeyCode: code, text: k.length === 1 ? k : undefined });
  await S('Input.dispatchKeyEvent', { type: 'keyUp', key: k, windowsVirtualKeyCode: code });
};

let failed = 0;
async function check(name, fn) {
  const before = errors.length;
  try {
    const why = await fn();
    if (why) throw new Error(why);
    if (errors.length > before) throw new Error(`the page logged: ${errors.slice(before).join(' | ').slice(0, 300)}`);
    console.log(`PASS  ${name}`);
  } catch (e) { failed++; console.log(`FAIL  ${name}: ${e.message}`); }
}

// ---------- the checks ----------
await check('a plain address, no key yet: the page says how to get in and does not reload', async () => {
  await S('Page.navigate', { url: new URL('/', url).href });              // a fresh profile: nothing stored
  if (!(await until('typeof TOKEN === "string" && !document.getElementById("err").hidden'))) return 'no error shown';
  if ((await js('TOKEN')) !== '') return 'a key from nowhere';
  if (!(await js('document.getElementById("err").textContent')).includes('open Mashup Studio again')) return 'no way in shown';
  await js('window.__mark = 1'); await sleep(2000);
  if ((await js('window.__mark')) !== 1) return 'the page reloaded';
});
await S('Page.navigate', { url });                                        // the address the Studio opens: #key=
await check('the Studio loads', async () => {
  if (!(await until('document.title.includes("Mashup Studio") && typeof S === "object" && S.state'))) return 'no state from the server';
  if (await js('location.hash.includes("key=")')) return 'the key stays in the address';
});
await check('a junk key in an address does not replace the stored one: the page comes back on it', async () => {
  const good = await js('TOKEN');
  await until(`localStorage.getItem("mf-key") === ${JSON.stringify(good)}`, 5000);   // kept once the server took it
  await S('Page.navigate', { url: new URL(`/#key=${'x'.repeat(32)}`, url).href });   // pasted into the open tab
  if (!(await until(`typeof TOKEN === "string" && TOKEN === ${JSON.stringify(good)} && typeof S === "object" && S.state`)))
    return 'the page did not come back on the stored key';
  if ((await js('localStorage.getItem("mf-key")')) !== good) return 'the junk key was stored';
});
await check('the tabs navigate', async () => {
  for (const view of ['briefs', 'runs', 'audition', 'mashups', 'editor', 'runs']) {
    await click(`nav button[data-view="${view}"]`);
    if (!(await until(`!document.getElementById("v-${view}").hidden && S.view === "${view}"`, 3000))) return `${view} did not open`;
  }
});
await check('job ids and exit codes from the server are never read as HTML', async () => {
  const bad = '1"><img src=x onerror="window.__pwned=1">';               // what another program on the port could send
  await js(`(async () => {
    const real = window.fetch, bad = ${JSON.stringify(bad)}, job = {id: bad, state: "running", title: "t", lines: [], rc: bad};
    window.fetch = async (u, o) => {
      if (String(u).startsWith("/api/state")) { const j = await (await real(u, o)).json(); j.jobs = [job]; return new Response(JSON.stringify(j)); }
      if (String(u).startsWith("/api/jobs")) return new Response(JSON.stringify([{...job, state: "failed"}]));
      return real(u, o);
    };
    try { await poll(); await drawLog(); } finally { window.fetch = real; }
  })()`);
  await sleep(300);
  const [pwned, made] = await js('[window.__pwned === 1, !!document.querySelector("#job img, #log img")]');
  await js('poll()');                                                     // back to the Studio's own jobs
  if (pwned || made) return 'a job id was read as HTML';
});
await check('a brief slider rewrites the brief and the live count follows', async () => {
  await click('nav button[data-view="briefs"]');
  if (!(await until(`document.querySelector('#brief-list button[data-brief="demo"]')`))) return 'no demo brief in the list';   // drawn after the view shows
  await click('#brief-list button[data-brief="demo"]');
  if (!(await until('/→/.test(document.getElementById("k-count").textContent)'))) return 'no count line';
  const before = await js('[document.getElementById("brief-text").value, document.getElementById("k-count").textContent]');
  await click('#k-pv');                                            // Per vocal, then to its end with the keyboard
  await key('End');
  if (!(await until(`/per_vocal = 60/.test(document.getElementById("brief-text").value)`, 3000))) return 'per_vocal not written';
  if (!(await until(`document.getElementById("k-count").textContent !== ${JSON.stringify(before[1])}`, 5000))) return `the count stayed "${before[1]}"`;
});
await check('Audition: Radio plays, K keeps and moves on, the arrow goes back, Space pauses', async () => {
  await js(`S.run = ${JSON.stringify(run)}; S.mashAll = false; show("audition")`);
  if (!(await until('document.querySelectorAll("#good .card").length >= 2'))) return 'fewer than 2 previews';
  await click('#radio');
  if (!(await until('!!document.querySelector("#good .card.cur audio") && !document.querySelector("#good .card.cur audio").paused', 8000))) return 'Radio did not play';
  const first = await js('document.querySelector("#good .card.cur").dataset.id');
  await key('k');
  if (!(await until(`S.data.marks[${first}] === "keep" && document.querySelector("#good .card.cur").dataset.id !== "${first}"`, 5000))) return 'K did not keep and move on';
  await key('ArrowLeft');
  if (!(await until(`document.querySelector("#good .card.cur").dataset.id === "${first}"`, 3000))) return 'the left arrow did not go back';
  await sleep(300); await key(' ');
  if (!(await until('[...document.querySelectorAll("#cards audio")].every((a) => a.paused)', 3000))) return 'Space did not pause';
  if (!(await until('!!document.querySelector("#good .card.cur canvas") && document.querySelector("#good .card.cur canvas").width > 0', 5000))) return 'no waveform';
});
await check('the editor: a split cuts a block with a crossfade, a fade handle drags', async () => {
  await S('Page.navigate', { url: `${new URL('/', url)}m/${encodeURIComponent(run)}/${encodeURIComponent(mashup)}/` });
  if (!(await until('window.mf && mf.arr && document.querySelectorAll(".blk").length > 0', 20000))) return 'the editor did not load';
  const n = await js('mf.arr.lanes.drums.length');
  await key('s');                                                  // the split tool, then a click inside a drums block
  await click('.lane.drums .blk', 6);
  if (!(await until(`mf.arr.lanes.drums.length === ${n + 1}`, 3000))) return 'no split';
  const xf = await js('(() => { const L = mf.arr.lanes.drums, r = mf.sel, l = L.find((b) => Math.abs(b.out_bar + b.bars - r.out_bar) < 1e-6); return [l && l.fade_out, r.fade_in]; })()');
  if (xf[0] !== 0.0625 || xf[1] !== 0.0625) return `no crossfade at the cut: ${JSON.stringify(xf)}`;
  await key('s');
  await js('document.querySelectorAll(".blk").forEach((b) => b.classList.add("sel"))');   // handles show on a selected block
  const blk = await js('(() => { const e = document.querySelector(".lane.bass .blk"); return mf.arr.lanes.bass.indexOf(e.block); })()');
  await drag('.lane.bass .blk .fh.out', -60);
  if (!(await until(`(mf.arr.lanes.bass[${blk}].fade_out || 0) > 0`, 3000))) return 'the fade-out handle did not set fade_out';
  const cut = await js(`(() => { const b = mf.arr.lanes.bass[${blk}]; mf.edit(() => { b.fade_out = b.bars; }); mf.splitAt(b, b.out_bar + b.bars - 1);
    const r = mf.sel; return [r.bars, r.fade_out]; })()`);                   // a split never leaves a fade-out longer than its block
  if (!(cut[1] <= cut[0])) return `the right half's fade_out ${cut[1]} outlasts its ${cut[0]} bars`;
});

await check('arrangement fields from the server are never read as HTML in the editor', async () => {
  const bad = '1"><img src=x onerror="window.__pwned=1">';               // what another program on the port could send
  const { identifier } = await S('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
    const real = window.fetch, bad = ${JSON.stringify(bad)};
    window.fetch = async (u, o) => {
      const r = await real(u, o);
      if (!String(u).endsWith("api/arrangement")) return r;
      const j = await r.json(), a = j.arrangement;
      a.sections = [{name: "s", out_bar: bad, bars: bad}];
      j.lanes = [...j.lanes, bad]; a.lanes[bad] = [];
      a.lanes[j.lanes[0]][0].src = bad;
      return new Response(JSON.stringify(j));
    };
  })()` });
  try {
    await S('Page.navigate', { url: `${new URL('/', url)}m/${encodeURIComponent(run)}/${encodeURIComponent(mashup)}/` });
    if (!(await until('window.mf && mf.arr && document.querySelectorAll(".blk").length > 0', 20000))) return 'the editor did not load';
    await js('sel = arr.lanes[LANES[0]][0]; inspect()');                // the inspector shows the selected block
    await sleep(300);
    const [pwned, made] = await js('[window.__pwned === 1, !!document.querySelector("#secs img, #lanes img, #heads img, #insp img")]');
    if (pwned || made) return 'an arrangement field was read as HTML';
  } finally { await S('Page.removeScriptToEvaluateOnNewDocument', { identifier }); }
});

console.log(failed ? `${failed} check(s) failed` : 'all page checks passed');
process.exit(failed ? 1 : 0);
