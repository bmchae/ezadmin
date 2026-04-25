"""
ezadmin - Portfolio Dashboard
ezgain/ezinvest의 포트폴리오 계좌별 보유종목/잔고를 조회하는 웹 대시보드
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

from flask import (Flask, Response, render_template, request, jsonify,
                   make_response, redirect)
from werkzeug.security import check_password_hash

from config_loader import load_all_portfolios
from db import init_db, upsert_today, get_recent_snapshots
from kis_client import (get_domestic_balance, get_overseas_balance,
                        get_domestic_today_realized_pl,
                        get_overseas_today_realized_pl,
                        get_pending_orders, get_pending_orders_overseas,
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
    """
    프로젝트 루트의 .env 파일을 로드한다.
    - '#'로 시작하는 라인은 주석
    - KEY=VALUE 형식, 값은 양쪽 공백 제거
    - 값이 작은따옴표 또는 큰따옴표로 감싸져 있으면 따옴표 제거 (해시의 '$' 보호용)
    - 이미 환경변수에 설정된 값은 덮어쓰지 않는다.
    """
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

app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, "templates"),
            static_folder=os.path.join(PROJECT_ROOT, "static"))

init_db(PROJECT_ROOT)

AUTH_USERNAME = os.environ.get("WEB_AUTH_USER", "")
AUTH_PASSWORD_HASH = os.environ.get("WEB_AUTH_PASSWORD_HASH", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

SESSION_COOKIE = "ezadmin_session"
SESSION_TTL = 24 * 60 * 60  # 24시간
# 인증 면제 API 엔드포인트 (web frontend 외 API 서버 용도)
API_PATH_SUFFIXES = ("/sell", "/cancel", "/askprice")
API_PATH_EXACT = ("/reload",)


def _client_ip():
    """클라이언트 IP를 반환. TRUST_PROXY=1 이면 X-Forwarded-For 최초값 사용."""
    if TRUST_PROXY:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _is_lan(ip):
    """localhost / RFC1918 사설 대역은 LAN으로 간주하여 인증 면제."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _is_https():
    """요청이 HTTPS인지 판단. TRUST_PROXY=1 일 때 X-Forwarded-Proto 사용."""
    if TRUST_PROXY:
        return request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    return request.is_secure


def _session_secret():
    """JWT 서명키. ezsplit과 동일하게 WEB_AUTH_SECRET 우선, 없으면 PASSWORD_HASH 폴백."""
    return os.environ.get("WEB_AUTH_SECRET") or AUTH_PASSWORD_HASH


def _b64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s):
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_encode(payload, secret):
    """HS256 JWT 생성 (ezsplit의 jose와 호환)."""
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def _jwt_decode(token, secret):
    """HS256 JWT 검증 + exp 체크. 성공 시 payload dict, 실패 시 None."""
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


def _is_api_path(path):
    """API 서버용 엔드포인트는 인증 면제 (web frontend만 보호)."""
    if path in API_PATH_EXACT:
        return True
    return any(path.endswith(suf) for suf in API_PATH_SUFFIXES)


def _set_session_cookie(resp, token, max_age):
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age,
        httponly=True,
        secure=_is_https(),
        samesite="Lax",
        path="/",
    )


@app.before_request
def _require_session():
    """web frontend만 세션 쿠키로 보호. API 엔드포인트/LAN/공개경로는 통과."""
    path = request.path
    # 공개 경로: 로그인/로그아웃/정적 파일 + API 서버 엔드포인트
    if (path in ("/login", "/logout")
            or path.startswith("/static/")
            or _is_api_path(path)):
        return None

    if _is_lan(_client_ip()):
        return None

    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        return Response(
            "외부 접근이 차단되어 있습니다. .env의 WEB_AUTH_USER / WEB_AUTH_PASSWORD_HASH를 설정하세요.",
            status=503,
            mimetype="text/plain; charset=utf-8",
        )

    secret = _session_secret()
    if not secret:
        return Response(
            "서버 인증 설정 오류: WEB_AUTH_SECRET 또는 WEB_AUTH_PASSWORD_HASH 필요.",
            status=503,
            mimetype="text/plain; charset=utf-8",
        )

    token = request.cookies.get(SESSION_COOKIE, "")
    payload = _jwt_decode(token, secret)
    if payload and payload.get("sub"):
        return None

    # 인증 실패 → 로그인 페이지로
    next_url = request.full_path.rstrip("?") if request.query_string else request.path
    return redirect(f"/login?next={quote(next_url)}")


@app.route("/login", methods=["GET", "POST"])
def login():
    """로그인 폼 / 자격 검증 후 JWT 쿠키 발급."""
    next_url = (request.values.get("next") or "/").strip()
    if not next_url.startswith("/"):
        next_url = "/"

    if request.method == "GET":
        return render_template("login.html", error=None, next_url=next_url)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        return render_template("login.html",
                               error="서버 인증이 설정되지 않았습니다.",
                               next_url=next_url), 503

    user_ok = hmac.compare_digest(username, AUTH_USERNAME)
    try:
        pass_ok = check_password_hash(AUTH_PASSWORD_HASH, password)
    except (ValueError, TypeError):
        pass_ok = False
    if not (user_ok and pass_ok):
        return render_template("login.html",
                               error="아이디 또는 비밀번호가 올바르지 않습니다.",
                               next_url=next_url), 401

    secret = _session_secret()
    if not secret:
        return render_template("login.html",
                               error="서버 설정 오류 (WEB_AUTH_SECRET).",
                               next_url=next_url), 503

    now = int(time.time())
    token = _jwt_encode({"sub": username, "iat": now, "exp": now + SESSION_TTL}, secret)

    resp = make_response(redirect(next_url))
    _set_session_cookie(resp, token, SESSION_TTL)
    return resp


@app.route("/logout", methods=["GET", "POST"])
def logout():
    resp = make_response(redirect("/login"))
    _set_session_cookie(resp, "", 0)
    return resp


# 포트폴리오 목록 캐시 (앱 시작 시 로드)
_portfolios = None

# 포트폴리오 요약 캐시: name -> (timestamp, summary_dict). TTL 5분.
_summary_cache = {}
SUMMARY_TTL = 60


def _get_portfolios():
    global _portfolios
    if _portfolios is None:
        _portfolios = load_all_portfolios()
    return _portfolios


def _fetch_balance(pf):
    """broker에 따라 KIS / Kiwoom / Upbit 잔고 조회를 호출한다."""
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
    """
    포트폴리오 리스트 카드에 표시할 요약을 조회한다.
    국내/해외 통화 통일 위해 해외는 원화 환산값을 사용한다.
    Kiwoom 해외는 환율 정보가 없어 원화 환산 불가 → USD 값을 그대로 사용 (참고용).
    당일 실현손익은 KIS 국내 실전계좌에서만 지원하며, 그 외에는 None.
    """
    try:
        holdings, summary = _fetch_balance(pf)
        if pf["market"] == "us":
            # 원화 환산값이 있으면 사용 (KIS), 없으면 USD값 fallback (Kiwoom)
            pchs = summary.get("원화총매수금액") or summary.get("총매수금액") or 0
            evlu = summary.get("원화총평가금액") or summary.get("총평가금액") or 0
            pnl  = summary.get("원화총손익금액") or summary.get("총손익금액") or 0
            rt   = summary.get("원화총수익률") or summary.get("총수익률") or 0
            cash = summary.get("원화예수금") or 0
            # KIS 가 직접 제공하는 "총자산" 이 있으면 그 값을 써서 증권사 HTS 와 동일하게 맞춘다.
            krw_tot = summary.get("원화총자산") or 0
        else:
            pchs = summary.get("총매수금액", 0) or 0
            evlu = summary.get("총평가금액", 0) or 0
            pnl  = summary.get("총손익금액", 0) or 0
            rt   = summary.get("총수익률", 0) or 0
            cash = summary.get("D+2예수금", 0) or 0
            krw_tot = 0

        # 당일 실현손익: broker + market 조합별 분기
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
        }
        # 일별 스냅샷 저장 (실패해도 무시)
        try:
            upsert_today(PROJECT_ROOT, pf["name"], result["총자산"], today_rlz)
        except Exception as e:
            print(f"[snapshot] upsert 실패 ({pf['name']}): {e}")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


def _chart_from_rows(rows, w=300, h=56):
    """
    rows = [(date, asset_or_None, realized_or_None), ...] 에서 SVG 차트 데이터 dict 생성.
    포트폴리오/오너 모두에서 공유 사용.
    """
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

    # 상단 70% 는 area, 하단 30% 는 bar 여유
    area_top_pad = 4
    area_h = h * 0.70
    mid = h * 0.72  # bar zero line
    bar_max_h = h - mid - 2  # 하단 공간에 맞춰

    def y_area(v):
        return area_top_pad + (1 - (v - min_a) / span_a) * (area_h - area_top_pad)

    # 좌표 세그먼트 (None 구간은 끊어서)
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

    area_parts = []
    line_parts = []
    for seg in segments:
        pts = " L ".join(f"{px:.1f},{py:.1f}" for px, py in seg)
        area_parts.append(f"M {seg[0][0]:.1f},{h:.1f} L {pts} L {seg[-1][0]:.1f},{h:.1f} Z")
        line_parts.append(f"M {pts}")

    # 바: realized_pl (위=수익 녹색, 아래=손실 빨강)
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

    # hover 툴팁용 포인트: 일자별 (x, y, date, asset, realized)
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
    """단일 포트폴리오의 일별 스냅샷 → 차트 데이터."""
    try:
        rows = get_recent_snapshots(PROJECT_ROOT, pf["name"], days=days)
    except Exception:
        return None
    return _chart_from_rows(rows, w=w, h=h)


def _build_owner_chart(owner_pfs, days=30, w=300, h=44):
    """
    오너 소속 포트폴리오들의 일별 스냅샷을 날짜별로 합산해 차트 데이터 생성.
    - 자산 (total_asset): 같은 날짜 모두 sum
    - 실현손익 (realized_pl): 같은 날짜 모두 sum
    """
    if not owner_pfs:
        return None

    by_date = {}  # date -> [asset_sum, realized_sum, has_any_asset]
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
        (d,
         by_date[d][0] if by_date[d][2] else None,
         by_date[d][1])
        for d in sorted_dates
    ]
    return _chart_from_rows(rows, w=w, h=h)


@app.route("/")
def index():
    portfolios = _get_portfolios()
    owners_order = ["bmchae", "hitomato", "0eh", "9bong"]
    grouped = {}
    for pf in portfolios:
        owner = pf.get("owner", "unknown")
        grouped.setdefault(owner, []).append(pf)
    sorted_owners = [o for o in owners_order if o in grouped]
    sorted_owners += [o for o in grouped if o not in owners_order]

    # 요약 병렬 조회 (TTL 캐시 적용)
    summaries = {}
    if portfolios:
        workers = min(8, len(portfolios))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_get_cached_summary, pf): pf["name"] for pf in portfolios}
            for fut in as_completed(futures):
                summaries[futures[fut]] = fut.result()

    # 오너별 총자산 합계 (ok인 것만 합산)
    owner_totals = {}
    for owner, pfs in grouped.items():
        total = 0
        for pf in pfs:
            s = summaries.get(pf["name"])
            if s and s.get("ok"):
                total += s.get("총자산", 0) or 0
        owner_totals[owner] = total

    # 포트폴리오별 30일 차트 데이터
    charts = {pf["name"]: _build_chart(pf) for pf in portfolios}

    # 오너별 1년 차트 데이터 (소속 포트폴리오들의 자산/실현손익 합산)
    owner_charts = {
        owner: _build_owner_chart(grouped[owner], days=365, w=720, h=80)
        for owner in sorted_owners
    }

    return render_template("index.html", grouped=grouped, owners=sorted_owners,
                           summaries=summaries, owner_totals=owner_totals,
                           charts=charts, owner_charts=owner_charts)


@app.route("/portfolio/<name>")
def portfolio_detail(name):
    portfolios = _get_portfolios()
    pf = None
    for p in portfolios:
        if p["name"] == name:
            pf = p
            break

    if pf is None:
        return render_template("portfolio.html", pf=None, error="포트폴리오를 찾을 수 없습니다.")

    try:
        acct_name = pf.get("account_config_name", "")
        holdings, summary = _fetch_balance(pf)
        currency = "USD" if pf["market"] == "us" else "KRW"
    except Exception as e:
        traceback.print_exc()
        return render_template("portfolio.html", pf=pf, error=str(e),
                               holdings=[], summary={}, currency="KRW")

    # 비중, 비중차이 계산
    universe = pf["portfolio_cfg"].get("universe") or {}
    total_evlu = sum(h["평가금액"] for h in holdings) if holdings else 0
    for h in holdings:
        code = h["종목코드"]
        target_weight = float(universe.get(code, {}).get("weight", 0)) if isinstance(universe.get(code), dict) else 0
        actual_weight = round(h["평가금액"] / total_evlu * 100, 2) if total_evlu else 0
        h["비중"] = actual_weight
        h["목표비중"] = target_weight
        h["비중차이"] = round(actual_weight - target_weight, 2)

    # 수익률 높은 순으로 정렬
    holdings.sort(key=lambda h: h["수익률"], reverse=True)

    # 미체결 주문 전체 조회 (매수+매도). Kiwoom / Upbit 은 미지원이므로 빈 리스트.
    if pf.get("broker", "kis") in ("kw", "upbit"):
        pending_orders = []
    elif pf["market"] == "us":
        pending_orders = get_pending_orders_overseas(pf["account_cfg"], pf["project_root"], acct_name)
    else:
        pending_orders = get_pending_orders(pf["account_cfg"], pf["project_root"], acct_name)

    # 종목명이 비어있는 경우 보유종목에서 보강
    holdings_name_map = {h["종목코드"]: h["종목명"] for h in holdings}
    for po in pending_orders:
        if not po.get("종목명"):
            po["종목명"] = holdings_name_map.get(po["종목코드"], po["종목코드"])

    # holdings 행 렌더에 사용할 종목코드별 미체결 매도 주문 존재 여부
    pending_sell_codes = {po["종목코드"] for po in pending_orders if po.get("주문구분") == "매도"}

    resp = make_response(render_template("portfolio.html", pf=pf, holdings=holdings,
                                         summary=summary, currency=currency, error=None,
                                         pending_orders=pending_orders,
                                         pending_sell_codes=pending_sell_codes))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/portfolio/<name>/sell", methods=["POST"])
def sell_order(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return jsonify({"ok": False, "error": "Upbit 매도는 지원되지 않습니다."})

    body = request.get_json()
    code = body.get("code", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))

    if not code or qty <= 0 or price <= 0:
        return jsonify({"ok": False, "error": "종목코드, 수량, 가격을 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = place_sell_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                              code, excg_cd, qty, price)
        else:
            result = place_sell_order(pf["account_cfg"], pf["project_root"], acct_name,
                                      code, qty, int(price))
        return jsonify({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/portfolio/<name>/cancel", methods=["POST"])
def cancel_sell_order(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return jsonify({"ok": False, "error": "Upbit 주문 취소는 지원되지 않습니다."})

    body = request.get_json()
    code = body.get("code", "")
    order_no = body.get("order_no", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))

    if not code or not order_no:
        return jsonify({"ok": False, "error": "종목코드, 주문번호를 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = cancel_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                           order_no, code, excg_cd, qty, price)
        else:
            krx_orgno = body.get("krx_orgno", "")
            result = cancel_order(pf["account_cfg"], pf["project_root"], acct_name,
                                  order_no, krx_orgno, code, qty, price)
        return jsonify({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/portfolio/<name>/askprice")
def get_askprice(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") == "upbit":
        return jsonify({"ok": False, "error": "Upbit 호가 조회는 지원되지 않습니다."})

    code = request.args.get("code", "")
    excg_cd = request.args.get("excg_cd", "")
    if not code:
        return jsonify({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            price = get_ask_price_overseas(pf["account_cfg"], pf["project_root"], acct_name, code, excg_cd)
        else:
            price = get_ask_price_domestic(pf["account_cfg"], pf["project_root"], acct_name, code)
        return jsonify({"ok": True, "price": price})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/portfolio/<name>/buy", methods=["POST"])
def buy_order(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") != "kis":
        return jsonify({"ok": False, "error": "매수는 KIS 계좌만 지원합니다."})

    body = request.get_json() or {}
    code = body.get("code", "")
    qty = int(body.get("qty", 0))
    price = float(body.get("price", 0))
    if not code or qty <= 0 or price <= 0:
        return jsonify({"ok": False, "error": "종목코드, 수량, 가격을 확인해주세요."})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            excg_cd = body.get("excg_cd", "")
            result = place_buy_order_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                              code, excg_cd, qty, price)
        else:
            result = place_buy_order(pf["account_cfg"], pf["project_root"], acct_name,
                                     code, qty, int(price))
        return jsonify({"ok": True, "order_no": result.get("주문번호", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/portfolio/<name>/orderbook")
def get_orderbook(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") != "kis":
        return jsonify({"ok": False, "error": "호가 조회는 KIS 계좌만 지원합니다."})

    code = request.args.get("code", "")
    excg_cd = request.args.get("excg_cd", "")
    if not code:
        return jsonify({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            data = get_orderbook_overseas(pf["account_cfg"], pf["project_root"], acct_name, code, excg_cd)
        else:
            data = get_orderbook_domestic(pf["account_cfg"], pf["project_root"], acct_name, code)
        return jsonify({"ok": True, "data": data, "market": pf["market"]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/portfolio/<name>/chart")
def get_chart(name):
    portfolios = _get_portfolios()
    pf = next((p for p in portfolios if p["name"] == name), None)
    if pf is None:
        return jsonify({"ok": False, "error": "포트폴리오를 찾을 수 없습니다."})
    if pf.get("broker") != "kis":
        return jsonify({"ok": False, "error": "차트 조회는 KIS 계좌만 지원합니다."})

    code = request.args.get("code", "")
    excg_cd = request.args.get("excg_cd", "")
    try:
        days = int(request.args.get("days", 120))
    except ValueError:
        days = 120
    if not code:
        return jsonify({"ok": False, "error": "종목코드 필요"})

    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            candles = get_daily_chart_overseas(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, excg_cd, days=days)
        else:
            candles = get_daily_chart_domestic(pf["account_cfg"], pf["project_root"], acct_name,
                                                code, days=days)
        return jsonify({"ok": True, "candles": candles, "market": pf["market"]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)})


@app.route("/reload")
def reload_config():
    global _portfolios
    _portfolios = None
    _summary_cache.clear()
    _get_portfolios()
    return {"status": "ok", "count": len(_portfolios)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9900, debug=True)
