"""
ezadmin - Portfolio Dashboard
ezgain/ezinvest의 포트폴리오 계좌별 보유종목/잔고를 조회하는 웹 대시보드
"""
import traceback
from flask import Flask, render_template
from config_loader import load_all_portfolios
from kis_client import get_domestic_balance, get_overseas_balance

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

    # 평가금액 큰 순으로 정렬
    holdings.sort(key=lambda h: h["평가금액"], reverse=True)

    return render_template("portfolio.html", pf=pf, holdings=holdings,
                           summary=summary, currency=currency, error=None)


@app.route("/reload")
def reload_config():
    global _portfolios
    _portfolios = None
    _get_portfolios()
    return {"status": "ok", "count": len(_portfolios)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000, debug=True)
