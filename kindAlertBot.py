import time
import re
import asyncio
import os
import json
import math
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import holidays
import requests
import telegram
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ==========================
# 설정
# ==========================

TEST_DAY = 2  # 숫자 넣으면 해당 날짜로 테스트 (예: 28)

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

KIND_URL = "https://kind.krx.co.kr/listinvstg/pubofrschdl.do?method=searchPubofrScholMain"
EVENT_TYPES = ["상장", "청약", "수요예측", "IR", "납입"]
HISTORY_FILE = "history.json"

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        logging.error(f"not found history file : {HISTORY_FILE}")
        return []

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        logging.error(f"history file load failed: {e}")
        return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def record_prediction(name, result):
    history = load_history()

    today = get_today().strftime("%Y-%m-%d")

    # 🔥 중복 방지 (같은 날짜 + 종목)
    for item in history:
        if item["date"] == today and item["name"] == name:
            return

    history.append({
        "date": today,
        "name": name,
        "predicted": result["expected_return"],
        "ratio": result["open_ratio"],
        "actual": None
    })

    save_history(history)


# ==========================
# Selenium 크롤링
# ==========================
def fetch_calendar():
    options = Options()
    options.add_argument("--headless")

    driver = webdriver.Chrome(options=options)
    driver.get(KIND_URL)

    # table 로딩 기다림
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
    )

    elements = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

    raw = []
    for el in elements:
        text = el.text.strip()
        if text:
            raw.append(text.split("\n"))

    driver.quit()
    return raw


# ==========================
# 날짜 유틸 (테스트 포함)
# ==========================
def get_today():
    kst = ZoneInfo("Asia/Seoul")

    if TEST_DAY:
        now = datetime.now(kst)
        today = now.replace(day=TEST_DAY).date()
    else:
        today = datetime.now(kst).date()
    return today


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


def filter_today(parsed):
    today = get_today().day
    #candidates = [today, today - 1, today + 1]
    candidates = [today]
    result = [x for x in parsed if x["date"] in candidates]

    logging.info(f"선택된 날짜: {today}, 후보: {candidates}, 결과: {len(result)}")
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
        logging.error(f"상세페이지 실패: {e}")
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

def fetch_38_all():
    url = "http://www.38.co.kr/html/fund/index.htm?o=k"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")

    data = {}

    for row in soup.select("table tr"):
        cols = row.find_all("td")
        if not cols:
            continue

        name = cols[0].get_text(strip=True).replace(" ", "")
        text = " ".join([c.get_text(strip=True) for c in cols])

        data[name] = {
            "text": text,
            "cols": cols
        }

    return data


def get_38_info(company, all_38):
    if company in cache_38:
        return cache_38[company]

    try:
        target = normalize_spac_name(company)

        item = all_38.get(target)
        if not item:
            return None

        text = item["text"]
        cols = item["cols"]

        logging.debug(f"[38 매칭] {target}")

        # 증권사
        brokers = extract_underwriters(text)

        # 상세 링크
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

        # 가격 정보
        price_info = parse_price_info_from_text(text)

        # 경쟁률
        competition = 0.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?::|대)\s*1", text)
        if match:
            competition = float(match.group(1))

        # 상세
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
    score += min(math.log10(comp + 1) * 20, 50)

    # 확약 (최대 25점)
    score += lock * 0.4

    # 유통물량 (감점)
    score -= float_ratio * 0.15

    # 공모가 위치
    if pos == "초과":
        score += 20
    elif pos == "상단":
        score += 12
    elif pos == "밴드내":
        score += 0
    elif pos == "하단":
        score -= 15

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
    pos = info.get("price_position", "미확인")

    # ==========================
    # 1. 공모가 기반 (핵심)
    # ==========================
    if pos == "초과":
        base = 1.9
    elif pos == "상단":
        base = 1.6
    elif pos == "밴드내":
        base = 1.3
    else:
        base = 1.1

    # ==========================
    # 2. 확약 (핵심)
    # ==========================
    lock_adj = (lock / 100) * 0.6   # 최대 +0.6

    # ==========================
    # 3. 경쟁률 (보조)
    # ==========================
    comp_adj = min(math.log10(comp + 1) * 0.3, 0.5)

    # ==========================
    # 4. 최종 ratio
    # ==========================
    ratio = base + lock_adj + comp_adj

    # 🔥 추가 보정 (중요)
    if pos == "상단" and lock < 10:
        ratio -= 0.2

    if comp > 1500 and lock > 50:
        ratio += 0.1

    ratio = min(ratio, 2.3)

    expected_return = int((ratio - 1) * 100)

    # ==========================
    # 5. 확률 재계산
    # ==========================
    if ratio >= 2.0:
        tt, db, fail = 70, 25, 5
    elif ratio >= 1.6:
        tt, db, fail = 50, 40, 10
    elif ratio >= 1.3:
        tt, db, fail = 30, 50, 20
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
# 메시지 생성
# ==========================

def grade(score):
    if score >= 80:
        return "S 등급"
    elif score >= 60:
        return "A 등급"
    elif score >= 40:
        return "B 등급"
    else:
        return "C 등급"


def get_sell_strategy(name, info, result):
    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)
    score = result.get("score", 0)

    # 따상 유력
    if is_ttasang_candidate(name, info, result) and "스팩" not in name:
        return "따상 홀딩 (장초반 관망 후 +80% 이상 매도)"

    # 표준
    if score >= 50:
        return "분할매도 (시초가 50% + 추가상승 매도)"

    # 약한 종목
    return "시초가 매도"


def format_company_block(name, info, result, is_listing=False):
    tag = "📈 상장" if is_listing else "📝 청약"

    line = f"{tag} | {name}\n"
    line += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    if is_listing:
        line += f"- 예상 수익: +{result['expected_return']}% (x{result['open_ratio']})\n"
        line += f"- 따상 확률: {result['ttasang']}%\n"
        line += f"- 매도전략: {get_sell_strategy(name, info, result)}\n"
    else:
        g = grade(result['score'])
        line += f"- 점수: {result['score']} ({g})\n"

    if info.get("competition", 0) == 0:
        line += "- 경쟁률: 데이터 없음\n"
    else:
        line += f"- 경쟁률: {info['competition']}:1\n"

    line += f"- 공모가: {info['offer_price']:,}원\n"
    line += f"- 유통 {info['float']}% / 확약 {info['lockup']}%\n"

    if info.get("brokers"):
        line += f"- {', '.join(info['brokers'])}\n"

    if info.get("warning"):
        line += "- ⚠️ 일부 데이터 누락\n"

    return line + "\n\n"


def is_ttasang_candidate(name, info, result):
    if "스팩" in name:
        return False

    comp = info.get("competition", 0)
    lock = info.get("lockup", 0)

    return (
        comp >= 800 and
        lock >= 40 and
        result.get("score", 0) >= 60
    )


def build_message(data):
    all_38 = fetch_38_all()
    
    priority = {
        "상장": 3,
        "청약": 2,
        "수요예측": 1
    }

    data.sort(key=lambda x: priority.get(x["event"], 0), reverse=True)

    today = get_today().strftime("%Y-%m-%d")

    if not data:
        return f"📭 {today} 공모 일정 없음"

    msg = f"📊 {today} 공모주 일정\n\n"

    listings = []
    subscriptions = []
    spac_list = []
    hot_list = []

    for item in data:
        name = item["company"]
        event = item["event"]

        info = get_38_info(name, all_38)
        if not info:
            logging.warning(f"38 데이터 없음: {name}")
            info = DEFAULT_38_INFO.copy()
            info["warning"] = True
            
        if "스팩" in name:
            spac_list.append((name, info))
            continue

        result = analyze_ipo(name, info)

        if "스팩" not in name and is_ttasang_candidate(name, info, result):
            hot_list.append((name, info, result, event))

        if event == "상장":
            listings.append((name, info, result))
        elif event == "청약":
            record_prediction(name, result)
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
        #msg += "📝 청약 종목\n==============================\n"
        for name, info, result in subscriptions:
            msg += format_company_block(name, info, result, is_listing=False) + "\n"

    # ==========================
    # 상장
    # ==========================
    if listings:
        #msg += "📈 상장 종목\n==============================\n"
        for name, info, result in listings:
            msg += format_company_block(name, info, result, is_listing=True) + "\n"

    # ==========================
    # 스팩 (참고용)
    # ==========================
    if spac_list:
        for name, info in spac_list:
            msg += f"🧾 스팩 | {name}\n"
            msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            if info.get("competition", 0) == 0:
                msg += "- 경쟁률: 데이터 없음\n"
            else:
                msg += f"- 경쟁률: {info['competition']}\n"

            if info.get("brokers"):
                msg += f"- {', '.join(info['brokers'])}\n"

            msg += "\n"

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
    today = get_today()

    # 주말 포함 자동 체크
    return today in kr_holidays or today.weekday() >= 5


# ==========================
# 실행
# ==========================
def main():
    try:
        if is_market_holiday():
            logging.info("휴장일이라 실행 안함")
            return

        raw = fetch_calendar()

        if not raw:
            raise Exception("KIND 데이터 없음")

        parsed = parse_calendar(raw)
        today_data = filter_today(parsed)

        msg = build_message(today_data)
        logging.info(msg)

        asyncio.run(send(msg))
    except Exception as e:
        logging.exception("전체 실행 실패")

        err_msg = f"❌ 봇 실행 오류\n{str(e)}"
        # asyncio.run(send(err_msg))


if __name__ == "__main__":
    main()
