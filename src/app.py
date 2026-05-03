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

        result = {
            "ok": True,
            "통화": "KRW",
            "총자산": krw_tot or (evlu + (cash or 0)),
            "현금": cash,
            "매수금액": pchs,
            "평가금액": evlu,
            "손익": pnl,
            "수익률": rt,
            "당일실현손익": today_rlz,
            "_holdings": holdings,    # 검색 기능용 (캐시에 함께 저장)
        }
        try:
            upsert_today(PROJECT_ROOT, pf["name"], result["총자산"], today_rlz)
        except Exception as e:
            print(f"[snapshot] upsert 실패 ({pf['name']}): {e}")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "_holdings": []}


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
        if r > 0:
            bars.append({"x": cx, "y": mid - bh, "w": bar_w, "h": bh, "fill": "#34c759"})
        else:
            bars.append({"x": cx, "y": mid, "w": bar_w, "h": bh, "fill": "#ff3b30"})

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
        "points": points,
    }


def _build_chart(pf, days=30, w=300, h=56):
    try:
        rows = get_recent_snapshots(PROJECT_ROOT, pf["name"], days=days)
    except Exception:
        return None
    return _chart_from_rows(rows, w=w, h=h)


def _build_owner_chart(owner_pfs, days=30, w=300, h=44):
    if not owner_pfs:
        return None
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
        return None
    sorted_dates = sorted(by_date.keys())
    rows = [
        (d, by_date[d][0] if by_date[d][2] else None, by_date[d][1])
        for d in sorted_dates
    ]
    return _chart_from_rows(rows, w=w, h=h)


# ─────────────────────────────────────────────────
# 라우트: 포트폴리오 목록 / 상세
# ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request, sort: str = "portfolio"):
    portfolios = _get_portfolios()
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
    for owner, pfs in grouped.items():
        total = 0
        for pf in pfs:
            s = summaries.get(pf["name"])
            if s and s.get("ok"):
                total += s.get("총자산", 0) or 0
        owner_totals[owner] = total

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
                -_pf_metric(p, "총자산"),
            ))

    charts = {pf["name"]: _build_chart(pf) for pf in portfolios}
    owner_charts = {
        owner: _build_owner_chart(grouped[owner], days=365, w=720, h=80)
        for owner in sorted_owners
    }

    return templates.TemplateResponse(
        request, "index.html",
        {
            "grouped": grouped,
            "owners": sorted_owners,
            "summaries": summaries,
            "owner_totals": owner_totals,
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
    except Exception as e:
        traceback.print_exc()
        return templates.TemplateResponse(
            request, "portfolio.html",
            {"pf": pf, "error": str(e), "holdings": [], "summary": {}, "currency": "KRW"},
        )

    universe = pf["portfolio_cfg"].get("universe") or {}
    total_evlu = sum(h["평가금액"] for h in holdings) if holdings else 0
    for h in holdings:
        code = h["종목코드"]
        target_weight = float(universe.get(code, {}).get("weight", 0)) if isinstance(universe.get(code), dict) else 0
        actual_weight = round(h["평가금액"] / total_evlu * 100, 2) if total_evlu else 0
        h["비중"] = actual_weight
        h["목표비중"] = target_weight
        h["비중차이"] = round(actual_weight - target_weight, 2)

    holdings.sort(key=lambda h: h["수익률"], reverse=True)

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
