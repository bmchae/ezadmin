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
    - Upbit(broker_type: upbit)는 지원하지 않으므로 None 반환
    - KIS 블록이 없으면 None 반환
    """
    if cfg.get("broker_type") == "upbit":
        return None
    kis = cfg.get("kis") or {}
    if not kis.get("app_key") or not kis.get("account_no"):
        return None

    account_no = str(kis.get("account_no", ""))
    if "-" in account_no:
        acct_stock, _, prod = account_no.partition("-")
    else:
        acct_stock, prod = account_no, "01"

    server = "vps" if kis.get("is_mock") else "prod"
    market_raw = str(cfg.get("market", "")).lower()
    market = "us" if market_raw in ("overseas", "us", "global", "foreign") else "kr"

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

    name = fname.replace(".yaml", "")
    description = f"(ezsplit) {cfg.get('name', name)}"
    return {
        "name": name,
        "description": description,
        "owner": _detect_owner(fname),
        "account_config_name": name,  # token 파일명에 사용됨 (KIS-{name}-YYYYMMDD)
        "project": "ezsplit",
        "project_root": EZSPLIT_ROOT,
        "market": market,
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

    # 계좌(CANO+PRDT) 중복 제거: ezsplit 우선
    ezsplit_accts = {
        (p["account_cfg"].get("my_acct_stock"), p["account_cfg"].get("my_prod"))
        for p in portfolios if p["project"] == "ezsplit"
    }
    seen = set()
    deduped = []
    for pf in portfolios:
        key = (pf["account_cfg"].get("my_acct_stock"), pf["account_cfg"].get("my_prod"))
        if pf["project"] != "ezsplit" and key in ezsplit_accts:
            continue  # ezsplit에 동일 계좌 존재 → 제외
        if key in seen:
            continue  # 같은 프로젝트 내에서도 중복 방지
        seen.add(key)
        deduped.append(pf)

    return deduped
