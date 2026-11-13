#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lowprice_bot - 물티슈 최저가 알림 봇 (카카오톡 '나에게 보내기' 연동)

요구사항:
1. 네이버 쇼핑 검색 스킬(Open API / 공개 BFF 엔드포인트) 방식으로 "베베앙 20개"를 최저가 순으로 검색
2. 최저가 순서로 상품 3개를 상품명, 가격, 쇼핑몰, 리뷰수, 링크와 함께 화면에 보기 좋게 출력
3. 반환된 광고 상품은 제거하고, 진짜(일반) 상품만 필터링
4. 1위 가격이 목표가 20,000원 이하인지 확인 후 아래 문구 중 하나를 출력
   - 목표가 이하일 때: "[특가발견] 목표 가격 이하입니다!" + 카카오톡 '나에게 보내기' 알림 발송
   - 목표가 초과일 때: "[유지] 아직 목표 가격보다 비싸요!" (화면에만 출력, 카카오톡 미발송)
5. 잘못된 값을 가져오거나 카카오톡 알림을 못 보냈을 경우 지어내거나 보낸 척하지 말고,
   무엇이 잘못되었는지 그대로 알려주고 멈춤
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# Windows 콘솔 한글 깨짐 방지
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_QUERY = "베베앙 20개"
TARGET_PRICE = 20_000
TOP_N = 3
ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class BotDataError(Exception):
    """잘못된 응답, 유효하지 않은 데이터, 또는 알림 발송 실패 시 발생시키는 예외."""


def load_env_file(env_path: Path = ENV_FILE_PATH) -> Dict[str, str]:
    """
    프로젝트 폴더의 .env 파일에서 환경변수 키-값을 읽어옵니다.
    코드에 키 값을 직접 하드코딩하지 않고 파일에서 로드합니다.
    """
    if not env_path.is_file():
        raise BotDataError(f".env 파일을 찾을 수 없습니다 (경로: {env_path})")

    try:
        raw_text = env_path.read_text(encoding="utf-8")
    except Exception as e:
        raise BotDataError(f".env 파일을 읽는 중 오류가 발생했습니다: {e}") from e

    env_map: Dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        env_map[key.strip()] = val.strip().strip('"').strip("'")

    return env_map


def update_env_refresh_token(new_refresh_token: str, env_path: Path = ENV_FILE_PATH) -> None:
    """카카오 토큰 갱신 시 새 refresh_token이 반환되면 .env 파일에 업데이트합니다."""
    if not env_path.is_file():
        return
    try:
        raw_text = env_path.read_text(encoding="utf-8")
        lines = raw_text.splitlines()
        updated = False
        new_lines: List[str] = []
        for line in lines:
            if line.strip().startswith("KAKAO_REFRESH_TOKEN="):
                new_lines.append(f"KAKAO_REFRESH_TOKEN={new_refresh_token}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"KAKAO_REFRESH_TOKEN={new_refresh_token}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def get_kakao_access_token(env_path: Path = ENV_FILE_PATH) -> str:
    """
    .env 파일 또는 환경변수(GitHub Actions Secrets 등)의
    KAKAO_REST_API_KEY, KAKAO_CLIENT_SECRET, KAKAO_REFRESH_TOKEN을 사용해
    카카오 OAuth 서버에서 액세스 토큰(access_token)을 발급받습니다.
    """
    env_vars: Dict[str, str] = {}
    if env_path.is_file():
        env_vars = load_env_file(env_path)

    rest_api_key = (
        env_vars.get("KAKAO_REST_API_KEY")
        or env_vars.get("REST_API_KEY")
        or os.environ.get("KAKAO_REST_API_KEY")
        or os.environ.get("REST_API_KEY")
    )
    client_secret = (
        env_vars.get("KAKAO_CLIENT_SECRET")
        or env_vars.get("CLIENT_SECRET")
        or os.environ.get("KAKAO_CLIENT_SECRET")
        or os.environ.get("CLIENT_SECRET")
    )
    refresh_token = (
        env_vars.get("KAKAO_REFRESH_TOKEN")
        or env_vars.get("REFRESH_TOKEN")
        or os.environ.get("KAKAO_REFRESH_TOKEN")
        or os.environ.get("REFRESH_TOKEN")
    )

    missing = []
    if not rest_api_key:
        missing.append("KAKAO_REST_API_KEY")
    if not client_secret:
        missing.append("KAKAO_CLIENT_SECRET")
    if not refresh_token:
        missing.append("KAKAO_REFRESH_TOKEN")

    if missing:
        raise BotDataError(
            f".env 파일에 카카오 인증 필수 항목이 누락되어 있습니다: {', '.join(missing)}"
        )

    token_url = "https://kauth.kakao.com/oauth/token"
    payload = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    data_bytes = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=data_bytes,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise BotDataError(
            f"카카오 액세스 토큰 갱신 실패 (HTTP {e.code}): {err_body or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise BotDataError(f"카카오 인증 서버 네트워크 연결 실패: {e.reason}") from e

    if status != 200:
        raise BotDataError(f"카카오 인증 서버가 비정상 상태 코드를 반환했습니다: HTTP {status}")

    try:
        token_info = json.loads(body)
    except json.JSONDecodeError as e:
        raise BotDataError(f"카카오 토큰 응답을 JSON으로 파싱할 수 없습니다: {e}") from e

    access_token = token_info.get("access_token")
    if not access_token:
        raise BotDataError(f"카카오 토큰 응답에 access_token이 없습니다: {token_info}")

    # 리프레시 토큰이 함께 갱신된 경우 .env 파일에 반영
    if token_info.get("refresh_token"):
        update_env_refresh_token(str(token_info["refresh_token"]), env_path)

    return str(access_token)


def send_kakao_alert(item: Dict[str, Any], env_path: Path = ENV_FILE_PATH) -> None:
    """
    1위 상품이 목표가 이하일 때 카카오톡 '나에게 보내기'로 알림을 전송합니다.
    - 포함 정보: 상품명, 가격, 쇼핑몰 이름, 리뷰 수
    - 버튼 설정: '구매하러 가기' 버튼에 상품 링크 연결
    - 실패 시: 보낸 척하지 않고 BotDataError 예외를 통해 실패 원인을 그대로 전달
    """
    access_token = get_kakao_access_token(env_path)

    message_text = (
        "[특가발견] 목표 가격 이하입니다!\n\n"
        f"• 상품명: {item['title']}\n"
        f"• 가격: {item['price']:,}원\n"
        f"• 쇼핑몰: {item['mall']}\n"
        f"• 리뷰수: {item['review_count']}"
    )

    template_object = {
        "object_type": "text",
        "text": message_text,
        "link": {
            "web_url": item["link"],
            "mobile_web_url": item["link"],
        },
        "button_title": "구매하러 가기",
    }

    send_url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    data_bytes = urllib.parse.urlencode(
        {"template_object": json.dumps(template_object, ensure_ascii=False)}
    ).encode("utf-8")

    req = urllib.request.Request(
        send_url,
        data=data_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise BotDataError(
            f"카카오톡 '나에게 보내기' API 호출 실패 (HTTP {e.code}): {err_body or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise BotDataError(f"카카오톡 API 네트워크 연결 실패: {e.reason}") from e

    if status != 200:
        raise BotDataError(f"카카오톡 API가 비정상 상태 코드를 반환했습니다: HTTP {status}")

    try:
        result = json.loads(body)
    except json.JSONDecodeError as e:
        raise BotDataError(f"카카오톡 API 응답을 JSON으로 파싱할 수 없습니다: {e}") from e

    if result.get("result_code") != 0:
        raise BotDataError(
            f"카카오톡 메시지 전송이 실패 결과 코드를 반환했습니다 (result_code={result.get('result_code')}): {result}"
        )


def strip_html_tags(text: str) -> str:
    """HTML 태그 및 엔티티를 제거하여 순수 텍스트로 변환합니다."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", "", str(text))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_int(value: Any) -> Optional[int]:
    """문자열 또는 숫자에서 정수 값을 안전하게 추출합니다."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def is_ad_item(raw_item: Dict[str, Any]) -> bool:
    """
    광고 상품 여부를 판별합니다.
    - 광고 플래그(isAd, ad, sponsored 등)가 참이거나
    - 광고 식별자(adId, adProductId 등)가 존재하거나
    - 링크가 광고 클릭 리다이렉트(adcr.naver.com / adcrUrl)인 경우 광고로 판정합니다.
    """
    ad_bool_keys = ("isAd", "is_ad", "ad", "sponsored", "isSponsored", "adYn")
    for key in ad_bool_keys:
        val = raw_item.get(key)
        if isinstance(val, bool) and val:
            return True
        if isinstance(val, str) and val.strip().upper() in ("Y", "TRUE", "1", "AD"):
            return True

    ad_id_keys = ("adId", "ad_id", "adProductId", "adcrUrl", "adcr_url", "adClickUrl")
    for key in ad_id_keys:
        val = raw_item.get(key)
        if val not in (None, "", 0, False):
            return True

    link = str(raw_item.get("link") or raw_item.get("url") or raw_item.get("productUrl") or "")
    if "adcr.naver.com" in link or "adcrUrl" in link:
        return True

    badge = str(raw_item.get("badge") or raw_item.get("type") or raw_item.get("slotType") or "")
    if "광고" in badge or badge.lower() in ("ad", "sponsored"):
        return True

    return False


def validate_and_normalize_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    상품 데이터의 필수 필드를 검증하고 표준 구조로 정규화합니다.
    필수값(상품명, 가격, 링크)이 잘못되었을 경우 지어내지 않고 예외를 발생시킵니다.
    """
    title = strip_html_tags(
        raw.get("title")
        or raw.get("productTitle")
        or raw.get("productName")
        or raw.get("name")
        or ""
    )
    price = parse_int(
        raw.get("lprice")
        or raw.get("price")
        or raw.get("lowPrice")
        or raw.get("salePrice")
        or raw.get("total_price")
    )
    mall = strip_html_tags(
        raw.get("mallName")
        or raw.get("mall_name")
        or raw.get("mall")
        or raw.get("shopName")
        or raw.get("mall_text")
        or ""
    )
    review_count = parse_int(
        raw.get("reviewCount")
        or raw.get("review_count")
        or raw.get("reviewCnt")
        or raw.get("commentCount")
    )
    link = str(
        raw.get("link")
        or raw.get("productUrl")
        or raw.get("mallProductUrl")
        or raw.get("url")
        or ""
    ).strip()

    if not title:
        raise BotDataError(f"상품명이 비어 있는 잘못된 데이터가 포함되어 있습니다: {raw}")
    if price is None or price <= 0:
        raise BotDataError(
            f"상품 '{title}'의 가격 값이 유효하지 않습니다 (수신된 값: {raw.get('price') or raw.get('lprice')})."
        )
    if not link or not link.startswith(("http://", "https://")):
        raise BotDataError(f"상품 '{title}'의 링크가 유효하지 않습니다 (수신된 값: {link!r}).")

    return {
        "title": title,
        "price": price,
        "mall": mall if mall else "네이버쇼핑(모음전)",
        "review_count": f"{review_count:,}개" if review_count is not None else "제공 안 됨(API 미포함)",
        "link": link,
    }


def fetch_via_naver_open_api(query: str, client_id: str, client_secret: str) -> List[Dict[str, Any]]:
    """
    1순위: 네이버 검색 Open API (shop.json) 호출
    - sort=asc (최저가순)
    """
    encoded_query = urllib.parse.quote(query)
    api_url = f"https://openapi.naver.com/v1/search/shop.json?query={encoded_query}&display=30&start=1&sort=asc"
    req = urllib.request.Request(
        api_url,
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise BotDataError(
            f"네이버 검색 Open API 호출 실패 (HTTP {e.code}): {err_body or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise BotDataError(f"네이버 검색 Open API 네트워크 연결 실패: {e.reason}") from e

    if status != 200:
        raise BotDataError(f"네이버 검색 Open API가 비정상 상태 코드를 반환했습니다: HTTP {status}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise BotDataError(f"네이버 검색 Open API 응답을 JSON으로 파싱할 수 없습니다: {e}") from e

    items = payload.get("items")
    if not isinstance(items, list):
        raise BotDataError(f"네이버 검색 Open API 응답에 'items' 리스트가 없습니다: {payload}")

    return items


def fetch_via_naver_bff(query: str) -> List[Dict[str, Any]]:
    """
    2순위: 네이버 쇼핑 공개 BFF JSON 엔드포인트 (shopping-paged-slot) 호출
    - sort=price_asc (최저가순)
    """
    params = urllib.parse.urlencode(
        {
            "query": query,
            "sort": "price_asc",
            "pagingIndex": 1,
            "pagingSize": 40,
        }
    )
    bff_url = f"https://ns-portal.shopping.naver.com/api/v2/shopping-paged-slot?{params}"
    req = urllib.request.Request(
        bff_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"https://search.shopping.naver.com/search/all?query={urllib.parse.quote(query)}&sort=price_asc",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise BotDataError(
            f"네이버 쇼핑 BFF 엔드포인트 호출 실패 (HTTP {e.code}): {err_body[:200] or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise BotDataError(f"네이버 쇼핑 BFF 엔드포인트 네트워크 연결 실패: {e.reason}") from e

    if status != 200:
        raise BotDataError(f"네이버 쇼핑 BFF 엔드포인트가 비정상 상태 코드를 반환했습니다: HTTP {status}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise BotDataError(
            f"네이버 쇼핑 BFF 응답이 유효한 JSON이 아닙니다 (차단 또는 포맷 변경 가능성): {e}"
        ) from e

    candidates: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in ("items", "products", "compositeList", "data", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                for entry in val:
                    if isinstance(entry, dict):
                        item = entry.get("item", entry)
                        if isinstance(item, dict):
                            candidates.append(item)
            elif isinstance(val, dict):
                sub_list = val.get("list") or val.get("items") or val.get("products")
                if isinstance(sub_list, list):
                    for entry in sub_list:
                        if isinstance(entry, dict):
                            item = entry.get("item", entry)
                            if isinstance(item, dict):
                                candidates.append(item)

    if not candidates:
        raise BotDataError(
            "네이버 쇼핑 BFF 응답에서 상품 목록을 찾을 수 없습니다 (응답 스키마 불일치 또는 빈 결과)."
        )

    return candidates


def fetch_via_kskill_cli(query: str) -> Optional[List[Dict[str, Any]]]:
    """
    시스템에 npx 및 k-skill CLI가 설치되어 있는 경우 스킬 명령어를 호출합니다.
    """
    cmd = [
        "npx",
        "-y",
        "@nomadamas/k-skill@0",
        "exec",
        "naver-shopping-search",
        "scripts/naver_shopping_search.py",
        "--",
        "search",
        query,
        "--sort",
        "price_asc",
        "--limit",
        "20",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            shell=(os.name == "nt"),
        )
    except Exception:
        return None

    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    return None


def search_lowest_price_products(query: str) -> List[Dict[str, Any]]:
    """
    네이버 쇼핑 검색 스킬 방식에 따라 상품을 조회하고,
    광고 상품을 제외한 뒤 최저가 오름차순으로 정렬하여 반환합니다.
    """
    errors: List[str] = []
    raw_items: Optional[List[Dict[str, Any]]] = None

    # .env 파일이 있으면 네이버 API 키도 함께 참조 가능하도록 로드
    env_vars: Dict[str, str] = {}
    if ENV_FILE_PATH.is_file():
        try:
            env_vars = load_env_file(ENV_FILE_PATH)
        except BotDataError:
            env_vars = {}

    # 1. k-skill CLI 시도
    cli_items = fetch_via_kskill_cli(query)
    if cli_items is not None:
        raw_items = cli_items

    # 2. 네이버 검색 Open API 키가 환경변수 또는 .env에 설정된 경우
    if raw_items is None:
        client_id = (
            os.environ.get("NAVER_SEARCH_CLIENT_ID")
            or os.environ.get("NAVER_CLIENT_ID")
            or env_vars.get("NAVER_SEARCH_CLIENT_ID")
            or env_vars.get("NAVER_CLIENT_ID")
        )
        client_secret = (
            os.environ.get("NAVER_SEARCH_CLIENT_SECRET")
            or os.environ.get("NAVER_CLIENT_SECRET")
            or env_vars.get("NAVER_SEARCH_CLIENT_SECRET")
            or env_vars.get("NAVER_CLIENT_SECRET")
        )
        if client_id and client_secret:
            try:
                raw_items = fetch_via_naver_open_api(query, client_id, client_secret)
            except BotDataError as e:
                errors.append(str(e))
        else:
            errors.append(
                "환경변수 NAVER_SEARCH_CLIENT_ID / NAVER_SEARCH_CLIENT_SECRET 이 설정되지 않아 공식 Open API를 건너뛰었습니다."
            )

    # 3. 키 없이 호출 가능한 네이버 쇼핑 공개 BFF 엔드포인트 시도
    if raw_items is None:
        try:
            raw_items = fetch_via_naver_bff(query)
        except BotDataError as e:
            errors.append(str(e))

    if not raw_items:
        error_detail = "\n  - ".join(errors) if errors else "알 수 없는 이유로 응답이 비어 있습니다."
        raise BotDataError(f"'{query}' 검색 결과를 가져오지 못했습니다.\n  - {error_detail}")

    # 요구사항 3: 반환된 광고 상품은 제거하고 진짜 상품만 필터링
    organic_raw_items = [item for item in raw_items if not is_ad_item(item)]
    if not organic_raw_items:
        raise BotDataError(
            f"검색된 상품 {len(raw_items)}개가 모두 광고 상품이거나 유효한 일반 상품이 없습니다."
        )

    # 데이터 검증 및 정규화 (잘못된 값이면 즉시 BotDataError 발생)
    normalized_items: List[Dict[str, Any]] = []
    for raw in organic_raw_items:
        normalized_items.append(validate_and_normalize_item(raw))

    # 요구사항 1 & 2: 최저가 순서(오름차순)로 정렬
    normalized_items.sort(key=lambda x: x["price"])

    if len(normalized_items) < TOP_N:
        raise BotDataError(
            f"광고 제거 후 유효한 상품 수가 {len(normalized_items)}개로, 요청된 {TOP_N}개보다 적습니다."
        )

    return normalized_items[:TOP_N]


def print_results(query: str, top_items: List[Dict[str, Any]], target_price: int) -> None:
    """
    요구사항 2 & 4:
    - 최저가 순서로 상품 3개를 상품명, 가격, 쇼핑몰, 리뷰수, 링크와 함께 보기 좋게 출력
    - 1위 가격과 목표가(20,000원)를 비교하여 상태 메시지 출력
    """
    print("=" * 70)
    print(f"🛒 네이버 쇼핑 최저가 검색 결과 (키워드: '{query}' / 광고 제외 상위 {len(top_items)}개)")
    print("=" * 70)

    for rank, item in enumerate(top_items, start=1):
        print(f"[{rank}위] {item['title']}")
        print(f"  • 가격   : {item['price']:,}원")
        print(f"  • 쇼핑몰 : {item['mall']}")
        print(f"  • 리뷰수 : {item['review_count']}")
        print(f"  • 링크   : {item['link']}")
        print("-" * 70)

    first_price = top_items[0]["price"]
    print(f"📌 1위 최저가: {first_price:,}원 (목표가: {target_price:,}원)")

    # 요구사항 4: 목표가 20,000원 이하 여부 판별
    if first_price <= target_price:
        print("[특가발견] 목표 가격 이하입니다!")
    else:
        print("[유지] 아직 목표 가격보다 비싸요!")
    print("=" * 70)


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    try:
        top_items = search_lowest_price_products(query)
        print_results(query, top_items, TARGET_PRICE)
    except BotDataError as err:
        # 요구사항 5: 잘못된 값을 가져올 경우 지어내지 말고, 무엇이 잘못되었는지 그대로 알려주고 멈춤
        print("❌ [오류 - 실행 중단] 잘못된 값을 가져왔거나 데이터를 정상적으로 조회할 수 없습니다.", file=sys.stderr)
        print(f"원인: {err}", file=sys.stderr)
        return 1
    except Exception as unexpected_err:
        print("❌ [오류 - 실행 중단] 예기치 못한 오류가 발생하여 실행을 멈춥니다.", file=sys.stderr)
        print(f"원인: {type(unexpected_err).__name__}: {unexpected_err}", file=sys.stderr)
        return 1

    # 카카오톡 알림: 1위 가격이 목표가 이하일 때만 전송 (목표가 초과 시 화면에만 출력하고 미전송)
    first_item = top_items[0]
    if first_item["price"] <= TARGET_PRICE:
        try:
            send_kakao_alert(first_item)
            print("📨 카카오톡 '나에게 보내기' 알림 전송 완료!")
        except BotDataError as kakao_err:
            # 알림을 못 보냈으면 보낸 척하지 않고 무엇이 잘못되었는지 그대로 알려줌
            print("❌ [카카오톡 알림 전송 실패] 카카오톡 알림을 보내지 못했습니다.", file=sys.stderr)
            print(f"원인: {kakao_err}", file=sys.stderr)
            return 1
        except Exception as unexpected_kakao_err:
            print("❌ [카카오톡 알림 전송 실패] 예기치 못한 오류로 카카오톡 알림을 보내지 못했습니다.", file=sys.stderr)
            print(f"원인: {type(unexpected_kakao_err).__name__}: {unexpected_kakao_err}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
