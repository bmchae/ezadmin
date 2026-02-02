"""
ezgain/ezinvest 프로젝트의 portfolio config 파일들을 스캔하고 로드한다.
"""
import os
import yaml

EZGAIN_ROOT = os.path.expanduser("~/ez/ezgain")
EZINVEST_ROOT = os.path.expanduser("~/ez/ezinvest")

KNOWN_OWNERS = ["bmchae", "hitomato", "0eh", "9bong", "hayeon"]


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

    return portfolios
