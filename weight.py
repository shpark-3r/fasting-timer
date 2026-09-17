"""체중 기록 CLI — 기록·체성분·이벤트·단백질·재개기준 감시를 한 번에.

사용법:
  python weight.py                             # 현황 요약
  python weight.py add 95.9                    # 오늘 피쿡 기록
  python weight.py add 95.9 --fat 29.9         # 체지방률 포함 → 지방/제지방 추세
  python weight.py add 94.6 --scale atply      # 앳플리 측정 (피쿡 환산 자동)
  python weight.py add 96.1 --date 2026-09-18 --note "마라톤 다음날"
  python weight.py event run 5km               # 이벤트: run swim walk inject fast travel binge other
  python weight.py event inject 5mg --date 2026-08-30
  python weight.py binge --note "야근 배달"      # = event binge
  python weight.py food "닭가슴살 2팩, 쉐이크 2스쿱, 계란 4개"   # 단백질 집계
  python weight.py food --show                 # 오늘 먹은 것
  python weight.py migrate PATH                # 기존 weight_log.md 표 → CSV 1회 이관

데이터: weight_data.csv (체중), weight_events.csv (이벤트), food_log.csv (식사),
        weight_config.json (기준값), food_table.json (개인 식품표 — 직접 추가/수정)
"""
import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "weight_data.csv"
EVENTS_PATH = HERE / "weight_events.csv"
FOOD_LOG = HERE / "food_log.csv"
FOOD_TABLE = HERE / "food_table.json"
CONFIG_PATH = HERE / "weight_config.json"
FIELDS = ["date", "weight_raw", "scale", "weight_pooq", "fat_pct", "excluded", "note"]
EVENT_FIELDS = ["date", "kind", "value", "note"]
FOOD_FIELDS = ["date", "item", "qty", "protein", "kcal", "raw"]
SCALE_KR = {"pooq": "피쿡", "atply": "앳플리", "unknown": "미명시"}
EVENT_KINDS = ["run", "swim", "walk", "inject", "fast", "travel", "binge", "other"]
EVENT_KR = {"run": "런닝", "swim": "수영", "walk": "걷기", "inject": "주사", "fast": "단식",
            "travel": "여행", "binge": "폭식", "other": "기타"}

DEFAULT_CONFIG = {
    "start_date": "2026-03-21",
    "start_weight": 107.7,
    "scale_offset": {"pooq": 0.0, "atply": 0.8, "unknown": 0.0},
    "thresholds": {"noise": 0.8, "rebound": 1.1, "waterdrop": -1.0},
    "protein_target": 120,
    "half_life_days": 5,
    "restart": {
        "active": True,
        "pause_start": "2026-08-30",
        "ceiling": 96.0,
        "consecutive": 3,
        "review_date": "2026-09-28",
        "binge_limit": 2,
    },
    "md_path": str(
        Path.home()
        / ".claude/projects/C--Users-Park-Documents-ClaudeCode-fasting-timer/memory/weight_log.md"
    ),
}


# ---------- 공통 ----------

def today():
    return datetime.now(KST).date()


def num(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def fmt_d(x):
    return f"{x:+.1f}kg"


def md(d):
    return f"{d.month}/{d.day}"


def parse_date(s):
    return date.fromisoformat(s) if s else today()


# ---------- IO ----------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def load_rows():
    if not CSV_PATH.exists():
        return []
    rows = []
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r["date"] = date.fromisoformat(r["date"])
            r["weight_raw"] = float(r["weight_raw"])
            r["weight_pooq"] = float(r["weight_pooq"])
            fp = r.get("fat_pct") or ""
            r["fat_pct"] = float(fp) if fp else None
            r["excluded"] = r["excluded"] == "1"
            rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def save_rows(rows):
    rows.sort(key=lambda r: r["date"])
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "date": r["date"].isoformat(),
                "weight_raw": num(r["weight_raw"]),
                "scale": r["scale"],
                "weight_pooq": num(r["weight_pooq"]),
                "fat_pct": num(r["fat_pct"]) if r["fat_pct"] is not None else "",
                "excluded": "1" if r["excluded"] else "0",
                "note": r["note"],
            })


def load_events():
    if not EVENTS_PATH.exists():
        return []
    out = []
    with EVENTS_PATH.open(encoding="utf-8", newline="") as f:
        for e in csv.DictReader(f):
            out.append({"date": date.fromisoformat(e["date"]), "kind": e["kind"],
                        "value": e.get("value") or "", "note": e.get("note") or ""})
    out.sort(key=lambda e: e["date"])
    return out


def append_csv(path, fields, row):
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def load_food_log():
    if not FOOD_LOG.exists():
        return []
    with FOOD_LOG.open(encoding="utf-8", newline="") as f:
        out = []
        for r in csv.DictReader(f):
            r["date"] = date.fromisoformat(r["date"])
            r["protein"] = float(r["protein"]) if r["protein"] else None
            r["kcal"] = float(r["kcal"]) if r["kcal"] else None
            out.append(r)
        return out


def load_food_table():
    if not FOOD_TABLE.exists():
        sys.exit(f"{FOOD_TABLE.name} 없음")
    return json.loads(FOOD_TABLE.read_text(encoding="utf-8"))


def to_pooq(raw, scale, cfg):
    return round(raw + cfg["scale_offset"].get(scale, 0.0), 2)


# ---------- 통계 ----------

def in_window(r, end, days):
    return end - timedelta(days=days - 1) <= r["date"] <= end


def window_mean(measured, end, days):
    """end 포함 직전 `days`일 창의 피쿡 환산 평균. 표본 2개 미만이면 None."""
    pts = [r["weight_pooq"] for r in measured if in_window(r, end, days)]
    return (round(mean(pts), 2), len(pts)) if len(pts) >= 2 else (None, len(pts))


def comp_window(measured, end, days):
    """체지방률 있는 표본으로 (지방kg, 제지방kg) 창 평균."""
    pts = [(r["weight_pooq"] * r["fat_pct"] / 100, r["weight_pooq"] * (1 - r["fat_pct"] / 100))
           for r in measured if r["fat_pct"] is not None and in_window(r, end, days)]
    if len(pts) < 2:
        return None, len(pts)
    return (round(mean(p[0] for p in pts), 1), round(mean(p[1] for p in pts), 1)), len(pts)


def residual_pct(days, half_life):
    return round(100 * 0.5 ** (days / half_life), 1)


def protein_today(food_log, d):
    items = [f for f in food_log if f["date"] == d]
    p = sum(f["protein"] for f in items if f["protein"] is not None)
    k = sum(f["kcal"] for f in items if f["kcal"] is not None)
    return round(p), round(k), items


def compute_stats(rows, cfg, events, food_log, now):
    measured = [r for r in rows if not r["excluded"]]
    if not measured:
        return None
    L = measured[-1]
    s = {"last": L, "prev": None, "record_before": None, "is_record": False}

    if len(measured) >= 2:
        P = measured[-2]
        s["prev"] = P
        s["gap"] = (L["date"] - P["date"]).days
        s["d_prev"] = round(L["weight_pooq"] - P["weight_pooq"], 2)
        rb = min(measured[:-1], key=lambda r: r["weight_pooq"])
        s["record_before"] = rb
        s["d_record"] = round(L["weight_pooq"] - rb["weight_pooq"], 2)
        s["is_record"] = L["weight_pooq"] < rb["weight_pooq"]

    s["d_start"] = round(L["weight_pooq"] - cfg["start_weight"], 2)
    s["ma7"], s["ma7_n"] = window_mean(measured, L["date"], 7)
    s["ma14"], s["ma14_n"] = window_mean(measured, L["date"], 14)
    ma7_prev, _ = window_mean(measured, L["date"] - timedelta(days=7), 7)
    s["pace"] = round((s["ma7"] - ma7_prev) / 7, 3) if s["ma7"] and ma7_prev else None

    # 체성분
    if L["fat_pct"] is not None:
        s["fat_kg"] = round(L["weight_pooq"] * L["fat_pct"] / 100, 1)
        s["lean_kg"] = round(L["weight_pooq"] - s["fat_kg"], 1)
    s["comp14"], s["comp14_n"] = comp_window(measured, L["date"], 14)
    comp_prev, _ = comp_window(measured, L["date"] - timedelta(days=14), 14)
    if s["comp14"] and comp_prev:
        s["d_fat14"] = round(s["comp14"][0] - comp_prev[0], 1)
        s["d_lean14"] = round(s["comp14"][1] - comp_prev[1], 1)

    # 이벤트
    s["recent_events"] = [e for e in events if now - timedelta(days=7) <= e["date"] <= now]
    injects = [e for e in events if e["kind"] == "inject"]
    if injects:
        li = injects[-1]
        days = (now - li["date"]).days
        s["inject"] = {"date": li["date"], "dose": li["value"], "days": days,
                       "residual": residual_pct(days, cfg["half_life_days"])}

    # 단백질
    s["protein"] = protein_today(food_log, now)

    # 재개 기준
    rs = cfg["restart"]
    if rs.get("active"):
        streak = 0
        for r in reversed(measured):
            if r["weight_pooq"] >= rs["ceiling"]:
                streak += 1
            else:
                break
        pause = date.fromisoformat(rs["pause_start"])
        review = date.fromisoformat(rs["review_date"])
        s["restart"] = {
            "streak": streak,
            "days_since_pause": (now - pause).days,
            "days_to_review": (review - now).days,
            "binges": [e for e in events if e["kind"] == "binge" and e["date"] >= pause],
        }
    return s


def judge(s, cfg):
    if s["prev"] is None:
        return "첫 기록"
    t = cfg["thresholds"]
    d, gap = s["d_prev"], s["gap"]
    pre = f"간격 {gap}일 — 일별 판정 신뢰도 낮음. " if gap > 3 else ""
    if abs(d) <= t["noise"]:
        core = "수분 노이즈 범위"
    elif d >= t["rebound"]:
        core = f"반등 서명(+{t['rebound']} 이상) — 원인 확인, 2~3일 추적"
    elif d <= t["waterdrop"] and gap <= 2:
        core = "급락 — 수분 저점 가능, 하루 이틀 되돌림 예상"
    elif d > 0:
        core = "노이즈 상단 — 하루 더 보고 판단"
    else:
        core = "하락 — 방향 유지"
    if s["is_record"]:
        core = "★ 역대 최저 갱신. " + core
    return pre + core


def flags(s, cfg):
    """경고 목록 — 재개 기준 + 근손실 의심."""
    out = []
    if s.get("d_lean14") is not None and s["d_lean14"] <= -0.5 and s["d_fat14"] > -0.3:
        out.append(f"⚠ 2주 제지방 {s['d_lean14']:+.1f}kg vs 지방 {s['d_fat14']:+.1f}kg — 근손실 의심, 단백질·저항운동 점검")
    r = s.get("restart")
    if not r:
        return out
    rs = cfg["restart"]
    if r["streak"] >= rs["consecutive"]:
        out.append(f"⚠ 체중 {rs['ceiling']}kg 이상 {r['streak']}회 연속 — 재개 기준 발동")
    if len(r["binges"]) >= rs["binge_limit"]:
        out.append(f"⚠ 폭식 {len(r['binges'])}회 — 재개 기준 발동")
    if r["days_to_review"] <= 0:
        out.append(f"⚠ 재평가일({rs['review_date']}) 도래 — 중단 유지/재개 결정 필요")
    return out


# ---------- 출력 ----------

def event_str(e):
    v = f" {e['value']}" if e["value"] else ""
    n = f" ({e['note']})" if e["note"] else ""
    return f"{md(e['date'])} {EVENT_KR.get(e['kind'], e['kind'])}{v}{n}"


def report(s, cfg):
    L = s["last"]
    lines = []
    head = f"[{L['date']}] {num(L['weight_raw'])}kg {SCALE_KR.get(L['scale'], L['scale'])}"
    if L["scale"] == "atply":
        head += f"  → 피쿡 환산 {num(L['weight_pooq'])}kg"
    if L["fat_pct"] is not None:
        head += f"   체지방 {num(L['fat_pct'])}%"
    lines.append(head)
    if L["note"]:
        lines.append(f"  메모        {L['note']}")
    if s["prev"]:
        P, R = s["prev"], s["record_before"]
        lines.append(f"  전회 대비   {fmt_d(s['d_prev'])}  ({md(P['date'])} {num(P['weight_pooq'])}, {s['gap']}일 전)")
        rec = "★ 신기록" if s["is_record"] else fmt_d(s["d_record"])
        lines.append(f"  역대 최저   {num(R['weight_pooq'])} ({md(R['date'])})  {rec}")
    lines.append(f"  3/21 대비   {fmt_d(s['d_start'])}  ({cfg['start_weight']} →)")
    ma7 = f"{num(s['ma7'])} (n={s['ma7_n']})" if s["ma7"] else f"표본 부족 (n={s['ma7_n']})"
    ma14 = f"{num(s['ma14'])} (n={s['ma14_n']})" if s["ma14"] else "-"
    lines.append(f"  진짜 라인   MA7 {ma7}   MA14 {ma14}")
    if s["pace"] is not None:
        lines.append(f"  페이스      {s['pace']:+.2f}kg/일 (MA7 주간 변화)")

    if s.get("fat_kg") is not None:
        c = f"  체성분      지방 {s['fat_kg']}kg / 제지방 {s['lean_kg']}kg"
        if s["comp14"]:
            c += f"   MA14 지방 {s['comp14'][0]} / 제지방 {s['comp14'][1]} (n={s['comp14_n']})"
            if s.get("d_lean14") is not None:
                c += f"   2주 전 대비 지방 {s['d_fat14']:+.1f} / 제지방 {s['d_lean14']:+.1f}"
        else:
            c += f"   (MA14 표본 부족 n={s['comp14_n']})"
        lines.append(c)

    lines.append(f"  판정        {judge(s, cfg)}")

    p, k, items = s["protein"]
    if items:
        lines.append(f"  단백질 오늘 {p}g / {cfg['protein_target']}g   ({k}kcal, {len(items)}항목)")
    if s["recent_events"]:
        lines.append("  최근 7일    " + " · ".join(event_str(e) for e in s["recent_events"]))

    r = s.get("restart")
    inj = s.get("inject")
    if r or inj:
        lines.append("── 마운자로")
        if inj:
            lines.append(f"  마지막 투여  {md(inj['date'])} {inj['dose']}  ({inj['days']}일 경과, 잔여 약효 ~{inj['residual']}%)")
        if r:
            rs = cfg["restart"]
            lines.append(f"  재개 기준    체중 ≥{rs['ceiling']} 연속 {r['streak']}/{rs['consecutive']}"
                         f" · 폭식 {len(r['binges'])}/{rs['binge_limit']}"
                         f" · 재평가 {rs['review_date']} " + (f"D-{r['days_to_review']}" if r["days_to_review"] > 0 else "도래"))
    lines.extend("  " + f for f in flags(s, cfg))
    return "\n".join(lines)


def md_row(s, cfg, user_note):
    """메모리 weight_log.md용 한 줄."""
    L = s["last"]
    parts = [SCALE_KR.get(L["scale"], L["scale"])]
    if L["scale"] == "atply":
        parts[0] += f"(피쿡 환산 {num(L['weight_pooq'])})"
    if L["fat_pct"] is not None:
        parts[0] += f", 체지방 {num(L['fat_pct'])}%"
    if s["prev"]:
        P = s["prev"]
        parts.append(f"{md(P['date'])}({num(P['weight_pooq'])}) 대비 {fmt_d(s['d_prev'])}")
        parts.append("**역대 최저 갱신**" if s["is_record"] else f"최저 대비 {fmt_d(s['d_record'])}")
    parts.append(f"3/21 대비 {fmt_d(s['d_start'])}")
    if s["ma7"]:
        parts.append(f"MA7 {num(s['ma7'])}")
    if s.get("d_lean14") is not None:
        parts.append(f"2주 체성분 지방 {s['d_fat14']:+.1f}/제지방 {s['d_lean14']:+.1f}")
    parts.append(judge(s, cfg))
    if s.get("inject"):
        parts.append(f"잔여 약효 ~{s['inject']['residual']}%")
    parts.extend(flags(s, cfg))
    if user_note:
        parts.append(user_note)
    return f"| {L['date']} | {num(L['weight_raw'])}kg | " + ". ".join(parts) + " |"


def append_md(row, cfg):
    p = Path(cfg["md_path"])
    if not p.exists():
        return False
    lines = p.read_text(encoding="utf-8").split("\n")
    idx = next((i for i, l in enumerate(lines) if l.startswith("**Why:**")), None)
    if idx is None:
        return False
    at = idx - 1 if idx > 0 and lines[idx - 1].strip() == "" else idx
    lines.insert(at, row)
    p.write_text("\n".join(lines), encoding="utf-8")
    return True


# ---------- 식품 파싱 ----------

KO_NUM = {"반": 0.5, "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5}
UNITS = "g|그램|ml|mL|팩|개|스쿱|조각|그릇|캔|모|장|잔|컵|병|포|인분|공기|줄|알"
QTY_RE = re.compile(rf"(\d+(?:\.\d+)?|반|한|두|세|네|다섯)\s*({UNITS})?")


def build_alias_index(table):
    idx = []
    for name, spec in table.items():
        for a in [name] + spec.get("aliases", []):
            idx.append((a, name))
    idx.sort(key=lambda x: -len(x[0]))
    return idx


def parse_food(text, table):
    """텍스트에서 식품명 위치를 찍고, 각 구간의 수량을 읽어 [(name, qty, seg)] 반환."""
    idx = build_alias_index(table)
    pat = re.compile("|".join(re.escape(a) for a, _ in idx))
    alias_to_name = dict(idx)
    hits, prev = [], None
    for h in pat.finditer(text):
        name = alias_to_name[h.group(0)]
        # "그릴리 직화 닭가슴살"처럼 같은 식품의 별칭이 연달아 나오면 하나로 취급
        if prev and alias_to_name[prev.group(0)] == name and not QTY_RE.search(text[prev.end():h.start()]):
            continue
        hits.append(h)
        prev = h
    unknown = [c.strip() for c in re.split(r"[,+\n/]", text) if c.strip() and not pat.search(c)]
    items = []
    for i, h in enumerate(hits):
        name = alias_to_name[h.group(0)]
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        seg = text[h.end():end]
        spec = table[name]
        qty = 1.0
        m = QTY_RE.search(seg)
        if m:
            n = KO_NUM.get(m[1]) if m[1] in KO_NUM else float(m[1])
            unit = m[2] or ""
            if unit in ("g", "그램", "ml", "mL") and spec.get("g_per_unit"):
                qty = n / spec["g_per_unit"]
            else:
                qty = n
        items.append((name, round(qty, 2), text[h.start():end].strip()))
    return items, unknown


# ---------- 명령 ----------

def stats_now(cfg, rows=None):
    return compute_stats(rows if rows is not None else load_rows(), cfg, load_events(), load_food_log(), today())


def cmd_status(args, cfg):
    s = stats_now(cfg)
    print(report(s, cfg) if s else "기록 없음. `add` 로 시작하거나 `migrate` 로 이관하세요.")


def cmd_add(args, cfg):
    rows = load_rows()
    d = parse_date(args.date)
    if any(r["date"] == d for r in rows) and not args.force:
        sys.exit(f"{d} 기록이 이미 있음. 덮어쓰려면 --force")
    rows = [r for r in rows if r["date"] != d]
    rows.append({
        "date": d, "weight_raw": args.weight, "scale": args.scale,
        "weight_pooq": to_pooq(args.weight, args.scale, cfg),
        "fat_pct": args.fat, "excluded": False, "note": args.note or "",
    })
    s = stats_now(cfg, rows)
    print(report(s, cfg))
    if args.dry_run:
        print("\n(dry-run: 저장 안 함)")
        return
    save_rows(rows)
    if args.no_md:
        print("\n✓ CSV 저장")
        return
    ok = append_md(md_row(s, cfg, args.note), cfg)
    print("\n✓ CSV 저장" + (" + 메모리 weight_log.md 반영" if ok else " (md 반영 실패: 경로 확인)"))


def cmd_event(args, cfg):
    d = parse_date(args.date)
    append_csv(EVENTS_PATH, EVENT_FIELDS,
               {"date": d.isoformat(), "kind": args.kind, "value": args.value or "", "note": args.note or ""})
    print(f"✓ 이벤트 기록: {event_str({'date': d, 'kind': args.kind, 'value': args.value or '', 'note': args.note or ''})}")
    s = stats_now(cfg)
    if s:
        if args.kind == "inject":
            i = s["inject"]
            print(f"  마지막 투여 {md(i['date'])} {i['dose']} — 잔여 약효 ~{i['residual']}%")
        if args.kind == "binge" and s.get("restart"):
            print(f"  중단 이후 폭식 누적 {len(s['restart']['binges'])}회")
        for f in flags(s, cfg):
            print("  " + f)


def cmd_binge(args, cfg):
    args.kind, args.value = "binge", ""
    cmd_event(args, cfg)


def cmd_food(args, cfg):
    d = parse_date(args.date)
    table = load_food_table()
    if args.show or not args.text:
        p, k, items = protein_today(load_food_log(), d)
        if not items:
            print(f"{d} 식사 기록 없음")
            return
        for f in items:
            pr = f"{f['protein']:g}g" if f["protein"] is not None else "?"
            print(f"  {f['item']} ×{float(f['qty']):g}  단백질 {pr}  {f['kcal'] or 0:g}kcal")
        print(f"합계  단백질 {p}g / {cfg['protein_target']}g   {k}kcal")
        return
    items, unknown = parse_food(args.text, table)
    if not items:
        sys.exit(f"인식된 식품 없음: {args.text}\n  → {FOOD_TABLE.name}에 추가하세요")
    for name, qty, raw in items:
        spec = table[name]
        pr, kc = round(spec["protein"] * qty, 1), round(spec["kcal"] * qty)
        append_csv(FOOD_LOG, FOOD_FIELDS, {"date": d.isoformat(), "item": name, "qty": qty,
                                            "protein": pr, "kcal": kc, "raw": raw})
        print(f"  + {name} ×{qty:g} {spec['unit']}  단백질 {pr:g}g  {kc}kcal")
    for u in unknown:
        print(f"  ? 모름: '{u}'  → {FOOD_TABLE.name}에 추가")
    p, k, all_items = protein_today(load_food_log(), d)
    gap = cfg["protein_target"] - p
    tail = f"  (남은 {gap}g ≈ 닭가슴살 {gap / table['닭가슴살']['protein']:.1f}팩)" if gap > 0 and "닭가슴살" in table else "  ✓ 목표 달성"
    print(f"오늘 합계  단백질 {p}g / {cfg['protein_target']}g   {k}kcal{tail}")


def cmd_migrate(args, cfg):
    """기존 weight_log.md 표에서 CSV 생성. 기존 CSV는 덮어씀."""
    src = Path(args.path)
    pat = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \| (.*?) \| (.*) \|$")
    rows = []
    for line in src.read_text(encoding="utf-8").split("\n"):
        m = pat.match(line)
        if not m:
            continue
        d, wcell, note = date.fromisoformat(m[1]), m[2], m[3]
        wm = re.search(r"([\d.]+)\s*kg", wcell)
        if not wm:  # 미측정
            continue
        raw = float(wm[1])
        excluded = "미채택" in wcell or "검증 필요" in wcell
        clean = note.replace("**", "").strip()
        if clean.startswith("앳플리"):
            scale = "atply"
        elif clean.startswith("측정기 미명시"):
            scale = "unknown"
        else:
            scale = "pooq"
        short = re.split(r"(?<=\.)\s", clean, maxsplit=1)[0][:100]
        rows.append({
            "date": d, "weight_raw": raw, "scale": scale,
            "weight_pooq": to_pooq(raw, scale, cfg), "fat_pct": None,
            "excluded": excluded, "note": ("[제외] " if excluded else "") + short,
        })
    save_rows(rows)
    n_ex = sum(r["excluded"] for r in rows)
    n_at = sum(r["scale"] == "atply" for r in rows)
    print(f"✓ {len(rows)}건 이관 (제외 {n_ex}건, 앳플리 {n_at}건) → {CSV_PATH.name}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="체중 기록 추가")
    a.add_argument("weight", type=float)
    a.add_argument("--fat", type=float, help="체지방률 %% (피쿡 앱 표시값)")
    a.add_argument("--scale", choices=["pooq", "atply", "unknown"], default="pooq")
    a.add_argument("--date", help="YYYY-MM-DD (기본 오늘)")
    a.add_argument("--note", default="")
    a.add_argument("--force", action="store_true", help="같은 날짜 덮어쓰기")
    a.add_argument("--no-md", action="store_true", help="메모리 md 반영 생략")
    a.add_argument("--dry-run", action="store_true")

    e = sub.add_parser("event", help="이벤트 기록")
    e.add_argument("kind", choices=EVENT_KINDS)
    e.add_argument("value", nargs="?", default="", help="예: 5km, 5mg, 24h")
    e.add_argument("--date")
    e.add_argument("--note", default="")

    b = sub.add_parser("binge", help="폭식 이벤트 (= event binge)")
    b.add_argument("--date")
    b.add_argument("--note", default="")

    fd = sub.add_parser("food", help="식사 기록 → 단백질 집계")
    fd.add_argument("text", nargs="?", help='예: "닭가슴살 2팩, 쉐이크 2스쿱"')
    fd.add_argument("--date")
    fd.add_argument("--show", action="store_true", help="오늘 먹은 것 보기")

    m = sub.add_parser("migrate", help="기존 md 표 → CSV")
    m.add_argument("path")

    sub.add_parser("status", help="현황 요약")

    args = ap.parse_args()
    {"add": cmd_add, "event": cmd_event, "binge": cmd_binge, "food": cmd_food,
     "migrate": cmd_migrate}.get(args.cmd, cmd_status)(args, cfg)


if __name__ == "__main__":
    main()
