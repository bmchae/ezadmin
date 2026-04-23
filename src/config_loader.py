"""
ezgain/ezinvest/ezsplit 프로젝트의 portfolio config 파일들을 스캔하고 로드한다.
"""
import os
import yaml

EZGAIN_ROOT = os.path.expanduser("~/ez/ezgain")
EZINVEST_ROOT = os.path.expanduser("~/ez/ezinvest")
EZSPLIT_ROOT = os.path.expanduser("~/ez/ezsplit")

KNOWN_OWNERS = ["bmchae", "hitomato", "0eh", "9bong", "hayeon"]

KIS_PROD_URL = "https://openapi.koreainvestment.com:9443"
KIS_VPS_URL  = "https://openapivts.koreainvestment.com:29443"


def _detect_owner(filename):
    """파일명에서 소유자를 추출한다. ex) portfolio-kis-bmchae-isa.yaml -> bmchae"""
    lower = filename.lower()
    for owner in KNOWN_OWNERS:
        if owner in lower:
            return owner
    return "unknown"


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _is_commented_out(path):
    """파일 전체가 주석처리되었는지 확인"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return False
    return True


def _ezsplit_to_portfolio(cfg, fname, fpath):
    """
    ezsplit의 config-*.yaml 을 ezadmin 포트폴리오 포맷으로 정규화한다.
    - Upbit(broker_type: upbit): broker="upbit", market="crypto"
    - kis/kw 블록 중 하나가 있으면 해당 브로커로
    - 셋 다 없으면 None 반환
    """
    name = fname.replace(".yaml", "")

    if cfg.get("broker_type") == "upbit":
        upbit = cfg.get("upbit") or {}
        if not (upbit.get("access_key") and upbit.get("secret_key")):
            return None
        acct_cfg = {
            "access_key": upbit.get("access_key"),
            "secret_key": upbit.get("secret_key"),
            # 카드 배지 표시용 공통 키 (Upbit은 계좌번호 개념 없음)
            "my_acct_stock": "UPBIT",
            "my_prod": "",
        }
        description = f"(ezsplit) {cfg.get('name', name)}"
        return {
            "name": name,
            "description": description,
            "owner": _detect_owner(fname),
            "account_config_name": name,
            "project": "ezsplit",
            "project_root": EZSPLIT_ROOT,
            "market": "crypto",
            "broker": "upbit",
            "account_config_path": fpath,
            "account_cfg": acct_cfg,
            "portfolio_cfg": cfg,
        }

    kis = cfg.get("kis") or {}
    kw  = cfg.get("kw") or {}

    market_raw = str(cfg.get("market", "")).lower()
    market = "us" if market_raw in ("overseas", "us", "global", "foreign") else "kr"

    if kis.get("app_key") and kis.get("account_no"):
        broker = "kis"
        account_no = str(kis.get("account_no", ""))
        if "-" in account_no:
            acct_stock, _, prod = account_no.partition("-")
        else:
            acct_stock, prod = account_no, "01"
        server = "vps" if kis.get("is_mock") else "prod"
        acct_cfg = {
            "my_app": kis.get("app_key", ""),
            "my_sec": kis.get("app_secret", ""),
            "my_acct_stock": acct_stock,
            "my_prod": prod,
            "my_htsid": cfg.get("name", ""),
            "server": server,
            "prod": KIS_PROD_URL,
            "vps": KIS_VPS_URL,
            "my_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/114.0.0.0 Safari/537.36"),
        }
    elif kw.get("app_key") and kw.get("account_no"):
        broker = "kw"
        account_no = str(kw.get("account_no", ""))
        if "-" in account_no:
            acct_stock, _, prod = account_no.partition("-")
        else:
            acct_stock, prod = account_no, ""
        acct_cfg = {
            # Kiwoom 클라이언트가 직접 사용하는 키명 유지
            "app_key": kw.get("app_key", ""),
            "app_secret": kw.get("app_secret", ""),
            "account_no": account_no,
            "is_mock": bool(kw.get("is_mock", False)),
            # 뱃지/카드 표시에 사용하기 위해 공통 키도 복제
            "my_acct_stock": acct_stock,
            "my_prod": prod,
        }
    else:
        return None

    description = f"(ezsplit) {cfg.get('name', name)}"
    return {
        "name": name,
        "description": description,
        "owner": _detect_owner(fname),
        "account_config_name": name,
        "project": "ezsplit",
        "project_root": EZSPLIT_ROOT,
        "market": market,
        "broker": broker,
        "account_config_path": fpath,
        "account_cfg": acct_cfg,
        "portfolio_cfg": cfg,
    }


def load_all_portfolios():
    """
    ezgain, ezinvest의 모든 portfolio config를 로드한다.
    Returns: list of dict with keys:
        - name: portfolio config 파일명 (확장자 제외)
        - description: 포트폴리오 설명
        - project: 'ezgain' or 'ezinvest'
        - market: 'kr' or 'us'
        - account_config_path: KIS 계정 설정 파일 경로
        - account_cfg: KIS 계정 설정 dict
        - portfolio_cfg: 전체 portfolio config dict
    """
    portfolios = []

    # ezgain portfolios
    config_dir = os.path.join(EZGAIN_ROOT, "config")
    if os.path.isdir(config_dir):
        for fname in sorted(os.listdir(config_dir)):
            if fname.startswith("portfolio-") and fname.endswith(".yaml"):
                fpath = os.path.join(config_dir, fname)
                if _is_commented_out(fpath):
                    continue
                cfg = _load_yaml(fpath)
                if cfg is None:
                    continue
                acct_file = cfg.get("account_config", "")
                if not acct_file:
                    continue
                acct_path = os.path.join(config_dir, acct_file)
                if not os.path.exists(acct_path):
                    continue
                acct_cfg = _load_yaml(acct_path)
                portfolios.append({
                    "name": fname.replace(".yaml", ""),
                    "description": cfg.get("description", fname),
                    "owner": _detect_owner(fname),
                    "account_config_name": acct_file,
                    "project": "ezgain",
                    "project_root": EZGAIN_ROOT,
                    "market": cfg.get("market", "kr"),
                    "broker": "kis",
                    "account_config_path": acct_path,
                    "account_cfg": acct_cfg,
                    "portfolio_cfg": cfg,
                })

    # ezinvest portfolios
    config_dir = os.path.join(EZINVEST_ROOT, "config")
    if os.path.isdir(config_dir):
        for fname in sorted(os.listdir(config_dir)):
            if fname.startswith("portfolio-") and fname.endswith(".yaml"):
                fpath = os.path.join(config_dir, fname)
                if _is_commented_out(fpath):
                    continue
                cfg = _load_yaml(fpath)
                if cfg is None:
                    continue
                acct_file = cfg.get("account_config", "")
                if not acct_file:
                    continue
                acct_path = os.path.join(config_dir, acct_file)
                if not os.path.exists(acct_path):
                    continue
                acct_cfg = _load_yaml(acct_path)
                portfolios.append({
                    "name": fname.replace(".yaml", ""),
                    "description": cfg.get("description", fname),
                    "owner": _detect_owner(fname),
                    "account_config_name": acct_file,
                    "project": "ezinvest",
                    "project_root": EZINVEST_ROOT,
                    "market": cfg.get("market", "kr"),
                    "broker": "kis",
                    "account_config_path": acct_path,
                    "account_cfg": acct_cfg,
                    "portfolio_cfg": cfg,
                })

    # ezsplit portfolios (config-*.yaml, 단일 파일에 계정+설정 통합)
    config_dir = os.path.join(EZSPLIT_ROOT, "config")
    if os.path.isdir(config_dir):
        for fname in sorted(os.listdir(config_dir)):
            if not (fname.startswith("config-") and fname.endswith(".yaml")):
                continue
            if fname.startswith("config_example"):
                continue
            fpath = os.path.join(config_dir, fname)
            if _is_commented_out(fpath):
                continue
            cfg = _load_yaml(fpath)
            if cfg is None:
                continue
            pf = _ezsplit_to_portfolio(cfg, fname, fpath)
            if pf is not None:
                portfolios.append(pf)

    # 계좌 중복 제거: ezsplit 우선.
    # broker 별 식별자:
    #   - kis/kw : (my_acct_stock, my_prod) = CANO + PRDT
    #   - upbit  : access_key (계좌번호 개념이 없으므로 키로 사용)
    # broker 를 키에 포함해 브로커 간 우연한 번호 충돌을 방지.
    def _key(p):
        broker = p.get("broker", "kis")
        acct = p["account_cfg"]
        if broker == "upbit":
            return (broker, acct.get("access_key", ""), "")
        return (broker, acct.get("my_acct_stock"), acct.get("my_prod"))

    ezsplit_accts = {_key(p) for p in portfolios if p["project"] == "ezsplit"}
    seen = set()
    deduped = []
    for pf in portfolios:
        key = _key(pf)
        if pf["project"] != "ezsplit" and key in ezsplit_accts:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pf)

    return deduped
