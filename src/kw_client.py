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


def _token_dir(project_root, acct_cfg=None):
    """
    토큰 파일 디렉토리.
    - acct_cfg["token_dir"] 가 있으면 그 경로 사용 (~ 확장)
    - 없으면 project_root 의 부모 + "tokens" (기본 ~/ez/tokens)
    """
    custom = (acct_cfg or {}).get("token_dir")
    if custom:
        d = os.path.expanduser(str(custom))
    else:
        d = os.path.join(os.path.dirname(project_root), "tokens")
    os.makedirs(d, exist_ok=True)
    return d


def _token_path_new(project_root, acct_cfg):
    """
    신규 명명: kw_<account_no>.token (실전) / kw_<account_no>_mock.token (모의).
    키움 계좌번호는 '3130-5086' 형식(4+4)으로 dash 만 제거해 모든 자릿수를 사용해야
    계좌가 유일하게 식별됨 (KIS 의 8+2 와 달리 prefix 만으로는 충돌 가능).
    """
    raw = str(acct_cfg.get("account_no", "")) \
          or acct_cfg.get("my_acct_stock", "") \
          or "unknown"
    acct = raw.replace("-", "") or "unknown"
    suffix = "_mock" if acct_cfg.get("is_mock") else ""
    return os.path.join(_token_dir(project_root, acct_cfg), f"kw_{acct}{suffix}.token")


def _token_path_legacy(project_root, cfg_name, acct_cfg=None):
    """구 명명: KW-<cfg_name>.json (호환용 fallback)."""
    return os.path.join(_token_dir(project_root, acct_cfg), f"KW-{cfg_name}.json")


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
    new_path = _token_path_new(project_root, acct_cfg)
    legacy_path = _token_path_legacy(project_root, cfg_name, acct_cfg)

    if not force_new:
        # 1) 신규 명명 우선
        token = _load_cached_token(new_path)
        if token:
            return token, base_url
        # 2) 구 명명 fallback — 만료될 때까지 호환
        token = _load_cached_token(legacy_path)
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
    _save_token(new_path, token, expires_at)
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


def _kw_post(base_url, token, app_key, app_secret, api_id, body, path="/api/dostk/acnt"):
    """Kiwoom REST POST 공통. path 미지정 시 /acnt (계좌 관련). 주문은 /ordr."""
    res = requests.post(
        f"{base_url}{path}",
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


@_retry_on_token_expiry
def get_domestic_today_realized_pl(acct_cfg, project_root, acct_config_name="",
                                    holdings=None, **_kwargs):
    """
    Kiwoom 국내 당일 실현손익 조회 (ka10074 일자별실현손익요청).

    공식 스펙: body={"strt_dt", "end_dt"} (YYYYMMDD), 오늘~오늘 범위로 쿼리.
    응답 top-level 필드:
      - rlzt_pl : 실현손익 (gross, 수수료·세금 차감 전)
      - trde_cmsn : 매매수수료
      - trde_tax : 매매세금
      - tot_buy_amt / tot_sell_amt : 총매수/매도금액
      - dt_rlzt_pl : 일자별 상세 LIST (실현손익 발생일만 채워짐)

    HTS/MTS 의 "당일 실현손익" 표시값 = net = rlzt_pl - trde_cmsn - trde_tax.
    holdings 인자는 시그니처 호환용(사용 안 함). 모의(is_mock)는 미지원.

    Returns: dict(실현손익=net int) 또는 None.
    """
    if acct_cfg.get("is_mock"):
        return None

    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    today = datetime.now().strftime("%Y%m%d")

    try:
        data = _kw_post(base_url, token,
                        acct_cfg["app_key"], acct_cfg["app_secret"],
                        "ka10074", {"strt_dt": today, "end_dt": today})
    except _KWTokenExpired:
        raise
    except Exception as e:
        print(f"[kw-domestic-rlz] ka10074 실패 ({acct_config_name}): {e}")
        return None

    rlz_gross = int(_f(data.get("rlzt_pl")))
    fees = int(_f(data.get("trde_cmsn")))
    taxes = int(_f(data.get("trde_tax")))
    rlz_net = rlz_gross - fees - taxes

    dt_items = data.get("dt_rlzt_pl") or []
    sell_amt = int(_f(data.get("tot_sell_amt")))
    buy_amt = int(_f(data.get("tot_buy_amt")))

    print(f"[kw-domestic-rlz] {acct_config_name} ka10074 "
          f"gross={rlz_gross:,} cmsn={fees:,} tax={taxes:,} net={rlz_net:,} "
          f"sell={sell_amt:,} buy={buy_amt:,} dt_items={len(dt_items)}")

    return {
        "실현손익": rlz_net,
        "당일매도손익_gross": rlz_gross,
        "수수료": fees,
        "세금": taxes,
        "매도금액": sell_amt,
        "매수금액": buy_amt,
    }


def _kw_pick(d, *keys, default=None):
    """방어적 필드 추출 — 여러 후보 키 중 첫 값(공백 아님) 반환."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _kw_normalize_trade_kind(v):
    """주문구분 정규화: '1매수'/'2매도' / '+매수'/'-매도' / '매수'/'매도' / '01'/'02' 등을 '매수'/'매도' 로."""
    s = str(v or "").strip()
    if not s:
        return ""
    # 한글 포함 시 우선 매칭
    if "매수" in s:
        return "매수"
    if "매도" in s:
        return "매도"
    # 숫자/기호 패턴
    if s in ("1", "+1", "+", "01", "02-매수"):
        return "매수"
    if s in ("2", "-1", "-", "02", "01-매도"):
        return "매도"
    return s


@_retry_on_token_expiry
def get_pending_orders(acct_cfg, project_root, acct_config_name=""):
    """
    Kiwoom 국내 금일 미체결 주문 조회 (ka10075 실시간미체결요청).

    Body: stex_tp="0"(통합) / trde_tp="0"(전체) / all_stk_tp="0"(전체) / stk_cd=""(전체)
    응답에 다양한 필드명이 있을 수 있어 _kw_pick 으로 방어 처리.

    Returns: list of dict(주문구분/종목코드/종목명/주문수량/주문단가/주문번호/krx_fwdg_ord_orgno)
    """
    try:
        token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    except Exception as e:
        print(f"[kw-pending] token error: {e}")
        return []

    body = {
        "all_stk_tp": "0",
        "trde_tp": "0",
        "stex_tp": "0",
        "stk_cd": "",
    }
    try:
        data = _kw_post(base_url, token,
                        acct_cfg["app_key"], acct_cfg["app_secret"],
                        "ka10075", body)
    except _KWTokenExpired:
        raise
    except Exception as e:
        print(f"[kw-pending] ka10075 실패 ({acct_config_name}): {e}")
        return []

    # 응답 본문 list 키 후보: oso, oso_lst, output, list 등
    rows = (data.get("oso") or data.get("oso_lst") or data.get("output")
            or data.get("list") or [])

    result = []
    for it in rows:
        rmn = int(_f(_kw_pick(it, "oso_qty", "rmnd_qty", "rmn_qty", "ord_rmnd_qty", "ord_rmn_qty", default=0)))
        ord_qty = int(_f(_kw_pick(it, "ord_qty", default=0)))
        cntr_qty = int(_f(_kw_pick(it, "cntr_qty", "ccld_qty", default=0)))
        if rmn == 0 and ord_qty > 0:
            rmn = ord_qty - cntr_qty
        if rmn <= 0:
            continue
        kind = _kw_normalize_trade_kind(_kw_pick(it, "io_tp_nm", "trde_tp_nm", "trde_tp", "io_tp", default=""))
        code = _kw_pick(it, "stk_cd", "code", default="") or ""
        # 키움 종목코드 가끔 'A' 접두사 있음
        if code.startswith("A") and len(code) == 7:
            code = code[1:]
        ord_pric = int(_f(_kw_pick(it, "ord_pric", "ord_unpr", "ord_price", default=0)))
        result.append({
            "주문구분": kind,
            "종목코드": code,
            "종목명": _kw_pick(it, "stk_nm", "prdt_name", default="") or code,
            "주문수량": rmn,
            "주문단가": ord_pric,
            "주문번호": str(_kw_pick(it, "ord_no", "odno", default="") or ""),
            "krx_fwdg_ord_orgno": "",  # 키움은 별도 필드 없이 ord_no 만으로 취소 가능
        })
    return result


@_retry_on_token_expiry
def get_today_trades_domestic(acct_cfg, project_root, acct_config_name=""):
    """
    Kiwoom 국내 금일 체결 내역 (ka10076 체결내역요청).

    Body: ord_dt=오늘 / qry_tp="1"(체결내역) / sell_tp="0"(전체) / stk_cd=""
    Returns: list of dict(시각, 종목코드, 종목명, 주문구분, 체결단가, 체결수량, 체결금액)
            최신 → 오래된 순.
    """
    try:
        token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    except Exception as e:
        print(f"[kw-trades] token error: {e}")
        return []

    today = datetime.now().strftime("%Y%m%d")
    body = {
        "ord_dt": today,
        "qry_tp": "1",     # 체결내역
        "sell_tp": "0",    # 전체
        "stk_cd": "",
        "fr_ord_no": "",
        "stex_tp": "0",
    }
    try:
        data = _kw_post(base_url, token,
                        acct_cfg["app_key"], acct_cfg["app_secret"],
                        "ka10076", body)
    except _KWTokenExpired:
        raise
    except Exception as e:
        print(f"[kw-trades] ka10076 실패 ({acct_config_name}): {e}")
        return []

    rows = (data.get("cntr") or data.get("cntr_lst") or data.get("output")
            or data.get("list") or [])

    result = []
    for it in rows:
        cntr_qty = int(_f(_kw_pick(it, "cntr_qty", "ccld_qty", default=0)))
        if cntr_qty <= 0:
            continue
        kind = _kw_normalize_trade_kind(_kw_pick(it, "io_tp_nm", "trde_tp_nm", "trde_tp", "io_tp", default=""))
        code = _kw_pick(it, "stk_cd", "code", default="") or ""
        if code.startswith("A") and len(code) == 7:
            code = code[1:]
        unit = int(_f(_kw_pick(it, "cntr_pric", "cntr_unpr", default=0)))
        amt_raw = _f(_kw_pick(it, "cntr_amt", "tot_ccld_amt", default=0))
        amt = int(amt_raw) if amt_raw else unit * cntr_qty
        # 시각: HHMMSS 또는 HH:MM:SS 형태로 응답
        tm = str(_kw_pick(it, "cntr_tm", "ord_tmd", "tm", default="") or "")
        if len(tm) == 6 and tm.isdigit():
            tm = f"{tm[:2]}:{tm[2:4]}:{tm[4:]}"
        result.append({
            "시각": tm,
            "종목코드": code,
            "종목명": _kw_pick(it, "stk_nm", "prdt_name", default="") or code,
            "주문구분": kind,
            "체결단가": unit,
            "체결수량": cntr_qty,
            "체결금액": amt,
        })
    result.sort(key=lambda x: x["시각"], reverse=True)
    return result


@_retry_on_token_expiry
def place_buy_order(acct_cfg, project_root, acct_config_name, stock_code, qty, price):
    """
    Kiwoom 국내 주식 매수주문 (kt10000).
    Returns: {"주문번호": str}
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": str(stock_code),
        "ord_qty": str(int(qty)),
        "ord_uv": str(int(price)),
        "trde_tp": "0",      # 0=보통 (지정가)
        "cond_uv": "",
    }
    data = _kw_post(base_url, token,
                    acct_cfg["app_key"], acct_cfg["app_secret"],
                    "kt10000", body, path="/api/dostk/ordr")
    return {"주문번호": str(data.get("ord_no") or data.get("odno") or "")}


@_retry_on_token_expiry
def place_sell_order(acct_cfg, project_root, acct_config_name, stock_code, qty, price):
    """
    Kiwoom 국내 주식 매도주문 (kt10001).
    Returns: {"주문번호": str}
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    body = {
        "dmst_stex_tp": "KRX",
        "stk_cd": str(stock_code),
        "ord_qty": str(int(qty)),
        "ord_uv": str(int(price)),
        "trde_tp": "0",
        "cond_uv": "",
    }
    data = _kw_post(base_url, token,
                    acct_cfg["app_key"], acct_cfg["app_secret"],
                    "kt10001", body, path="/api/dostk/ordr")
    return {"주문번호": str(data.get("ord_no") or data.get("odno") or "")}


@_retry_on_token_expiry
def get_orderbook_domestic(acct_cfg, project_root, acct_config_name, stock_code):
    """
    Kiwoom 국내 10단계 호가 + 현재가 조회 (ka10004 주식호가요청).
    Returns KIS get_orderbook_domestic 와 동일 dict 구조.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    body = {"stk_cd": str(stock_code)}
    data = _kw_post(base_url, token,
                    acct_cfg["app_key"], acct_cfg["app_secret"],
                    "ka10004", body, path="/api/dostk/mrkcond")

    def _i(v):
        try:
            return int(float(str(v or 0).replace(",", "").replace("+", "").replace("-", "-")))
        except (TypeError, ValueError):
            return 0

    def _abs_i(v):
        try:
            return abs(int(float(str(v or 0).replace(",", "").replace("+", ""))))
        except (TypeError, ValueError):
            return 0

    # 매도호가: sel_pric_1~10 (낮은가→높은가 = 1→10), 수량 sel_req_1~10
    asks = []
    for i in range(1, 11):
        # 키움 호가는 1=가까운 → 10=먼. KIS 와 동일하게 1->10 순으로.
        p = _i(_kw_pick(data, f"sel_pric_{i}", f"sel_fpr_bid_{i}", default=0))
        q = _abs_i(_kw_pick(data, f"sel_req_{i}", f"sel_fpr_req_{i}", default=0))
        asks.append((p, q))
    bids = []
    for i in range(1, 11):
        p = _i(_kw_pick(data, f"buy_pric_{i}", f"buy_fpr_bid_{i}", default=0))
        q = _abs_i(_kw_pick(data, f"buy_req_{i}", f"buy_fpr_req_{i}", default=0))
        bids.append((p, q))

    return {
        "current": _i(_kw_pick(data, "cur_prc", "stck_prpr", default=0)),
        "change": _i(_kw_pick(data, "pred_pre", "prdy_vrss", default=0)),
        "change_rate": float(_f(_kw_pick(data, "prc_chg_rt", "prdy_ctrt", default=0))),
        "open": _i(_kw_pick(data, "open_pric", "stck_oprc", default=0)),
        "high": _i(_kw_pick(data, "high_pric", "stck_hgpr", default=0)),
        "low": _i(_kw_pick(data, "low_pric", "stck_lwpr", default=0)),
        "asks": asks,
        "bids": bids,
    }


@_retry_on_token_expiry
def get_daily_chart_domestic(acct_cfg, project_root, acct_config_name, stock_code, days=120):
    """
    Kiwoom 국내 일봉 히스토리 (ka10081 주식일봉차트조회).
    Returns: list[dict(date, open, high, low, close, volume)] 오래된→최신 순.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    today = datetime.now().strftime("%Y%m%d")
    body = {
        "stk_cd": str(stock_code),
        "base_dt": today,
        "upd_stkpc_tp": "1",   # 1=수정주가
    }
    data = _kw_post(base_url, token,
                    acct_cfg["app_key"], acct_cfg["app_secret"],
                    "ka10081", body, path="/api/dostk/chart")

    rows = (data.get("stk_dt_pole_chart_qry") or data.get("output") or data.get("list")
            or data.get("output2") or [])

    def _i(v):
        try:
            return int(float(str(v or 0).replace(",", "").replace("+", "").replace("--", "-")))
        except (TypeError, ValueError):
            return 0

    out = []
    for r in rows:
        dt = str(_kw_pick(r, "dt", "date", "stck_bsop_date", default="") or "")
        if len(dt) == 8 and dt.isdigit():
            iso = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
        else:
            iso = dt
        if not iso:
            continue
        out.append({
            "date": iso,
            "open":  _i(_kw_pick(r, "open_pric", "stck_oprc", default=0)),
            "high":  _i(_kw_pick(r, "high_pric", "stck_hgpr", default=0)),
            "low":   _i(_kw_pick(r, "low_pric",  "stck_lwpr", default=0)),
            "close": _i(_kw_pick(r, "cur_prc", "stck_clpr", default=0)),
            "volume": _i(_kw_pick(r, "trde_qty", "acml_vol", default=0)),
        })
    out.sort(key=lambda x: x["date"])  # 오래된 → 최신
    return out[-days:]


@_retry_on_token_expiry
def cancel_order(acct_cfg, project_root, acct_config_name, order_no, stk_cd, qty=0):
    """
    Kiwoom 국내 주문 취소 (kt10003 주식주문취소요청).

    Body:
      - dmst_stex_tp: "KRX" (국내 정규시장)
      - orig_ord_no:  원주문번호
      - stk_cd:       종목코드
      - cncl_qty:     취소수량 ("0" 이면 잔량 전체 취소)

    Returns: {"주문번호": "<취소주문번호>"} 형태로 KIS 와 동일하게 맞춤.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    body = {
        "dmst_stex_tp": "KRX",
        "orig_ord_no": str(order_no),
        "stk_cd": str(stk_cd),
        "cncl_qty": str(int(qty)) if qty else "0",
    }
    data = _kw_post(base_url, token,
                    acct_cfg["app_key"], acct_cfg["app_secret"],
                    "kt10003", body, path="/api/dostk/ordr")
    return {"주문번호": str(data.get("ord_no") or data.get("odno") or "")}
