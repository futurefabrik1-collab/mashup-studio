"""Audition page: one static HTML file next to the previews (runs/<run>/audition.html). `mf app` also serves it,
built fresh, at /r/<run>/audition.html: there keep/skip go to the run's picks.json through the app, the same marks
the app shows. Opened as a file, the marks stay in this browser (localStorage) and export as picks.json."""
import html, json, os

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mashup Finder: __TITLE__</title>
<style>
:root{--bg:#fff3b8;--fg:#24113a;--mut:#6a5580;--card:#fffdf6;--line:#24113a;--soft:#ecdcae;--ok:#12a150;--no:#e3172d;
--straw:#ff2e7e;--orange:#ff8a00;--lemon:#ffd21f;--kiwi:#3ed35f;--grape:#7b2cff;
--round:ui-rounded,"SF Pro Rounded","Arial Rounded MT Bold","Nunito",-apple-system,system-ui,sans-serif}
html{color-scheme:light}*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:500 15px/1.45 var(--round);
background-image:radial-gradient(circle at 12px 12px,rgba(255,138,0,.22) 2.5px,transparent 3px);background-size:28px 28px;background-attachment:fixed}
header{background:var(--lemon);border-bottom:3px solid var(--line);box-shadow:0 4px 0 rgba(36,17,58,.12);padding:12px 16px}
header h1{margin:0;font-size:22px;font-weight:900;letter-spacing:-.02em}header h1 i{font-style:normal;display:inline-block;transform:rotate(-8deg);margin-right:8px}
main{max-width:980px;margin:0 auto;padding:18px 16px 80px}.sub{color:var(--mut);margin:0 0 14px;font-weight:600}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
button{font:inherit;font-weight:700;border:2.5px solid var(--line);border-radius:999px;background:var(--card);color:var(--fg);padding:5px 14px;cursor:pointer;
box-shadow:3px 3px 0 var(--line);transition:transform .08s,box-shadow .08s}
button:hover{transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--line);background:#fff}
button:active{transform:translate(2px,2px);box-shadow:1px 1px 0 var(--line)}
button:focus-visible{outline:3px solid var(--grape);outline-offset:2px}
button.on{background:var(--straw);color:#fff}
.card{background:var(--card);border:3px solid var(--line);border-radius:18px;box-shadow:5px 5px 0 var(--lemon);padding:14px 18px;margin:14px 0;
display:grid;grid-template-columns:44px 1fr;gap:4px 12px}
.card:nth-child(4n+1){box-shadow:5px 5px 0 var(--straw)}.card:nth-child(4n+2){box-shadow:5px 5px 0 var(--orange)}
.card:nth-child(4n+3){box-shadow:5px 5px 0 var(--kiwi)}.card:nth-child(4n+4){box-shadow:5px 5px 0 var(--grape)}
.card.cur{outline:3px solid var(--grape);outline-offset:3px}
.card.keep{background:#e9ffe9;box-shadow:5px 5px 0 var(--ok)!important}.card.skip{opacity:.45;box-shadow:none!important}
.rk{font-size:22px;font-weight:900;color:var(--mut);grid-row:span 4}.t{font-weight:800;font-size:16px}.t small{font-weight:500;color:var(--mut)}
.m{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}
audio{width:100%;max-width:560px;margin-top:6px;display:block;border:2.5px solid var(--line);border-radius:999px;height:40px}
.acts{display:flex;gap:8px;margin-top:6px}.acts button[data-v=keep].on{background:var(--kiwi);color:var(--fg)}
.fail{color:var(--no);font-weight:700}.rej{margin-top:24px;color:var(--mut)}.rej summary{cursor:pointer;padding:8px 0;font-weight:700}
kbd{border:2px solid var(--line);border-radius:6px;background:var(--card);font-weight:700;padding:0 4px;font-size:12px}
#msg.err{color:var(--no);font-weight:700}
@media (prefers-reduced-motion:reduce){button{transition:none}}
</style></head><body>
<header><h1><i>&#x1F353;</i>Mashup Finder</h1></header><main>
<p class="sub">__TITLE__ · __N__ pairs with previews · vocal of the first track over the instrumental of the second.
Keys: <kbd>j</kbd>/<kbd>k</kbd> next/prev, <kbd>space</kbd> play, <kbd>y</kbd> keep, <kbd>n</kbd> skip.</p>
<div class="bar"><button id="exp">Export picks</button><button id="flt">Show kept only</button><span id="msg" class="m"></span></div>
<div id="list">__CARDS__</div></main>
<script>
"use strict";
// Served by `mf app` (SERVED, with the key the Studio's tab keeps), marks are the run's picks.json; opened as a
// file, they live in this browser.
const TOKEN = "__TOKEN__", RUN = __RUN__, SERVED = __SERVED__;
const KEY = "mf-picks-" + RUN, H = {"X-Token": TOKEN};
const cards = [...document.querySelectorAll(".card")].filter((c) => !c.closest(".rej"));
let picks = {}, cur = 0;
const say = (t, err) => { const m = document.getElementById("msg"); m.textContent = t; m.className = err ? "err" : "m"; };
const answer = async (r) => {
  const j = await r.json().catch(() => ({})), k = r.ok ? "" : mfKey.get();       // a newer key: Restart (edit.NO_TOKEN)
  if (r.status === 403 && j.token === false && k && k !== TOKEN) location.reload();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const store = SERVED ? {                           // load() -> marks; save(id, mark or null) -> marks
  load: () => fetch("/api/run?name=" + encodeURIComponent(RUN), {headers: H}).then(answer).then((j) => j.marks),
  save: (id, m) => fetch("/api/mark", {method: "POST", headers: {...H, "Content-Type": "application/json"},
                                       body: JSON.stringify({run: RUN, id: +id, mark: m})}).then(answer).then((j) => j.marks),
} : {
  load: async () => { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } },
  save: async (id, m) => {
    const p = {...picks}; if (m) p[id] = m; else delete p[id];
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) {}
    return p;
  },
};
function paint() {
  cards.forEach((c, i) => {
    const p = picks[c.dataset.id];
    c.classList.toggle("keep", p === "keep"); c.classList.toggle("skip", p === "skip"); c.classList.toggle("cur", i === cur);
    c.querySelectorAll(".acts button").forEach((b) => b.classList.toggle("on", b.dataset.v === p));
  });
}
async function mark(c, v) {
  const id = c.dataset.id;
  try { picks = await store.save(id, picks[id] === v ? null : v); if (SERVED) say("Saved in the run (picks.json)."); }
  catch (e) { say(e.message, true); }
  paint();
}
cards.forEach((c, i) => {
  c.querySelectorAll(".acts button").forEach((b) => { b.onclick = () => { cur = i; mark(c, b.dataset.v); }; });
  const a = c.querySelector("audio");
  if (a) a.addEventListener("play", () => { cur = i; paint(); });
});
document.addEventListener("play", (e) => { document.querySelectorAll("audio").forEach((a) => { if (a !== e.target && !a.paused) a.pause(); }); }, true);
function go(d) { cur = Math.max(0, Math.min(cards.length - 1, cur + d)); cards[cur].scrollIntoView({block: "center", behavior: "smooth"}); paint(); }
document.onkeydown = (e) => {
  const c = cards[cur], a = c && c.querySelector("audio");
  if (e.key === "j") go(1); else if (e.key === "k") go(-1);
  else if (e.key === "y" && c) mark(c, "keep"); else if (e.key === "n" && c) mark(c, "skip");
  else if (e.key === " " && a) { e.preventDefault(); a.paused ? a.play() : a.pause(); }
};
document.getElementById("exp").onclick = () => {
  const data = cards.filter((c) => picks[c.dataset.id] === "keep").map((c) => JSON.parse(c.dataset.info));
  const u = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  const l = document.createElement("a"); l.href = u; l.download = "picks.json"; l.click();
};
let only = false;
document.getElementById("flt").onclick = (e) => {
  only = !only; e.target.textContent = only ? "Show all" : "Show kept only";
  cards.forEach((c) => { c.style.display = only && picks[c.dataset.id] !== "keep" ? "none" : ""; });
};
if (SERVED) {                                      // previews need the token; Export is the app's job
  document.getElementById("exp").hidden = true;
  document.querySelectorAll("audio").forEach((a) => { a.src = a.getAttribute("src") + "?t=" + encodeURIComponent(TOKEN); });
}
store.load().then((p) => { picks = p; if (SERVED) say("Marks are the run's own (picks.json), the same as in Mashup Studio."); paint(); },
                  (e) => say(e.message, true));
</script></body></html>"""


def _card(rank, p):
    d = p.get("deep", {})
    a, b = html.escape(p["vocal_label"]), html.escape(p["inst_label"])
    info = html.escape(json.dumps({k: p[k] for k in ("id", "vocal_label", "inst_label", "vocal_path", "inst_path")} | {"deep": d}))
    meta = (f"score {p.get('final', p['score']):.0f} · stretch {d.get('stretch_audio', p['stretch']) * 100:+.1f}%"
            f"{' (half/double-time)' if d.get('tempo_mult', p['tempo_mult']) != 1 else ''}"
            f" · vocal shift {d.get('shift_audio', p['shift']):+d} st · catalog keys {p['vocal_key']} → {p['inst_key']} ({p['relation']})")
    if d.get("ok"):
        meta2 = (f"harmonic fit rank {d['harm_rank']}/12 (z {d['harm_z']:+.1f}) · vocal active {d['vocal_active'] * 100:.0f}% · "
                 f"vocal exposure {d['vocal_share'] * 100:.0f}% · {d['bpm_a']:.1f} → {d['bpm_b']:.1f} BPM · {d['bars']} bars"
                 f" · beat drift {d.get('drift_ms', '?')} ms ({d.get('warp', 'linear')} warp){' ⚠' if (d.get('drift_ms') or 0) > 40 else ''}")
        audio = f'<audio controls preload="none" src="{html.escape(d["preview"])}"></audio>'
    else:
        meta2 = f'<span class="fail">not rendered: {html.escape(d.get("why", "not checked"))}</span>'
        audio = ""
    return (f'<div class="card" data-id="{p["id"]}" data-info="{info}"><div class="rk">{rank}</div>'
            f'<div class="t">{a} <small>vocal</small><br>{b} <small>instrumental</small></div>'
            f'<div class="m">{meta}<br>{meta2}</div><div>{audio}</div>'
            f'<div class="acts"><button data-v="keep">Keep</button> <button data-v="skip">Skip</button></div></div>')


def page(pairs, title, run, served=False):
    """The page for a run's checked pairs; run: its folder name (the marks' key); served: by `mf app`."""
    ok = [p for p in pairs if p.get("deep", {}).get("ok")]
    bad = [p for p in pairs if not p.get("deep", {}).get("ok")]
    cards = "".join(_card(i + 1, p) for i, p in enumerate(ok))
    if bad:
        # Rejected by the audio check (mostly catalog BPM far from the real tempo): no preview to play.
        cards += (f'<details class="rej"><summary>{len(bad)} pairs rejected by the audio check (no preview)</summary>'
                  + "".join(_card("–", p) for p in bad) + "</details>")
    run_js = json.dumps(run).replace("<", "\\u003c")           # a JS string inside <script>
    return (PAGE.replace("__TITLE__", html.escape(title)).replace("__N__", str(len(ok)))
            .replace("__RUN__", run_js).replace("__SERVED__", "true" if served else "false").replace("__CARDS__", cards))


def write(run_dir, pairs, title):
    f = os.path.join(run_dir, "audition.html")
    with open(f, "w") as fh:
        fh.write(page(pairs, title, os.path.basename(run_dir)))
    return f
