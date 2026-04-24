import re
import os
import json
import math
import logging
import asyncio
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
import telegram
from bs4 import BeautifulSoup


# ==========================
# 설정
# ==========================

TEST_DAY = None  # 테스트 날짜 예: 21

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

URL_38 = "http://www.38.co.kr/html/fund/index.htm?o=k"
HISTORY_FILE = "history.json"
WEIGHT_FILE = "weights.json"

SPIKE_ABS_THRESHOLD = 200.0
SPIKE_RATIO_THRESHOLD = 2.0

DEFAULT_38_INFO = {
    "competition": 0.0,
    "float": 50.0,
    "lockup": 0.0,
    "offer_price": 0,
    "band_low": 0,
    "band_high": 0,
    "price_position": "미확인",
    "brokers": []
}

DEFAULT_WEIGHTS = {
    "comp": 22.0,
    "lock": 0.4,
    "float": 0.15,
    "price": {
        "초과": 25.0,
        "상단": 10.0,
        "밴드내": 0.0,
        "하단": -25.0
    }
}

WEIGHTS = json.loads(json.dumps(DEFAULT_WEIGHTS, ensure_ascii=False))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ==========================
# 날짜
# ==========================

def get_today():
    kst = ZoneInfo("Asia/Seoul")
    now = datetime.now(kst)

    if TEST_DAY:
        return now.replace(day=TEST_DAY).date()

    return now.date()


def normalize_text(text):
    return re.sub(r"\s+", "", text or "")


def parse_subscription_period(text):
    text = normalize_text(text)

    match = re.search(
        r"(\d{4})\.(\d{1,2})\.(\d{1,2})~(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})",
        text
    )

    if not match:
        return None

    start_year = int(match.group(1))
    start_month = int(match.group(2))
    start_day = int(match.group(3))

    end_year = int(match.group(4)) if match.group(4) else start_year
    end_month = int(match.group(5))
    end_day = int(match.group(6))

    return (
        date(start_year, start_month, start_day),
        date(end_year, end_month, end_day)
    )


def is_today_in_subscription(text):
    period = parse_subscription_period(text)
    if not period:
        return False

    start, end = period
    today = get_today()

    return start <= today <= end


def is_last_day_subscription(text):
    period = parse_subscription_period(text)
    if not period:
        return False

    _, end = period
    return get_today() == end


# ==========================
# history / weights
# ==========================

def load_json_file(path, default_value):
    if not os.path.exists(path):
        return default_value

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"{path} load failed: {e}")
        return default_value


def save_json_file(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def load_history():
    return load_json_file(HISTORY_FILE, [])


def save_history(history):
    save_json_file(HISTORY_FILE, history)


def load_weights():
    global WEIGHTS

    saved = load_json_file(WEIGHT_FILE, None)
    if not saved:
        return

    merged = json.loads(json.dumps(DEFAULT_WEIGHTS, ensure_ascii=False))
    for key, value in saved.items():
        if key == "price" and isinstance(value, dict):
            merged["price"].update(value)
        else:
            merged[key] = value

    WEIGHTS = merged


def save_weights():
    save_json_file(WEIGHT_FILE, WEIGHTS)


def get_previous_competition(name):
    history = load_history()
    today = get_today().strftime("%Y-%m-%d")

    for item in reversed(history):
        if item.get("name") == name and item.get("date") != today:
            return float(item.get("competition", 0) or 0)

    return 0.0


def has_today_snapshot(name):
    history = load_history()
    today = get_today().strftime("%Y-%m-%d")

    return any(
        item.get("date") == today and item.get("name") == name
        for item in history
    )


def save_today_snapshot(name, info, result):
    if has_today_snapshot(name):
        return

    history = load_history()
    today = get_today().strftime("%Y-%m-%d")

    history.append({
        "date": today,
        "name": name,
        "competition": info.get("competition", 0),
        "predicted": result.get("expected_return", 0),
        "ratio": result.get("open_ratio", 0),
        "score": result.get("score", 0),
        "ttasang": result.get("ttasang", 0),
        "lockup": info.get("lockup", 0),
        "float": info.get("float", 50),
        "offer_price": info.get("offer_price", 0),
        "actual": None
    })

    save_history(history)


def is_competition_spike(prev, now):
    if prev <= 0 or now <= 0:
        return False

    if now - prev >= SPIKE_ABS_THRESHOLD:
        return True

    if now >= prev * SPIKE_RATIO_THRESHOLD:
        return True

    return False


# ==========================
# Auto Tune
# ==========================

def clamp(value, low, high):
    return max(low, min(high, value))


def auto_tune_weights():
    """
    history.json에 actual_ratio가 채워진 데이터가 10개 이상 있을 때만
    가중치를 아주 조금씩 보정한다.

    actual_ratio 예:
    {
      "name": "종목명",
      "ratio": 1.6,
      "actual_ratio": 1.2,
      ...
    }
    """
    history = load_history()

    samples = [
        item for item in history
        if item.get("actual_ratio") is not None and item.get("ratio") is not None
    ]

    if len(samples) < 10:
        logging.info(f"[AUTO-TUNE] sample 부족: {len(samples)}/10")
        return

    comp_error = 0.0
    lock_error = 0.0
    float_error = 0.0
    count = 0

    for item in samples:
        try:
            predicted_ratio = float(item.get("ratio", 1.0) or 1.0)
            actual_ratio = float(item.get("actual_ratio", 1.0) or 1.0)

            error = predicted_ratio - actual_ratio

            competition = float(item.get("competition", 0) or 0)
            lockup = float(item.get("lockup", 0) or 0)
            float_ratio = float(item.get("float", 50) or 50)

            comp_error += error * clamp(competition / 1000.0, 0.0, 2.0)
            lock_error += error * clamp(lockup / 100.0, 0.0, 1.0)
            float_error += error * clamp(float_ratio / 100.0, 0.0, 1.0)
            count += 1

        except Exception:
            continue

    if count == 0:
        return

    comp_error /= count
    lock_error /= count
    float_error /= count

    # 과대평가(error > 0)면 comp/lock 가중치를 낮추고, float 감점을 키운다.
    WEIGHTS["comp"] -= comp_error * 2.0
    WEIGHTS["lock"] -= lock_error * 0.2
    WEIGHTS["float"] += float_error * 0.2

    WEIGHTS["comp"] = clamp(WEIGHTS["comp"], 10.0, 30.0)
    WEIGHTS["lock"] = clamp(WEIGHTS["lock"], 0.1, 1.0)
    WEIGHTS["float"] = clamp(WEIGHTS["float"], 0.05, 0.5)

    logging.info(
        "[AUTO-TUNE] weights updated: "
        f"comp={WEIGHTS['comp']:.3f}, "
        f"lock={WEIGHTS['lock']:.3f}, "
        f"float={WEIGHTS['float']:.3f}"
    )


# ==========================
# 38 크롤링
# ==========================

def make_38_detail_url(href):
    if not href:
        return None

    href = href.strip()

    if href.startswith("http"):
        return href

    if href.startswith("/"):
        return "http://www.38.co.kr" + href

    return "http://www.38.co.kr/html/fund/" + href


def find_ipo_schedule_table(soup):
    for table in soup.select("table"):
        text = table.get_text(" ", strip=True)
        if "공모주일정" in text and "종목명" in text:
            return table

    for table in soup.select("table"):
        text = table.get_text(" ", strip=True)
        if "종목명" in text and ("청약" in text or "공모" in text):
            return table

    return None


def fetch_38_all_and_today_events():
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(URL_38, headers=headers, timeout=10)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    target_table = find_ipo_schedule_table(soup)

    if not target_table:
        raise Exception("38 공모주일정 테이블을 찾지 못했습니다.")

    all_38 = {}
    today_events = []

    for row in target_table.select("tr"):
        cols = row.find_all("td")

        if len(cols) < 5:
            continue

        link_tag = cols[0].find("a")
        if not link_tag:
            continue

        name = link_tag.get_text(strip=True).replace(" ", "")
        if not name:
            continue

        row_text = " ".join(c.get_text(" ", strip=True) for c in cols)
        subscription_text = cols[1].get_text(" ", strip=True)

        detail_url = None
        if link_tag.has_attr("href"):
            detail_url = make_38_detail_url(link_tag["href"])

        all_38[name] = {
            "text": row_text,
            "cols": cols,
            "detail_url": detail_url,
            "subscription_text": subscription_text
        }

        if is_today_in_subscription(subscription_text):
            today_events.append({
                "date": get_today().day,
                "event": "청약",
                "company": name,
                "subscription_text": subscription_text
            })

    logging.info(f"[38 기준] 청약 기간 내 종목 {len(today_events)}건")
    return all_38, today_events


# ==========================
# 가격 파싱
# ==========================

def parse_price_info_from_text(text):
    offer_price = 0
    band_low = 0
    band_high = 0
    price_position = "미확인"

    band_match = re.search(
        r"(\d{1,3}(?:,\d{3})+)\s*~\s*(\d{1,3}(?:,\d{3})+)",
        text
    )

    if band_match:
        band_low = int(band_match.group(1).replace(",", ""))
        band_high = int(band_match.group(2).replace(",", ""))

        before_band = text[:band_match.start()]
        prices = re.findall(r"\d{1,3}(?:,\d{3})+", before_band)
        prices = [
            int(p.replace(",", ""))
            for p in prices
            if int(p.replace(",", "")) >= 1000
        ]

        if prices:
            offer_price = prices[-1]

    if not offer_price:
        prices = re.findall(r"\d{1,3}(?:,\d{3})+", text)
        prices = [
            int(p.replace(",", ""))
            for p in prices
            if int(p.replace(",", "")) >= 1000
        ]

        for price in prices:
            if price != band_low and price != band_high:
                offer_price = price
                break

    if offer_price and band_high:
        if offer_price > band_high:
            price_position = "초과"
        elif offer_price == band_high:
            price_position = "상단"
        elif offer_price == band_low:
            price_position = "하단"
        elif band_low < offer_price < band_high:
            price_position = "밴드내"

    return {
        "offer_price": offer_price,
        "band_low": band_low,
        "band_high": band_high,
        "price_position": price_position
    }


# ==========================
# 상세페이지 파싱
# ==========================

def parse_percent_candidates(text):
    values = []

    for match in re.finditer(r"([\d]+(?:\.\d+)?)\s*%", text):
        try:
            val = float(match.group(1))
            if 0 <= val <= 100:
                values.append(val)
        except ValueError:
            continue

    return values


def parse_38_detail(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")

        float_ratio = None
        lockup = None

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cols = [
                    c.get_text(" ", strip=True)
                    for c in row.find_all(["td", "th"])
                ]

                if not cols:
                    continue

                row_text = " ".join(cols)
                row_key = normalize_text(row_text)
                percents = parse_percent_candidates(row_text)

                if not percents:
                    continue

                if float_ratio is None and (
                    "유통가능물량" in row_key or
                    "유통가능주식" in row_key or
                    "유통가능" in row_key
                ):
                    float_ratio = percents[-1]

                if lockup is None and (
                    "의무보유확약" in row_key or
                    "의무보유" in row_key or
                    "확약" in row_key
                ):
                    lockup = percents[-1]

        full_text = soup.get_text(" ", strip=True)
        full_key = normalize_text(full_text)

        if float_ratio is None:
            for keyword in ["유통가능물량", "유통가능주식", "유통가능"]:
                idx = full_key.find(keyword)
                if idx >= 0:
                    window = full_key[idx:idx + 250]
                    percents = parse_percent_candidates(window)
                    if percents:
                        float_ratio = percents[-1]
                        break

        if lockup is None:
            for keyword in ["의무보유확약", "의무보유", "확약"]:
                idx = full_key.find(keyword)
                if idx >= 0:
                    window = full_key[idx:idx + 250]
                    percents = parse_percent_candidates(window)
                    if percents:
                        lockup = percents[-1]
                        break

        if float_ratio is None:
            float_ratio = 50.0

        if lockup is None:
            lockup = 0.0

        return {
            "float": float_ratio,
            "lockup": lockup
        }

    except Exception as e:
        logging.error(f"상세페이지 실패: {url} | {e}")
        return {
            "float": 50.0,
            "lockup": 0.0
        }


# ==========================
# 증권사 추출
# ==========================

def extract_underwriters(text):
    brokers = []

    candidates = re.findall(
        r"[가-힣A-Za-z0-9]+(?:증권|투자증권|금융투자)",
        text
    )

    for candidate in candidates:
        candidate = re.sub(r"^\d+\s*", "", candidate.strip())

        if candidate and candidate not in brokers:
            brokers.append(candidate)

    return brokers


def normalize_spac_name(company):
    if "스팩" not in company:
        return company

    name = company.replace("제", "")

    match = re.search(r"(\d+)", name)
    if not match:
        return name

    number = match.group(1)
    prefix = name.split(number)[0]
    prefix = prefix.replace("호", "").replace("스팩", "")

    return f"{prefix}스팩{number}호"


# ==========================
# 38 정보 조립
# ==========================

cache_38 = {}


def get_38_info(company, all_38):
    if company in cache_38:
        return cache_38[company]

    try:
        target = normalize_spac_name(company)

        item = all_38.get(target) or all_38.get(company)

        if not item:
            info = DEFAULT_38_INFO.copy()
            info["warning"] = True
            cache_38[company] = info
            return info

        text = item["text"]
        detail_url = item.get("detail_url")

        brokers = extract_underwriters(text)
        price_info = parse_price_info_from_text(text)

        competition = 0.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?::|대)\s*1", text)
        if match:
            competition = float(match.group(1))

        detail_info = {"float": 50.0, "lockup": 0.0}
        if detail_url:
            detail_info = parse_38_detail(detail_url)

        result = {
            "competition": competition,
            "lockup": detail_info["lockup"],
            "float": detail_info["float"],
            "offer_price": price_info["offer_price"],
            "band_low": price_info["band_low"],
            "band_high": price_info["band_high"],
            "price_position": price_info["price_position"],
            "brokers": brokers
        }

        cache_38[company] = result
        return result

    except Exception as e:
        logging.error(f"38 처리 실패: {company} | {e}")

        info = DEFAULT_38_INFO.copy()
        info["warning"] = True
        cache_38[company] = info
        return info


# ==========================
# 점수 + 예측
# ==========================

def calc_score(info):
    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)
    float_ratio = info.get("float", 50)
    pos = info.get("price_position", "미확인")

    score = 0.0

    if comp > 0:
        score += min(math.log10(comp + 1) * WEIGHTS["comp"], 55)

    # 구간 보정 + auto-tune weight 혼합
    if lock < 10:
        score -= 10
    elif lock < 30:
        score += 5
    elif lock < 50:
        score += 15
    else:
        score += 25

    score += lock * WEIGHTS["lock"]

    if float_ratio < 20:
        score += 20
    elif float_ratio < 40:
        score += 5
    elif float_ratio < 60:
        score -= 5
    else:
        score -= 20

    score -= float_ratio * WEIGHTS["float"]

    score += WEIGHTS["price"].get(pos, 0)

    if comp > 1000 and lock > 50:
        score += 10

    if comp < 100 and float_ratio > 50:
        score -= 10

    return round(score, 1)


def analyze_ipo(name, info):
    if "스팩" in name:
        return {
            "score": 0,
            "ttasang": 0,
            "double": 0,
            "fail": 100,
            "open_ratio": 1.0,
            "expected_return": 0
        }

    score = calc_score(info)

    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)
    float_ratio = info.get("float", 50)
    pos = info.get("price_position", "미확인")

    if pos == "초과":
        base = 1.9
    elif pos == "상단":
        base = 1.6
    elif pos == "밴드내":
        base = 1.3
    else:
        base = 1.1

    lock_adj = (lock / 100) * 0.6
    comp_adj = min(math.log10(comp + 1) * 0.35, 0.6)

    ratio = base + lock_adj + comp_adj

    if float_ratio > 50:
        ratio -= 0.25
    elif float_ratio < 20:
        ratio += 0.15

    if lock > 60:
        ratio += 0.15
    elif lock < 10:
        ratio -= 0.2

    if comp > 1500:
        ratio += 0.15
    elif comp < 100:
        ratio -= 0.2

    if pos == "하단":
        ratio -= 0.3

    ratio = max(1.0, min(ratio, 2.3))
    expected_return = int((ratio - 1) * 100)

    strong = (
        comp >= 800 and
        lock >= 40 and
        float_ratio <= 40 and
        pos in ["상단", "초과"]
    )

    very_strong = (
        comp >= 1500 and
        lock >= 50 and
        float_ratio <= 30 and
        pos == "초과"
    )

    weak = (
        comp < 200 or
        lock < 10 or
        float_ratio > 60 or
        pos == "하단"
    )

    if very_strong:
        tt, db, fail = 80, 15, 5
    elif strong:
        tt, db, fail = 60, 30, 10
    elif weak:
        tt, db, fail = 10, 30, 60
    else:
        if ratio >= 1.8:
            tt, db, fail = 50, 35, 15
        elif ratio >= 1.5:
            tt, db, fail = 35, 45, 20
        elif ratio >= 1.3:
            tt, db, fail = 20, 50, 30
        else:
            tt, db, fail = 10, 40, 50

    return {
        "score": score,
        "ttasang": tt,
        "double": db,
        "fail": fail,
        "open_ratio": round(ratio, 2),
        "expected_return": expected_return
    }


# ==========================
# 메시지
# ==========================

def grade(score):
    if score >= 80:
        return "S 등급"
    elif score >= 60:
        return "A 등급"
    elif score >= 40:
        return "B 등급"
    return "C 등급"


def is_ttasang_candidate(name, info, result):
    if "스팩" in name:
        return False

    return (
        info.get("competition", 0) >= 800 and
        info.get("lockup", 0) >= 40 and
        result.get("score", 0) >= 60
    )


def get_sell_strategy(name, info, result):
    if is_ttasang_candidate(name, info, result):
        return "따상 홀딩 (장초반 관망 후 +80% 이상 매도)"

    if result.get("score", 0) >= 50:
        return "분할매도 (시초가 50% + 추가상승 매도)"

    return "시초가 매도"


def format_company_block(item):
    name = item["name"]
    info = item["info"]
    result = item["result"]

    line = f"📝 청약 | {name}\n"
    line += "━━━━━━━━━━━━━━━\n"

    if item["is_last_day"]:
        line += "- 청약: 마지막날\n"

    if item["is_spike"]:
        line += (
            f"- 🚨 경쟁률 급등: "
            f"{item['prev_competition']}:1 → {info['competition']}:1\n"
        )

    line += f"- 점수: {result['score']} ({grade(result['score'])})\n"

    if info.get("competition", 0) == 0:
        line += "- 경쟁률: 데이터 없음\n"
    else:
        line += f"- 경쟁률: {info['competition']}:1\n"

    if info.get("offer_price", 0):
        line += f"- 공모가: {info['offer_price']:,}원\n"
    else:
        line += "- 공모가: 데이터 없음\n"

    line += f"- 유통 {info['float']}% / 확약 {info['lockup']}%\n"
    line += f"- 예상 수익: +{result['expected_return']}% (x{result['open_ratio']})\n"
    line += f"- 따상 확률: {result['ttasang']}%\n"

    strategy = get_sell_strategy(name, info, result)
    line += f"- 매도전략: {strategy}\n"

    if info.get("brokers"):
        line += f"- {', '.join(info['brokers'])}\n"

    if info.get("warning"):
        line += "- ⚠️ 일부 데이터 누락\n"

    return line + "\n"


def format_spac_block(item):
    name = item["name"]
    info = item["info"]

    line = f"🧾 스팩 | {name}\n"
    line += "━━━━━━━━━━━━━━━\n"

    if item["is_last_day"]:
        line += "- 청약: 마지막날\n"

    if info.get("competition", 0) == 0:
        line += "- 경쟁률: 데이터 없음\n"
    else:
        line += f"- 경쟁률: {info['competition']}:1\n"

    if info.get("offer_price", 0):
        line += f"- 공모가: {info['offer_price']:,}원\n"

    line += f"- 유통 {info['float']}% / 확약 {info['lockup']}%\n"

    if info.get("brokers"):
        line += f"- {', '.join(info['brokers'])}\n"

    return line + "\n"


def build_items(today_data, all_38):
    items = []

    for raw in today_data:
        name = raw["company"]
        sub_text = raw.get("subscription_text", "")

        info = get_38_info(name, all_38)
        result = analyze_ipo(name, info)

        prev_comp = get_previous_competition(name)
        now_comp = info.get("competition", 0)

        is_last = is_last_day_subscription(sub_text)
        is_spike = is_competition_spike(prev_comp, now_comp)

        save_today_snapshot(name, info, result)

        items.append({
            "name": name,
            "subscription_text": sub_text,
            "info": info,
            "result": result,
            "prev_competition": prev_comp,
            "is_last_day": is_last,
            "is_spike": is_spike,
            "is_spac": "스팩" in name
        })

    return items


def build_message(today_data, all_38):
    today = get_today().strftime("%Y-%m-%d")
    items = build_items(today_data, all_38)

    if not items:
        return f"📭 {today} 공모 일정 없음"

    msg = f"📊 {today} 공모주 청약 정보\n\n"

    normal_items = sorted(
        [x for x in items if not x["is_spac"]],
        key=lambda x: (
            x["result"].get("ttasang", 0),
            x["info"].get("competition", 0),
            x["result"].get("score", 0)
        ),
        reverse=True
    )

    if normal_items:
        msg += "📝 청약 종목 - 따상 확률 높은 순\n"
        msg += "===============\n"
        for item in normal_items:
            msg += format_company_block(item)
        msg += "\n"

    spac_items = [x for x in items if x["is_spac"]]
    if spac_items:
        msg += "🧾 스팩\n"
        msg += "===============\n"
        for item in spac_items:
            msg += format_spac_block(item)
        msg += "\n"

    spike_items = [
        x for x in normal_items
        if x["is_last_day"] and x["is_spike"]
    ]

    if spike_items:
        msg += "🚨 청약 마지막날 + 경쟁률 급등\n"
        msg += "===============\n"
        for item in spike_items:
            msg += f"🔥 {item['name']}\n"
            msg += (
                f"- 경쟁률: {item['prev_competition']}:1 "
                f"→ {item['info']['competition']}:1\n"
            )
            msg += f"- 따상 확률: {item['result']['ttasang']}%\n"
            msg += f"- 예상 수익: +{item['result']['expected_return']}%\n\n"

    top3_items = sorted(
        normal_items,
        key=lambda x: x["info"].get("competition", 0),
        reverse=True
    )[:3]

    if top3_items:
        msg += "🏆 경쟁률 TOP3\n"
        msg += "===============\n"
        for idx, item in enumerate(top3_items, start=1):
            msg += (
                f"{idx}. {item['name']} "
                f"({item['info']['competition']}:1, "
                f"따상 {item['result']['ttasang']}%)\n"
            )
        msg += "\n"

    hot_items = [
        x for x in normal_items
        if is_ttasang_candidate(x["name"], x["info"], x["result"])
    ]

    if hot_items:
        msg += "🚀🔥 따상 유력\n"
        msg += "===============\n"
        for item in hot_items:
            msg += (
                f"📝 {item['name']} "
                f"(+{item['result']['expected_return']}%, "
                f"경쟁률 {item['info']['competition']}:1)\n"
            )
        msg += "\n"

    return msg.strip()


# ==========================
# 텔레그램
# ==========================

async def send(msg):
    if not TOKEN or not CHAT_ID:
        logging.warning("TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 전송하지 않습니다.")
        #print(msg)
        return

    bot = telegram.Bot(token=TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=msg)


# ==========================
# 실행
# ==========================

def main():
    try:
        load_weights()
        auto_tune_weights()
        save_weights()

        all_38, today_data = fetch_38_all_and_today_events()
        msg = build_message(today_data, all_38)

        logging.info(msg)
        asyncio.run(send(msg))

    except Exception:
        logging.exception("전체 실행 실패")


if __name__ == "__main__":
    main()
