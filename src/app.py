"""
ezadmin - Portfolio Dashboard (FastAPI 포팅 버전)
ezgain/ezinvest/ezsplit/ezadmin 의 포트폴리오 계좌별 보유종목/잔고를 조회하는 웹 대시보드.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from werkzeug.security import check_password_hash

from config_loader import load_all_portfolios, KNOWN_OWNERS
from db import init_db, upsert_today, get_recent_snapshots
from market_index import get_all_indices
from kis_client import (get_domestic_balance, get_overseas_balance,
                        get_domestic_today_realized_pl,
                        get_overseas_today_realized_pl,
                        get_pending_orders, get_pending_orders_overseas,
                        get_today_trades_domestic, get_today_trades_overseas,
                        place_buy_order, place_buy_order_overseas,
                        place_sell_order, place_sell_order_overseas,
                        get_ask_price_domestic, get_ask_price_overseas,
                        get_orderbook_domestic, get_orderbook_overseas,
                        get_daily_chart_domestic, get_daily_chart_overseas,
                        cancel_order, cancel_order_overseas)
import kw_client
import upbit_client

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """프로젝트 루트의 .env 파일을 로드 (Flask 버전과 동일)."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


_load_dotenv()

app = FastAPI(title="ezadmin", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory=os.path.join(PROJECT_ROOT, "templates"))

_static_dir = os.path.join(PROJECT_ROOT, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

init_db(PROJECT_ROOT)

# 인증 활성 여부: 프로젝트 루트의 .env 파일 존재 시에만 동작.
#   - .env 있음 → 기존처럼 LAN 면제 + WAN JWT 쿠키 인증
#   - .env 없음 → 모든 요청 통과 (개발/내부망 한정 사용)
AUTH_ENABLED = os.path.exists(os.path.join(PROJECT_ROOT, ".env"))
templates.env.globals["auth_enabled"] = AUTH_ENABLED

AUTH_USERNAME = os.environ.get("WEB_AUTH_USER", "")
AUTH_PASSWORD_HASH = os.environ.get("WEB_AUTH_PASSWORD_HASH", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

SESSION_COOKIE = "ezadmin_session"
SESSION_TTL = 24 * 60 * 60        # 24시간
SESSION_REFRESH_THRESHOLD = 12 * 60 * 60   # 만료까지 12시간 미만이면 토큰 재발급
# 인증 면제 API 엔드포인트 (web frontend 외 API 서버 용도)
API_PATH_SUFFIXES = ("/sell", "/cancel", "/askprice")
API_PATH_EXACT = ("/reload",)


# ─────────────────────────────────────────────────
# 요청 컨텍스트 헬퍼 (Starlette Request 기반)
# ─────────────────────────────────────────────────
def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return (request.client.host if request.client else "") or ""


def _is_lan(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _is_https(request: Request) -> bool:
    if TRUST_PROXY:
        return request.headers.get("x-forwarded-proto", "").lower() == "https"
    return (request.url.scheme or "").lower() == "https"


def _session_secret() -> str:
    return os.environ.get("WEB_AUTH_SECRET") or AUTH_PASSWORD_HASH


# ─────────────────────────────────────────────────
# JWT (HS256) — Flask 버전과 동일 포맷
# ─────────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_encode(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def _jwt_decode(token: str, secret: str):
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    h_b64, p_b64, s_b64 = parts
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        got = _b64url_decode(s_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(_b64url_decode(p_b64))
    except (ValueError, TypeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return payload


def _is_api_path(path: str) -> bool:
    if path in API_PATH_EXACT:
        return True
    return any(path.endswith(suf) for suf in API_PATH_SUFFIXES)


def _set_session_cookie(response: Response, token: str, max_age: int, secure: bool):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


# ─────────────────────────────────────────────────
# 인증 미들웨어 (before/after_request 등가)
# ─────────────────────────────────────────────────
@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """세션 검증 + 임계값 갱신을 한 미들웨어에서 처리."""
    # .env 파일이 없으면 인증 자체를 비활성화 — 모든 요청 그대로 통과
    if not AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path
    public = (
        path in ("/login", "/logout")
        or path.startswith("/static/")
        or _is_api_path(path)
    )

    new_refresh_token = None
    if not public and not _is_lan(_client_ip(request)):
        if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
            return Response(
                "외부 접근이 차단되어 있습니다. .env의 WEB_AUTH_USER / WEB_AUTH_PASSWORD_HASH를 설정하세요.",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        secret = _session_secret()
        if not secret:
            return Response(
                "서버 인증 설정 오류: WEB_AUTH_SECRET 또는 WEB_AUTH_PASSWORD_HASH 필요.",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )

        token = request.cookies.get(SESSION_COOKIE, "")
        payload = _jwt_decode(token, secret)
        if payload and payload.get("sub"):
            exp = payload.get("exp", 0)
            now = int(time.time())
            if isinstance(exp, (int, float)) and exp - now < SESSION_REFRESH_THRESHOLD:
                new_refresh_token = _jwt_encode(
                    {"sub": payload["sub"], "iat": now, "exp": now + SESSION_TTL},
                    secret,
                )
        else:
            qs = request.url.query
            next_url = f"{path}?{qs}" if qs else path
            return RedirectResponse(f"/login?next={quote(next_url)}", status_code=302)

    response = await call_next(request)
    if new_refresh_token:
        _set_session_cookie(response, new_refresh_token, SESSION_TTL, _is_https(request))
    return response


# ─────────────────────────────────────────────────
# 로그인 / 로그아웃
# ─────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    next_url = next.strip()
    if not next_url.startswith("/"):
        next_url = "/"
    return templates.TemplateResponse(
        request, "login.html",
        {"error": None, "next_url": next_url},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    next: str = Form("/"),
    username: str = Form(""),
    password: str = Form(""),
):
    next_url = (next or "/").strip()
    if not next_url.startswith("/"):
        next_url = "/"

    def _err(msg: str, status: int):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": msg, "next_url": next_url},
            status_code=status,
        )

    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        return _err("서버 인증이 설정되지 않았습니다.", 503)

    user_ok = hmac.compare_digest(username.strip(), AUTH_USERNAME)
    try:
        pass_ok = check_password_hash(AUTH_PASSWORD_HASH, password)
    except (ValueError, TypeError):
        pass_ok = False
    if not (user_ok and pass_ok):
        return _err("아이디 또는 비밀번호가 올바르지 않습니다.", 401)

    secret = _session_secret()
    if not secret:
        return _err("서버 설정 오류 (WEB_AUTH_SECRET).", 503)

    now = int(time.time())
    token = _jwt_encode({"sub": username.strip(), "iat": now, "exp": now + SESSION_TTL}, secret)

    # POST → GET redirect 는 303 see other
    resp = RedirectResponse(next_url, status_code=303)
    _set_session_cookie(resp, token, SESSION_TTL, _is_https(request))
    return resp


@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    _set_session_cookie(resp, "", 0, _is_https(request))
    return resp


# ─────────────────────────────────────────────────
# 포트폴리오 데이터 캐시
# ─────────────────────────────────────────────────
_portfolios = None
_summary_cache = {}     # name -> (timestamp, summary_dict)
SUMMARY_TTL = 60


def _get_portfolios():
    global _portfolios
    if _portfolios is None:
        _portfolios = load_all_portfolios()
    return _portfolios


def _fetch_balance(pf):
    """broker 별 잔고 조회 디스패치."""
    broker = pf.get("broker", "kis")
    acct_name = pf.get("account_config_name", "")
    is_us = pf["market"] == "us"
    if broker == "upbit":
        fn = upbit_client.get_balance
    elif broker == "kw":
        fn = kw_client.get_overseas_balance if is_us else kw_client.get_domestic_balance
    else:
        fn = get_overseas_balance if is_us else get_domestic_balance
    return fn(pf["account_cfg"], pf["project_root"], acct_name)


def _rebal_alert(pf, holdings):
    """ezgain 자산배분/active 카테고리에서 비중차이 절대값이
    yaml rebalancing.tolerance 의 70% 이상이면 True (리밸런싱 임박 알림)."""
    sub = _ezgain_subcat(pf)
    if sub not in ("자산배분", "active"):
        return False
    rb = (pf.get("portfolio_cfg") or {}).get("rebalancing") or {}
    try:
        tol = float(rb.get("tolerance", 0) or 0)
    except (TypeError, ValueError):
        tol = 0.0
    if tol <= 0:
        return False
    threshold_pct = tol * 100 * 0.7

    universe = (pf.get("portfolio_cfg") or {}).get("universe") or {}
    universe_codes = {
        code for code, info in universe.items()
        if isinstance(info, dict) and float(info.get("weight", 0) or 0) > 0
    }
    total_target_weight = sum(
        float(v.get("weight", 0)) for v in universe.values()
        if isinstance(v, dict)
    )
    # universe 가 정의되어 있는데 universe 종목을 하나도 보유하지 않은 경우 (미진입)
    if universe_codes:
        held_codes = {h.get("종목코드") for h in (holdings or [])}
        if not (universe_codes & held_codes):
            return True
        # universe 에 없는 종목을 보유한 경우 (불필요 잔여 종목)
        if held_codes - universe_codes:
            return True
    if not holdings:
        return False
    total_evlu = sum(float(h.get("평가금액", 0) or 0) for h in holdings)
    if total_evlu <= 0:
        return False
    for h in holdings:
        code = h.get("종목코드")
        info = universe.get(code, {}) if isinstance(universe.get(code), dict) else {}
        raw_w = float(info.get("weight", 0))
        tgt = raw_w / total_target_weight * 100 if total_target_weight > 0 else raw_w
        actual = float(h.get("평가금액", 0) or 0) / total_evlu * 100
        if abs(actual - tgt) >= threshold_pct:
            return True
    return False


def _extract_foreign(summary, allow_cash_fallback=True):
    """
    overseas-style summary 에서 외화 평가/현금/원화환산 추출.
    summary 가 비어있거나 외화 정보가 없으면 None 반환.

    allow_cash_fallback=True: (원화총자산 - 원화총평가) / 환율 로 외화현금 보강.
        US 전용 계좌일 때만 안전. KR+US 혼합 계좌에서는 원화총자산이 KR 자산까지
        포함하므로 외화현금이 부풀려진다. 혼합계좌는 False 로 호출.
    """
    if not isinstance(summary, dict):
        return None
    foreign_evlu = float(summary.get("총평가금액") or 0)        # USD 평가
    exrt = float(summary.get("환율") or 0)
    krw_tot_v = float(summary.get("원화총자산") or 0)
    krw_evlu_v = float(summary.get("원화총평가금액") or 0)
    raw_cash = float(summary.get("외화예수금") or 0)

    # 외화현금: raw 외화예수금 우선, 0 이고 fallback 허용시 원화 차이로 역산
    if raw_cash > 0:
        foreign_cash = raw_cash
    elif allow_cash_fallback and exrt > 0 and krw_tot_v > 0 and krw_evlu_v >= 0:
        foreign_cash = max(0.0, (krw_tot_v - krw_evlu_v) / exrt)
    else:
        foreign_cash = 0.0

    foreign_total = foreign_evlu + foreign_cash
    if foreign_total <= 0:
        return None
    if exrt > 0:
        foreign_krw = foreign_total * exrt
    elif krw_tot_v > 0:
        foreign_krw = krw_tot_v
    else:
        foreign_krw = 0.0
    return {
        "외화평가금액":   foreign_evlu,
        "외화현금":       foreign_cash,
        "외화자산":       foreign_total,
        "원화환산외화자산": foreign_krw,
        "환율":           exrt,
    }


def _fetch_list_summary(pf):
    """포트폴리오 카드용 요약 (해외는 원화 환산 우선)."""
    try:
        holdings, summary = _fetch_balance(pf)
        if pf["market"] == "us":
            pchs = summary.get("원화총매수금액") or summary.get("총매수금액") or 0
            evlu = summary.get("원화총평가금액") or summary.get("총평가금액") or 0
            pnl  = summary.get("원화총손익금액") or summary.get("총손익금액") or 0
            rt   = summary.get("원화총수익률") or summary.get("총수익률") or 0
            cash = summary.get("원화예수금") or 0
            krw_tot = summary.get("원화총자산") or 0
        else:
            pchs = summary.get("총매수금액", 0) or 0
            evlu = summary.get("총평가금액", 0) or 0
            pnl  = summary.get("총손익금액", 0) or 0
            rt   = summary.get("총수익률", 0) or 0
            cash = summary.get("D+2예수금", 0) or 0
            krw_tot = 0

        today_rlz = None
        broker = pf.get("broker", "kis")
        market = pf["market"]
        today_fn = None
        if broker == "kis" and market == "kr":
            today_fn = get_domestic_today_realized_pl
        elif broker == "kis" and market == "us":
            today_fn = get_overseas_today_realized_pl
        elif broker == "kw" and market == "kr":
            today_fn = kw_client.get_domestic_today_realized_pl
        if today_fn is not None:
            try:
                today_rlz_raw = today_fn(
                    pf["account_cfg"], pf["project_root"],
                    pf.get("account_config_name", ""),
                    holdings=holdings)
                if today_rlz_raw is not None:
                    today_rlz = today_rlz_raw.get("실현손익", 0) or 0
            except Exception as e:
                print(f"[today_rlz] {pf.get('name','?')} ({broker}/{market}): {e}")
                today_rlz = None

        # 외화자산: US 계좌는 balance summary 에서, KR+KIS 계좌는 overseas balance 추가 조회
        foreign = None
        f_krw_total = 0.0  # KIS 가 보고하는 KR+외화 합산 원화총자산
        if pf["market"] == "us":
            foreign = _extract_foreign(summary)
        elif pf.get("broker") == "kis":
            try:
                _, fsummary = get_overseas_balance(
                    pf["account_cfg"], pf["project_root"],
                    pf.get("account_config_name", ""))
                # KR+US 혼합계좌: 원화총자산이 KR 자산까지 포함하므로 cash fallback 금지
                foreign = _extract_foreign(fsummary, allow_cash_fallback=False)
                # '원화전용' summary(원화현금만 남은 해외계좌)는 KR 총자산 산정에서 제외.
                # KR 계좌 자신의 원화현금이 tot_asst_amt 로 잡혀 이중계상되는 것을 방지.
                if fsummary and not fsummary.get("원화전용"):
                    f_krw_total = float(fsummary.get("원화총자산") or 0)
                # KIS 원화총자산이 있으면 외화현금을 (원화총자산 - KR자산) / 환율 로 보정.
                # 매도 미정산(T+2 결제 대기) USD 까지 포함되므로 증권사 앱과 일치한다.
                if foreign and f_krw_total > 0 and foreign["환율"] > 0:
                    kr_value = (evlu or 0) + (cash or 0)
                    fcash_full = (f_krw_total - kr_value) / foreign["환율"] - foreign["외화평가금액"]
                    if fcash_full > foreign["외화현금"]:
                        foreign["외화현금"] = fcash_full
                        foreign["외화자산"] = foreign["외화평가금액"] + fcash_full
                        foreign["원화환산외화자산"] = foreign["외화자산"] * foreign["환율"]
            except Exception as e:
                print(f"[foreign-kr] {pf.get('name','?')}: {e}")
        foreign = foreign or {
            "외화평가금액": 0.0, "외화현금": 0.0,
            "외화자산": 0.0, "원화환산외화자산": 0.0, "환율": 0.0,
        }

        # 총자산:
        # - fallback: KR 평가 + KR 현금 + 외화환산자산 (항상 산정 가능)
        # - KIS 가 보고하는 원화총자산(f_krw_total) 은 fallback 이상일 때만 신뢰.
        #   fallback 보다 작으면 KIS overseas API 가 KR 평가를 누락한 케이스
        #   (hitomato/자산배분2: KR holdings + USD 매도미정산 조합) 이므로 fallback 사용.
        # - 단, 보유종목이 없는 단타 사이클 직후 계좌(myBog 등)에서는 KIS '원화총자산'이
        #   결제 흐름 반영이 지연되어 D+2예수금보다 크게 보고되는 경우가 있어
        #   fallback (= D+2예수금 + 외화환산) 을 사용해야 당일증감이 정확.
        fallback_total = krw_tot or (evlu + (cash or 0))
        if pf["market"] != "us" and foreign["원화환산외화자산"] > 0:
            fallback_total = fallback_total + foreign["원화환산외화자산"]

        has_kr_holdings = bool(holdings)
        if pf["market"] != "us" and has_kr_holdings and f_krw_total >= fallback_total:
            total_assets = f_krw_total
        else:
            total_assets = fallback_total

        result = {
            "ok": True,
            "통화": "KRW",
            "총자산": total_assets,
            "현금": cash,
            "매수금액": pchs,
            "평가금액": evlu,
            "손익": pnl,
            "수익률": rt,
            "당일실현손익": today_rlz,
            **foreign,
            "rebal_alert": _rebal_alert(pf, holdings),
            "_holdings": holdings,    # 검색 기능용 (캐시에 함께 저장)
        }

        # 당일자산증감 = 오늘 총자산 - 전일 총자산 (오늘 스냅샷 upsert 전에 전일치 조회)
        day_chg = None
        day_chg_rate = None
        try:
            from datetime import datetime as _dt
            today_str = _dt.now().strftime("%Y-%m-%d")
            rows = get_recent_snapshots(PROJECT_ROOT, pf["name"], days=14)
            prev_assets = [a for d, a, _ in rows if d != today_str and a is not None]
            prev_asset = prev_assets[-1] if prev_assets else None
            if prev_asset and prev_asset > 0:
                day_chg = result["총자산"] - prev_asset
                day_chg_rate = (day_chg / prev_asset) * 100
        except Exception as e:
            print(f"[day_chg] {pf.get('name','?')}: {e}")
        result["당일자산증감"] = day_chg
        result["당일자산증감률"] = day_chg_rate

        try:
            upsert_today(PROJECT_ROOT, pf["name"], result["총자산"], today_rlz)
        except Exception as e:
            print(f"[snapshot] upsert 실패 ({pf['name']}): {e}")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "_holdings": []}


STALE_FALLBACK_MAX = 3600  # 직전 정상값을 fallback 으로 쓸 수 있는 최대 시간 (초)

def _get_cached_summary(pf):
    name = pf["name"]
    now = time.time()
    entry = _summary_cache.get(name)
    if entry and now - entry[0] < SUMMARY_TTL:
        return entry[1]
    result = _fetch_list_summary(pf)
    if result.get("ok"):
        _summary_cache[name] = (now, result)
        return result
    # 일시적 fetch 실패: 직전 정상값이 STALE_FALLBACK_MAX 이내면 stale 로 반환
    # (Upbit ticker 일시 실패 등으로 총자산이 폭락 표시되는 것 방지)
    if entry and now - entry[0] < STALE_FALLBACK_MAX:
        stale = dict(entry[1])
        stale["_stale"] = True
        return stale
    return result


# ─────────────────────────────────────────────────
# SVG 차트 빌더
# ─────────────────────────────────────────────────
def _chart_from_rows(rows, w=300, h=56):
    if not rows:
        return None
    assets = [(d, a) for d, a, _ in rows if a is not None]
    if len(assets) < 2:
        return None

    n = len(rows)
    min_a = min(a for _, a in assets)
    max_a = max(a for _, a in assets)
    span_a = max(max_a - min_a, 1)

    realized = [(r if r is not None else 0) for _, _, r in rows]
    max_abs_r = max((abs(r) for r in realized), default=0) or 1

    def x(i):
        return i / (n - 1) * w if n > 1 else w / 2

    area_top_pad = 4
    area_h = h * 0.70
    mid = h * 0.72
    bar_max_h = h - mid - 2

    def y_area(v):
        return area_top_pad + (1 - (v - min_a) / span_a) * (area_h - area_top_pad)

    segments = []
    cur = []
    for i, (_d, a, _r) in enumerate(rows):
        if a is None:
            if len(cur) >= 2:
                segments.append(cur)
            cur = []
        else:
            cur.append((x(i), y_area(a)))
    if len(cur) >= 2:
        segments.append(cur)

    area_parts, line_parts = [], []
    for seg in segments:
        pts = " L ".join(f"{px:.1f},{py:.1f}" for px, py in seg)
        area_parts.append(f"M {seg[0][0]:.1f},{h:.1f} L {pts} L {seg[-1][0]:.1f},{h:.1f} Z")
        line_parts.append(f"M {pts}")

    bar_w = max(1.5, w / n * 0.55)
    bars = []
    for i, r in enumerate(realized):
        if r == 0:
            continue
        bh = abs(r) / max_abs_r * bar_max_h
        if bh < 1.5:
            bh = 1.5
        cx = x(i) - bar_w / 2
        # 수익=빨강, 손실=파랑
        if r > 0:
            bars.append({"x": cx, "y": mid - bh, "w": bar_w, "h": bh, "fill": "#ff3b30"})
        else:
            bars.append({"x": cx, "y": mid, "w": bar_w, "h": bh, "fill": "#0d6efd"})

    points = []
    for i, (d, a, r) in enumerate(rows):
        pt = {
            "x": round(x(i), 2),
            "date": d,
            "asset": None if a is None else float(a),
            "realized": 0 if r is None else float(r),
        }
        if a is not None:
            pt["y"] = round(y_area(a), 2)
        points.append(pt)

    first_asset = assets[0][1]
    last_asset = assets[-1][1]
    asset_chg = last_asset - first_asset
    asset_chg_rate = (asset_chg / first_asset * 100) if first_asset else 0.0

    return {
        "area": " ".join(area_parts),
        "line": " ".join(line_parts),
        "bars": bars,
        "w": w,
        "h": h,
        "mid": mid,
        "first_date": rows[0][0],
        "last_date": rows[-1][0],
        "realized_30d": int(sum(realized)),
        "asset_chg": float(asset_chg),
        "asset_chg_rate": float(asset_chg_rate),
        "points": points,
    }


def _build_chart(pf, days=30, w=300, h=56):
    try:
        rows = get_recent_snapshots(PROJECT_ROOT, pf["name"], days=days)
    except Exception:
        return None
    return _chart_from_rows(rows, w=w, h=h)


def _owner_aggregate_rows(owner_pfs, days):
    """오너 산하 포트폴리오들의 일자별 자산/실현손익 집계 rows 반환."""
    if not owner_pfs:
        return []
    by_date = {}
    for pf in owner_pfs:
        try:
            rows = get_recent_snapshots(PROJECT_ROOT, pf["name"], days=days)
        except Exception:
            continue
        for d, a, r in rows:
            entry = by_date.setdefault(d, [0.0, 0.0, False])
            if a is not None:
                entry[0] += a
                entry[2] = True
            if r is not None:
                entry[1] += r
    if not by_date:
        return []
    return [
        (d, by_date[d][0] if by_date[d][2] else None, by_date[d][1])
        for d in sorted(by_date.keys())
    ]


def _build_owner_chart(owner_pfs, days=30, w=300, h=44):
    rows = _owner_aggregate_rows(owner_pfs, days)
    if not rows:
        return None
    return _chart_from_rows(rows, w=w, h=h)


def _owner_asset_change(owner_pfs, days):
    """오너 단위 자산 증감액/증감률 (첫 유효 스냅샷 → 마지막 유효 스냅샷)."""
    rows = _owner_aggregate_rows(owner_pfs, days)
    assets = [a for _, a, _ in rows if a is not None]
    if len(assets) < 2:
        return None, None
    first, last = assets[0], assets[-1]
    chg = last - first
    rate = (chg / first * 100) if first else 0.0
    return chg, rate


# ─────────────────────────────────────────────────
# 라우트: 포트폴리오 목록 / 상세
# ─────────────────────────────────────────────────
EZGAIN_SUBCAT_ORDER = {"자산배분": 0, "active": 1, "bog": 2}


def _ezgain_subcat(pf):
    """ezgain 포트폴리오를 active/bog/자산배분 으로 분류 (대소문자 무시)."""
    if pf.get("project") != "ezgain":
        return None
    s = ((pf.get("name") or "") + " " + (pf.get("description") or "")).lower()
    if "bog" in s:
        return "bog"
    if "active" in s:
        return "active"
    return "자산배분"


@app.get("/", response_class=HTMLResponse)
def index(request: Request, sort: str = "portfolio"):
    portfolios = _get_portfolios()
    for pf in portfolios:
        pf["ezgain_subcat"] = _ezgain_subcat(pf)
    grouped = {}
    for pf in portfolios:
        owner = pf.get("owner", "unknown")
        grouped.setdefault(owner, []).append(pf)
    sorted_owners = [o for o in KNOWN_OWNERS if o in grouped]
    sorted_owners += sorted(o for o in grouped if o not in KNOWN_OWNERS)

    summaries = {}
    if portfolios:
        workers = min(8, len(portfolios))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_get_cached_summary, pf): pf["name"] for pf in portfolios}
            for fut in as_completed(futures):
                summaries[futures[fut]] = fut.result()

    owner_totals = {}
    owner_day_chg = {}        # 오너별 당일자산증감 (전일 데이터 있는 포트폴리오만 합산)
    owner_day_chg_rate = {}
    for owner, pfs in grouped.items():
        total = 0
        chg_sum = 0.0
        yesterday_sum = 0.0
        for pf in pfs:
            s = summaries.get(pf["name"])
            if not (s and s.get("ok")):
                continue
            total += s.get("총자산", 0) or 0
            chg = s.get("당일자산증감")
            today_t = s.get("총자산", 0) or 0
            if chg is not None:
                chg_sum += chg
                yesterday_sum += (today_t - chg)
        owner_totals[owner] = total
        if yesterday_sum > 0:
            owner_day_chg[owner] = chg_sum
            owner_day_chg_rate[owner] = chg_sum / yesterday_sum * 100
        else:
            owner_day_chg[owner] = None
            owner_day_chg_rate[owner] = None

    sort_mode = sort if sort in ("portfolio", "asset", "cash", "realized") else "portfolio"

    def _pf_metric(pf, key):
        s = summaries.get(pf["name"])
        if not (s and s.get("ok")):
            return 0
        v = s.get(key)
        return 0 if v is None else v

    PROJECT_ORDER = {"ezgain": 0, "ezinvest": 1, "ezsplit": 2, "ezadmin": 3}
    for owner in grouped:
        if sort_mode == "asset":
            grouped[owner].sort(key=lambda p: -_pf_metric(p, "총자산"))
        elif sort_mode == "cash":
            grouped[owner].sort(key=lambda p: -_pf_metric(p, "현금"))
        elif sort_mode == "realized":
            grouped[owner].sort(key=lambda p: -_pf_metric(p, "당일실현손익"))
        else:
            grouped[owner].sort(key=lambda p: (
                PROJECT_ORDER.get(p.get("project"), 99),
                EZGAIN_SUBCAT_ORDER.get(p.get("ezgain_subcat"), 0),
                -_pf_metric(p, "총자산"),
            ))

    charts = {pf["name"]: _build_chart(pf) for pf in portfolios}
    owner_charts = {
        owner: _build_owner_chart(grouped[owner], days=365, w=720, h=80)
        for owner in sorted_owners
    }
    owner_month_chg = {}
    owner_month_chg_rate = {}
    for owner in sorted_owners:
        chg, rate = _owner_asset_change(grouped[owner], days=30)
        owner_month_chg[owner] = chg
        owner_month_chg_rate[owner] = rate

    # 오너별 DD(역대 최고 합산총자산 대비 현재 총자산 하락률).
    # 현재값은 헤더에 표시되는 라이브 총자산(owner_totals)을 사용해 표시값과 일관성 유지.
    # peak(역대 최고 합산총자산)는 '오늘 제외, 어제까지'의 DB 시계열 최고로 계산.
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    _kst_today = _dt2.now(_tz2(_td2(hours=9))).strftime("%Y-%m-%d")
    owner_peak = {}       # 어제까지의 역대 최고 합산총자산
    owner_dd_rate = {}    # 낙폭률
    for owner in sorted_owners:
        ch = owner_charts.get(owner)
        prior_assets = [p.get("asset") for p in ((ch or {}).get("points") or [])
                        if p.get("asset") is not None and p.get("date", "") < _kst_today]
        cur = owner_totals.get(owner)
        if not cur or not prior_assets:
            continue
        peak = max(prior_assets)
        if peak > 0:
            owner_peak[owner] = peak
            owner_dd_rate[owner] = (cur - peak) / peak * 100

    return templates.TemplateResponse(
        request, "index.html",
        {
            "grouped": grouped,
            "owners": sorted_owners,
            "summaries": summaries,
            "owner_totals": owner_totals,
            "owner_day_chg": owner_day_chg,
            "owner_day_chg_rate": owner_day_chg_rate,
            "owner_month_chg": owner_month_chg,
            "owner_month_chg_rate": owner_month_chg_rate,
            "owner_peak": owner_peak,
            "owner_dd_rate": owner_dd_rate,
            "charts": charts,
            "owner_charts": owner_charts,
            "sort_mode": sort_mode,
        },
    )


@app.get("/portfolio/{name}", response_class=HTMLResponse)
def portfolio_detail(name: str, request: Request):
    # 상세 진입 시마다 yaml 재스캔
    global _portfolios
    _portfolios = None
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)

    if pf is None:
        return templates.TemplateResponse(
            request, "portfolio.html",
            {"pf": None, "error": "포트폴리오를 찾을 수 없습니다."},
        )

    try:
        acct_name = pf.get("account_config_name", "")
        holdings, summary = _fetch_balance(pf)
        currency = "USD" if pf["market"] == "us" else "KRW"
        foreign_holdings = []
        foreign_summary = {}
        # USD 계좌에서 KIS raw 외화예수금이 0 으로 나오는 경우 보강
        if pf["market"] == "us":
            foreign = _extract_foreign(summary)
            if foreign and foreign["외화현금"] > 0:
                summary["외화예수금"] = foreign["외화현금"]
        # KR + KIS 계좌도 외화 보유분이 있으면 조회해서 별도 노출
        elif pf.get("broker") == "kis":
            try:
                fh, fsummary = get_overseas_balance(
                    pf["account_cfg"], pf["project_root"], acct_name)
                # 혼합계좌: cash fallback 금지 (원화총자산이 KR 자산 포함)
                foreign = _extract_foreign(fsummary, allow_cash_fallback=False)
                # KIS 원화총자산 기반 보정: 매도 미정산 USD 까지 포함
                f_krw_total = float(fsummary.get("원화총자산") or 0) if fsummary else 0.0
                if foreign and f_krw_total > 0 and foreign["환율"] > 0:
                    kr_evlu = float(summary.get("총평가금액") or 0)
                    kr_cash = float(summary.get("D+2예수금") or summary.get("예수금") or 0)
                    fcash_full = (f_krw_total - kr_evlu - kr_cash) / foreign["환율"] - foreign["외화평가금액"]
                    if fcash_full > foreign["외화현금"]:
                        foreign["외화현금"] = fcash_full
                        foreign["외화자산"] = foreign["외화평가금액"] + fcash_full
                        foreign["원화환산외화자산"] = foreign["외화자산"] * foreign["환율"]
                if foreign:
                    foreign_holdings = fh or []
                    foreign_summary = fsummary or {}
                    foreign_summary["_extracted"] = foreign
                    foreign_summary["_krw_total"] = f_krw_total
            except Exception as e:
                print(f"[detail-foreign] {pf.get('name','?')}: {e}")
    except Exception as e:
        traceback.print_exc()
        return templates.TemplateResponse(
            request, "portfolio.html",
            {"pf": pf, "error": str(e), "holdings": [], "summary": {}, "currency": "KRW"},
        )

    universe = pf["portfolio_cfg"].get("universe") or {}
    total_evlu = sum(h["평가금액"] for h in holdings) if holdings else 0

    # ezgain 은 yaml 의 weight 합이 100 이 아닐 수 있으므로 정규화하여 목표비중(%) 산출.
    # 그 외 프로젝트는 yaml weight 를 그대로 % 로 사용 (기존 동작).
    is_ezgain = pf.get("project") == "ezgain"
    total_target_weight = 0.0
    if is_ezgain and universe:
        total_target_weight = sum(
            float(v.get("weight", 0)) for v in universe.values()
            if isinstance(v, dict)
        )

    for h in holdings:
        code = h["종목코드"]
        raw_w = float(universe.get(code, {}).get("weight", 0)) if isinstance(universe.get(code), dict) else 0
        if is_ezgain and total_target_weight > 0:
            target_weight = round(raw_w / total_target_weight * 100, 2)
        else:
            target_weight = round(raw_w, 2)
        actual_weight = round(h["평가금액"] / total_evlu * 100, 2) if total_evlu else 0
        h["비중"] = actual_weight
        h["목표비중"] = target_weight
        h["비중차이"] = round(actual_weight - target_weight, 2)

    holdings.sort(key=lambda h: h["수익률"], reverse=True)

    # ezgain 자산배분/active 행 표시 (보유×universe 교집합 여부 + 비중차이 임계 + 미보유 universe 종목)
    sub = _ezgain_subcat(pf)
    rebal_view = sub in ("자산배분", "active")
    threshold_pct = 0.0
    if rebal_view:
        try:
            tol = float((pf["portfolio_cfg"].get("rebalancing") or {}).get("tolerance", 0) or 0)
        except (TypeError, ValueError):
            tol = 0.0
        threshold_pct = tol * 100 * 0.7
        held_codes = {h["종목코드"] for h in holdings}
        for h in holdings:
            code = h["종목코드"]
            in_uni = isinstance(universe.get(code), dict) and \
                     float(universe.get(code, {}).get("weight", 0) or 0) > 0
            h["_in_universe"] = in_uni
            h["_overweight_alert"] = bool(
                in_uni and threshold_pct > 0 and abs(h["비중차이"]) >= threshold_pct
            )
            h["_phantom"] = False
        # 미보유 universe 종목을 phantom row 로 추가 (가장 아래)
        for code, info in universe.items():
            if not isinstance(info, dict) or code in held_codes:
                continue
            raw_w = float(info.get("weight", 0) or 0)
            if raw_w <= 0:
                continue
            tgt = round(raw_w / total_target_weight * 100, 2) if total_target_weight > 0 else round(raw_w, 2)
            holdings.append({
                "종목코드": code,
                "종목명": info.get("name", code),
                "보유수량": 0,
                "매수평균가": 0,
                "현재가": 0,
                "매수금액": 0,
                "평가금액": 0,
                "손익금액": 0,
                "수익률": 0,
                "당일손익금액": None,
                "당일수익률": None,
                "거래소코드": "",
                "비중": 0,
                "목표비중": tgt,
                "비중차이": -tgt,
                "_in_universe": True,
                "_overweight_alert": False,
                "_phantom": True,
            })

    broker = pf.get("broker", "kis")
    if broker == "upbit":
        pending_orders = []
    elif broker == "kw":
        try:
            pending_orders = kw_client.get_pending_orders(pf["account_cfg"], pf["project_root"], acct_name)
        except Exception as e:
            print(f"[pending-kw] {pf.get('name','?')}: {e}")
            pending_orders = []
    elif pf["market"] == "us":
        pending_orders = get_pending_orders_overseas(pf["account_cfg"], pf["project_root"], acct_name)
    else:
        pending_orders = get_pending_orders(pf["account_cfg"], pf["project_root"], acct_name)

    holdings_name_map = {h["종목코드"]: h["종목명"] for h in holdings}
    for po in pending_orders:
        if not po.get("종목명"):
            po["종목명"] = holdings_name_map.get(po["종목코드"], po["종목코드"])

    pending_sell_codes = {po["종목코드"] for po in pending_orders if po.get("주문구분") == "매도"}

    today_trades = []
    try:
        if broker == "kw":
            today_trades = kw_client.get_today_trades_domestic(pf["account_cfg"], pf["project_root"], acct_name)
        elif broker == "kis":
            if pf["market"] == "us":
                today_trades = get_today_trades_overseas(pf["account_cfg"], pf["project_root"], acct_name)
            else:
                today_trades = get_today_trades_domestic(pf["account_cfg"], pf["project_root"], acct_name)
    except Exception as e:
        print(f"[today-trades] {pf.get('name','?')}: {e}")
        today_trades = []

    response = templates.TemplateResponse(
        request, "portfolio.html",
        {
            "pf": pf, "holdings": holdings,
            "summary": summary, "currency": currency, "error": None,
            "pending_orders": pending_orders,
            "pending_sell_codes": pending_sell_codes,
            "today_trades": today_trades,
            "foreign_holdings": foreign_holdings,
            "foreign_summary": foreign_summary,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ─────────────────────────────────────────────────
# 라우트: 주문 / 호가 / 차트 / 체결조회
# ─────────────────────────────────────────────────
def _find_pf(name: str):
    portfolios = _get_portfolios()
    return next((p for p in portfolios if p["name"] == name), None)


@app.post("/portfolio/{name}/sell")
async def sell_order(name: str, request: Request):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return JSONResponse({"ok": False, "error": "Upbit 매도는 지원되지 않습니다."})

    body = await request.json()
    code = body.get("code", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))
    if not code or qty <= 0 or price <= 0:
        return JSONResponse({"ok": False, "error": "종목코드, 수량, 가격을 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        broker = pf.get("broker", "kis")
        if broker == "kw":
            result = kw_client.place_sell_order(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, qty, int(price))
        elif pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = place_sell_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                              code, excg_cd, qty, price)
        else:
            result = place_sell_order(pf["account_cfg"], pf["project_root"], acct_name,
                                      code, qty, int(price))
        return JSONResponse({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/portfolio/{name}/cancel")
async def cancel_order_route(name: str, request: Request):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return JSONResponse({"ok": False, "error": "Upbit 주문 취소는 지원되지 않습니다."})

    body = await request.json()
    code = body.get("code", "")
    order_no = body.get("order_no", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))
    if not code or not order_no:
        return JSONResponse({"ok": False, "error": "종목코드, 주문번호를 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        broker = pf.get("broker", "kis")
        if broker == "kw":
            result = kw_client.cancel_order(pf["account_cfg"], pf["project_root"], acct_name,
                                            order_no, code, qty)
        elif pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = cancel_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                           order_no, code, excg_cd, qty, price)
        else:
            krx_orgno = body.get("krx_orgno", "")
            result = cancel_order(pf["account_cfg"], pf["project_root"], acct_name,
                                  order_no, krx_orgno, code, qty, price)
        return JSONResponse({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/portfolio/{name}/askprice")
def get_askprice(name: str, code: str = "", excg_cd: str = ""):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return JSONResponse({"ok": False, "error": "Upbit 호가 조회는 지원되지 않습니다."})
    if not code:
        return JSONResponse({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            price = get_ask_price_overseas(pf["account_cfg"], pf["project_root"], acct_name, code, excg_cd)
        else:
            price = get_ask_price_domestic(pf["account_cfg"], pf["project_root"], acct_name, code)
        return JSONResponse({"ok": True, "price": price})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/portfolio/{name}/buy")
async def buy_order(name: str, request: Request):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") not in ("kis", "kw"):
        return JSONResponse({"ok": False, "error": "매수는 KIS / 키움 계좌만 지원합니다."})

    body = await request.json() or {}
    code = body.get("code", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))
    if not code or qty <= 0 or price <= 0:
        return JSONResponse({"ok": False, "error": "종목코드, 수량, 가격을 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        broker = pf.get("broker", "kis")
        if broker == "kw":
            result = kw_client.place_buy_order(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, qty, int(price))
        elif pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = place_buy_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                              code, excg_cd, qty, price)
        else:
            result = place_buy_order(pf["account_cfg"], pf["project_root"], acct_name,
                                     code, qty, int(price))
        return JSONResponse({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/portfolio/{name}/orderbook")
def get_orderbook(name: str, code: str = "", excg_cd: str = ""):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") not in ("kis", "kw"):
        return JSONResponse({"ok": False, "error": "호가 조회는 KIS / 키움 계좌만 지원합니다."})
    if not code:
        return JSONResponse({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        broker = pf.get("broker", "kis")
        if broker == "kw":
            data = kw_client.get_orderbook_domestic(pf["account_cfg"], pf["project_root"], acct_name, code)
        elif pf["market"] == "us":
            data = get_orderbook_overseas(pf["account_cfg"], pf["project_root"], acct_name, code, excg_cd)
        else:
            data = get_orderbook_domestic(pf["account_cfg"], pf["project_root"], acct_name, code)
        return JSONResponse({"ok": True, "data": data, "market": pf["market"]})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/portfolio/{name}/chart")
def get_chart(name: str, code: str = "", excg_cd: str = "", days: int = 120):
    pf = _find_pf(name)
    if pf is None:
        return JSONResponse({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") not in ("kis", "kw"):
        return JSONResponse({"ok": False, "error": "차트 조회는 KIS / 키움 계좌만 지원합니다."})
    if not code:
        return JSONResponse({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        broker = pf.get("broker", "kis")
        if broker == "kw":
            candles = kw_client.get_daily_chart_domestic(pf["account_cfg"], pf["project_root"], acct_name,
                                                          code, days=days)
        elif pf["market"] == "us":
            candles = get_daily_chart_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, excg_cd, days=days)
        else:
            candles = get_daily_chart_domestic(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, days=days)
        return JSONResponse({"ok": True, "candles": candles, "market": pf["market"]})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, owner: str = ""):
    """
    보유종목 통계 — 모든 포트폴리오의 보유종목을 종목코드 기준으로 집계.
    - ?owner=<name> 으로 특정 오너만 필터 (빈값/누락 = 전체)
    - KRW 환산: US 종목은 portfolio summary 의 환율 사용
    - 상위 종목 분포 + 시장/오너/프로젝트별 분포 차트
    """
    all_portfolios = _get_portfolios()

    # 오너 탭 목록 — owners.yaml 순서 + 그 외 알파벳순
    all_owners = []
    seen = set()
    for o in KNOWN_OWNERS:
        if any(p.get("owner") == o for p in all_portfolios) and o not in seen:
            all_owners.append(o); seen.add(o)
    for p in all_portfolios:
        o = p.get("owner") or "unknown"
        if o not in seen:
            all_owners.append(o); seen.add(o)

    selected_owner = owner.strip() if owner else ""
    if selected_owner and selected_owner not in all_owners:
        selected_owner = ""

    portfolios = (
        [p for p in all_portfolios if p.get("owner") == selected_owner]
        if selected_owner else all_portfolios
    )

    summaries = {}
    if portfolios:
        with ThreadPoolExecutor(max_workers=min(8, len(portfolios))) as ex:
            futures = {ex.submit(_get_cached_summary, pf): pf["name"] for pf in portfolios}
            for fut in as_completed(futures):
                summaries[futures[fut]] = fut.result()

    by_stock = {}        # (code, market) -> 집계
    by_market = {"kr": 0, "us": 0, "crypto": 0}
    by_owner = {}
    by_project = {}

    for pf in portfolios:
        s = summaries.get(pf["name"])
        if not s or not s.get("ok"):
            continue
        market = pf["market"]
        # 환율: US 면 summary["환율"], 없으면 평가금액 자체가 이미 KRW (kw 등)
        exrt = 1.0
        if market == "us":
            exrt = float(s.get("환율") or 0) or 1300.0  # fallback
            # summary 자체 dict 가 아닌 raw 응답을 사용해야 환율을 얻을 수 있음
            # _get_cached_summary 결과에는 환율이 안 들어 있으므로 holdings 기반으로 재계산
            # → 여기서는 portfolio level 의 평가금액(원화 환산)을 사용해 비례 분배

        owner = pf.get("owner", "unknown")
        project = pf.get("project", "?")
        # ezgain 은 서브카테고리(자산배분/active/bog) 별로 집계
        if project == "ezgain":
            sub = _ezgain_subcat(pf) or "자산배분"
            project = f"ezgain ({sub})"

        # 포트폴리오 단위 KRW 평가 (이미 _fetch_list_summary 에서 정리된 값)
        pf_eval_krw = float(s.get("평가금액") or 0) if market != "us" else 0
        if market == "us":
            # US 는 _holdings 의 평가금액 합 * 환율 ≈ 원화총평가금액 (summary 내장)
            # 안전을 위해 holdings 평가합 × 추정환율로 계산
            usd_sum = sum(float(h.get("평가금액") or 0) for h in (s.get("_holdings") or []))
            pf_eval_krw = usd_sum * exrt
        by_market[market] = by_market.get(market, 0) + pf_eval_krw
        by_owner[owner]   = by_owner.get(owner, 0) + pf_eval_krw
        by_project[project] = by_project.get(project, 0) + pf_eval_krw

        for h in (s.get("_holdings") or []):
            code = str(h.get("종목코드", "") or "").strip()
            name = str(h.get("종목명", code) or code).strip()
            if not code:
                continue
            qty   = float(h.get("보유수량") or 0)
            amt   = float(h.get("평가금액") or 0)
            pnl   = float(h.get("손익금액") or 0)
            amt_krw = amt * exrt if market == "us" else amt
            pnl_krw = pnl * exrt if market == "us" else pnl

            key = (code, market)
            entry = by_stock.setdefault(key, {
                "code": code, "name": name, "market": market,
                "amount_krw": 0.0, "pnl_krw": 0.0,
                "qty_total": 0.0, "accounts": [],
            })
            entry["amount_krw"] += amt_krw
            entry["pnl_krw"]    += pnl_krw
            entry["qty_total"]  += qty
            entry["accounts"].append({
                "owner": owner,
                "portfolio_name": pf["name"],
                "portfolio_desc": pf.get("description", pf["name"]),
                "qty": qty,
                "amount": amt,
                "pnl": pnl,
                "currency": "USD" if market == "us" else "KRW",
            })

    # 평가금액 10원 이하의 dust 종목은 통계에서 제외
    DUST_THRESHOLD = 10
    stocks = sorted(
        (v for v in by_stock.values() if v["amount_krw"] > DUST_THRESHOLD),
        key=lambda x: -x["amount_krw"],
    )
    total_amount = sum(s["amount_krw"] for s in stocks)
    total_pnl    = sum(s["pnl_krw"] for s in stocks)

    # 총자산/현금: portfolio summary 기준 합산 (오너 헤더 합계와 동일 로직)
    # KIS 의 '총자산' = 원화총자산 (US 는 실시간 환율 반영) → 가장 정확
    total_asset = 0.0
    total_cash  = 0.0
    for pf in portfolios:
        s = summaries.get(pf["name"])
        if not s or not s.get("ok"):
            continue
        total_asset += float(s.get("총자산") or 0)
        total_cash  += float(s.get("현금") or 0)

    return templates.TemplateResponse(
        request, "stats.html",
        {
            "stocks": stocks,
            "total_amount": total_amount,
            "total_pnl": total_pnl,
            "total_asset": total_asset,
            "total_cash": total_cash,
            "by_market": by_market,
            "by_owner": by_owner,
            "by_project": by_project,
            "all_owners": all_owners,
            "selected_owner": selected_owner,
        },
    )


@app.get("/indices", response_class=HTMLResponse)
def indices_page(request: Request):
    """지수 상세 페이지: 5종목 카드 + 50일 평균 대비 격차 차트.
    카드는 MA(50) 마지막 편차가 큰 순서대로 정렬 (과열 종목이 상단)."""
    data = get_all_indices(w=140, h=40)

    def _last_dev(d):
        if not d.get("ok"):
            return float("-inf")
        dev = d.get("ma_dev") or []
        return dev[-1] if dev else float("-inf")

    data.sort(key=_last_dev, reverse=True)

    return templates.TemplateResponse(
        request, "indices.html",
        {"indices": data},
    )


@app.get("/reload")
def reload_config():
    global _portfolios
    _portfolios = None
    _summary_cache.clear()
    _get_portfolios()
    return {"status": "ok", "count": len(_portfolios)}


@app.get("/api/search")
def search_holdings(q: str = ""):
    """
    종목명/종목코드로 검색해 모든 포트폴리오의 보유 현황을 반환.
    캐시된 holdings (_get_cached_summary 의 _holdings 필드) 를 사용해 추가 API 호출 없음.
    """
    q = (q or "").strip().lower()
    if not q:
        return {"query": "", "results": []}

    portfolios = _get_portfolios()
    results = []
    if not portfolios:
        return {"query": q, "results": []}

    workers = min(8, len(portfolios))
    fetched = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_get_cached_summary, pf): pf["name"] for pf in portfolios}
        for fut in as_completed(futures):
            fetched[futures[fut]] = fut.result()

    for pf in portfolios:
        s = fetched.get(pf["name"])
        if not s or not s.get("ok"):
            continue
        holdings = s.get("_holdings") or []
        matches = []
        for h in holdings:
            code = str(h.get("종목코드", ""))
            name = str(h.get("종목명", ""))
            if q in code.lower() or q in name.lower():
                matches.append({
                    "code": code,
                    "name": name,
                    "qty": h.get("보유수량", 0),
                    "avg_price": h.get("매수평균가", 0),
                    "current": h.get("현재가", 0),
                    "buy_amount": h.get("매수금액", 0),
                    "eval_amount": h.get("평가금액", 0),
                    "pnl": h.get("손익금액", 0),
                    "rate": h.get("수익률", 0),
                })
        if matches:
            results.append({
                "owner": pf.get("owner", "unknown"),
                "name": pf["name"],
                "description": pf.get("description", pf["name"]),
                "project": pf.get("project", ""),
                "broker": pf.get("broker", ""),
                "market": pf.get("market", ""),
                "currency": "USD" if pf["market"] == "us" else "KRW",
                "matches": matches,
            })
    return {"query": q, "results": results}


# ─────────────────────────────────────────────────
# 단독 실행
# ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "9900")),
        log_level="info",
    )
