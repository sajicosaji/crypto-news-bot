// 閲覧の記録：POST /api/hit { v, e, k, app }（持ち主だけが見られる集計ページ /stats/<鍵> 用）
// 記録するのは「端末ごとのランダムな番号・日時・何をしたか・端末の種類・都道府県・LINEの中から開いたか」だけ。IPアドレス・名前は保存しない
// 番号は Cookie（wv・約400日）にも書いておき、次からはそちらを正とする（iPhone の Safari がブラウザの保存を消しても、同じ人とわかるように）

// 人ではなく機械（検索エンジン・リンクの下見・自動ブラウザ）らしいアクセス
const BOT_UA = /bot|crawl|spider|slurp|preview|headless|lighthouse|pagespeed|facebookexternalhit|embedly|curl|wget|python|httpx|go-http|java\/|phantom|puppeteer|playwright|scanner|monitor/i;
// 大手クラウドなどデータセンターの回線番号（ASN）。iPhone のプライベートリレー（Cloudflare/Akamai/Fastly）は人なので入れない
const DC_ASN = new Set([15169, 396982, 16509, 14618, 8075, 16276, 14061, 24940, 63949, 31898, 45102, 132203, 12876, 51167, 20473, 9009, 60068, 212238, 136907, 55990, 49981]);
const TABS = ['home', 'news', 'guide', 'risk', 'market', 'learn', 'words'];
const EV = { open: [''], tab: TABS, share: ['line', 'copy', 'home'] };
const jstDay = (ms) => new Date(ms + 9 * 3600e3).toISOString().slice(0, 10);
const clean = (v, n) => String(v || '').replace(/[^a-z0-9]/gi, '').slice(0, n);

function deviceOf(ua) {
  if (/iphone|ipad|ipod/i.test(ua)) return 'iPhone・iPad';
  if (/android/i.test(ua)) return 'Android';
  if (/mobile/i.test(ua)) return 'その他のスマホ';
  return 'パソコン';
}
function cookieVid(req) { const m = /(?:^|;\s*)wv=([a-z0-9]{8,40})(?:;|$)/i.exec(req.headers.get('cookie') || ''); return m ? m[1] : ''; }
function withVid(vid) {
  return new Response(JSON.stringify({ v: vid }), { status: 200, headers: {
    'content-type': 'application/json', 'cache-control': 'no-store',
    'set-cookie': `wv=${vid}; Max-Age=34560000; Path=/; Secure; HttpOnly; SameSite=Lax` } });
}

export async function onRequestPost({ request: req, env }) {
  const none = new Response(null, { status: 204, headers: { 'cache-control': 'no-store' } });
  if (!env.DB) return none;
  let b = {};
  try { const t = await req.text(); if (t.length > 1000) return none; b = JSON.parse(t || '{}'); } catch (e) { return none; }
  const vid = cookieVid(req) || clean(b.v, 40);
  if (vid.length < 8) return none;
  const ev = Object.prototype.hasOwnProperty.call(EV, b.e) ? b.e : '';
  if (!ev) return none;
  const kind = EV[ev].includes(b.k) ? b.k : EV[ev][0];
  const now = Date.now();
  // 同じ端末・同じ操作は、開いた記録は10分、ほかは30秒のあいだ1回とみなす（開き直し・二度押しで増えないように）
  const win = ev === 'open' ? 10 * 60e3 : 30e3;
  const dup = await env.DB.prepare('SELECT 1 FROM hits WHERE vid = ? AND ev = ? AND kind = ? AND ts > ? LIMIT 1').bind(vid, ev, kind, now - win).first();
  if (dup) return withVid(vid);
  const cf = req.cf || {}, ua = req.headers.get('user-agent') || '';
  const asn = Number(cf.asn) || 0;
  const bot = BOT_UA.test(ua) || DC_ASN.has(asn) ? 1 : 0;
  await env.DB.prepare('INSERT INTO hits (ts, day, vid, ev, kind, dev, app, line, country, region, bot, asn, org) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)')
    .bind(now, jstDay(now), vid, ev, kind, deviceOf(ua), b.app ? 1 : 0, / Line\//.test(ua) ? 1 : 0, String(cf.country || ''), String(cf.region || ''), bot, asn, String(cf.asOrganization || '').slice(0, 60)).run();
  return withVid(vid);
}
