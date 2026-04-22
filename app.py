"""
ezadmin - Portfolio Dashboard
ezgain/ezinvest의 포트폴리오 계좌별 보유종목/잔고를 조회하는 웹 대시보드
"""
import traceback
from flask import Flask, render_template, request, jsonify, make_response
from config_loader import load_all_portfolios
from kis_client import (get_domestic_balance, get_overseas_balance,
                        get_pending_orders, get_pending_orders_overseas,
                        place_sell_order, place_sell_order_overseas,
                        get_ask_price_domestic, get_ask_price_overseas,
                        cancel_order, cancel_order_overseas)

app = Flask(__name__)

# 포트폴리오 목록 캐시 (앱 시작 시 로드)
_portfolios = None


def _get_portfolios():
    global _portfolios
    if _portfolios is None:
        _portfolios = load_all_portfolios()
    return _portfolios


@app.route("/")
def index():
    portfolios = _get_portfolios()
    owners_order = ["bmchae", "hitomato", "0eh", "9bong"]
    grouped = {}
    for pf in portfolios:
        owner = pf.get("owner", "unknown")
        grouped.setdefault(owner, []).append(pf)
    # 정렬: 지정된 순서 우선, 나머지는 뒤에
    sorted_owners = [o for o in owners_order if o in grouped]
    sorted_owners += [o for o in grouped if o not in owners_order]
    return render_template("index.html", grouped=grouped, owners=sorted_owners)


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
    _get_portfolios()
    return {"status": "ok", "count": len(_portfolios)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9900, debug=True)
