"""見せる用のリポジトリ（sajicosaji/kaia-jpyc-watch）に、サイトの部分だけを写す。

・写すもの: kaia_site/public（knowledge.json は向こうで作り直す）・functions・build.py・schema.sql・package.json、kaia_glossary.yaml、kaia_knowledge.yaml
・kaia_knowledge.yaml からは、ページで使っていないアンバサダー用の部分（会話のコツのカード・心得・そのまま使える文章）と本人のメモを外す
・写し先は、このリポジトリのとなりのフォルダ kaia-jpyc-watch（なければ作らない）
・変わったところがあれば、向こうで commit して push する

使い方: python kaia_site/sync_view_repo.py ["変更の説明"]
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

MAIN = Path(__file__).resolve().parents[1]
VIEW = MAIN.parent / "kaia-jpyc-watch"
DROP_KEYS = ("etiquette", "templates")

HEADER = """# Unifi・JPYC・Kaia ウォッチの文章（解説・基本データ・年表・リンク・リスク・使い方・リワード率）
#
# kaia_site/build.py が、これと kaia_glossary.yaml から kaia_site/public/knowledge.json を作る。
# - 事実には日付を付ける。変わりやすいもの（利率・キャンペーン）は特に。
# - 出典(src)は公式を優先。報道・解説記事は「〜と報じられています」の扱い。
# - 値上がりの話・おすすめ・「絶対」「必ず」は書かない。
#
# src の形: {t: タイトル, u: URL, d: 日付(YYYY-MM-DD, 分からなければ省略)}

"""


def filter_knowledge(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("as_of:"))
    lines = lines[start:]

    # 上の階層の区切り（「key:」で始まる行）で分ける。直前の「# ====」の見出しは、その区切りに含める
    def top_key(ln):
        m = re.match(r"^([a-z_]+):", ln)
        return m.group(1) if m else None

    out, skip = [], False
    for i, ln in enumerate(lines):
        k = top_key(ln)
        nxt = next((top_key(x) for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
        if ln.startswith("# ===") and nxt in DROP_KEYS:
            skip = True
            continue
        if k:
            skip = k in DROP_KEYS
        if ln.startswith("# ===") and nxt and nxt not in DROP_KEYS:
            skip = False
        if not skip:
            out.append(ln)
    text = "".join(out)

    # 会話のコツ（hidden: true）のカードと、その見出し・分類を外す
    head, sep, rest = text.partition("\ncards:\n")
    m = re.search(r"\n(?=# ===)", rest)
    cards, tail = (rest[: m.start() + 1], rest[m.start() + 1:]) if m else (rest, "")
    items = re.split(r"(?m)^(?=  - id: |  # ===)", cards)
    keep = [x for x in items if "\n    hidden: true\n" not in x and not (x.startswith("  # ===") and "会話のコツ" in x.splitlines()[0])]
    text = head + sep + "".join(keep) + tail
    text = re.sub(r"(?m)^  - \{id: talk, [^\n]*\n", "", text)

    # 本人のメモ（方針の引用）
    text = re.sub(r"(?m)^# 本人[^\n]*\n", "", text)
    return HEADER + text


def expected(kb: dict) -> dict:
    kb = {k: v for k, v in kb.items() if k not in DROP_KEYS}
    kb["cards"] = [c for c in kb["cards"] if not c.get("hidden")]
    kb["categories"] = [c for c in kb["categories"] if c["id"] != "talk"]
    return kb


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(VIEW), *args], check=check, capture_output=True, text=True, encoding="utf-8")


def main():
    if not (VIEW / ".git").exists():
        raise SystemExit(f"写し先がありません: {VIEW}")
    src_kb = (MAIN / "kaia_knowledge.yaml").read_text(encoding="utf-8")
    filtered = filter_knowledge(src_kb)
    # 外したあとの中身が、外すべきものだけ違うことを確かめる
    assert yaml.safe_load(filtered) == expected(yaml.safe_load(src_kb)), "kaia_knowledge.yaml の絞り込みがずれました"
    for word in ("# 本人", "忖度", "talk-", "etiquette:", "templates:", "会話のコツ"):
        assert word not in filtered, f"消し忘れ: {word}"

    # フォルダごと消すと OneDrive がつかんでいて失敗することがあるので、ファイル単位で上書きし、本番に無いファイルだけ消す
    for d in ("kaia_site/public", "kaia_site/functions"):
        shutil.copytree(MAIN / d, VIEW / d, ignore=shutil.ignore_patterns("knowledge.json"), dirs_exist_ok=True)
        for f in (VIEW / d).rglob("*"):
            if f.is_file() and f.name != "knowledge.json" and not (MAIN / f.relative_to(VIEW)).exists():
                f.unlink()
    for f in ("kaia_site/build.py", "kaia_site/schema.sql", "kaia_site/package.json", "kaia_glossary.yaml"):
        shutil.copy2(MAIN / f, VIEW / f)
    (VIEW / "kaia_knowledge.yaml").write_text(filtered, encoding="utf-8", newline="\n")
    # 改行は LF にそろえる（Windows の作業コピーは CRLF のことがあり、写すたびに全行が変わったことになるのを防ぐ）
    for f in VIEW.rglob("*"):
        if f.is_file() and ".git" not in f.parts and (f.suffix in (".html", ".js", ".json", ".py", ".sql", ".yaml", ".md", ".txt", ".webmanifest") or f.name == "_headers"):
            b = f.read_bytes()
            if b"\r\n" in b:
                f.write_bytes(b.replace(b"\r\n", b"\n"))

    subprocess.run([sys.executable, str(VIEW / "kaia_site" / "build.py")], check=True, capture_output=True)
    made = json.loads((VIEW / "kaia_site/public/knowledge.json").read_text(encoding="utf-8"))
    main_kb = json.loads((MAIN / "kaia_site/public/knowledge.json").read_text(encoding="utf-8"))
    assert made == expected(main_kb), "knowledge.json が本番（アンバサダー用を外したもの）と一致しません"

    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("変わったところはありません")
        return
    msg = sys.argv[1] if len(sys.argv) > 1 else "サイトの最新版を写す"
    git("commit", "-q", "-m", msg + "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    print(git("push", "-q", "origin", "main", check=False).stderr.strip() or "push しました")


if __name__ == "__main__":
    main()
