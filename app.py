"""
ezadmin - Portfolio Dashboard
ezgain/ezinvest의 포트폴리오 계좌별 보유종목/잔고를 조회하는 웹 대시보드
"""
import ipaddress
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, Response, render_template, request, jsonify, make_response
from werkzeug.security import check_password_hash

from config_loader import load_all_portfolios
from kis_client import (get_domestic_balance, get_overseas_balance,
                        get_pending_orders, get_pending_orders_overseas,
                        place_sell_order, place_sell_order_overseas,
                        get_ask_price_domestic, get_ask_price_overseas,
                        cancel_order, cancel_order_overseas)

def _load_dotenv():
    """
    프로젝트 루트의 .env 파일을 로드한다.
    - '#'로 시작하는 라인은 주석
    - KEY=VALUE 형식, 값은 양쪽 공백 제거
    - 값이 작은따옴표 또는 큰따옴표로 감싸져 있으면 따옴표 제거 (해시의 '$' 보호용)
    - 이미 환경변수에 설정된 값은 덮어쓰지 않는다.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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

app = Flask(__name__)

AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"


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


def _auth_required_response():
    return Response(
        "이 자원에 접근하려면 인증이 필요합니다.",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="ezadmin"'},
    )


@app.before_request
def _wan_basic_auth():
    """LAN은 통과, WAN은 Basic Auth 강제."""
    if _is_lan(_client_ip()):
        return None

    if not AUTH_USERNAME or not AUTH_PASSWORD_HASH:
        # 외부에서 접근 중이나 인증 설정이 없음 → 접근 차단 (안전한 기본값)
        return Response(
            "외부 접근이 차단되어 있습니다. .env의 AUTH_USERNAME / AUTH_PASSWORD_HASH를 설정하세요.",
            status=503,
            mimetype="text/plain; charset=utf-8",
        )

    auth = request.authorization
    if (not auth
            or auth.username != AUTH_USERNAME
            or not check_password_hash(AUTH_PASSWORD_HASH, auth.password or "")):
        return _auth_required_response()
    return None


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


def _fetch_list_summary(pf):
    """
    포트폴리오 리스트 카드에 표시할 요약을 조회한다.
    국내/해외 통화 통일 위해 해외는 원화 환산값을 사용한다.
    Returns: {ok, 통화, 총자산, 현금, 매수금액, 평가금액, 손익, 수익률, error?}
    """
    try:
        acct_name = pf.get("account_config_name", "")
        if pf["market"] == "us":
            _, summary = get_overseas_balance(pf["account_cfg"], pf["project_root"], acct_name)
            pchs = summary.get("원화총매수금액", 0) or 0
            evlu = summary.get("원화총평가금액", 0) or 0
            pnl  = summary.get("원화총손익금액", 0) or 0
            rt   = summary.get("원화총수익률", 0) or 0
            cash = summary.get("원화예수금", 0) or 0  # 외화예수금의 원화 환산 합계
        else:
            _, summary = get_domestic_balance(pf["account_cfg"], pf["project_root"], acct_name)
            pchs = summary.get("총매수금액", 0) or 0
            evlu = summary.get("총평가금액", 0) or 0
            pnl  = summary.get("총손익금액", 0) or 0
            rt   = summary.get("총수익률", 0) or 0
            cash = summary.get("D+2예수금", 0) or 0
        return {
            "ok": True,
            "통화": "KRW",
            "총자산": evlu + (cash or 0),
            "현금": cash,
            "매수금액": pchs,
            "평가금액": evlu,
            "손익": pnl,
            "수익률": rt,
        }
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

    return render_template("index.html", grouped=grouped, owners=sorted_owners,
                           summaries=summaries, owner_totals=owner_totals)


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
        if pf["market"] == "us":
            holdings, summary = get_overseas_balance(pf["account_cfg"], pf["project_root"], acct_name)
            currency = "USD"
        else:
            holdings, summary = get_domestic_balance(pf["account_cfg"], pf["project_root"], acct_name)
            currency = "KRW"
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

    # 미체결 주문 전체 조회 (매수+매도)
    if pf["market"] == "us":
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


@app.route("/reload")
def reload_config():
    global _portfolios
    _portfolios = None
    _summary_cache.clear()
    _get_portfolios()
    return {"status": "ok", "count": len(_portfolios)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9900, debug=True)
