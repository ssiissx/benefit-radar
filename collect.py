#!/usr/bin/env python3
"""
혜택 레이더 수집기
- 보조금24 (행정안전부 공공서비스(혜택) 정보, data.go.kr 무료 API)
- 온통청년 청년정책 API (무료)
를 모아서 config/profile.json 조건으로 점수를 매기고 docs/data/benefits.json 으로 저장한다.

사용법
  python scripts/collect.py            # 실제 수집 (환경변수 GOV24_API_KEY, YOUTH_API_KEY 필요)
  python scripts/collect.py --sample   # samples/ 폴더의 예시 응답으로 동작 확인
  python scripts/collect.py --probe    # 키가 잘 되는지, 응답 필드가 뭔지 확인만
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# 두 가지 폴더 구조 지원
#  - 기본: scripts/collect.py, config/profile.json, docs/index.html, docs/data/
#  - 휴대폰 업로드용(평평한 구조): collect.py, profile.json, index.html, data/  (모두 저장소 맨 위)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "scripts" else HERE
PROFILE_PATH = ROOT / "config" / "profile.json" if (ROOT / "config" / "profile.json").exists() else ROOT / "profile.json"
DATA_DIR = ROOT / "docs" / "data" if (ROOT / "docs").is_dir() else ROOT / "data"
OUT_PATH = DATA_DIR / "benefits.json"
STATE_PATH = DATA_DIR / "state.json"
SAMPLE_DIR = ROOT / "samples"

KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).date()

GOV24_BASE = "https://api.odcloud.kr/api/gov24/v3"
YOUTH_URL = "https://www.youthcenter.go.kr/go/ythip/getPlcy"
YOUTH_DETAIL = "https://www.youthcenter.go.kr/youthPolicy/ythPlcyTotalSearch/ythPlcyDetail/{}"

SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
        "충청북", "충북", "충청남", "충남", "전북", "전라북", "전라남", "전남",
        "경상북", "경북", "경상남", "경남", "제주"]

# 보조금24 supportConditions 의 대상 코드(값 'Y' = 해당 대상 포함).
# 명세서 기준 대표 코드만. 코드가 바뀌면 여기만 고치면 됨.
JA_LABELS = {
    "JA0301": "예비부부/난임", "JA0302": "임산부", "JA0303": "출산/입양",
    "JA0313": "농업인", "JA0314": "어업인", "JA0315": "축산업인", "JA0316": "임업인",
    "JA0317": "초등학생", "JA0318": "중학생", "JA0319": "고등학생",
    "JA0320": "대학생/대학원생", "JA0326": "근로자/직장인", "JA0327": "구직자/실업자",
    "JA0328": "장애인", "JA0329": "국가보훈대상자", "JA0330": "질병/질환자",
    "JA0401": "다문화가족", "JA0402": "북한이탈주민", "JA0403": "한부모/조손가정",
    "JA0404": "1인가구", "JA0411": "다자녀가구", "JA0412": "무주택세대", "JA0413": "신규전입",
}
# 내 상태 → 코드
STATUS_TO_JA = {
    "대학생": "JA0320", "휴학생": "JA0320", "대학원생": "JA0320",
    "구직자": "JA0327", "미취업": "JA0327", "실업자": "JA0327", "취업준비": "JA0327",
    "1인가구": "JA0404", "무주택": "JA0412", "직장인": "JA0326", "장애인": "JA0328",
}
PERSONAL_JA = [c for c in JA_LABELS if c.startswith("JA03")]


# ───────────────────────── 공통 유틸 ─────────────────────────
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def clean(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\r", "").strip()
    return re.sub(r"\n{3,}", "\n\n", s)


def http_get_json(url: str, params: dict, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=40,
                             headers={"User-Agent": "benefit-radar/1.0"})
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (401, 403):
                break  # 키 문제는 재시도해도 소용없음
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)


DATE_RE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DATE8_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")


def parse_deadline(text: str):
    """신청기한 문자열에서 마감일 추출. (마감일 or None, 상시여부)"""
    t = clean(text)
    if not t:
        return None, False
    dates = []
    for y, m, d in DATE_RE.findall(t) + DATE8_RE.findall(t):
        try:
            dates.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    always = bool(re.search(r"상시|연중|수시", t))
    if dates:
        return max(dates), False
    return None, always


MONTH_RANGE_RE = re.compile(r"(\d{1,2})\s*월?\s*[~\-–]\s*(\d{1,2})\s*월")
MONTH_ONE_RE = re.compile(r"(?<![\d~\-–.])(\d{1,2})\s*월(?!\s*[~\-–])")


def month_windows(text: str):
    """'6~7월, 10~12월' / '1학기:4~5월' 같은 매년 반복 접수 기간 → [(시작월, 끝월), …]"""
    t = clean(text)
    wins = [(int(a), int(b)) for a, b in MONTH_RANGE_RE.findall(t)]
    rest = MONTH_RANGE_RE.sub(" ", t)
    wins += [(int(a), int(a)) for a in MONTH_ONE_RE.findall(rest)]
    return [(a, b) for a, b in wins if 1 <= a <= 12 and 1 <= b <= 12]


def schedule_fields(text: str):
    """마감일/상시/매년 접수월을 한꺼번에 계산"""
    deadline, always = parse_deadline(text)
    out = {"deadline": deadline.isoformat() if deadline else None, "always": always,
           "open_now": None, "opens_month": None}
    if deadline or always:
        return out
    wins = month_windows(text)
    if not wins:
        return out
    m = TODAY.month
    for a, b in wins:
        inside = a <= m <= b if a <= b else (m >= a or m <= b)
        if inside:
            end_year = TODAY.year if (a <= b or m >= a) and b >= m else TODAY.year + 1
            last = (dt.date(end_year + (b == 12), (b % 12) + 1, 1) - dt.timedelta(days=1))
            out.update(open_now=True, deadline=last.isoformat())
            return out
    starts = sorted(a for a, _ in wins)
    nxt = next((a for a in starts if a > m), starts[0])
    out.update(open_now=False, opens_month=nxt)
    return out


def age_of(profile) -> int | None:
    by = profile.get("birth_year")
    if not by:
        return None
    return TODAY.year - int(by)  # 대략 만 나이(생일 전이면 -1)


def to_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


# ───────────────────────── 지역 (여러 곳 + 기간) ─────────────────────────
def load_regions(profile):
    """profile['regions'] (목록) 또는 예전 형식 profile['region'] 을 읽는다. 기간이 끝난 곳은 뺀다."""
    regs = profile.get("regions") or ([profile["region"]] if profile.get("region") else [])
    out = []
    for r in regs:
        r = dict(r)
        r.setdefault("sigungu", "")
        r["short"] = re.sub(r"(시|군|구)$", "", r["sigungu"]) if r["sigungu"] else ""
        r["name"] = r.get("name") or r["short"] or r["sido"]
        r["sido_short"] = r["sido"][:2]  # 경기, 강원 …
        zc = r.get("zip_codes") or ([r["zip_code"]] if r.get("zip_code") else [])
        r["zip_codes"] = [str(z) for z in zc]
        until = r.get("until")
        if until and dt.date.fromisoformat(until) < TODAY:
            continue  # 이미 떠난 곳
        frm = r.get("from")
        r["future"] = bool(frm and dt.date.fromisoformat(frm) > TODAY)
        out.append(r)
    return out


def region_note(label, regions):
    for r in regions:
        if label in (r["name"], r["sido"]):
            if r["future"]:
                y, m = r["from"][:7].split("-")
                return f"{y}년 {int(m)}월 이사 후 대상"
            if r.get("until"):
                y, m = r["until"][:7].split("-")
                return f"이사 전({y}년 {int(m)}월까지) 신청"
    return ""


# ───────────────────────── 점수 매기기 ─────────────────────────
def score_item(item: dict, profile: dict, ja_row: dict | None = None):
    title = item["title"]
    body = " ".join([item.get("summary", ""), item.get("target", ""), item.get("criteria", "")])
    reasons, score = [], 0

    kws = list(dict.fromkeys(profile.get("boost_keywords", []) + profile.get("status", [])))
    hit = [k for k in kws if k and (k in title or k in body)]
    if hit:
        score += min(len(hit), 4)
        reasons.append("키워드: " + ", ".join(hit[:5]))
    if "청년" in title:
        score += 2

    pen_t = [k for k in profile.get("penalty_keywords", []) if k in title]
    pen_b = [k for k in profile.get("penalty_keywords", []) if k in item.get("target", "") and k not in pen_t]
    if pen_t or pen_b:
        score -= min(3 * len(pen_t) + 2 * len(pen_b), 6)
        reasons.append("다른 대상 위주: " + ", ".join((pen_t + pen_b)[:4]))

    regions = profile["_regions"]
    if item["region"] in {r["name"] for r in regions}:  # 시·군 단위 = 우리 동네
        score += 2
        reasons.append(f"{item['region']} 사업")
    elif item["region"] in {r["sido"] for r in regions}:
        score += 1
    note = region_note(item["region"], regions)
    if note:
        item["region_note"] = note

    if ja_row:
        ys = [c for c in PERSONAL_JA if str(ja_row.get(c, "")).upper() == "Y"]
        mine = {STATUS_TO_JA[s] for s in profile.get("status", []) if s in STATUS_TO_JA}
        if profile.get("household") == "1인가구":
            mine.add("JA0404")
        if profile.get("homeless"):
            mine.add("JA0412")
        if 0 < len(ys) <= 5:  # 대상을 좁게 정해둔 서비스일 때만 의미 있음
            match = [JA_LABELS[c] for c in ys if c in mine]
            if match:
                score += 3
                reasons.append("대상 일치: " + ", ".join(match))
            else:
                score -= 2
                reasons.append("대상: " + ", ".join(JA_LABELS[c] for c in ys))

    return score, reasons


# ───────────────────────── 보조금24 ─────────────────────────
def gov24_fetch_all(endpoint: str, key: str, per_page=1000, max_pages=60):
    rows, page = [], 1
    while page <= max_pages:
        data = http_get_json(f"{GOV24_BASE}/{endpoint}",
                             {"serviceKey": key, "page": page, "perPage": per_page, "returnType": "JSON"})
        chunk = data.get("data") or []
        rows.extend(chunk)
        total = data.get("totalCount") or data.get("matchCount") or 0
        log(f"  보조금24 {endpoint} p{page}: {len(chunk)}건 (누적 {len(rows)}/{total})")
        if not chunk or len(rows) >= total:
            break
        page += 1
    return rows


OTHER_REGION_TOKENS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "충북", "충남", "충청",
                       "전북", "전남", "전라", "경북", "경남", "경상", "제주", "강원", "경기"]


def is_other_region(text, regions):
    mine = {r["sido_short"] for r in regions} | {r["short"] for r in regions if r["short"]}
    if any(m in text for m in mine):
        return False
    return any(tok in text for tok in OTHER_REGION_TOKENS if tok not in mine)


def gov24_region(row, regions):
    """전국 / 시·도 이름 / 우리 시·군 이름 / None(관계없는 지역)"""
    typ = clean(row.get("소관기관유형"))
    name = clean(row.get("소관기관명"))
    if typ == "중앙행정기관":
        return "전국"
    if typ == "공공기관":
        # 지역 재단·장학회(예: (재)인천인재평생교육진흥원)는 이름으로 지역을 판단
        text = re.sub(r"재단법인|\(재\)|사단법인", "", name) + " " + clean(row.get("서비스명"))
        if is_other_region(text, regions):
            return None
        for r in regions:
            if r["short"] and r["short"] in text:
                return r["name"]
        for r in regions:
            if r["sido_short"] in text:
                return r["sido"]
        return "전국"
    for r in regions:
        if r["short"] and r["short"] in name and name.startswith(r["sido_short"]):
            return r["name"]
    for r in regions:
        if name.startswith(r["sido_short"]):
            rest = re.sub(r"^" + r["sido_short"] + r"(특별자치도|도)?", "", name).strip()
            first = rest.split(" ")[0] if rest else ""
            if first and re.search(r"(시|군|구)$", first):
                return None  # 같은 도의 다른 시·군
            return r["sido"]
    if any(name.startswith(x) for x in SIDO):
        return None  # 다른 시·도
    return "전국"


def gov24_items(profile, services, conditions):
    cond_map = {c.get("서비스ID"): c for c in conditions}
    age = age_of(profile)
    items, dropped = [], {"region": 0, "age": 0}
    for s in services:
        sid = clean(s.get("서비스ID"))
        if not sid:
            continue
        region = gov24_region(s, profile["_regions"])
        if region is None:
            dropped["region"] += 1
            continue
        ja = cond_map.get(sid)
        a_from = to_int(ja.get("JA0110")) if ja else None
        a_to = to_int(ja.get("JA0111")) if ja else None
        age_range = None
        if a_from is not None and a_to is not None and not (a_from <= 0 and a_to >= 100):
            age_range = [a_from, a_to]
            if age is not None and not (a_from <= age <= a_to):
                dropped["age"] += 1
                continue
        deadline_text = clean(s.get("신청기한"))
        item = {
            "id": "g24-" + sid,
            "source": "보조금24",
            "title": clean(s.get("서비스명")),
            "summary": clean(s.get("서비스목적요약")),
            "target": clean(s.get("지원대상")),
            "criteria": clean(s.get("선정기준")),
            "support": clean(s.get("지원내용")),
            "support_type": clean(s.get("지원유형")),
            "how": clean(s.get("신청방법")),
            "deadline_text": deadline_text,
            **schedule_fields(deadline_text),
            "agency": " ".join(x for x in [clean(s.get("소관기관명")), clean(s.get("부서명"))] if x),
            "phone": clean(s.get("전화문의")),
            "region": region,
            "category": clean(s.get("서비스분야")) or "기타",
            "url": clean(s.get("상세조회URL")) or f"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/{sid}",
            "age_range": age_range,
            "src_updated": clean(s.get("수정일시")),
        }
        item["score"], item["reasons"] = score_item(item, profile, ja)
        items.append(item)
    log(f"  보조금24: {len(items)}건 사용 (다른 지역 {dropped['region']}, 나이 불일치 {dropped['age']} 제외)")
    return items


# ───────────────────────── 온통청년 ─────────────────────────
def pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return clean(v)
    return ""


def find_list(obj):
    """응답 JSON 안에서 정책 목록(list[dict])을 찾아낸다 (구조 변경 대비)."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj
    if isinstance(obj, dict):
        for k in ("youthPolicyList", "policyList", "list", "data", "items", "result"):
            if k in obj:
                r = find_list(obj[k])
                if r is not None:
                    return r
        for v in obj.values():
            r = find_list(v)
            if r is not None:
                return r
    return None


def youth_fetch_all(key: str, page_size=100, max_pages=80):
    rows, page = [], 1
    while page <= max_pages:
        data = http_get_json(YOUTH_URL, {"apiKeyNm": key, "pageNum": page,
                                         "pageSize": page_size, "rtnType": "json"})
        chunk = find_list(data) or []
        rows.extend(chunk)
        total = 0
        res = data.get("result") if isinstance(data, dict) else None
        if isinstance(res, dict):
            pg = res.get("pagging") or res.get("paging") or {}
            total = to_int(pg.get("totCount")) or 0
        log(f"  온통청년 p{page}: {len(chunk)}건 (누적 {len(rows)}/{total or '?'})")
        if len(chunk) < page_size or (total and len(rows) >= total):
            break
        page += 1
    return rows


def youth_items(profile, rows):
    age = age_of(profile)
    regions = profile["_regions"]
    my_zips = {z for r in regions for z in r["zip_codes"]}
    items, dropped = [], {"region": 0, "age": 0}
    for p in rows:
        pid = pick(p, "plcyNo", "bizId")
        if not pid:
            continue
        zips = [z.strip() for z in pick(p, "zipCd").split(",") if z.strip()]
        if zips and my_zips and not (my_zips & set(zips)):
            dropped["region"] += 1
            continue
        region = "전국"
        if zips and len(zips) <= 150:
            for r in regions:
                if not set(r["zip_codes"]) & set(zips):
                    continue
                if len(zips) <= 3:
                    region = r["name"]
                elif all(z[:2] in {c[:2] for c in r["zip_codes"]} for z in zips):
                    region = r["sido"]
                break
        lo, hi = to_int(p.get("sprtTrgtMinAge")), to_int(p.get("sprtTrgtMaxAge"))
        age_range = None
        if pick(p, "sprtTrgtAgeLmtYn") != "Y" and lo and hi and not (lo <= 0 and hi >= 100):
            age_range = [lo, hi]
            if age is not None and not (lo <= age <= hi):
                dropped["age"] += 1
                continue
        deadline_text = pick(p, "aplyYmd") or pick(p, "bizPrdEndYmd")
        item = {
            "id": "yc-" + pid,
            "source": "온통청년",
            "title": pick(p, "plcyNm", "polyBizSjnm"),
            "summary": pick(p, "plcyExplnCn", "polyItcnCn"),
            "target": " / ".join(x for x in [
                f"만 {lo}~{hi}세" if age_range else "",
                pick(p, "addAplyQlfcCndCn"), pick(p, "earnEtcCn")] if x),
            "criteria": pick(p, "ptcpPrpTrgtCn"),
            "support": pick(p, "plcySprtCn", "sporCn"),
            "support_type": pick(p, "mclsfNm"),
            "how": pick(p, "plcyAplyMthdCn"),
            "deadline_text": deadline_text or "상시",
            **schedule_fields(deadline_text or "상시"),
            "agency": pick(p, "sprvsnInstCdNm", "operInstCdNm", "rgtrInstCdNm"),
            "phone": "",
            "region": region,
            "category": pick(p, "lclsfNm") or "청년정책",
            "url": pick(p, "aplyUrlAddr") or YOUTH_DETAIL.format(pid),
            "detail_url": YOUTH_DETAIL.format(pid),
            "age_range": age_range,
            "src_updated": pick(p, "lastMdfcnDt", "frstRegDt"),
        }
        item["score"], item["reasons"] = score_item(item, profile)
        item["score"] += 1  # 청년 전용 소스 가산
        items.append(item)
    log(f"  온통청년: {len(items)}건 사용 (다른 지역 {dropped['region']}, 나이 불일치 {dropped['age']} 제외)")
    return items


# ───────────────────────── 상태(신규/변경) & 저장 ─────────────────────────
def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t)


def fingerprint(it):
    s = "|".join([it["title"], it["support"], it["target"], it["deadline_text"]])
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def apply_state(items, profile):
    state = load_json(STATE_PATH, {})
    meta = state.setdefault("_meta", {})
    first_run = "initialized" not in meta
    if first_run:
        meta["initialized"] = TODAY.isoformat()
    seen = state.setdefault("items", {})
    soon = profile.get("deadline_soon_days", 14)
    for it in items:
        fp = fingerprint(it)
        rec = seen.get(it["id"])
        if rec is None:
            rec = seen[it["id"]] = {"first_seen": TODAY.isoformat(), "fp": fp, "changed": None}
        elif rec.get("fp") != fp:
            rec["fp"], rec["changed"] = fp, TODAY.isoformat()
        rec["last_seen"] = TODAY.isoformat()
        it["first_seen"] = rec["first_seen"]
        it["changed_on"] = rec.get("changed")
        fs = dt.date.fromisoformat(rec["first_seen"])
        it["is_new"] = (fs > dt.date.fromisoformat(meta["initialized"])) and (TODAY - fs).days <= 7
        it["is_updated"] = bool(rec.get("changed")) and (TODAY - dt.date.fromisoformat(rec["changed"])).days <= 7
        if it["deadline"]:
            dday = (dt.date.fromisoformat(it["deadline"]) - TODAY).days
            it["dday"] = dday
            it["expired"] = dday < 0 and not it["always"]
            it["closing_soon"] = 0 <= dday <= soon
        else:
            it["dday"], it["expired"], it["closing_soon"] = None, False, False
        it["recommended"] = it["score"] >= profile.get("recommend_threshold", 3) and not it["expired"]
        if it["is_new"]:
            it["score"] += 1
    # 90일 넘게 안 보인 항목은 상태에서 정리
    cutoff = (TODAY - dt.timedelta(days=90)).isoformat()
    for k in [k for k, v in seen.items() if v.get("last_seen", "9") < cutoff]:
        del seen[k]
    return state, first_run


def notify_telegram(items, first_run):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or first_run:
        return
    new = [i for i in items if i["recommended"] and i["is_new"] and i["first_seen"] == TODAY.isoformat()]
    soon = [i for i in items if i["recommended"] and i.get("dday") in (7, 3, 1)]
    if not new and not soon:
        log("  알림: 보낼 새 소식 없음")
        return
    lines = ["🔔 혜택 레이더"]
    if new:
        lines.append(f"\n🆕 새로 올라온 추천 혜택 {len(new)}건")
        lines += [f"• {i['title']} ({i['region']})\n  {i['url']}" for i in new[:15]]
    if soon:
        lines.append(f"\n⏰ 마감 임박")
        lines += [f"• D-{i['dday']} {i['title']}\n  {i['url']}" for i in soon[:15]]
    page = os.getenv("SITE_URL")
    if page:
        lines.append(f"\n전체 보기: {page}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[i:i + 3900], "disable_web_page_preview": "true"},
                      timeout=20)
    log(f"  알림: 텔레그램 전송 (신규 {len(new)}, 임박 {len(soon)})")


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="samples/ 예시 데이터로 실행")
    ap.add_argument("--probe", action="store_true", help="API 응답 필드만 확인")
    args = ap.parse_args()

    profile = load_json(PROFILE_PATH, None)
    if not profile:
        sys.exit("config/profile.json 을 읽을 수 없어요.")
    profile["_regions"] = load_regions(profile)
    log("지역:", ", ".join(r["name"] + (" (이사 예정)" if r["future"] else "") for r in profile["_regions"]))

    gkey, ykey = os.getenv("GOV24_API_KEY", "").strip(), os.getenv("YOUTH_API_KEY", "").strip()

    if args.probe:
        if gkey:
            for ep in ("serviceList", "supportConditions"):
                d = http_get_json(f"{GOV24_BASE}/{ep}", {"serviceKey": gkey, "page": 1, "perPage": 1})
                print(f"[보조금24 {ep}] total={d.get('totalCount')} 필드:", list((d.get('data') or [{}])[0].keys()))
        if ykey:
            d = http_get_json(YOUTH_URL, {"apiKeyNm": ykey, "pageNum": 1, "pageSize": 1, "rtnType": "json"})
            lst = find_list(d) or [{}]
            print("[온통청년] 필드:", list(lst[0].keys()))
        if not gkey and not ykey:
            print("GOV24_API_KEY / YOUTH_API_KEY 환경변수가 없어요.")
        return

    prev_doc = load_json(OUT_PATH, {})
    prev = [] if prev_doc.get("meta", {}).get("sample") else prev_doc.get("items", [])
    items, status = [], {}

    # 보조금24
    try:
        if args.sample:
            sv = load_json(SAMPLE_DIR / "gov24_serviceList.json", {})["data"]
            cd = load_json(SAMPLE_DIR / "gov24_supportConditions.json", {})["data"]
        elif gkey:
            log("보조금24 수집 중…")
            sv = gov24_fetch_all("serviceList", gkey)
            cd = gov24_fetch_all("supportConditions", gkey)
        else:
            sv = None
        if sv is None:
            log("  보조금24: 키 없음 → 건너뜀")
            status["보조금24"] = "off"
        else:
            items += gov24_items(profile, sv, cd)
            status["보조금24"] = "ok"
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠ 보조금24 실패: {e} → 이전 데이터 유지")
        items += [i for i in prev if i.get("source") == "보조금24"]
        status["보조금24"] = f"실패: {str(e)[:120]}"

    # 온통청년
    try:
        if args.sample:
            yr = find_list(load_json(SAMPLE_DIR / "youth.json", {})) or []
        elif ykey:
            log("온통청년 수집 중…")
            yr = youth_fetch_all(ykey)
        else:
            yr = None
        if yr is None:
            log("  온통청년: 키 없음 → 건너뜀")
            status["온통청년"] = "off"
            raise StopIteration
        yi = youth_items(profile, yr)
        g_titles = {norm_title(i["title"]) for i in items if i["source"] == "보조금24"}
        yi = [i for i in yi if norm_title(i["title"]) not in g_titles]  # 중복 제거
        items += yi
        status["온통청년"] = "ok"
    except StopIteration:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"  ⚠ 온통청년 실패: {e} → 이전 데이터 유지")
        items += [i for i in prev if i.get("source") == "온통청년"]
        status["온통청년"] = f"실패: {str(e)[:120]}"

    if not any(v == "ok" for v in status.values()):
        log("모든 소스가 실패했어요. 키 설정을 확인해 주세요. (기존 데이터는 그대로 둡니다)")
        sys.exit(1)

    state, first_run = apply_state(items, profile)
    items.sort(key=lambda i: (-i["recommended"], -i["score"], i["deadline"] or "9999"))

    out = {
        "meta": {
            "updated_at": dt.datetime.now(KST).isoformat(timespec="minutes"),
            "sample": bool(args.sample),
            "sources": status,
            "profile": {**{k: profile.get(k) for k in ("birth_year", "status", "household", "homeless")},
                        "regions": [{k: r.get(k) for k in ("name", "sido", "sigungu", "from", "until")} for r in profile["_regions"]]},
            "counts": {
                "total": len(items),
                "recommended": sum(i["recommended"] for i in items),
                "new": sum(i["is_new"] for i in items),
                "closing_soon": sum(i["recommended"] and i["closing_soon"] for i in items),
            },
        },
        "items": items,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if not args.sample:  # 예시 실행은 신규/변경 기록을 남기지 않음
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"완료: {out['meta']['counts']}")

    if not args.sample:
        notify_telegram(items, first_run)


if __name__ == "__main__":
    main()
