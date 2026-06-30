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

    "offer_price":0,
    "band_low":0,
    "band_high":0,
    "price_position":"미확인",

    "sales":None,
    "operating_profit":None,
    "net_income":None,

    "assets":None,
    "liabilities":None,
    "equity":None,

    "debt_ratio":None,
    "roe":None,
    "per":None,

    "financial_grade":"미평가",

    "brokers":[]
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


def parse_number_value(text):
    text = normalize_text(str(text))

    if not text or text in ["-", "N/A", "NA"]:
        return None

    negative = text.startswith("-")
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    raw = match.group(0).replace(",", "")

    try:
        value = float(raw)
    except ValueError:
        return None

    if value.is_integer():
        value = int(value)

    return value


def parse_percent_value(text):
    value = parse_number_value(text)
    if value is None:
        return None
    return round(float(value), 2)


def to_million_won(value):
    if value is None:
        return None
    return int(round(float(value) / 1_000_000))


def table_rows(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if cells:
            rows.append(cells)
    return rows


def pick_financial_column(header_cells):
    """
    38 상세 페이지는 2026년 1분기, 2025년, 2024년처럼 열이 섞인다.
    알림에는 분기보다 최근 온기 실적을 우선 사용한다.
    """
    normalized = [normalize_text(x) for x in header_cells]

    for i, h in enumerate(normalized):
        if "2025" in h and "1분기" not in h:
            return i

    for i, h in enumerate(normalized):
        if re.search(r"20\d{2}", h) and "1분기" not in h:
            return i

    for i, h in enumerate(normalized):
        if re.search(r"20\d{2}", h):
            return i

    return 1 if len(header_cells) > 1 else None


def extract_value_from_labeled_rows(rows, label, prefer_col=None, percent=False, million_won=False):
    label_key = normalize_text(label)

    for cells in rows:
        if not cells:
            continue

        label_idx = None
        for i, cell in enumerate(cells[:3]):
            if normalize_text(cell) == label_key or label_key in normalize_text(cell):
                label_idx = i
                break

        if label_idx is None:
            continue

        candidates = []
        # header 기준 prefer_col은 보통 '구분' 다음부터 값 열이 시작한다.
        if prefer_col is not None:
            value_idx = label_idx + prefer_col
            if value_idx < len(cells):
                candidates.append(cells[value_idx])

        candidates.extend(cells[label_idx + 1:])

        for cell in candidates:
            if percent:
                value = parse_percent_value(cell)
            else:
                value = parse_number_value(cell)
                if million_won:
                    value = to_million_won(value)

            if value is not None:
                return value

    return None


def classify_financial_grade(info):
    score = financial_score(info)

    if score >= 45:
        return "우량"
    if score >= 25:
        return "양호"
    if score >= 5:
        return "보통"
    if score < 0:
        return "주의"
    return "미평가"


def parse_financials_from_detail_soup(soup):
    info = {
        "sales": None,
        "operating_profit": None,
        "net_income": None,
        "assets": None,
        "liabilities": None,
        "equity": None,
        "debt_ratio": None,
        "roe": None,
        "per": None,
        "financial_year": None,
        "financial_grade": "미평가",
    }

    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue

        text = table.get_text(" ", strip=True)
        key = normalize_text(text)

        # 공모분석 본문 내 재무적 성장성: 매출/영업이익/순이익
        if "재무적성장성" in key and "영업이익" in key and "당기순이익" in key:
            header = next((r for r in rows if r and normalize_text(r[0]) == "구분"), None)
            col = pick_financial_column(header) if header else None

            if header and col is not None and col < len(header):
                year_match = re.search(r"20\d{2}", header[col])
                if year_match:
                    info["financial_year"] = year_match.group(0)

            sales = extract_value_from_labeled_rows(rows, "매출액", col)
            operating_profit = extract_value_from_labeled_rows(rows, "영업이익", col)
            net_income = extract_value_from_labeled_rows(rows, "당기순이익", col)

            if sales is not None:
                info["sales"] = sales
            if operating_profit is not None:
                info["operating_profit"] = operating_profit
            if net_income is not None:
                info["net_income"] = net_income

        # 재무제표 요약: 자산/부채/자본총계. 원 단위로 들어오므로 백만원 변환.
        if "자산총계" in key and "부채총계" in key and "자본총계" in key and "동종업체" not in key and "유사기업" not in key:
            header = next((r for r in rows if r and normalize_text(r[0]) in ["구분", "구분"] and any("2025" in x for x in r)), None)
            col = pick_financial_column(header) if header else None

            assets = extract_value_from_labeled_rows(rows, "자산총계", col, million_won=True)
            liabilities = extract_value_from_labeled_rows(rows, "부채총계", col, million_won=True)
            equity = extract_value_from_labeled_rows(rows, "자본총계", col, million_won=True)

            if assets is not None:
                info["assets"] = assets
            if liabilities is not None:
                info["liabilities"] = liabilities
            if equity is not None:
                info["equity"] = equity

        # 재무비율/재무적 안정성: 부채비율, ROE
        if "부채비율" in key or "자기자본수익률" in key:
            header = next((r for r in rows if r and normalize_text(r[0]) in ["구분", "구분"] and any("2025" in x for x in r)), None)
            col = pick_financial_column(header) if header else None

            debt_ratio = extract_value_from_labeled_rows(rows, "부채비율", col, percent=True)
            roe = extract_value_from_labeled_rows(rows, "자기자본수익률", col, percent=True)

            if debt_ratio is not None:
                info["debt_ratio"] = debt_ratio
            if roe is not None:
                info["roe"] = roe

        # 주가지표: PER
        if "주가지표" in key or "PER" in key:
            header = next((r for r in rows if r and any("2025" in x for x in r)), None)
            col = pick_financial_column(header) if header else None
            for cells in rows:
                if not cells or "PER" not in cells[0]:
                    continue

                candidates = []
                if col is not None and col < len(cells):
                    candidates.append(cells[col])
                candidates.extend(cells[1:])

                for cell in candidates:
                    value = parse_number_value(cell)
                    if value is not None:
                        info["per"] = float(value)
                        break

                if info["per"] is not None:
                    break

    info["financial_grade"] = classify_financial_grade(info)
    return info


def parse_38_detail(url):
    empty_financials = {
        "sales": None,
        "operating_profit": None,
        "net_income": None,
        "assets": None,
        "liabilities": None,
        "equity": None,
        "debt_ratio": None,
        "roe": None,
        "per": None,
        "financial_year": None,
        "financial_grade": "미평가",
    }

    try:
        headers = {"User-Agent": "Mozilla/5.0"}

        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()

        # 38커뮤니케이션 상세 페이지는 euc-kr인 경우가 많다.
        if not res.encoding or res.encoding.lower() in ["iso-8859-1", "ascii"]:
            res.encoding = res.apparent_encoding or "euc-kr"

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

        financials = parse_financials_from_detail_soup(soup)

        return {
            "float": float_ratio,
            "lockup": lockup,
            **financials,
        }

    except Exception as e:
        logging.error(f"상세페이지 실패: {url} | {e}")
        return {
            "float": 50.0,
            "lockup": 0.0,
            **empty_financials,
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

        result={
            "competition":competition,

            "lockup":detail_info["lockup"],
            "float":detail_info["float"],

            "sales":detail_info["sales"],
            "operating_profit":detail_info["operating_profit"],
            "net_income":detail_info["net_income"],

            "assets":detail_info["assets"],
            "liabilities":detail_info["liabilities"],
            "equity":detail_info["equity"],

            "debt_ratio":detail_info["debt_ratio"],
            "roe":detail_info["roe"],
            "per":detail_info["per"],

            "financial_grade":detail_info["financial_grade"],

            "offer_price":price_info["offer_price"],
            "band_low":price_info["band_low"],
            "band_high":price_info["band_high"],
            "price_position":price_info["price_position"],

            "brokers":brokers
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

    score += financial_score(info) * 0.3

    return round(score,1)


def financial_score(info):

    score=0

    sales=info.get("sales")
    op=info.get("operating_profit")
    net=info.get("net_income")
    debt=info.get("debt_ratio")
    roe=info.get("roe")

    if sales and sales>0:
        score+=10

    if op is not None:
        if op>0:
            score+=15
        else:
            score-=10

    if net is not None:
        if net>0:
            score+=15
        else:
            score-=10

    if debt is not None:
        if debt<50:
            score+=10
        elif debt<100:
            score+=5
        elif debt>200:
            score-=10

    if roe is not None:
        if roe>=20:
            score+=10
        elif roe>=10:
            score+=5
        elif roe<0:
            score-=10

    return score


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

    finance = financial_score(info)

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

    if finance >= 40:
        ratio += 0.10
    elif finance <= 0:
        ratio -= 0.10

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
        line += "🔥🔥 오늘 청약 마감 🔥🔥\n"

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

    year_label = f"({info['financial_year']}년)" if info.get("financial_year") else ""
    line += f"\n📊 재무 {year_label}\n"

    if info["sales"]:
        line += f"- 매출 : {info['sales']:,}백만원\n"

    if info["operating_profit"] is not None:

        icon="🟢" if info["operating_profit"]>0 else "🔴"

        line += f"- 영업이익 : {info['operating_profit']:,} {icon}\n"

    if info["net_income"] is not None:

        icon="🟢" if info["net_income"]>0 else "🔴"

        line += f"- 순이익 : {info['net_income']:,} {icon}\n"

    if info.get("assets") is not None:
        line += f"- 자산총계 : {info['assets']:,}백만원\n"

    if info.get("liabilities") is not None:
        line += f"- 부채총계 : {info['liabilities']:,}백만원\n"

    if info.get("equity") is not None:
        line += f"- 자본총계 : {info['equity']:,}백만원\n"

    if info["debt_ratio"] is not None:
        line += f"- 부채비율 : {info['debt_ratio']}%\n"

    if info["roe"] is not None:
        line += f"- ROE : {info['roe']}%\n"

    if info.get("per") is not None:
        line += f"- PER : {info['per']}배\n"

    line += f"- 기업평가 : {info['financial_grade']}\n\n"

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
        line += "🔥🔥 오늘 청약 마감 🔥🔥\n"

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
            x["is_last_day"],
            x["result"].get("ttasang", 0),
            x["info"].get("competition", 0),
            x["result"].get("score", 0)
        ),
        reverse=True
    )

    last_day_items = [x for x in normal_items if x["is_last_day"]]

    if last_day_items:
        msg += "⏰ 오늘 청약 마감\n"
        msg += "===============\n"

        for item in last_day_items:
            comp = item["info"].get("competition", 0)

            if comp:
                msg += f"🔥 {item['name']} ({comp}:1)\n"
            else:
                msg += f"🔥 {item['name']}\n"

        msg += "\n"

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

    financial_top3 = sorted(
        normal_items,
        key=lambda x: financial_score(x["info"]),
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

    if financial_top3:
        msg += "🏢 재무 우량 TOP3\n"
        msg += "===============\n"

        for idx, item in enumerate(financial_top3, start=1):
            msg += (
                f"{idx}. {item['name']} "
                f"(재무점수 {financial_score(item['info'])}, "
                f"{item['info']['financial_grade']})\n"
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

        # 청약 종목 없으면 전송 안함
        if not today_data:
            logging.info("오늘 청약 종목 없음 - 전송 생략")
            return

        msg = build_message(today_data, all_38)

        logging.info(msg)
        asyncio.run(send(msg))

    except Exception:
        logging.exception("전체 실행 실패")


if __name__ == "__main__":
    main()


# ===== Added: Financial scoring =====
import re
def calculate_financial_score(fin):
    score=0
    comments=[]
    def num(v):
        if v is None: return None
        m=re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",",""))
        return float(m.group()) if m else None
    op=num(fin.get("operating_profit"))
    net=num(fin.get("net_income"))
    roe=num(fin.get("roe"))
    debt=num(fin.get("debt_ratio"))
    if op and op>0: score+=10; comments.append("영업이익 흑자")
    if net and net>0: score+=5; comments.append("순이익 흑자")
    if roe is not None:
        score += 10 if roe>=20 else 8 if roe>=15 else 5 if roe>=10 else 0
    if debt is not None:
        score += 10 if debt<=100 else 7 if debt<=150 else 4 if debt<=200 else 0
    grade="A" if score>=30 else "B" if score>=22 else "C" if score>=15 else "D"
    return {"financial_score":score,"financial_grade":grade,"comments":comments}
