"""知識ベース（kaia_knowledge.yaml）と用語集（kaia_glossary.yaml）を、ページが読む public/knowledge.json にする。

使い方（crypto-news-bot のフォルダで）:
    python kaia_site/build.py
そのあと kaia_site で wrangler pages deploy public --project-name kaia-jpyc-desk
"""
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "public" / "knowledge.json"


def build() -> dict:
    kb = yaml.safe_load((ROOT / "kaia_knowledge.yaml").read_text(encoding="utf-8"))
    glossary = yaml.safe_load((ROOT / "kaia_glossary.yaml").read_text(encoding="utf-8"))["terms"]

    cats = {c["id"] for c in kb["categories"]}
    problems = []
    seen = set()
    for c in kb["cards"]:
        if c["id"] in seen:
            problems.append(f"カードのidが重複: {c['id']}")
        seen.add(c["id"])
        for key in ("q", "one", "easy"):
            if not str(c.get(key, "")).strip():
                problems.append(f"{c['id']}: {key} が空")
        if c.get("cat") not in cats:
            problems.append(f"{c['id']}: 分類 {c.get('cat')} が categories に無い")
        if c.get("tags"):
            try:
                re.compile(c["tags"])
            except re.error as e:
                problems.append(f"{c['id']}: tags の正規表現が壊れている ({e})")
        for s in c.get("src", []):
            if not str(s.get("u", "")).startswith("https://"):
                problems.append(f"{c['id']}: 出典URLが https で始まらない")
    if problems:
        raise SystemExit("知識ベースに問題があります:\n- " + "\n- ".join(problems))

    for c in kb["cards"]:
        for key in ("easy", "pro"):
            if isinstance(c.get(key), str):
                c[key] = c[key].strip()
        # ページ側はJavaScriptの正規表現で使うので、Python専用の書き方 (?-i:...) を外す
        if c.get("tags"):
            c["tags"] = re.sub(r"\(\?-i:([^()]*(?:\([^()]*\)[^()]*)*)\)", r"\1", c["tags"])
    for t in kb.get("templates", []):
        t["text"] = t["text"].strip()

    kb["glossary"] = [{"term": t["term"], "cat": t.get("cat", "basic"), "desc": t["desc"], "pro": t.get("pro", "")}
                      for t in glossary]
    return kb


def main() -> None:
    kb = build()
    OUT.write_text(json.dumps(kb, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{OUT} を書き出しました（カード{len(kb['cards'])}枚・用語{len(kb['glossary'])}語・"
          f"年表{len(kb.get('timeline', []))}件・{OUT.stat().st_size:,} bytes）")


if __name__ == "__main__":
    sys.exit(main())
