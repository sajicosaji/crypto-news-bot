// 持ち主だけが見られる集計ページ：GET /stats/<秘密の鍵>、この端末の記録を消す：POST /stats/<鍵>/forget { v }
// 鍵は Pages の secret（STATS_KEY）。合わないときは「ない」ことにする（404）

const PREF = {
  hokkaido: '北海道', aomori: '青森', iwate: '岩手', miyagi: '宮城', akita: '秋田', yamagata: '山形', fukushima: '福島',
  ibaraki: '茨城', tochigi: '栃木', gunma: '群馬', saitama: '埼玉', chiba: '千葉', tokyo: '東京', kanagawa: '神奈川',
  niigata: '新潟', toyama: '富山', ishikawa: '石川', fukui: '福井', yamanashi: '山梨', nagano: '長野', gifu: '岐阜',
  shizuoka: '静岡', aichi: '愛知', mie: '三重', shiga: '滋賀', kyoto: '京都', osaka: '大阪', hyogo: '兵庫', nara: '奈良',
  wakayama: '和歌山', tottori: '鳥取', shimane: '島根', okayama: '岡山', hiroshima: '広島', yamaguchi: '山口',
  tokushima: '徳島', kagawa: '香川', ehime: '愛媛', kochi: '高知', fukuoka: '福岡', saga: '佐賀', nagasaki: '長崎',
  kumamoto: '熊本', oita: '大分', miyazaki: '宮崎', kagoshima: '鹿児島', okinawa: '沖縄',
};
const TAB = { home: '🏠 ホーム', news: '🗞 ニュース', guide: '🧭 使い方', risk: '😈 リスク', market: '📈 データ', learn: '📚 解説', words: '📖 用語' };
const SHARE = { line: '💬 LINEで送った', copy: '🔗 リンクをコピーした', home: '📲 ホーム画面に追加を押した' };
let REGION_JA = null;
try { REGION_JA = new Intl.DisplayNames(['ja'], { type: 'region' }); } catch (e) { REGION_JA = null; }
const HOUR = 3600e3, DAY = 24 * HOUR, WD = '日月火水木金土';
const jstDay = (ms) => new Date(ms + 9 * HOUR).toISOString().slice(0, 10);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clean = (v) => String(v || '').replace(/[^a-z0-9]/gi, '').slice(0, 40);
function cookieVid(req) { const m = /(?:^|;\s*)wv=([a-z0-9]{8,40})(?:;|$)/i.exec(req.headers.get('cookie') || ''); return m ? m[1] : ''; }
function same(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
function placeName(country, region) {
  if (country === 'JP') {
    const k = String(region || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[-\s]?(prefecture|ken|fu|to)$/, '').trim();
    return PREF[k] || region || '日本';
  }
  try { if (REGION_JA && country) return REGION_JA.of(country); } catch (e) { /* 表にない国 */ }
  return country || '不明';
}
const md = (day) => { const [y, m, d] = day.split('-').map(Number); return { s: `${m}/${d}`, w: WD[new Date(Date.UTC(y, m - 1, d)).getUTCDay()] }; };
const fmtTs = (ts) => { const d = new Date(ts + 9 * HOUR); return `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${d.getUTCHours()}:${String(d.getUTCMinutes()).padStart(2, '0')}`; };
const NOT_FOUND = () => new Response('Not Found', { status: 404 });
const NOSTORE = { 'cache-control': 'no-store', 'x-robots-tag': 'noindex, nofollow' };

export async function onRequest({ request: req, env, params }) {
  const path = Array.isArray(params.path) ? params.path : [params.path];
  const key = String(path[0] || '');
  if (!env.STATS_KEY || !env.DB || !same(key, env.STATS_KEY)) return NOT_FOUND();
  if (path[1] === 'forget' && req.method === 'POST') return forget(req, env);
  if (path.length !== 1 || req.method !== 'GET') return NOT_FOUND();
  return stats(env);
}

async function forget(req, env) {
  let b = {};
  try { b = await req.json(); } catch (e) { b = {}; }
  const ids = [...new Set([clean(b.v), cookieVid(req)].filter((v) => v.length >= 8))];
  if (!ids.length) return Response.json({ deleted: 0 }, { headers: NOSTORE });
  const r = await env.DB.prepare(`DELETE FROM hits WHERE vid IN (${ids.map(() => '?').join(',')})`).bind(...ids).run();
  return Response.json({ deleted: (r.meta && r.meta.changes) || 0 }, { headers: NOSTORE });
}

function what(r) {
  if (r.ev === 'open') return `開いた${r.app ? '（ホーム画面から）' : r.line ? '（LINEの中から）' : ''}`;
  if (r.ev === 'tab') return `${TAB[r.kind] || r.kind} を見た`;
  if (r.ev === 'share') return SHARE[r.kind] || '共有した';
  return r.ev;
}

async function stats(env) {
  const now = Date.now(), today = jstDay(now), d7 = jstDay(now - 6 * DAY);
  const [all, bots] = await env.DB.batch([
    env.DB.prepare('SELECT ts, day, vid, ev, kind, dev, app, line, country, region FROM hits WHERE bot = 0 ORDER BY ts LIMIT 50000'),
    env.DB.prepare('SELECT COUNT(*) n FROM hits WHERE bot = 1'),
  ]);
  const rows = all.results || [];
  // 一人ずつ（同じ番号ごと）にまとめる。#番号は、はじめて来た順
  const people = new Map();
  for (const r of rows) {
    let v = people.get(r.vid);
    if (!v) { v = { no: people.size + 1, first: r.day, days: new Set(), opens: 0, tabs: new Set(), share: 0, last: 0, dev: '', place: '', app: false, line: false }; people.set(r.vid, v); }
    if (r.ev === 'open') { v.opens++; v.days.add(r.day); }
    if (r.ev === 'tab') v.tabs.add(r.kind);
    if (r.ev === 'share') v.share++;
    if (r.app) v.app = true;
    if (r.line) v.line = true;
    if (r.ts >= v.last) { v.last = r.ts; v.dev = r.dev; v.place = placeName(r.country, r.region); }
  }
  const opens = rows.filter((r) => r.ev === 'open'), shares = rows.filter((r) => r.ev === 'share'), tabs = rows.filter((r) => r.ev === 'tab');
  const ppl = (list) => new Set(list.map((r) => r.vid)).size;
  const T = opens.filter((r) => r.day === today), Wk = opens.filter((r) => r.day >= d7);
  const pl = [...people.values()];
  const days = [];
  for (let i = 29; i >= 0; i--) { const d = jstDay(now - i * DAY), o = opens.filter((r) => r.day === d); days.push({ d, p: ppl(o), v: o.length, n: pl.filter((v) => v.first === d).length }); }
  const count = (list, f) => { const m = new Map(); for (const v of list) { const k = f(v); m.set(k, (m.get(k) || 0) + 1); } return [...m.entries()].sort((a, b) => b[1] - a[1]); };
  const tabPeople = Object.keys(TAB).map((k) => [TAB[k], k === 'home' ? ppl(opens) : ppl(tabs.filter((r) => r.kind === k))]).sort((a, b) => b[1] - a[1]);
  const s = {
    now, bots: (bots.results && bots.results[0] && bots.results[0].n) || 0,
    tiles: [['今日', ppl(T), `開いた ${T.length}回`], ['この7日間', ppl(Wk), `開いた ${Wk.length}回`], ['これまで', ppl(opens), `開いた ${opens.length}回`],
      ['ホーム画面から開いた人', ppl(opens.filter((r) => r.app)), `開いた ${opens.filter((r) => r.app).length}回`],
      ['LINEの中から開いた人', ppl(opens.filter((r) => r.line)), `開いた ${opens.filter((r) => r.line).length}回`],
      ['人に送った人', ppl(shares), `送った・コピー ${shares.length}回`]],
    days, tabPeople, shareKinds: count(shares, (r) => SHARE[r.kind] || r.kind),
    devs: count(pl, (v) => v.dev || '不明').slice(0, 6), places: count(pl, (v) => v.place || '不明').slice(0, 10),
    returning: pl.filter((v) => v.days.size >= 2).length, todayNew: pl.filter((v) => v.first === today && v.days.has(today)).length, todayBack: pl.filter((v) => v.first < today && v.days.has(today)).length,
    people: pl.sort((a, b) => b.last - a.last).slice(0, 40),
    recent: rows.slice(-40).reverse().map((r) => ({ ts: r.ts, no: people.get(r.vid).no, text: what(r) })),
  };
  return new Response(page(s), { headers: Object.assign({ 'content-type': 'text/html; charset=utf-8', 'referrer-policy': 'no-referrer' }, NOSTORE) });
}

function bars(list, unit) {
  const max = Math.max(1, ...list.map((x) => x[1]));
  return list.map(([name, n]) => `<li><span class="nm">${esc(name)}</span><span class="track"><i style="width:${(n / max) * 100}%"></i></span><b>${n}${unit}</b></li>`).join('');
}
function page(s) {
  const empty = !s.people.length;
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><title>Unifiウォッチ 閲覧数</title>
<style>
:root{--bg:#f3f5f1;--panel:#ffffff;--ink:#15201a;--muted:#66736b;--line:#dfe5df;--acc:#1f9d63;--acc2:#5cc28f;--wash:#e3f4ea;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1512;--panel:#18201c;--ink:#e6eee9;--muted:#9aa89f;--line:#2a3530;--acc:#4cc58a;--acc2:#2f8f62;--wash:#1d3328;color-scheme:dark}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Sans","Noto Sans JP","Yu Gothic UI",system-ui,sans-serif}
main{max-width:780px;margin:0 auto;padding:calc(env(safe-area-inset-top,0px) + 22px) 16px calc(env(safe-area-inset-bottom,0px) + 40px);display:grid;gap:16px}
.kicker{font-size:12px;letter-spacing:.2em;color:var(--acc);margin:0;font-weight:700}h1{font-size:23px;margin:2px 0;font-weight:800}.sub{color:var(--muted);font-size:13px;margin:0}
h2{font-size:14px;letter-spacing:.08em;color:var(--acc);margin:0 0 10px;font-weight:700}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;min-width:0}
.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}@media(min-width:620px){.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;min-width:0}.tile .lb{font-size:12px;color:var(--muted)}.tile .big{font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;margin:2px 0}.tile .big span{font-size:13px;font-weight:500;margin-left:3px;color:var(--muted)}.tile .sm{font-size:12px;color:var(--muted)}
.chartwrap{position:relative}#chart svg{display:block}.grid{stroke:var(--line);stroke-width:1}.tick{fill:var(--muted);font-size:11px}.bar{fill:var(--acc2)}.bar.today{fill:var(--acc)}.mk{fill:var(--ink)}.hit{fill:transparent}
#tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12.5px;white-space:nowrap;transform:translate(-50%,-110%);box-shadow:0 4px 12px rgba(0,0,0,.12)}
ul{list-style:none;margin:0;padding:0}.people li,.recent li{padding:8px 0;border-bottom:1px solid var(--line);font-size:13.5px;line-height:1.6}
.people li{display:grid;grid-template-columns:3em 1fr auto;gap:8px;align-items:start}.people small{display:block;color:var(--muted);font-size:12px}
.no{color:var(--acc);font-weight:800;font-variant-numeric:tabular-nums}
time{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.recent li{display:flex;gap:10px}.recent time{min-width:6.6em}
.pill{display:inline-block;font-size:11.5px;border-radius:999px;padding:0 8px;background:var(--wash);color:var(--acc);font-weight:700;margin-right:4px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}.pills span{border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:13px;color:var(--muted)}.pills b{color:var(--ink)}
.two{display:grid;gap:16px}@media(min-width:620px){.two{grid-template-columns:1fr 1fr}}
.bars li{display:grid;grid-template-columns:9em 1fr 3em;gap:8px;align-items:center;font-size:13.5px;padding:4px 0}.bars .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.track i{display:block;height:100%;background:var(--acc2)}.bars b{text-align:right}
.empty,.note{color:var(--muted);font-size:13px;line-height:1.7;margin:0}
button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:999px;padding:9px 16px;cursor:pointer}button.on{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:700}
</style></head><body><main>
<header><p class="kicker">自分専用のページ</p><h1>Unifi・JPYC・Kaia ウォッチ　閲覧数</h1><p class="sub">${fmtTs(s.now)} 時点・開き直すと最新になります</p></header>
<div class="tiles">${s.tiles.map(([lb, n, sm]) => `<div class="tile"><div class="lb">${lb}</div><div class="big">${n}<span>人</span></div><div class="sm">${sm}</div></div>`).join('')}</div>
<section><h2>毎日の人数（30日間）</h2>${empty ? '<p class="empty">まだ記録がありません。だれかがサイトを開くと、ここに数が出ます。</p>' : `<div class="chartwrap"><div id="chart" role="img" aria-label="30日間の毎日の人数"></div><div id="tip" hidden></div></div>
<script>var DAYS=${JSON.stringify(s.days.map((x) => { const m = md(x.d); return { l: m.s, w: m.w, p: x.p, v: x.v, n: x.n }; }))};</script><p class="note" style="margin-top:6px">棒＝その日に開いた人数、黒い点＝その日にはじめて来た人数</p>`}</section>
<div class="two"><section><h2>見られているタブ（人数）</h2><ul class="bars">${bars(s.tabPeople, '人')}</ul></section>
<section><h2>人に広めた回数</h2>${s.shareKinds.length ? `<ul class="bars">${bars(s.shareKinds, '回')}</ul>` : '<p class="empty">まだありません</p>'}</section></div>
<section><h2>だれが・どれだけ</h2>
<div class="pills"><span>2日以上来た人 <b>${s.returning}人</b></span><span>今日はじめて <b>${s.todayNew}人</b></span><span>今日また来た <b>${s.todayBack}人</b></span></div>
${s.people.length ? `<ul class="people">${s.people.map((v) => `<li><b class="no">#${v.no}</b><span>${esc(v.dev)}・${esc(v.place)}${v.app ? '・ホーム画面から' : ''}${v.line ? '・LINEの中から' : ''}<small>${Math.max(1, v.days.size)}日来た・開いた${v.opens}回${v.tabs.size ? `・見たタブ ${[...v.tabs].map((k) => esc((TAB[k] || k).split(' ')[0])).join('')}` : ''}${v.share ? `　<span class="pill">広めた${v.share}</span>` : ''}</small></span><time>${fmtTs(v.last)}</time></li>`).join('')}</ul>` : '<p class="empty">まだありません</p>'}</section>
<section><h2>最近のできごと</h2>${s.recent.length ? `<ul class="recent">${s.recent.map((r) => `<li><time>${fmtTs(r.ts)}</time><span><b class="no">#${r.no}</b> ${esc(r.text)}</span></li>`).join('')}</ul>` : '<p class="empty">まだありません</p>'}</section>
<div class="two"><section><h2>端末</h2>${s.devs.length ? `<ul class="bars">${bars(s.devs, '人')}</ul>` : '<p class="empty">まだありません</p>'}</section>
<section><h2>地域（都道府県）</h2>${s.places.length ? `<ul class="bars">${bars(s.places, '人')}</ul>` : '<p class="empty">まだありません</p>'}</section></div>
<section><h2>自分の閲覧を数えない</h2><p class="note" style="margin-bottom:10px">使う端末ごとに1回押すと、その端末で開いても数えなくなり、これまでの記録も消えます。</p><button id="me" type="button"></button><p id="meMsg" class="note" style="margin-top:10px"></p></section>
<p class="note">機械のアクセスは除いています（${s.bots}件）。このページのURLは人に教えないでください。</p>
</main>
<script>
(function(){
  var b=document.getElementById('me'),K='watch.noCount';
  function get(){try{return localStorage.getItem(K)==='1'}catch(e){return false}}
  function draw(){var on=get();b.textContent=on?'この端末は数えていません（押すと元に戻す）':'この端末を数えない';b.className=on?'on':''}
  b.addEventListener('click',function(){
    var turnOn=!get();try{if(turnOn)localStorage.setItem(K,'1');else localStorage.removeItem(K)}catch(e){}draw();
    if(!turnOn)return;var v='';try{v=localStorage.getItem('watch.vid')||''}catch(e){}
    var msg=document.getElementById('meMsg');
    fetch(location.pathname.replace(/[/]$/,'')+'/forget',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({v:v})}).then(function(r){return r.json()}).then(function(j){msg.textContent='この端末のこれまでの記録'+j.deleted+'件を消しました。開き直すと数字に反映されます。'}).catch(function(){msg.textContent='記録を消せませんでした。電波のある所でもう一度押してください。'});
  });draw();
  var el=document.getElementById('chart');if(!el)return;
  function drawChart(){
    var W=Math.max(260,el.clientWidth),H=200,L=30,R=6,T=10,B=24,pw=W-L-R,ph=H-T-B,n=DAYS.length,slot=pw/n,bw=Math.max(3,slot-2);
    var max=1;DAYS.forEach(function(d){if(d.p>max)max=d.p;if(d.n>max)max=d.n});
    var steps=[1,2,5,10,20,50,100,200,500,1000,2000,5000],step=max;
    for(var i=0;i<steps.length;i++){if(max/steps[i]<=4){step=steps[i];break}}
    var top=Math.max(step,Math.ceil(max/step)*step),y=function(v){return T+ph-v/top*ph},o='';
    for(var v=0;v<=top;v+=step)o+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v)+'" y2="'+y(v)+'" class="grid"/><text x="'+(L-6)+'" y="'+(y(v)+4)+'" class="tick" text-anchor="end">'+v+'</text>';
    DAYS.forEach(function(d,i){
      var x0=L+i*slot+(slot-bw)/2,h=d.p/top*ph,yb=T+ph;
      if(d.p>0)o+='<rect class="bar'+(i===n-1?' today':'')+'" x="'+x0+'" y="'+(yb-h)+'" width="'+bw+'" height="'+h+'" rx="'+Math.min(3,bw/2)+'"/>';
      if(d.n>0)o+='<circle class="mk" cx="'+(x0+bw/2)+'" cy="'+y(d.n)+'" r="3"/>';
      if(i%5===4||i===n-1)o+='<text x="'+(x0+bw/2)+'" y="'+(H-7)+'" class="tick" text-anchor="middle">'+(i===n-1?'今日':d.l)+'</text>';
      o+='<rect class="hit" x="'+(L+i*slot)+'" y="'+T+'" width="'+slot+'" height="'+ph+'" data-t="'+d.l+'('+d.w+') '+d.p+'人・開いた'+d.v+'回・はじめて'+d.n+'人"/>';
    });
    el.innerHTML='<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'">'+o+'</svg>';
  }
  drawChart();window.addEventListener('resize',drawChart);
  var tip=document.getElementById('tip'),wrap=tip.parentNode;
  function show(e){var t=e.target;if(!t.classList||!t.classList.contains('hit')){tip.hidden=true;return}var r=t.getBoundingClientRect(),w=wrap.getBoundingClientRect();tip.textContent=t.getAttribute('data-t');tip.style.left=Math.min(Math.max(r.left+r.width/2-w.left,70),w.width-70)+'px';tip.style.top=(r.top-w.top+40)+'px';tip.hidden=false}
  wrap.addEventListener('pointermove',show);wrap.addEventListener('pointerdown',show);wrap.addEventListener('pointerleave',function(){tip.hidden=true});
})();
</script></body></html>`;
}
