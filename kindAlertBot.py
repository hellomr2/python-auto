import time
import re
import asyncio
import os
from datetime import datetime, date

import holidays
import requests
import telegram
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


# ==========================
# 설정
# ==========================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

KIND_URL = "https://kind.krx.co.kr/listinvstg/pubofrschdl.do?method=searchPubofrScholMain"
EVENT_TYPES = ["상장", "청약", "수요예측", "IR", "납입"]

DEFAULT_38_INFO = {
    "competition": 0,
    "float": 0,
    "lockup": 0,
    "offer_price": 0,
    "band_low": 0,
    "band_high": 0,
    "price_position": "미확인",
    "brokers": []
}

# ==========================
# 전략 설정
# ==========================
TTASANG_THRESHOLD = 70   # 따상 유력 기준 (%)
SCORE_THRESHOLD = 70     # 점수 기준 (옵션)
USE_SCORE_BASED = False  # True면 score 기준 사용


def is_ttasang_candidate(name, info, result):
    if "스팩" in name:
        return False

    if USE_SCORE_BASED:
        return result.get("score", 0) >= SCORE_THRESHOLD

    return result.get("ttasang", 0) >= TTASANG_THRESHOLD


# ==========================
# Selenium 크롤링
# ==========================
def fetch_calendar():
    options = Options()
    options.add_argument("--headless")

    driver = webdriver.Chrome(options=options)
    driver.get(KIND_URL)
    time.sleep(3)

    elements = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

    raw = []
    for el in elements:
        text = el.text.strip()
        if text:
            raw.append(text.split("\n"))

    driver.quit()
    return raw


# ==========================
# 파싱
# ==========================
def parse_calendar(raw):
    parsed = []
    current_date = None
    current_event = None

    for row in raw:
        for item in row:
            item = item.strip()

            if not item:
                continue

            if item.isdigit():
                current_date = int(item)
                continue

            if item in EVENT_TYPES:
                current_event = item
                continue

            if current_date and current_event:
                parsed.append({
                    "date": current_date,
                    "event": current_event,
                    "company": item.replace(" ", "")
                })

    return parsed


def filter_today(parsed, test_day=None):
    today = test_day if test_day else int(datetime.now().strftime("%d"))

    result = [x for x in parsed if x["date"] == today]

    print(f"[DEBUG] 선택된 날짜: {today}, 개수: {len(result)}")
    return result


# ==========================
# 가격 파싱
# ==========================
def parse_price_info_from_text(text):
    offer_price = 0
    band_low = 0
    band_high = 0
    price_position = "미확인"

    band_match = re.search(r"(\d{1,3}(?:,\d{3})+)\s*~\s*(\d{1,3}(?:,\d{3})+)", text)
    if band_match:
        band_low = int(band_match.group(1).replace(",", ""))
        band_high = int(band_match.group(2).replace(",", ""))

        before_band = text[:band_match.start()]
        prices = re.findall(r"\d{1,3}(?:,\d{3})+", before_band)
        prices = [int(p.replace(",", "")) for p in prices if int(p.replace(",", "")) >= 1000]

        if prices:
            offer_price = prices[-1]

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
# 38 상세 파싱
# ==========================
def parse_38_detail(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")

        float_ratio = None
        lockup = None

        tables = soup.find_all("table")

        for table in tables:
            text = table.get_text(" ", strip=True)

            if "유통가능물량" not in text:
                continue

            rows = table.find_all("tr")

            for row in rows:
                cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]

                if not cols:
                    continue

                row_text = " ".join(cols)

                if "유통가능물량" in row_text:
                    for c in cols:
                        match = re.search(r"([\d\.]+)\s*%", c)
                        if match:
                            val = float(match.group(1))
                            if val <= 100:
                                float_ratio = val

                if "의무보유" in row_text or "확약" in row_text:
                    for c in cols:
                        match = re.search(r"([\d\.]+)\s*%", c)
                        if match:
                            val = float(match.group(1))
                            if val <= 100:
                                lockup = val

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
        print("상세페이지 실패:", e)
        return {
            "float": 50.0,
            "lockup": 0.0
        }


# ==========================
# 증권사 추출
# ==========================
def extract_underwriters(text):
    """
    38 텍스트에서 증권사 추출
    """
    match = re.search(r"1\s+(.+)$", text)
    if not match:
        return []

    tail = match.group(1)
    parts = tail.split(",")

    brokers = []
    for part in parts:
        part = part.strip()
        if "증권" in part or "투자" in part:
            brokers.append(part)

    return brokers


def normalize_spac_name(company):
    """
    KIND → 38 형식으로 변환
    """
    if "스팩" not in company:
        return company

    # 제 제거
    name = company.replace("제", "")

    # 숫자 추출
    match = re.search(r"(\d+)", name)
    if not match:
        return name

    number = match.group(1)

    # 증권사 이름 추출
    prefix = name.split(number)[0]
    prefix = prefix.replace("호", "").replace("스팩", "")

    # 38 스타일로 변환
    return f"{prefix}스팩{number}호"


# ==========================
# 38 메인 파싱
# ==========================

cache_38 = {}

def get_38_info_cached(company):
    if company in cache_38:
        return cache_38[company]

    result = get_38_info(company)
    cache_38[company] = result
    return result

 
def get_38_info(company):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = "http://www.38.co.kr/html/fund/index.htm?o=k"
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        rows = soup.select("table tr")

        for row in rows:
            cols = row.find_all("td")
            if not cols:
                continue

            name = cols[0].get_text(strip=True).replace(" ", "")
            target = normalize_spac_name(company)

            if name != target:
                continue

            text = " ".join([c.get_text(strip=True) for c in cols])
            print(f"[DEBUG 정확매칭] {text}")

            brokers = extract_underwriters(text)

            link_tag = cols[0].find("a")
            detail_url = None

            if link_tag and "href" in link_tag.attrs:
                href = link_tag["href"]

                if href.startswith("http"):
                    detail_url = href
                elif href.startswith("/"):
                    detail_url = "http://www.38.co.kr" + href
                else:
                    detail_url = "http://www.38.co.kr/html/fund/" + href

            price_info = parse_price_info_from_text(text)

            competition = 0.0
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?::|대)\s*1", text)
            if match:
                competition = float(match.group(1))

            detail_info = {"float": 50.0, "lockup": 0.0}

            if detail_url:
                print(f"[DEBUG 상세URL] {detail_url}")
                detail_info = parse_38_detail(detail_url)

            return {
                "competition": competition,
                "lockup": detail_info["lockup"],
                "float": detail_info["float"],
                "offer_price": price_info["offer_price"],
                "band_low": price_info["band_low"],
                "band_high": price_info["band_high"],
                "price_position": price_info["price_position"],
                "brokers": brokers
            }

        return None

    except Exception as e:
        print("38 실패:", company, e)
        return None


# ==========================
# 점수 + 예측
# ==========================
def calc_score(info):
    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)
    float_ratio = info.get("float", 50)
    pos = info.get("price_position", "미확인")

    score = 0

    # 경쟁률 (최대 50점)
    score += min(comp / 30, 50)

    # 확약 (최대 25점)
    score += lock * 0.4

    # 유통물량 (감점)
    score -= float_ratio * 0.15

    # 공모가 위치
    if pos == "상단":
        score += 10
    elif pos == "초과":
        score += 15
    elif pos == "하단":
        score -= 10

    return round(score, 1)


def analyze_ipo(info):
    score = calc_score(info)

    # 확률 변환
    if score >= 70:
        tt, db, fail = 75, 20, 5
    elif score >= 50:
        tt, db, fail = 60, 30, 10
    elif score >= 30:
        tt, db, fail = 40, 40, 20
    else:
        tt, db, fail = 10, 30, 60

    comp = info.get("competition", 0)
    pos = info.get("price_position", "미확인")

    # 시초가 예측
    ratio = 1.3
    if comp >= 500:
        ratio = 1.6
    if comp >= 1000:
        ratio = 1.9
    if comp >= 1500:
        ratio = 2.05

    if pos == "상단":
        ratio += 0.05
    elif pos == "초과":
        ratio += 0.1

    ratio = min(ratio, 2.3)
    expected_return = int((ratio - 1) * 100)

    return {
        "score": score,
        "ttasang": tt,
        "double": db,
        "fail": fail,
        "open_ratio": round(ratio, 2),
        "expected_return": expected_return
    }


# ==========================
# 메시지 생성
# ==========================

def format_company_block(name, info, result, is_listing=False):
    tag = "📈 상장" if is_listing else "📝 청약"

    line = f"{tag} | 📌 {name}\n"
    line += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    if is_listing:
        line += f"📈 예상 수익: +{result['expected_return']}%\n"
        line += f"🎯 따상 확률: {result['ttasang']}%\n"
        line += f"💡 매도전략: {get_sell_strategy(name, info, result)}\n"
    else:
        decision = "YES" if result['score'] >= 60 else "NO"
        line += f"🔥 참여: {decision} ({result['score']}점)\n"

    line += f"📊 경쟁률: {info['competition']}\n"
    line += f"💰 공모가: {info['offer_price']:,}원\n"
    line += f"📦 유통 {info['float']}% / 확약 {info['lockup']}%\n"

    if info.get("brokers"):
        line += f"🏦 {', '.join(info['brokers'])}\n"

    return line + "\n\n"


def get_sell_strategy(name, info, result):
    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)
    score = result.get("score", 0)

    # 따상 유력
    if is_ttasang_candidate(name, info, result) and "스팩" not in info:
        return "따상 홀딩 (장초반 관망 후 +80% 이상 매도)"

    # 표준
    if score >= 50:
        return "분할매도 (시초가 50% + 추가상승 매도)"

    # 약한 종목
    return "시초가 매도"


def build_message(data):
    today = datetime.now().strftime("%Y-%m-%d")

    if not data:
        return f"📭 {today} 공모 일정 없음"

    msg = f"📊 {today} 공모주 일정\n\n"

    listings = []
    subscriptions = []
    spac = []
    hot_list = []

    for item in data:
        name = item["company"]
        event = item["event"]

        info = get_38_info_cached(name)
        if not info:
            info = DEFAULT_38_INFO.copy()

        result = analyze_ipo(info)

        if "스팩" not in name and is_ttasang_candidate(name, info, result):
            hot_list.append((name, info, result, event))

        if event == "상장":
            listings.append((name, info, result))
        elif event == "청약":
            subscriptions.append((name, info, result))


    if hot_list:
        msg += "🚀🔥 오늘의 따상 유력\n"
        msg += "==============================\n"

        for name, info, result, event in hot_list:
            tag = "📈" if event == "상장" else "📝"
            msg += f"{tag} {name} (+{result['expected_return']}%)\n"

        msg += "\n\n"
    
    # ==========================
    # 청약
    # ==========================
    if subscriptions:
        msg += "📝 청약 종목\n==============================\n"
        for name, info, result in subscriptions:
            msg += format_company_block(name, info, result, is_listing=False) + "\n"

    # ==========================
    # 상장
    # ==========================
    if listings:
        msg += "📈 상장 종목\n==============================\n"
        for name, info, result in listings:
            msg += format_company_block(name, info, result, is_listing=True) + "\n"

    return msg.strip()


# ==========================
# 텔레그램
# ==========================
async def send(msg):
    bot = telegram.Bot(token=TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=msg)


# ==========================
# 휴장일 체크
# ==========================
def is_market_holiday():
    kr_holidays = holidays.KR()
    today = date.today()

    # 주말 포함 자동 체크
    return today in kr_holidays or today.weekday() >= 5


# ==========================
# 실행
# ==========================
def main():
    if is_market_holiday():
        print("휴장일이라 실행 안함")
        return

    raw = fetch_calendar()
    parsed = parse_calendar(raw)
    today_data = filter_today(parsed)
    # today_data = filter_today(parsed, test_day=28)

    msg = build_message(today_data)

    print(msg)
    asyncio.run(send(msg))


if __name__ == "__main__":
    main()
