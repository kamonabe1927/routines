#!/usr/bin/env python3
"""model-watch: AI モデルの新着（新モデル・新顔の提供元・価格改定・廃止予定）の候補を集める。

使い方:
    python3 watch.py raw.json [時間=48]

raw.json に候補を書き、標準出力に件数と判定（decision）を出す。
判定（decision）:
    proceed  true なら SKILL.md の手順 2 以降（既出の確認・仕分け・投稿）に進む。false なら何もせずに終了してよい
    reasons  判定の理由

取得元（どれも実行環境のシェルから届くもの）:
    models.dev  公開カタログ github.com/sst/models.dev の git 履歴。models/<ラボ>/<モデル>.toml の追加を新モデルとして拾う
    LiteLLM     github.com/BerriAI/litellm の model_prices_and_context_window.json の差分。
                廃止予定日・価格の変更と、新しく足された行
    HN          Hacker News で伸びた、モデル公開らしい話題と、その日の大きな話題。新しいモデルの名前での検索
    はてブ      はてなブックマークのテクノロジー人気エントリーのうち、モデルの発表・公開らしい記事

提供元やモデルの名前の一覧では絞らない。名前を知らない新興の提供元を落とさないため。
"""

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 以前
    tomllib = None

NOW = datetime.now(timezone.utc)
RECENT_DAYS = 30  # 公開日がこの日数以内のモデルを新着とする（カタログへの登録は公開から遅れることがある）
BULK = 8  # 1 つのコミットでこれ以上のモデルが足されたら、まとめ登録（過去分の整理など）とみなす
MAX_READ = 60  # 中身を読むモデルの上限
MAX_ROWS = 80  # LiteLLM の各一覧の上限
HN_POINTS = 80
HN_TOP_POINTS = 300  # これ以上伸びた話題は、題名に関わらず候補に入れる（題名がモデル名だけの発表を拾うため）
UA = {"User-Agent": "Mozilla/5.0 (model-watch)"}
SOURCES = ["models.dev", "litellm", "hn", "hatena"]
PRICE_KEYS = ("input_cost_per_token", "output_cost_per_token")
# 提供先として名前を出すもの（候補を絞る条件には使わない）
NOTABLE_HOSTS = {"amazon-bedrock": "Amazon Bedrock", "google-vertex": "Vertex AI", "google-vertex-anthropic": "Vertex AI",
                 "azure": "Azure", "github-copilot": "GitHub Copilot", "openrouter": "OpenRouter"}

MODEL_RE = re.compile(r"\b(models?|LLMs?|SLMs?|weights|checkpoints?|multimodal|diffusion|embeddings?|text-to-\w+|TTS|GPT)\b", re.I)
LAUNCH_RE = re.compile(r"\b(introduc\w+|announc\w+|launch\w*|releas\w+|open[- ]sourc\w+|unveil\w*|debut\w*|now available|show hn)\b", re.I)
AI_RE = re.compile(r"\b(AI|ML|neural|agents?|inference|reasoning)\b", re.I)
JA_SUBJECT = re.compile(r"モデル|LLM|AI", re.I)
JA_LAUNCH = re.compile(r"発表|公開|リリース|登場|提供開始|オープンソース|release|launch", re.I)


# ---- ネットワークを使わない部分（試験の対象） ----

def norm(s):
    """モデル名を比べやすい形にする（小文字、記号は - に、「:free」などの接尾辞は落とす）。"""
    return re.sub(r"[^a-z0-9]+", "-", s.lower().split(":")[0]).strip("-")


def parse_log(text):
    """`git log --name-only --pretty=format:@%cI%x09%s` の出力を、コミットごとの (時刻, 件名, [パス]) にする。"""
    commits = []
    for line in text.splitlines():
        if line.startswith("@"):
            at, _, subject = line[1:].partition("\t")
            commits.append((at, subject, []))
        elif line.strip() and commits:
            commits[-1][2].append(line.strip())
    return commits


def parse_toml(text):
    if tomllib:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pass
    return dict(re.findall(r'^(name|family|description|release_date|type)\s*=\s*"([^"]*)"', text, re.M))


def is_recent(release_date, today):
    """公開日が RECENT_DAYS 日以内なら True、古ければ False、日付が無い・読めないなら None。"""
    s = str(release_date)[:10]
    if re.fullmatch(r"\d{4}-\d{2}", s):  # 月までしか無い日付は、その月の 1 日として扱う
        s += "-01"
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return -7 <= (today - d).days <= RECENT_DAYS


def looks_like_model_news(title):
    return bool(MODEL_RE.search(title) or (LAUNCH_RE.search(title) and AI_RE.search(title)))


def per_million(v):
    return round(v * 1e6, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def diff_litellm(before, after):
    """LiteLLM の価格表の前後を比べ、(新しい行, 廃止予定日の変更, 価格の変更) を返す。価格は 100 万トークンあたりの米ドル。"""
    new, deprecations, prices = [], [], []
    for key, v in after.items():
        if not isinstance(v, dict) or key == "sample_spec":
            continue
        row = {"key": key, "provider": v.get("litellm_provider")}
        old = before.get(key)
        if not isinstance(old, dict):
            new.append({**row, "mode": v.get("mode"), "input": per_million(v.get("input_cost_per_token")),
                        "output": per_million(v.get("output_cost_per_token"))})
            continue
        if v.get("deprecation_date") != old.get("deprecation_date"):
            deprecations.append({**row, "before": old.get("deprecation_date"), "after": v.get("deprecation_date")})
        if any(v.get(k) != old.get(k) for k in PRICE_KEYS):
            prices.append({**row, "input_before": per_million(old.get(PRICE_KEYS[0])),
                           "input_after": per_million(v.get(PRICE_KEYS[0])),
                           "output_before": per_million(old.get(PRICE_KEYS[1])),
                           "output_after": per_million(v.get(PRICE_KEYS[1]))})
    return new, deprecations, prices


def is_fresh(m):
    """新着として扱うモデルか。公開日が新しいもの。公開日が分からないものは、まとめ登録でなければ新着とみなす。"""
    return bool(m["recent"] or (m["recent"] is None and not m["bulk"]))


def decide(o):
    """この回で手順 2 以降に進むか（proceed）を決める。"""
    fresh = [m["id"] for m in o["new_models"] if is_fresh(m)]
    reasons = []
    if fresh:
        reasons.append("新しいモデル: " + ", ".join(fresh[:8]))
    if o["new_labs"]:
        reasons.append("初めて載った提供元: " + ", ".join(lab["lab"] for lab in o["new_labs"][:8]))
    if o["deprecations"]:
        reasons.append(f"廃止予定日の変更 {len(o['deprecations'])} 件")
    if o["price_changes"]:
        reasons.append(f"価格の変更 {len(o['price_changes'])} 件")
    if o["hn"] or o["hatena"]:
        reasons.append(f"モデル公開らしい話題 HN {len(o['hn'])} 件・はてブ {len(o['hatena'])} 件")
    failed = {e.split(":", 1)[0] for e in o["errors"]}
    if all(s in failed for s in SOURCES):
        reasons.append("取得元がすべて失敗")
    proceed = bool(reasons)
    if not proceed:
        reasons.append("新着の候補が無い")
    return {"proceed": proceed, "reasons": reasons}


# ---- 取得 ----

out = {"since": None, "until": NOW.isoformat(), "new_models": [], "new_labs": [], "litellm_new": [], "deprecations": [],
       "price_changes": [], "hn": [], "hatena": [], "errors": []}
SINCE = NOW - timedelta(hours=48)  # main() で引数の時間に置き換える


def guard(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 取得元ごとに失敗を記録して続ける
        out["errors"].append(f"{name}: {str(e)[:300]}")


def get(url, as_json=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        d = r.read()
    return json.loads(d) if as_json else d.decode("utf-8", "replace")


def git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, check=True, timeout=180).stdout


def clone(repo):
    """履歴だけを浅く取る（ファイルの中身は読むときに取りに行く）。"""
    d = tempfile.mkdtemp(prefix="model-watch-")
    day = (SINCE - timedelta(days=3)).date().isoformat()
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"--shallow-since={day}",
                    f"https://github.com/{repo}", d], check=True, timeout=300, capture_output=True)
    return d


def rev_before(d):
    """期間の始まりより前の最後のコミット。浅い履歴に無ければ、いちばん古いコミット。"""
    r = git(d, "rev-list", "-1", f"--before={SINCE.isoformat()}", "HEAD").strip()
    return r or git(d, "rev-list", "HEAD").split()[-1]


def show_toml(d, path):
    try:
        return parse_toml(git(d, "show", f"HEAD:{path}"))
    except subprocess.CalledProcessError:  # その後に消えた・改名された
        return None


def models_dev():
    d = clone("sst/models.dev")
    tree = git(d, "ls-tree", "-r", "--name-only", rev_before(d), "--", "models/")
    labs_before = {p.split("/")[1] for p in tree.splitlines() if p.count("/") == 2}
    log = git(d, "log", f"--since={SINCE.isoformat()}", "--diff-filter=A", "--name-only", "--pretty=format:@%cI%x09%s",
              "--", "models/", "providers/")
    added, hosted = [], []
    for at, subject, paths in parse_log(log):
        models = [m for m in (re.fullmatch(r"models/([^/]+)/([^/]+)\.toml", p) for p in paths) if m]
        added += [(m.group(1), m.group(2), at, subject, len(models) >= BULK) for m in models]
        for p in paths:
            m = re.fullmatch(r"providers/([^/]+)/models/(?:.+/)?([^/]+)\.toml", p)
            if m:
                hosted.append((m.group(1), norm(m.group(2))))
    out["provider_rows_added"] = len(hosted)
    added.sort(key=lambda a: a[4])  # まとめ登録でないものを先に読む
    for lab, stem, at, subject, bulk in added[:MAX_READ]:
        t = show_toml(d, f"models/{lab}/{stem}.toml")
        if t is None:
            continue
        hosts = sorted({p for p, s in hosted if norm(stem) in s})
        m = {"id": f"{lab}/{stem}", "lab": lab, "name": t.get("name") or stem, "family": t.get("family"),
             "type": t.get("type"), "release_date": str(t.get("release_date") or "") or None,
             "open_weights": t.get("open_weights"), "description": str(t.get("description") or "")[:200],
             "input": (t.get("modalities") or {}).get("input") if isinstance(t.get("modalities"), dict) else None,
             "output": (t.get("modalities") or {}).get("output") if isinstance(t.get("modalities"), dict) else None,
             "context": (t.get("limit") or {}).get("context") if isinstance(t.get("limit"), dict) else None,
             "added_at": at, "commit": subject[:120], "bulk": bulk, "lab_is_new": lab not in labs_before,
             "recent": is_recent(t.get("release_date"), NOW.date()), "hosts_added": len(hosts),
             "notable_hosts": sorted({NOTABLE_HOSTS[h] for h in hosts if h in NOTABLE_HOSTS})}
        if m["recent"]:
            p = show_toml(d, f"providers/{lab}/models/{stem}.toml")  # ラボ自身の提供分があれば、その価格
            text = ((p or {}).get("cost") or {}).get("text") or {}
            price = {k: (text.get(k) or {}).get("price") for k in ("input", "output") if isinstance(text.get(k), dict)}
            if price:
                m["price_per_million"] = price
        out["new_models"].append(m)
    if len(added) > MAX_READ:
        out["errors"].append(f"models.dev(一部): 追加 {len(added)} 件のうち {MAX_READ} 件だけ読んだ")
    order = {True: 0, None: 1, False: 2}
    out["new_models"].sort(key=lambda m: (order[m["recent"]], not m["lab_is_new"], m["bulk"]))
    # 初めて載った提供元。過去分のまとめ登録で入っただけの提供元は含めない
    for lab in sorted({m["lab"] for m in out["new_models"] if m["lab_is_new"] and is_fresh(m)}):
        info = show_toml(d, f"labs/{lab}/lab.toml") or {}
        out["new_labs"].append({"lab": lab, "name": info.get("name") or lab,
                                "models": [m["id"] for m in out["new_models"] if m["lab"] == lab]})


def litellm():
    d = clone("BerriAI/litellm")
    f = "model_prices_and_context_window.json"
    if not git(d, "log", "-1", f"--since={SINCE.isoformat()}", "--pretty=format:%H", "--", f).strip():
        return  # 期間内に変更なし
    before = json.loads(git(d, "show", f"{rev_before(d)}:{f}"))
    after = json.loads(git(d, "show", f"HEAD:{f}"))
    new, deprecations, prices = diff_litellm(before, after)
    out["litellm_new"], out["deprecations"], out["price_changes"] = new[:MAX_ROWS], deprecations[:MAX_ROWS], prices[:MAX_ROWS]
    for name, rows in (("新しい行", new), ("廃止予定日", deprecations), ("価格", prices)):
        if len(rows) > MAX_ROWS:
            out["errors"].append(f"litellm(一部): {name} {len(rows)} 件のうち {MAX_ROWS} 件だけ載せた")


def hn_search(query, since, min_points, n=50):
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "tags": "story", "hitsPerPage": n,
         "numericFilters": f"created_at_i>{int(since.timestamp())},points>={min_points}"})
    time.sleep(0.3)
    return get(url, True)["hits"]


def hn_item(h, why):
    link = f"https://news.ycombinator.com/item?id={h['objectID']}"
    return {"title": h.get("title") or "", "url": h.get("url") or link, "hn": link, "points": h.get("points", 0),
            "comments": h.get("num_comments", 0), "date": h["created_at"], "why": why}


def hn():
    seen = {}
    for q in ["model", "LLM", "open weights", "introducing", "launch", "release", "AI", "Show HN"]:
        for h in hn_search(q, SINCE, HN_POINTS):
            if h["objectID"] not in seen and looks_like_model_news(h.get("title") or ""):
                seen[h["objectID"]] = hn_item(h, "題名")
    for h in hn_search("", SINCE, HN_TOP_POINTS):
        seen.setdefault(h["objectID"], hn_item(h, "大きな話題"))
    out["hn"] = sorted(seen.values(), key=lambda x: -x["points"])[:40]


def hn_stories_for_models():
    """新着のモデルの名前で HN を検索し、いちばん伸びた話題を付ける（発表元のページであることが多い）。"""
    for m in [m for m in out["new_models"] if is_fresh(m)][:12]:
        hits = [h for h in hn_search(f'"{m["name"]}"', NOW - timedelta(days=10), 20, n=10)
                if norm(m["name"]) in norm(h.get("title") or "")]
        if hits:
            m["hn_story"] = hn_item(max(hits, key=lambda h: h.get("points", 0)), "モデル名")


def hatena():
    # 人気エントリーは公開から遅れて入るので、公開 72 時間以内を対象にする
    def ln(e):
        return e.tag.rsplit("}", 1)[-1]
    root = ET.fromstring(get("https://b.hatena.ne.jp/hotentry/it.rss"))
    for it in root.iter():
        if ln(it) != "item":
            continue
        f = {ln(c): (c.text or "").strip() for c in it}
        try:
            dt = datetime.fromisoformat(f.get("date", ""))
        except ValueError:
            continue
        title = f.get("title", "")
        if dt < NOW - timedelta(hours=72) or not (JA_SUBJECT.search(title) and JA_LAUNCH.search(title)):
            continue
        out["hatena"].append({"title": f.get("title", ""), "url": f.get("link", ""), "date": dt.isoformat(),
                              "users": int(f.get("bookmarkcount") or 0), "desc": f.get("description", "")[:160]})
    out["hatena"].sort(key=lambda a: -a["users"])
    out["hatena"] = out["hatena"][:30]


def main():
    global SINCE
    if len(sys.argv) < 2:
        sys.exit("使い方: python3 watch.py raw.json [時間=48]")
    SINCE = NOW - timedelta(hours=int(sys.argv[2]) if len(sys.argv) > 2 else 48)
    out["since"] = SINCE.isoformat()
    guard("models.dev", models_dev)
    guard("litellm", litellm)
    guard("hn", hn)
    guard("hn(モデル名)", hn_stories_for_models)
    guard("hatena", hatena)
    out["counts"] = {"new_models": len(out["new_models"]),
                     "recent_models": sum(1 for m in out["new_models"] if m["recent"]),
                     "new_labs": len(out["new_labs"]), "litellm_new": len(out["litellm_new"]),
                     "deprecations": len(out["deprecations"]), "price_changes": len(out["price_changes"]),
                     "hn": len(out["hn"]), "hatena": len(out["hatena"])}
    out["decision"] = decide(out)
    with open(sys.argv[1], "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({"counts": out["counts"], "errors": out["errors"], "decision": out["decision"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
