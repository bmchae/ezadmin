"""
ezgain/ezinvest/ezsplit 프로젝트의 portfolio config 파일들을 스캔하고 로드한다.

네이밍 규칙(portfolio-*.yaml, config-*.yaml)에 의존하지 않고,
YAML 내용(shape)을 보고 포트폴리오 여부를 판별한다.
"""
import os
import yaml

EZGAIN_ROOT = os.path.expanduser("~/ez/ezgain")
EZINVEST_ROOT = os.path.expanduser("~/ez/ezinvest")
EZSPLIT_ROOT = os.path.expanduser("~/ez/ezsplit")

PROJECT_ROOTS = [
    ("ezgain",   EZGAIN_ROOT),
    ("ezinvest", EZINVEST_ROOT),
    ("ezsplit",  EZSPLIT_ROOT),
]

KNOWN_OWNERS = ["bmchae", "hitomato", "0eh", "9bong", "hayeon"]

KIS_PROD_URL = "https://openapi.koreainvestment.com:9443"
KIS_VPS_URL  = "https://openapivts.koreainvestment.com:29443"
DEFAULT_UA   = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/114.0.0.0 Safari/537.36")


# ─────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────
def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _is_commented_out(path):
    """파일 전체가 주석처리되었거나 비어있는지 확인"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return False
    return True


def _detect_owner(fname, cfg=None, acct_cfg=None):
    """
    파일명 → cfg.description/name → acct_cfg.my_htsid 순서로 오너를 탐색.
    알려진 오너 문자열이 하나라도 포함되면 그 값을 반환, 아니면 'unknown'.
    """
    parts = [fname or ""]
    if isinstance(cfg, dict):
        parts.append(str(cfg.get("description") or ""))
        parts.append(str(cfg.get("name") or ""))
    if isinstance(acct_cfg, dict):
        parts.append(str(acct_cfg.get("my_htsid") or ""))
    combined = " ".join(parts).lower()
    for owner in KNOWN_OWNERS:
        if owner in combined:
            return owner
    return "unknown"


# ─────────────────────────────────────────────────
# Shape 분류기
# ─────────────────────────────────────────────────
def _classify(cfg):
    """
    YAML 내용(dict)을 4종의 shape 중 하나로 분류.
      - 'ezsplit'        : 최상위에 kis/kw/upbit 섹션 + credentials
      - 'portfolio-ref'  : account_config 필드로 외부 KIS 계정 파일 참조
      - 'bog'            : env + broker + account + bog (ezgain bog 스타일)
      - 'kis-account'    : my_app/my_acct_stock 단독 KIS 계정 파일 (skip)
      - 'unknown'        : 위 어느 것도 아님 (skip)
    """
    if not isinstance(cfg, dict):
        return "unknown"

    # ezsplit 스타일: 최상위 kis/kw/upbit 블록
    kis = _as_dict(cfg.get("kis"))
    kw  = _as_dict(cfg.get("kw"))
    up  = _as_dict(cfg.get("upbit"))
    if (kis.get("app_key") and kis.get("account_no")) \
       or (kw.get("app_key") and kw.get("account_no")) \
       or (up.get("access_key") and up.get("secret_key")):
        return "ezsplit"

    # ezinvest/ezgain portfolio 스타일: 외부 계정 파일 참조
    if isinstance(cfg.get("account_config"), str) and cfg.get("account_config"):
        return "portfolio-ref"

    # ezgain bog 스타일: 3-level 구조
    if all(k in cfg for k in ("env", "broker", "account", "bog")):
        return "bog"

    # 단독 KIS account 파일: 다른 파일이 참조할 때만 사용됨
    if cfg.get("my_app") and cfg.get("my_acct_stock"):
        return "kis-account"

    return "unknown"


# ─────────────────────────────────────────────────
# Shape 별 → 포트폴리오 dict 변환
# ─────────────────────────────────────────────────
def _build_ezsplit(cfg, fname, fpath, project, project_root):
    """
    ezsplit의 config-*.yaml (통합형) 을 ezadmin 포트폴리오 포맷으로 정규화한다.
    - Upbit(broker_type: upbit): broker='upbit', market='crypto'
    - kis/kw 블록 중 하나가 있으면 해당 브로커
    - 자격증명 누락 시 None
    """
    name = fname.replace(".yaml", "")

    if cfg.get("broker_type") == "upbit":
        upbit = _as_dict(cfg.get("upbit"))
        if not (upbit.get("access_key") and upbit.get("secret_key")):
            return None
        acct_cfg = {
            "access_key": upbit.get("access_key"),
            "secret_key": upbit.get("secret_key"),
            "my_acct_stock": "UPBIT",
            "my_prod": "",
        }
        return {
            "name": name,
            "description": f"({project}) {cfg.get('name', name)}",
            "owner": _detect_owner(fname, cfg, acct_cfg),
            "account_config_name": name,
            "project": project,
            "project_root": project_root,
            "market": "crypto",
            "broker": "upbit",
            "account_config_path": fpath,
            "account_cfg": acct_cfg,
            "portfolio_cfg": cfg,
        }

    kis = _as_dict(cfg.get("kis"))
    kw  = _as_dict(cfg.get("kw"))

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
            "my_agent": DEFAULT_UA,
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

    return {
        "name": name,
        "description": f"({project}) {cfg.get('name', name)}",
        "owner": _detect_owner(fname, cfg, acct_cfg),
        "account_config_name": name,
        "project": project,
        "project_root": project_root,
        "market": market,
        "broker": broker,
        "account_config_path": fpath,
        "account_cfg": acct_cfg,
        "portfolio_cfg": cfg,
    }


def _build_portfolio_ref(cfg, fname, project, project_root, config_dir):
    """
    ezinvest/ezgain portfolio 스타일: account_config 필드로 외부 KIS 계정 파일을 가리키는 구조.
    """
    acct_file = cfg.get("account_config", "")
    if not acct_file:
        return None
    acct_path = os.path.join(config_dir, acct_file)
    if not os.path.exists(acct_path):
        print(f"[config_loader] {fname}: account_config '{acct_file}' not found")
        return None
    try:
        acct_cfg = _load_yaml(acct_path)
    except Exception as e:
        print(f"[config_loader] {fname}: account '{acct_file}' load 실패 ({e})")
        return None
    if not isinstance(acct_cfg, dict):
        return None

    account_type = str(cfg.get("account_type", "kis")).lower()
    broker = "kw" if account_type in ("kw", "kiwoom") else "kis"

    return {
        "name": fname.replace(".yaml", ""),
        "description": cfg.get("description", fname),
        "owner": _detect_owner(fname, cfg, acct_cfg),
        "account_config_name": acct_file,
        "project": project,
        "project_root": project_root,
        "market": cfg.get("market", "kr"),
        "broker": broker,
        "account_config_path": acct_path,
        "account_cfg": acct_cfg,
        "portfolio_cfg": cfg,
    }, acct_file


def _build_bog(cfg, fname, fpath, project, project_root):
    """
    ezgain bog 스타일: env/broker/account/bog 4개 섹션.
    account[] 리스트의 각 항목을 개별 포트폴리오로 변환.
    """
    portfolios = []
    brokers = {}
    for b in (cfg.get("broker") or []):
        if isinstance(b, dict) and b.get("name"):
            brokers[b["name"]] = b

    bog = cfg.get("bog") or {}
    default_market = str(bog.get("market", "kr")).lower()

    for acct in (cfg.get("account") or []):
        if not isinstance(acct, dict):
            continue
        app_key = acct.get("app_key", "")
        sec_key = acct.get("sec_key", "")
        account_no = acct.get("account", "")
        if not (app_key and sec_key and account_no):
            continue
        prod_id = str(acct.get("prod_id", "01"))
        name = acct.get("name") or f"acct-{account_no}"

        broker_info = brokers.get(acct.get("broker_name", ""), {})
        company = str(broker_info.get("company", "kis")).lower()
        broker = "kw" if company in ("kw", "kiwoom") else "kis"

        server = "prod" if acct.get("is_real", True) else "vps"
        acct_cfg = {
            "my_app": app_key,
            "my_sec": sec_key,
            "my_acct_stock": str(account_no),
            "my_prod": prod_id,
            "my_htsid": broker_info.get("user_id", ""),
            "server": server,
            "prod": KIS_PROD_URL,
            "vps": KIS_VPS_URL,
            "my_agent": DEFAULT_UA,
        }
        base_name = fname.replace(".yaml", "")
        portfolios.append({
            "name": f"{base_name}-{name}",
            "description": f"({project}/bog) {name}",
            "owner": _detect_owner(fname, cfg, acct_cfg),
            "account_config_name": name,
            "project": project,
            "project_root": project_root,
            "market": default_market if default_market in ("kr", "us") else "kr",
            "broker": broker,
            "account_config_path": fpath,
            "account_cfg": acct_cfg,
            "portfolio_cfg": cfg,
        })
    return portfolios


# ─────────────────────────────────────────────────
# 프로젝트별 스캔
# ─────────────────────────────────────────────────
def _scan_project(project, project_root):
    """
    project_root/config/ 의 모든 yaml 을 읽어 내용(shape) 으로 분류해
    포트폴리오 리스트를 생성.
    """
    config_dir = os.path.join(project_root, "config")
    if not os.path.isdir(config_dir):
        return []

    # 모든 yaml 파일 로드 (심링크는 skip — 원본이 따로 잡힘)
    loaded = []  # [(fname, fpath, cfg)]
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith(".yaml"):
            continue
        if "example" in fname.lower():
            continue
        fpath = os.path.join(config_dir, fname)
        if os.path.islink(fpath):
            continue
        try:
            if _is_commented_out(fpath):
                continue
        except Exception:
            continue
        try:
            cfg = _load_yaml(fpath)
        except Exception as e:
            print(f"[config_loader] yaml load 실패 {fpath}: {e}")
            continue
        if cfg is None:
            continue
        loaded.append((fname, fpath, cfg))

    portfolios = []
    for fname, fpath, cfg in loaded:
        shape = _classify(cfg)
        if shape == "ezsplit":
            pf = _build_ezsplit(cfg, fname, fpath, project, project_root)
            if pf:
                portfolios.append(pf)
        elif shape == "portfolio-ref":
            r = _build_portfolio_ref(cfg, fname, project, project_root, config_dir)
            if r:
                portfolios.append(r[0])
        elif shape == "bog":
            portfolios.extend(_build_bog(cfg, fname, fpath, project, project_root))
        elif shape in ("kis-account", "unknown"):
            # 단독 KIS 계정 파일은 portfolio-ref 가 참조해서만 사용됨.
            # unknown 은 포트폴리오 스키마와 무관 (예: investingcom.yaml, auth 전용 등)
            pass
    return portfolios


# ─────────────────────────────────────────────────
# 퍼블릭 API
# ─────────────────────────────────────────────────
def load_all_portfolios():
    """
    ezgain / ezinvest / ezsplit 의 config 디렉토리를 모두 스캔해
    포트폴리오 리스트를 반환. 파일명 규칙에 의존하지 않는다.
    Returns: list of dict, keys:
        - name, description, owner, account_config_name
        - project, project_root
        - market ('kr'|'us'|'crypto'), broker ('kis'|'kw'|'upbit')
        - account_config_path, account_cfg, portfolio_cfg
    """
    portfolios = []
    for project, root in PROJECT_ROOTS:
        portfolios.extend(_scan_project(project, root))

    # 계좌 중복 제거: ezsplit 우선.
    # 식별 키:
    #   - kis/kw : (broker, my_acct_stock, my_prod) = CANO + PRDT
    #   - upbit  : (broker, access_key, "")
    def _key(p):
        broker = p.get("broker", "kis")
        acct = p.get("account_cfg") or {}
        if broker == "upbit":
            return (broker, acct.get("access_key", ""), "")
        return (broker, acct.get("my_acct_stock", ""), acct.get("my_prod", ""))

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
