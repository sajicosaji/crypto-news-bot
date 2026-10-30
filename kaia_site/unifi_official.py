"""Unifi公式サイト（unifi.me）の「よくある質問」「お知らせ」「説明ページ」「利用規約」を読んでテキストに保存する。

unifi.me は画面を後から組み立てる作りで、ふつうのダウンロードでは本文が取れないので、ブラウザ（Playwright）で開く。
知識ベース（kaia_knowledge.yaml）を最新にするときの調べもの用。Actionsでは使わない。

使い方（Playwright入りのPythonで）:
    python kaia_site/unifi_official.py [保存先フォルダ]
"""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "data/unifi_official")
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1 Line/14.0.0")
PAGES = ["faq", "guide", "doc/wallet", "doc/trust", "doc/jpyc", "doc/usdt", "reward/kaia", "reward/usdt",
         "term/TERMS_OF_SERVICE/UNIFI", "term/TERMS_OF_SERVICE/WALLET", "term/TERMS_OF_SERVICE/AGGREGATOR"]


def text_of(page) -> str:
    return re.sub(r"\n\s*\n+", "\n", page.evaluate("() => document.body.innerText") or "").strip()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ja-JP", viewport={"width": 430, "height": 900}, user_agent=UA)
        page = ctx.new_page()
        for path in PAGES:
            page.goto(f"https://www.unifi.me/{path}?lng=ja-JP", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            (OUT / f"{path.replace('/', '_')}.txt").write_text(text_of(page), encoding="utf-8")
            print("保存:", path)

        # お知らせは一覧から1件ずつ開く（タイトルの次の行が日付）
        page.goto("https://www.unifi.me/announcement?lng=ja-JP", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        lines = [x.strip() for x in text_of(page).split("\n") if x.strip()]
        items = [(lines[i - 1], x) for i, x in enumerate(lines) if i and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)]
        notices = []
        for title, date in items:
            page.goto("https://www.unifi.me/announcement?lng=ja-JP", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            page.get_by_text(title, exact=True).first.click(timeout=8000)
            page.wait_for_timeout(2500)
            body = text_of(page)
            notices.append({"date": date, "title": title, "url": page.url, "text": body[max(body.find(title), 0):]})
            print("お知らせ:", date, title[:40])
        (OUT / "announcements.json").write_text(json.dumps(notices, ensure_ascii=False, indent=1), encoding="utf-8")
        browser.close()


if __name__ == "__main__":
    main()
