"""
KIS Open API를 직접 호출하여 계좌 잔고를 조회한다.
ezgain/ezinvest의 기존 모듈을 import하지 않고 독립적으로 동작한다.
(기존 모듈은 글로벌 상태를 사용하여 여러 계좌를 동시에 조회할 수 없음)
"""
import json
import os
import time
from datetime import datetime

import requests
import yaml


def _read_token_file(path):
    """토큰 파일을 읽어 만료되지 않았으면 토큰 문자열을 반환, 아니면 None."""
    try:
        with open(path, encoding="utf-8") as f:
            tkg = yaml.safe_load(f)
        if tkg and "token" in tkg and "valid-date" in tkg:
            exp_dt = datetime.strftime(tkg["valid-date"], "%Y-%m-%d %H:%M:%S")
            now_dt = datetime.today().strftime("%Y-%m-%d %H:%M:%S")
            if exp_dt > now_dt:
                return tkg["token"]
    except Exception:
        pass
    return None


def _find_existing_token(token_dir, cfg_name):
    """
    프로젝트 token/ 디렉토리에서 cfg_name에 해당하는 유효한 토큰을 찾는다.
    오늘 날짜 파일 우선, 없으면 같은 cfg_name의 다른 날짜 파일도 확인한다.
    """
    today = datetime.today().strftime("%Y%m%d")
    today_file = os.path.join(token_dir, f"KIS-{cfg_name}-{today}")

    # 오늘 날짜 파일 우선
    if os.path.exists(today_file):
        token = _read_token_file(today_file)
        if token:
            return token

    # 같은 cfg_name의 다른 날짜 파일 확인 (최신 파일 우선)
    prefix = f"KIS-{cfg_name}-"
    if os.path.isdir(token_dir):
        candidates = sorted(
            [f for f in os.listdir(token_dir) if f.startswith(prefix)],
            reverse=True,
        )
        for fname in candidates:
            token = _read_token_file(os.path.join(token_dir, fname))
            if token:
                return token

    return None


class _KISTokenExpired(Exception):
    """KIS 서버가 토큰 만료(EGW00123)를 반환했을 때 발생."""


def _get_token(acct_cfg, project_root, acct_config_name="", force_new=False):
    """
    OAuth2 토큰을 반환한다.
    1) force_new=False 이면 token/ 디렉토리에서 기존 유효 토큰을 찾아 재사용
    2) 없거나 force_new=True 이면 새로 발급하고 파일 덮어쓰기
    """
    svr = acct_cfg.get("server", "prod")
    base_url = acct_cfg[svr]
    app_key = acct_cfg["my_app"]
    app_secret = acct_cfg["my_sec"]

    # cfg_name: account config 파일명에서 .yaml 제거 (ezgain 방식과 동일)
    cfg_name = acct_config_name.replace(".yaml", "") if acct_config_name else acct_cfg.get("my_htsid", "unknown")

    token_dir = os.path.join(project_root, "token")
    os.makedirs(token_dir, exist_ok=True)

    if not force_new:
        # 기존 토큰 확인
        token = _find_existing_token(token_dir, cfg_name)
        if token:
            return token, base_url

    # 새 토큰 발급
    url = f"{base_url}/oauth2/tokenP"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "User-Agent": acct_cfg.get("my_agent", ""),
    }
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    res = requests.post(url, data=json.dumps(body), headers=headers)
    if res.status_code != 200:
        raise Exception(f"토큰 발급 실패: {res.status_code} {res.text}")

    data = res.json()
    new_token = data["access_token"]
    expired = data["access_token_token_expired"]

    # 토큰 저장
    today = datetime.today().strftime("%Y%m%d")
    token_file = os.path.join(token_dir, f"KIS-{cfg_name}-{today}")
    with open(token_file, "w", encoding="utf-8") as f:
        valid_date = datetime.strptime(expired, "%Y-%m-%d %H:%M:%S")
        f.write(f"token: {new_token}\n")
        f.write(f"valid-date: {valid_date}\n")

    return new_token, base_url


def _retry_on_token_expiry(fn):
    """
    대상 함수가 KIS API 호출 중 EGW00123(토큰 만료)을 감지하면
    _KISTokenExpired를 raise하도록 구현되어 있을 때,
    강제 재발급 후 1회 재시도한다.
    """
    def wrapper(acct_cfg, project_root, acct_config_name="", *args, **kwargs):
        try:
            return fn(acct_cfg, project_root, acct_config_name, *args, **kwargs)
        except _KISTokenExpired:
            print(f"[token] EGW00123 감지 → 강제 재발급 후 재시도 ({acct_config_name})")
            _get_token(acct_cfg, project_root, acct_config_name, force_new=True)
            return fn(acct_cfg, project_root, acct_config_name, *args, **kwargs)
    return wrapper


def _is_token_expired_response(res):
    """
    KIS 응답(Response)에서 EGW00123(토큰 만료)인지 판별.
    status_code와 무관하게 JSON 본문을 먼저 검사한다.
    """
    try:
        body = res.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    if body.get("msg_cd") == "EGW00123":
        return True
    if body.get("error_code") == "EGW00123":
        return True
    return False


def _make_headers(token, app_key, app_secret, tr_id, agent=""):
    return {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "User-Agent": agent,
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
        "tr_cont": "",
    }


@_retry_on_token_expiry
def get_domestic_balance(acct_cfg, project_root, acct_config_name=""):
    """
    국내주식 잔고조회.
    Returns: (holdings_list, summary_dict)
      holdings_list: [{종목명, 종목코드, 보유수량, 매수평균가, 현재가, 평가금액, 손익금액, 수익률}, ...]
      summary_dict: {총매수금액, 총평가금액, 총손익금액, 총수익률, 예수금}
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            "TTTC8434R", acct_cfg.get("my_agent", ""))

    all_holdings = []
    fk100 = ""
    nk100 = ""
    output2 = [{}]

    for _ in range(10):  # pagination
        params = {
            "CANO": acct_no,
            "ACNT_PRDT_CD": prod_cd,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": fk100,
            "CTX_AREA_NK100": nk100,
        }

        res = requests.get(f"{base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
                           headers=headers, params=params)
        if _is_token_expired_response(res):
            raise _KISTokenExpired()
        if res.status_code != 200:
            raise Exception(f"잔고조회 실패: {res.status_code} {res.text}")

        data = res.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"잔고조회 오류: {data.get('msg_cd')} {data.get('msg1')}")

        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])

        for item in output1:
            qty = int(item.get("hldg_qty", 0))
            if qty == 0:
                continue
            day_chg = int(item.get("bfdy_cprs_icdc", 0))
            all_holdings.append({
                "종목코드": item.get("pdno", ""),
                "종목명": item.get("prdt_name", ""),
                "보유수량": qty,
                "매수평균가": float(item.get("pchs_avg_pric", 0)),
                "현재가": int(item.get("prpr", 0)),
                "매수금액": int(item.get("pchs_amt", 0)),
                "평가금액": int(item.get("evlu_amt", 0)),
                "손익금액": int(item.get("evlu_pfls_amt", 0)),
                "수익률": float(item.get("evlu_pfls_rt", 0)),
                "당일손익금액": day_chg * qty,
                "당일수익률": float(item.get("fltt_rt", 0)),
            })

        # 연속조회 확인
        tr_cont = res.headers.get("tr_cont", "")
        if tr_cont in ("M", "F"):
            fk100 = data.get("ctx_area_fk100", "")
            nk100 = data.get("ctx_area_nk100", "")
            headers["tr_cont"] = "N"
            time.sleep(0.1)
        else:
            break

    # summary
    summary = {}
    if output2:
        s = output2[0] if isinstance(output2, list) else output2
        summary = {
            "총매수금액": int(s.get("pchs_amt_smtl_amt", 0)),
            "총평가금액": int(s.get("evlu_amt_smtl_amt", 0)),
            "총손익금액": int(s.get("evlu_pfls_smtl_amt", 0)),
            "총수익률": float(s.get("evlu_pfls_rt", 0)) if s.get("evlu_pfls_rt") else 0,
            "예수금": int(s.get("dnca_tot_amt", 0)),
            "D+2예수금": int(s.get("prvs_rcdl_excc_amt", 0)),
        }
        if summary["총매수금액"] > 0:
            summary["총수익률"] = round(summary["총손익금액"] / summary["총매수금액"] * 100, 2)

    return all_holdings, summary


@_retry_on_token_expiry
def get_overseas_balance(acct_cfg, project_root, acct_config_name=""):
    """
    해외주식 체결기준현재잔고 조회.
    Returns: (holdings_list, summary_dict)
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            "CTRP6504R", acct_cfg.get("my_agent", ""))

    all_output1 = []
    output2 = []
    output3 = {}
    fk200 = ""
    nk200 = ""

    for _ in range(20):
        params = {
            "CANO": acct_no,
            "ACNT_PRDT_CD": prod_cd,
            "WCRC_FRCR_DVSN_CD": "02",  # 외화
            "NATN_CD": "000",  # 전체
            "TR_MKET_CD": "00",  # 전체
            "INQR_DVSN_CD": "00",  # 전체
            "CTX_AREA_FK200": fk200,
            "CTX_AREA_NK200": nk200,
        }

        res = requests.get(f"{base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance",
                           headers=headers, params=params)
        if _is_token_expired_response(res):
            raise _KISTokenExpired()
        if res.status_code != 200:
            raise Exception(f"해외잔고조회 실패: {res.status_code} {res.text}")

        data = res.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"해외잔고조회 오류: {data.get('msg_cd')} {data.get('msg1')}")

        page_output1 = data.get("output1", [])
        all_output1.extend(page_output1)
        output2 = data.get("output2", [])
        output3 = data.get("output3", {})

        tr_cont = res.headers.get("tr_cont", "")
        if tr_cont in ("M", "F"):
            fk200 = data.get("ctx_area_fk200", "") or data.get("CTX_AREA_FK200", "")
            nk200 = data.get("ctx_area_nk200", "") or data.get("CTX_AREA_NK200", "")
            headers["tr_cont"] = "N"
            time.sleep(0.1)
        else:
            break

    output1 = all_output1

    holdings = []
    for item in output1:
        # ord_psbl_qty1: 주문가능수량 (당일 매수분 포함)
        # cblc_qty13: 결제기준잔고수량 (당일 매수분 미포함)
        qty = float(item.get("ord_psbl_qty1", 0)) or float(item.get("cblc_qty13", 0))
        if qty == 0:
            continue
        day_chg = float(item.get("bfdy_cprs_icdc", 0))
        holdings.append({
            "종목코드": item.get("pdno", ""),
            "종목명": item.get("prdt_name", ""),
            "보유수량": qty,
            "매수평균가": float(item.get("avg_unpr3", 0)),
            "현재가": float(item.get("ovrs_now_pric1", 0)),
            "매수금액": float(item.get("frcr_pchs_amt", 0)),
            "평가금액": float(item.get("frcr_evlu_amt2", 0)),
            "손익금액": float(item.get("evlu_pfls_amt2", 0)),
            "수익률": float(item.get("evlu_pfls_rt1", 0)),
            "당일손익금액": day_chg * qty,
            "당일수익률": float(item.get("fltt_rt", 0)),
            "거래소코드": item.get("ovrs_excg_cd", ""),
        })

    # 환율: output2의 frst_bltn_exrt 사용 (output3에 bass_exrt 없음)
    exrt = 0.0
    if isinstance(output2, list) and output2:
        exrt = float(output2[0].get("frst_bltn_exrt", 0))

    # output3에서 합계 추출 (올바른 필드명)
    summary = {}
    if output3:
        s = output3 if isinstance(output3, dict) else (output3[0] if output3 else {})
        usd_pchs = float(s.get("pchs_amt_smtl", 0))
        usd_evlu = float(s.get("evlu_amt_smtl", 0))
        usd_pnl  = float(s.get("evlu_pfls_amt_smtl", 0))
        usd_rt   = float(s.get("evlu_erng_rt1", 0))
        krw_pchs = float(s.get("pchs_amt_smtl_amt", 0))
        krw_evlu = float(s.get("evlu_amt_smtl_amt", 0))
        krw_pnl  = float(s.get("tot_evlu_pfls_amt", 0))
        if usd_evlu:
            summary = {
                "총매수금액": usd_pchs,
                "총평가금액": usd_evlu,
                "총손익금액": usd_pnl,
                "총수익률": usd_rt,
                "원화총매수금액": krw_pchs,
                "원화총평가금액": krw_evlu,
                "원화총손익금액": krw_pnl,
                "원화총수익률": round(krw_pnl / krw_pchs * 100, 2) if krw_pchs else usd_rt,
                "환율": exrt,
            }

    # output3 실패 시 holdings에서 직접 계산
    if not summary.get("총평가금액") and holdings:
        pchs = sum(h["매수금액"] for h in holdings)
        evlu = sum(h["평가금액"] for h in holdings)
        pnl  = sum(h["손익금액"] for h in holdings)
        summary = {
            "총매수금액": pchs,
            "총평가금액": evlu,
            "총손익금액": pnl,
            "총수익률": round(pnl / pchs * 100, 2) if pchs else 0,
            "원화총매수금액": round(pchs * exrt) if exrt else 0,
            "원화총평가금액": round(evlu * exrt) if exrt else 0,
            "원화총손익금액": round(pnl  * exrt) if exrt else 0,
            "원화총수익률": round(pnl / pchs * 100, 2) if pchs else 0,
            "환율": exrt,
        }

    return holdings, summary


def get_pending_orders_overseas(acct_cfg, project_root, acct_config_name=""):
    """
    해외주식 계좌의 미체결 주문 전체 조회 (매수+매도).
    Returns: list of dicts with 주문구분/종목코드/종목명/주문수량/주문단가/주문번호/거래소코드
    """
    try:
        token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    except Exception as e:
        print(f"[pending-overseas] token error: {e}")
        return []

    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]
    svr = acct_cfg.get("server", "prod")
    tr_id = "TTTS3018R" if svr == "prod" else "VTTS3018R"

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            tr_id, acct_cfg.get("my_agent", ""))
    params = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "OVRS_EXCG_CD": "%",
        "SORT_SQN": "DS",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }

    try:
        res = requests.get(
            f"{base_url}/uapi/overseas-stock/v1/trading/inquire-nccs",
            headers=headers, params=params)
        if res.status_code != 200:
            print(f"[pending-overseas] http {res.status_code}: {res.text[:200]}")
            return []
        data = res.json()
        if data.get("rt_cd") != "0":
            print(f"[pending-overseas] rt_cd={data.get('rt_cd')} msg={data.get('msg1')}")
            return []
    except Exception as e:
        print(f"[pending-overseas] request error: {e}")
        return []

    output = data.get("output", []) or []
    print(f"[pending-overseas] {acct_no}-{prod_cd} rows={len(output)}")

    result = []
    for item in output:
        code = item.get("pdno", "")
        nccs_qty = int(float(item.get("nccs_qty", 0) or 0))
        if not code or nccs_qty == 0:
            continue
        # 해외: 01=매수, 02=매도
        sll_buy = item.get("sll_buy_dvsn_cd", "")
        order_type = "매도" if sll_buy == "02" else "매수" if sll_buy == "01" else sll_buy
        result.append({
            "주문구분": order_type,
            "종목코드": code,
            "종목명": item.get("prdt_name", "") or code,
            "주문수량": nccs_qty,
            "주문단가": float(item.get("ft_ord_unpr3", 0) or 0),
            "주문번호": item.get("odno", ""),
            "거래소코드": item.get("ovrs_excg_cd", ""),
        })
    print(f"[pending-overseas] matched={len(result)}")
    return result


def place_sell_order_overseas(acct_cfg, project_root, acct_config_name, stock_code, excg_cd, qty, price):
    """
    해외주식 지정가 매도 주문.
    Returns: {"주문번호": str}
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]

    svr = acct_cfg.get("server", "prod")
    tr_id = "TTTT1006U" if svr == "prod" else "VTTT1006U"  # 매도: 1006, 매수: 1002

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            tr_id, acct_cfg.get("my_agent", ""))
    body = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "OVRS_EXCG_CD": excg_cd,
        "PDNO": stock_code,
        "ORD_DVSN": "00",           # 00=지정가
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "ORD_SVR_DVSN_CD": "0",
    }

    res = requests.post(
        f"{base_url}/uapi/overseas-stock/v1/trading/order",
        data=json.dumps(body), headers=headers)
    if res.status_code != 200:
        raise Exception(f"주문 실패: {res.status_code} {res.text}")

    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"주문 오류: {data.get('msg_cd')} {data.get('msg1')}")

    return {"주문번호": data.get("output", {}).get("ODNO", "")}


def cancel_order(acct_cfg, project_root, acct_config_name, order_no, krx_orgno, stock_code, qty, price):
    """
    국내주식 주문 취소.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]
    svr = acct_cfg.get("server", "prod")
    tr_id = "TTTC0803U" if svr == "prod" else "VTTC0803U"

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            tr_id, acct_cfg.get("my_agent", ""))
    body = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "KRX_FWDG_ORD_ORGNO": krx_orgno,
        "ORGN_ODNO": order_no,
        "ORD_DVSN": "00",
        "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0",
        "QTY_ALL_ORD_YN": "Y",
    }
    res = requests.post(
        f"{base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl",
        data=json.dumps(body), headers=headers)
    if res.status_code != 200:
        raise Exception(f"주문취소 실패: {res.status_code} {res.text}")
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"주문취소 오류: {data.get('msg_cd')} {data.get('msg1')}")
    return {"주문번호": data.get("output", {}).get("ODNO", "")}


def cancel_order_overseas(acct_cfg, project_root, acct_config_name, order_no, stock_code, excg_cd, qty, price):
    """
    해외주식 주문 취소.
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]
    svr = acct_cfg.get("server", "prod")
    tr_id = "TTTT1004U" if svr == "prod" else "VTTT1004U"

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            tr_id, acct_cfg.get("my_agent", ""))
    body = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "OVRS_EXCG_CD": excg_cd,
        "PDNO": stock_code,
        "ORGN_ODNO": order_no,
        "RVSE_CNCL_DVSN_CD": "02",   # 02=취소
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": f"{price:.2f}",
        "QTY_ALL_ORD_YN": "Y",
    }
    res = requests.post(
        f"{base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl",
        data=json.dumps(body), headers=headers)
    if res.status_code != 200:
        raise Exception(f"주문취소 실패: {res.status_code} {res.text}")
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"주문취소 오류: {data.get('msg_cd')} {data.get('msg1')}")
    return {"주문번호": data.get("output", {}).get("ODNO", "")}


def get_pending_orders(acct_cfg, project_root, acct_config_name=""):
    """
    국내주식 계좌의 미체결 주문 전체 조회 (매수+매도).
    Returns: list of dicts with 주문구분/종목코드/종목명/주문수량/주문단가/주문번호/krx_fwdg_ord_orgno
    잔여수량(rmn_qty)이 0인 건은 제외한다.
    """
    try:
        token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    except Exception as e:
        print(f"[pending-domestic] token error: {e}")
        return []

    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            "TTTC8036R", acct_cfg.get("my_agent", ""))
    params = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
        "INQR_DVSN_1": "0",   # 0=전체
        "INQR_DVSN_2": "0",
    }

    try:
        res = requests.get(
            f"{base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            headers=headers, params=params)
        if res.status_code != 200:
            print(f"[pending-domestic] http {res.status_code}: {res.text[:200]}")
            return []
        data = res.json()
        if data.get("rt_cd") != "0":
            print(f"[pending-domestic] rt_cd={data.get('rt_cd')} msg={data.get('msg1')}")
            return []
    except Exception as e:
        print(f"[pending-domestic] request error: {e}")
        return []

    output = data.get("output", []) or []
    print(f"[pending-domestic] {acct_no}-{prod_cd} rows={len(output)}")

    result = []
    for item in output:
        code = item.get("pdno", "")
        rmn_qty = int(item.get("rmn_qty", 0) or 0)
        if not code or rmn_qty == 0:
            continue
        # 국내: 01=매도, 02=매수
        sll_buy = item.get("sll_buy_dvsn_cd", "")
        order_type = "매도" if sll_buy == "01" else "매수" if sll_buy == "02" else sll_buy
        result.append({
            "주문구분": order_type,
            "종목코드": code,
            "종목명": item.get("prdt_name", "") or code,
            "주문수량": rmn_qty,
            "주문단가": int(float(item.get("ord_unpr", 0) or 0)),
            "주문번호": item.get("odno", "") or item.get("ord_no", ""),
            "krx_fwdg_ord_orgno": item.get("krx_fwdg_ord_orgno", ""),
        })
    print(f"[pending-domestic] matched={len(result)}")
    return result


def place_sell_order(acct_cfg, project_root, acct_config_name, stock_code, qty, price):
    """
    국내주식 지정가 매도 주문.
    Returns: {"주문번호": str}
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    acct_no = acct_cfg["my_acct_stock"]
    prod_cd = acct_cfg["my_prod"]

    svr = acct_cfg.get("server", "prod")
    tr_id = "TTTC0801U" if svr == "prod" else "VTTC0801U"

    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            tr_id, acct_cfg.get("my_agent", ""))
    body = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "PDNO": stock_code,
        "ORD_DVSN": "00",        # 00=지정가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(price),
    }

    res = requests.post(
        f"{base_url}/uapi/domestic-stock/v1/trading/order-cash",
        data=json.dumps(body), headers=headers)
    if res.status_code != 200:
        raise Exception(f"주문 실패: {res.status_code} {res.text}")

    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"주문 오류: {data.get('msg_cd')} {data.get('msg1')}")

    return {"주문번호": data.get("output", {}).get("ODNO", "")}


def get_ask_price_domestic(acct_cfg, project_root, acct_config_name, stock_code):
    """
    국내주식 매도호가1 조회 (TR: FHKST01010200)
    Returns: int (매도호가1, 원화)
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            "FHKST01010200", acct_cfg.get("my_agent", ""))
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }
    res = requests.get(
        f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        headers=headers, params=params)
    if res.status_code != 200:
        raise Exception(f"호가 조회 실패: {res.status_code} {res.text}")
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"호가 조회 오류: {data.get('msg_cd')} {data.get('msg1')}")
    output = data.get("output1", {})
    ask = int(output.get("askp1", 0))
    if not ask:
        raise Exception("매도호가 없음 (장 종료 또는 호가 없음)")
    return ask


_EXCG_CD_MAP = {
    # CTRP6504R ovrs_excg_cd → HHDFS76200200 EXCD
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
    "SEHK": "HKS",
    "SHAA": "SHS",
    "SZAA": "SZS",
    "TKSE": "TSE",
    "HASE": "HSX",
    "VNSE": "HNX",
}


def get_ask_price_overseas(acct_cfg, project_root, acct_config_name, stock_code, excg_cd):
    """
    해외주식 매도호가 조회 (TR: HHDFS76200200)
    Returns: float (매도호가, USD)
    """
    token, base_url = _get_token(acct_cfg, project_root, acct_config_name)
    headers = _make_headers(token, acct_cfg["my_app"], acct_cfg["my_sec"],
                            "HHDFS76200200", acct_cfg.get("my_agent", ""))
    # 거래소코드 형식 변환 (NASD→NAS 등)
    excd = _EXCG_CD_MAP.get(excg_cd.upper(), excg_cd)
    params = {
        "AUTH": "",
        "EXCD": excd,
        "SYMB": stock_code,
    }
    res = requests.get(
        f"{base_url}/uapi/overseas-price/v1/quotations/price-detail",
        headers=headers, params=params)
    if res.status_code != 200:
        raise Exception(f"호가 조회 실패: {res.status_code} {res.text}")
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"호가 조회 오류: {data.get('msg_cd')} {data.get('msg1')}")
    output = data.get("output", {})
    # askp(매도호가) 없으면 last(현재가) 사용
    ask = float(output.get("askp", 0)) or float(output.get("last", 0))
    if not ask:
        raise Exception(f"호가 없음 (excd={excd}, symb={stock_code})")
    return ask
