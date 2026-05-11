"""
Upbit Open API 잔고 조회.
kis_client / kw_client 와 동일한 (holdings, summary) 포맷을 반환한다.
ezsplit 과 독립적으로 동작하도록 PyJWT + requests 로 직접 호출한다.
"""
import hashlib
import uuid
from urllib.parse import urlencode

import jwt
import requests

UPBIT_API_URL = "https://api.upbit.com"


def _auth_headers(access_key, secret_key, query_params=None):
    payload = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    if query_params:
        q = urlencode(query_params, doseq=True).encode()
        h = hashlib.sha512()
        h.update(q)
        payload["query_hash"] = h.hexdigest()
        payload["query_hash_alg"] = "SHA512"
    token = jwt.encode(payload, secret_key, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return {"Authorization": f"Bearer {token}"}


def get_balance(acct_cfg, project_root="", acct_config_name=""):  # noqa: ARG001
    del project_root, acct_config_name  # kis/kw 클라이언트와 시그니처 맞춤용
    """
    Upbit 잔고조회.
    Returns: (holdings_list, summary_dict)
      holdings: KIS 국내와 동일 키명 (종목코드/종목명/보유수량/매수평균가/현재가/평가금액/손익금액/수익률 등)
      summary: 총매수금액/총평가금액/총손익금액/총수익률/예수금/D+2예수금
    주: Upbit 은 현물 암호화폐 거래소로 현금 결제가 즉시 반영되므로 D+2예수금 = KRW 가용잔고.
    """
    access_key = acct_cfg.get("access_key") or ""
    secret_key = acct_cfg.get("secret_key") or ""
    if not access_key or not secret_key:
        raise Exception("Upbit access_key/secret_key 가 설정되어 있지 않습니다.")

    headers = _auth_headers(access_key, secret_key)
    res = requests.get(f"{UPBIT_API_URL}/v1/accounts", headers=headers, timeout=10)
    if res.status_code != 200:
        raise Exception(f"Upbit 잔고조회 실패: {res.status_code} {res.text}")
    items = res.json() or []

    krw = 0
    coins = []
    for it in items:
        currency = it.get("currency", "")
        total = float(it.get("balance") or 0) + float(it.get("locked") or 0)
        if currency == "KRW":
            krw = int(round(total))
            continue
        if total <= 0:
            continue
        coins.append({
            "currency": currency,
            "total": total,
            "avg": float(it.get("avg_buy_price") or 0),
        })

    # 현재가 일괄 조회 — 상장폐지/미상장 티커가 섞이면 /v1/ticker 가 404 전체실패하므로
    # 먼저 /v1/market/all 로 유효한 KRW 마켓만 필터링한다.
    # 실패 시 silent 0 으로 두면 총자산이 폭락 표시되므로 raise 하여 상위 캐시가 직전 정상값을 유지하게 함.
    price_map = {}
    if coins:
        try:
            mr = requests.get(f"{UPBIT_API_URL}/v1/market/all",
                              params={"isDetails": "false"}, timeout=10)
            mr.raise_for_status()
            valid_krw = {m["market"].replace("KRW-", "")
                         for m in (mr.json() or [])
                         if str(m.get("market", "")).startswith("KRW-")}
        except Exception as e:
            raise Exception(f"Upbit /v1/market/all 조회 실패: {e}") from e

        tradable = [c["currency"] for c in coins if c["currency"] in valid_krw]
        if tradable:
            markets = ",".join(f"KRW-{c}" for c in tradable)
            try:
                r = requests.get(f"{UPBIT_API_URL}/v1/ticker",
                                 params={"markets": markets}, timeout=10)
                r.raise_for_status()
                for t in r.json() or []:
                    sym = str(t.get("market", "")).replace("KRW-", "")
                    price_map[sym] = float(t.get("trade_price") or 0)
            except Exception as e:
                raise Exception(f"Upbit /v1/ticker batch 조회 실패: {e}") from e

            # Upbit batch 가 가끔 일부 코인을 누락하는 케이스 대비: 누락된 코인은 단건 재시도
            missing = [c for c in tradable if c not in price_map]
            for c in missing:
                try:
                    r = requests.get(f"{UPBIT_API_URL}/v1/ticker",
                                     params={"markets": f"KRW-{c}"}, timeout=5)
                    if r.status_code == 200:
                        for t in r.json() or []:
                            sym = str(t.get("market", "")).replace("KRW-", "")
                            price_map[sym] = float(t.get("trade_price") or 0)
                except Exception:
                    # 단건마저 실패하면 평가금액 0 으로 둠 (개별 코인만 영향, 전체는 보존)
                    pass

    holdings = []
    total_pchs = 0
    total_evlu = 0
    for c in coins:
        qty = c["total"]
        avg = c["avg"]
        cur = price_map.get(c["currency"], 0.0)
        pchs_amt = int(round(avg * qty))
        evlu_amt = int(round(cur * qty))
        pnl = evlu_amt - pchs_amt
        rt = (pnl / pchs_amt * 100) if pchs_amt else 0.0
        total_pchs += pchs_amt
        total_evlu += evlu_amt
        holdings.append({
            "종목코드": c["currency"],
            "종목명": f"{c['currency']}/KRW",
            "보유수량": qty,
            "매수평균가": avg,
            "현재가": cur,
            "매수금액": pchs_amt,
            "평가금액": evlu_amt,
            "손익금액": pnl,
            "수익률": round(rt, 2),
            "당일손익금액": 0,
            "당일수익률": 0.0,
        })

    pnl_total = total_evlu - total_pchs
    rt_total = (pnl_total / total_pchs * 100) if total_pchs else 0.0
    summary = {
        "총매수금액": total_pchs,
        "총평가금액": total_evlu,
        "총손익금액": pnl_total,
        "총수익률": round(rt_total, 2),
        "예수금": krw,
        "D+2예수금": krw,
    }
    return holdings, summary
