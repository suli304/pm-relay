# -*- coding: utf-8 -*-
"""
fetch_pm.py · Polymarket → 静态 JSON 中继（在 GitHub Actions 里跑，不依赖本机网络）

产出（写进仓库 relay/ 目录，本机用 sync-relay.ps1 从 raw.githubusercontent 拉）：
  relay/markets.json   全部在售市场的精简快照（Gamma /events + /markets 分页）
  relay/series.json    命中 queries.json 的市场的价格序列（CLOB /prices-history）
  relay/meta.json      本次运行元信息（时间/条数/错误），供本机判断新鲜度

设计要点：
  - 只用标准库，Actions 里 `python fetch_pm.py` 直接跑
  - 分页有上限与早停（volume24hr 低于地板就停），不把全网拉一遍
  - 序列只抓 queries.json 命中的市场，且有硬上限，避免把他家 API 打爆
  - 任一请求失败不致命：记进 errors[]，其它照常产出

用法：
  python fetch_pm.py --out relay
  python fetch_pm.py --out relay --max-markets 1200 --series-cap 60 --no-series
  python fetch_pm.py --selftest          # 离线：只验证 JSON 形状与去重逻辑
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "dsh-pm-relay/1.0 (+local research)"}


def get_json(url, timeout=25, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def jlist(v):
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v) if isinstance(v, str) else []
        return x if isinstance(x, list) else []
    except Exception:
        return []


def slim_market(m):
    """只留本机要用到的字段（Gamma 契约，字段名以官方返回为准）"""
    return {
        "id": str(m.get("id") or ""),
        "question": m.get("question") or "",
        "slug": m.get("slug") or "",
        "outcomes": jlist(m.get("outcomes")),
        "outcomePrices": jlist(m.get("outcomePrices")),
        "clobTokenIds": jlist(m.get("clobTokenIds")),
        "volume": float(m.get("volume") or 0),
        "liquidity": float(m.get("liquidity") or 0),
        "volume24hr": float(m.get("volume24hr") or 0),
        "active": bool(m.get("active")),
        "closed": bool(m.get("closed")),
        "endDate": m.get("endDate"),
        "groupItemTitle": m.get("groupItemTitle"),
        "events": [{"id": str((e or {}).get("id") or ""), "title": (e or {}).get("title") or ""}
                   for e in (m.get("events") or [])][:2],
    }


def fetch_markets(max_markets=1200, floor_24h=1000.0, page=100, max_pages=60):
    out, seen, errors = [], set(), []
    offset = 0
    for _ in range(max_pages):
        url = ("%s/events?limit=%d&offset=%d&active=true&closed=false"
               "&order=volume24hr&ascending=false" % (GAMMA, page, offset))
        try:
            evs = get_json(url)
        except Exception as e:                       # noqa: BLE001
            errors.append("events offset=%d: %s" % (offset, e))
            break
        if not isinstance(evs, list) or not evs:
            break
        low = 0
        for ev in evs:
            v24 = float((ev or {}).get("volume24hr") or 0)
            if v24 < floor_24h:
                low += 1
            for m in ((ev or {}).get("markets") or []):
                mid = str(m.get("id") or "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                rec = slim_market(m)
                rec["volume24hr"] = rec["volume24hr"] or v24
                out.append(rec)
        offset += len(evs)
        if len(out) >= max_markets:
            break
        if low >= max(3, len(evs) // 2):             # 尾部已明显低量 → 早停
            break
    # 去重后再按 24h 量排序，截断
    out.sort(key=lambda x: -x["volume24hr"])
    return out[:max_markets], errors


def load_queries(path):
    if not path or not os.path.exists(path):
        return []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    qs = d.get("queries") if isinstance(d, dict) else d
    return [str(q).strip().lower() for q in (qs or []) if str(q).strip()]


def match_queries(markets, queries, cap=60):
    if not queries:
        return markets[:cap]
    hit = []
    for m in markets:
        blob = (m["question"] + " " + m["slug"] + " " +
                " ".join(e["title"] for e in m.get("events") or [])).lower()
        if any(q in blob for q in queries):
            hit.append(m)
        if len(hit) >= cap:
            break
    return hit


def fetch_series(markets, cap=60, intervals=(("1w", 60), ("max", 1440))):
    """对命中市场抓两份序列：近一周小时线 + 全历史日线。
    返回 {token: {"1h": [[t,p],...], "1d": [[t,p],...]}}"""
    out, errors = {}, []
    for m in markets[:cap]:
        toks = m["clobTokenIds"]
        if not toks:
            continue
        tok = str(toks[0])                           # outcomes[0] 通常是 Yes
        rec = {}
        for interval, fidelity in intervals:
            url = ("%s/prices-history?market=%s&interval=%s&fidelity=%d"
                   % (CLOB, urllib.parse.quote(tok), interval, fidelity))
            try:
                d = get_json(url)
            except Exception as e:                   # noqa: BLE001
                errors.append("series %s %s: %s" % (m["id"], interval, e))
                continue
            pts = []
            for p in (d.get("history") or []):
                t = p.get("t") or p.get("timestamp")
                v = p.get("p") if "p" in p else p.get("price")
                if t is None or v is None:
                    continue
                pts.append([int(t), round(float(v), 4)])
            if pts:
                rec["1h" if interval == "1w" else "1d"] = pts
        if rec:
            rec["_market"] = m["id"]
            out[tok] = rec
    return out, errors


def cmd_selftest():
    sample_market = {"id": "553001", "question": "Fed rate hike in October 2026?", "slug": "fed-hike-oct",
                     "outcomes": '["Yes","No"]', "outcomePrices": '["0.41","0.59"]',
                     "clobTokenIds": '["tokA_yes","tokA_no"]', "volume": 1240000, "volume24hr": 88000,
                     "liquidity": 88000, "active": True, "closed": False,
                     "endDate": "2026-10-29T00:00:00Z", "events": [{"id": "e1", "title": "Fed decision"}]}
    s = slim_market(sample_market)
    checks = [
        ("JSON 字符串字段被解析成 list", s["outcomes"] == ["Yes", "No"] and s["clobTokenIds"][0] == "tokA_yes",
         str(s["outcomes"])),
        ("数值字段转 float", isinstance(s["volume"], float) and s["volume"] == 1240000.0, str(s["volume"])),
        ("events 精简保留", s["events"][0]["title"] == "Fed decision", str(s["events"])),
    ]
    mk = [s, {"id": "1", "question": "US government shutdown in 2026?", "slug": "shutdown",
              "clobTokenIds": ["t2"], "volume": 1, "volume24hr": 2, "events": []}]
    hit = match_queries(mk, ["fed", "shutdown"], cap=10)
    checks.append(("queries 命中过滤", len(hit) == 2, "%d 命中" % len(hit)))
    hit2 = match_queries(mk, ["nonexistent"], cap=10)
    checks.append(("未命中不硬塞", len(hit2) == 0, "%d 命中" % len(hit2)))
    ok = True
    for name, good, ev in checks:
        ok = ok and good
        print(("  PASS  " if good else "  FAIL  ") + name + "  [" + ev[:90] + "]")
    print("RESULT:", "ALL PASS" if ok else "HAS FAIL")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="relay")
    ap.add_argument("--queries", default="relay/queries.json")
    ap.add_argument("--max-markets", type=int, default=1200)
    ap.add_argument("--floor-24h", type=float, default=1000.0)
    ap.add_argument("--series-cap", type=int, default=60)
    ap.add_argument("--no-series", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return cmd_selftest()

    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    queries = load_queries(a.queries)
    mk, errs = fetch_markets(a.max_markets, a.floor_24h)
    picked = match_queries(mk, queries, cap=a.series_cap) if queries else mk[:a.series_cap]
    ser, errs2 = ({}, []) if a.no_series else fetch_series(picked, a.series_cap)

    json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "n": len(mk), "markets": mk},
              open(os.path.join(a.out, "markets.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    json.dump(ser, open(os.path.join(a.out, "series.json"), "w", encoding="utf-8"), ensure_ascii=False)
    meta = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "n_markets": len(mk), "n_queries": len(queries), "n_series": len(ser),
            "elapsed_s": round(time.time() - t0, 1),
            "errors": (errs + errs2)[:20]}
    json.dump(meta, open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
