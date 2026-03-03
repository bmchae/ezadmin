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


def _get_token(acct_cfg, project_root, acct_config_name=""):
    """
    OAuth2 토큰을 반환한다.
    1) 프로젝트 token/ 디렉토리에서 기존 유효 토큰을 찾아 재사용
    2) 없으면 새로 발급
    """
    svr = acct_cfg.get("server", "prod")
    base_url = acct_cfg[svr]
    app_key = acct_cfg["my_app"]
    app_secret = acct_cfg["my_sec"]

    # cfg_name: account config 파일명에서 .yaml 제거 (ezgain 방식과 동일)
    cfg_name = acct_config_name.replace(".yaml", "") if acct_config_name else acct_cfg.get("my_htsid", "unknown")

    token_dir = os.path.join(project_root, "token")
    os.makedirs(token_dir, exist_ok=True)

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

    params = {
        "CANO": acct_no,
        "ACNT_PRDT_CD": prod_cd,
        "WCRC_FRCR_DVSN_CD": "02",  # 외화
        "NATN_CD": "000",  # 전체
        "TR_MKET_CD": "00",  # 전체
        "INQR_DVSN_CD": "00",  # 전체
    }

    res = requests.get(f"{base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance",
                       headers=headers, params=params)
    if res.status_code != 200:
        raise Exception(f"해외잔고조회 실패: {res.status_code} {res.text}")

    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"해외잔고조회 오류: {data.get('msg_cd')} {data.get('msg1')}")

    output1 = data.get("output1", [])
    output3 = data.get("output3", {})

    holdings = []
    for item in output1:
        qty = float(item.get("cblc_qty13", 0))
        if qty == 0:
            continue
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
        })

    summary = {}
    if output3:
        s = output3 if isinstance(output3, dict) else output3
        summary = {
            "총매수금액": float(s.get("frcr_pchs_amt1", 0)),
            "총평가금액": float(s.get("tot_frcr_evlu_amt", 0)),
            "총손익금액": float(s.get("ovrs_tot_pfls", 0)),
            "총수익률": float(s.get("tot_evlu_pfls_rt", 0)) if s.get("tot_evlu_pfls_rt") else 0,
        }

    return holdings, summary
