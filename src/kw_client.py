"""
키움증권 REST API 클라이언트 (ezadmin용 경량 버전).

지원 기능: 국내/해외 계좌 잔고 조회 + 토큰 자동 재발급/재시도
미지원: 주문, 취소, 미체결 조회, 호가 조회

ezsplit의 broker_kw.py 를 참고하되 ezadmin의 포트폴리오 포맷에 맞춰 단순화했다.
"""
import json
import os
from datetime import datetime, timedelta

import requests

MOCK_URL = "https://mockapi.kiwoom.com"
REAL_URL = "https://api.kiwoom.com"


class _KWTokenExpired(Exception):
    """Kiwoom 토큰 만료/무효를 나타내는 예외."""


def _token_path(project_root, cfg_name):
    token_dir = os.path.join(project_root, "token")
    os.makedirs(token_dir, exist_ok=True)
    return os.path.join(token_dir, f"KW-{cfg_name}.json")


def _load_cached_token(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        expires = datetime.fromisoformat(data["expires"])
        if datetime.now() < expires:
            return data["token"]
    except Exception:
        return None
    return None


def _save_token(path, token, expires_at):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"token": token, "expires": expires_at.isoformat()}, f)


def _get_token(acct_cfg, project_root, acct_config_name="", force_new=False):
    """
    Kiwoom OAuth 토큰을 반환. force_new=True면 강제 재발급.
    Returns: (token, base_url)
    """
    base_url = MOCK_URL if acct_cfg.get("is_mock") else REAL_URL
    cfg_name = acct_config_name.replace(".yaml", "") if acct_config_name else "kw"
    path = _token_path(project_root, cfg_name)

    if not force_new:
        token = _load_cached_token(path)
        if token:
            return token, base_url

    res = requests.post(
        f"{base_url}/oauth2/token",
        headers={"content-type": "application/json"},
        json={
            "grant_type": "client_credentials",
            "appkey": acct_cfg["app_key"],
            "secretkey": acct_cfg["app_secret"],
        },
        timeout=10,
    )
    if res.status_code != 200:
        raise Exception(f"Kiwoom 토큰 발급 실패: {res.status_code} {res.text}")
    data = res.json()
    token = data.get("token")
    if not token:
        raise Exception(f"Kiwoom 토큰 응답 오류: {data}")
    expires_in = int(data.get("expires_in", 86400))
    # 1시간 마진을 두고 만료 처리
    expires_at = datetime.now() + timedelta(seconds=max(expires_in - 3600, 60))
    _save_token(path, token, expires_at)
    return token, base_url


def _headers(token, app_key, app_secret, api_id=""):
    """Kiwoom REST API 는 TR 식별자로 'api-id' 헤더를 사용한다."""
    h = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "secretkey": app_secret,
    }
    if api_id:
        h["api-id"] = api_id
    return h


def _retry_on_token_expiry(fn):
    """_KWTokenExpired 가 발생하면 강제 재발급 후 1회 재시도."""
    def wrapper(acct_cfg, project_root, acct_config_name="", *args, **kwargs):
        try:
            return fn(acct_cfg, project_root, acct_config_name, *args, **kwargs)
        except _KWTokenExpired:
            print(f"[kw-token] 재발급 후 재시도 ({acct_config_name})")
            _get_token(acct_cfg, project_root, acct_config_name, force_new=True)
            return fn(acct_cfg, project_root, acct_config_name, *args, **kwargs)
    return wrapper


def _is_token_expired(res, data=None):
    """응답에서 토큰 만료로 판단될 만한 시그널 탐지."""
    if res.status_code in (401, 403):
        return True
    if data is None:
        try:
            data = res.json()
        except Exception:
            return False
    if isinstance(data, dict):
        msg = (str(data.get("return_msg", "")) + " " + str(data.get("error_description", ""))).lower()
        if "token" in msg and ("expire" in msg or "invalid" in msg or "만료" in msg):
            return True
    return False


def _kw_post(base_url, token, app_key, app_secret, api_id, body):
    """Kiwoom REST POST 공통. 응답 본문(dict)과 토큰 만료 여부 반환."""
    res = requests.post(
        f"{base_url}/api/dostk/acnt",
        headers=_headers(token, app_key, app_secret, api_id),
        json=body,
        timeout=15,
    )
    try:
        data = res.json()
    except Exception:
        data = None
    if _is_token_expired(res, data):
        raise _KWTokenExpired()
    if res.status_code != 200:
        raise Exception(f"Kiwoom {api_id} HTTP {res.status_code}: {res.text[:200]}")
    if not isinstance(data, dict):
        raise Exception(f"Kiwoom {api_id} 응답 파싱 실패")
    rc = str(data.get("return_code", ""))
    if rc not in ("0", ""):
        raise Exception(f"Kiwoom {api_id} 오류: {data.get('return_msg')}")
    return data


def _f(val):
    """문자열/숫자를 float로 안전하게 변환 (콤마/공백 제거)."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@_retry_on_token_expiry
def get_domestic_balance(acct_cfg, project_root, acct_config_name=""):
    """
    Kiwoom 국내 계좌 잔고 조회.
    - kt00018: 계좌평가잔고내역요청 (보유종목 + 계좌 합계)
    - kt00001: 예수금상세현황요청 (D+2 예수금)
    Returns: (holdings, summary)
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    app_key = acct_cfg["app_key"]
    app_secret = acct_cfg["app_secret"]

    # 보유종목 + 계좌 합계
    pos_data = _kw_post(base_url, token, app_key, app_secret,
                        "kt00018",
                        {"qry_tp": "1", "dmst_stex_tp": "KRX"})

    # Kiwoom 응답 필드명이 버전별로 달라 여러 후보 지원
    items = (pos_data.get("acnt_evlt_remn_indv_tot")
             or pos_data.get("output1")
             or pos_data.get("output")
             or [])
    print(f"[kw-balance] kt00018 items={len(items)} top-level-keys={list(pos_data.keys())[:20]}")
    # 계좌 합계 레벨 필드 덤프
    _kw_top_debug = {k: v for k, v in pos_data.items() if not isinstance(v, (list, dict))}
    print(f"[kw-balance] kt00018 totals: {_kw_top_debug}")
    if items:
        print(f"[kw-balance] FULL sample item #0: {items[0]}")

    holdings = []
    total_pchs = 0.0
    total_evlu = 0.0
    for item in items:
        qty = int(_f(item.get("rmnd_qty") or item.get("hldg_qty") or 0))
        if qty <= 0:
            continue
        avg = _f(item.get("pur_pric") or item.get("avg_buy_prc") or item.get("avg_prc"))
        cur = _f(item.get("cur_prc"))
        evlu = _f(item.get("evlt_amt") or item.get("eval_amt"))
        pchs_raw = _f(item.get("pur_amt") or item.get("stk_pur_amt"))
        pchs = pchs_raw if pchs_raw > 0 else (avg * qty)
        pnl = evlu - pchs
        rt = (pnl / pchs * 100) if pchs else 0.0
        holdings.append({
            "종목코드": (item.get("stk_cd") or "").strip().lstrip("A"),
            "종목명": (item.get("stk_nm") or "").strip(),
            "보유수량": qty,
            "매수평균가": avg,
            "현재가": cur,
            "매수금액": int(pchs),
            "평가금액": int(evlu),
            "손익금액": int(pnl),
            "수익률": round(rt, 2),
            "당일손익금액": None,
            "당일수익률": None,
        })
        total_pchs += pchs
        total_evlu += evlu

    # 예수금 (kt00001)
    cash = 0
    try:
        cash_data = _kw_post(base_url, token, app_key, app_secret,
                             "kt00001",
                             {"qry_tp": "3"})
        cash = int(_f(cash_data.get("d2_entra") or cash_data.get("entr") or 0))
    except Exception as e:
        print(f"[kw-cash] 예수금 조회 실패(kt00001): {e}")

    # Kiwoom 응답 계좌 합계 우선 사용 (아이템 합산은 backup)
    api_tot_pchs = _f(pos_data.get("tot_pur_amt") or pos_data.get("pchs_amt_smtl_amt"))
    api_tot_evlu = _f(pos_data.get("tot_evlt_amt") or pos_data.get("evlu_amt_smtl_amt"))
    api_tot_pnl  = _f(pos_data.get("tot_evltv_prft") or pos_data.get("evlu_pfls_smtl_amt"))
    api_tot_rt   = _f(pos_data.get("tot_prft_rt") or pos_data.get("evlu_pfls_rt"))

    sum_pchs = api_tot_pchs if api_tot_pchs > 0 else total_pchs
    sum_evlu = api_tot_evlu if api_tot_evlu > 0 else total_evlu
    sum_pnl  = api_tot_pnl if api_tot_pnl != 0 else (sum_evlu - sum_pchs)
    sum_rt   = api_tot_rt if api_tot_rt != 0 else (
        round(sum_pnl / sum_pchs * 100, 2) if sum_pchs else 0)

    summary = {
        "총매수금액": int(sum_pchs),
        "총평가금액": int(sum_evlu),
        "총손익금액": int(sum_pnl),
        "총수익률": round(sum_rt, 2),
        "예수금": cash,
        "D+2예수금": cash,
    }
    return holdings, summary


@_retry_on_token_expiry
def get_overseas_balance(acct_cfg, project_root, acct_config_name=""):
    """
    Kiwoom 해외 계좌 잔고 조회 (USD 기준).
    환율 정보를 별도로 받지 않으므로 원화 환산 필드는 생략한다.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    res = requests.get(
        f"{base_url}/api/ovsstk/acntbal",
        headers=_headers(token, acct_cfg["app_key"], acct_cfg["app_secret"]),
        params={"acnt_no": acct_cfg["account_no"], "natn_cd": "840", "crcy_cd": "USD"},
        timeout=15,
    )
    try:
        data = res.json()
    except Exception:
        data = None
    if _is_token_expired(res, data):
        raise _KWTokenExpired()
    if res.status_code != 200:
        raise Exception(f"Kiwoom 해외잔고조회 실패: {res.status_code} {res.text[:200]}")
    if not isinstance(data, dict):
        raise Exception("Kiwoom 해외잔고조회 응답 파싱 실패")
    if str(data.get("return_code")) != "0":
        raise Exception(f"Kiwoom 해외잔고조회 오류: {data.get('return_msg')}")

    output = data.get("output") or {}
    output1 = data.get("output1") or []

    holdings = []
    total_pchs = 0.0
    total_evlu = 0.0
    for item in output1:
        qty = float(item.get("hldg_qty", 0) or 0)
        if qty <= 0:
            continue
        avg = float(item.get("avg_buy_prc", 0) or 0)
        cur = float(item.get("cur_prc", 0) or 0)
        evlu = float(item.get("eval_amt", 0) or 0)
        pchs = avg * qty
        pnl = evlu - pchs
        rt = (pnl / pchs * 100) if pchs else 0.0
        holdings.append({
            "종목코드": item.get("stk_cd", ""),
            "종목명": item.get("stk_nm", ""),
            "보유수량": qty,
            "매수평균가": avg,
            "현재가": cur,
            "매수금액": pchs,
            "평가금액": evlu,
            "손익금액": pnl,
            "수익률": round(rt, 2),
            "당일손익금액": None,
            "당일수익률": None,
            "거래소코드": "NAS",  # default, Kiwoom 응답에 명시적 필드 없음
        })
        total_pchs += pchs
        total_evlu += evlu

    usd_cash = float(output.get("frcr_ord_psbl_amt", 0) or 0)
    total_pnl = total_evlu - total_pchs
    summary = {
        "총매수금액": total_pchs,
        "총평가금액": total_evlu,
        "총손익금액": total_pnl,
        "총수익률": round(total_pnl / total_pchs * 100, 2) if total_pchs else 0,
        "외화예수금": usd_cash,
        # 환율 정보가 없어 원화 환산은 생략 (필요 시 환율 API 연동 추가)
        "환율": 0,
    }
    return holdings, summary
