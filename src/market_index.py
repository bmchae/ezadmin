"""
시장 지표 카드용 시세 fetcher.
yfinance 일봉 30일치를 가져와 현재가/전일대비/스파크라인을 만든다.
메모리 캐시 TTL 5분 (장중에도 자주 호출하지 않도록).
"""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional

try:
    import yfinance as yf
except ImportError:
    yf = None

# (label, yfinance 심볼, 화면 표시용 코드)
INDICES = [
    ("코스피",       "^KS11",     "^KS11"),
    ("코스닥",       "^KQ11",     "^KQ11"),
    ("나스닥",       "^IXIC",     "IXIC"),
    ("금",          "GC=F",      "GC=F"),
    ("KODEX 반도체", "091160.KS", "091160"),
]

_cache: Dict[str, tuple] = {}   # yf_symbol -> (timestamp, data)
TTL = 300                        # 5분


MA_WINDOW = 50      # 이동평균 기간
MA_DEV_DAYS = 7     # 카드에 보여줄 편차 일수


def _fetch_one(yf_symbol: str) -> Optional[dict]:
    """120일치 일봉 → 30일 스파크라인 + MA(50) 대비 최근 7일 편차(%)."""
    if yf is None:
        return None
    try:
        # MA(50) 을 최근 7일분 계산하려면 최소 50 + 7 - 1 = 56 거래일 필요.
        # 비거래일 버퍼 포함해 120 캘린더일 요청.
        df = yf.Ticker(yf_symbol).history(period="120d", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return None
        closes_raw = df["Close"].tolist()
        # NaN 필터
        closes_full = [float(c) for c in closes_raw if c == c and c is not None]
        if len(closes_full) < 2:
            return None

        last = closes_full[-1]
        prev = closes_full[-2]
        chg = last - prev
        rate = (chg / prev * 100) if prev else 0.0

        # 스파크라인용 최근 30일
        closes = closes_full[-30:]

        # MA(50) 대비 최근 7일 편차 (%): (close[i] - MA50[i]) / MA50[i] * 100
        ma_dev = None
        if len(closes_full) >= MA_WINDOW + MA_DEV_DAYS - 1:
            ma_dev = []
            start = len(closes_full) - MA_DEV_DAYS
            for i in range(start, len(closes_full)):
                window = closes_full[i - MA_WINDOW + 1 : i + 1]
                ma = sum(window) / MA_WINDOW
                dev = ((closes_full[i] - ma) / ma * 100) if ma else 0.0
                ma_dev.append(dev)
        elif len(closes_full) >= MA_DEV_DAYS:
            # MA(50) 데이터가 부족하면 가용 윈도우 그대로 사용 (최선 노력)
            ma_dev = []
            start = len(closes_full) - MA_DEV_DAYS
            for i in range(start, len(closes_full)):
                w_size = min(MA_WINDOW, i + 1)
                window = closes_full[i - w_size + 1 : i + 1]
                ma = sum(window) / w_size
                dev = ((closes_full[i] - ma) / ma * 100) if ma else 0.0
                ma_dev.append(dev)

        return {
            "last": last,
            "prev": prev,
            "chg": chg,
            "rate": rate,
            "closes": closes,
            "ma_dev": ma_dev,
        }
    except Exception as e:
        print(f"[market_index] fetch {yf_symbol} failed: {e}")
        return None


def _get_one_cached(yf_symbol: str) -> Optional[dict]:
    now = time.time()
    entry = _cache.get(yf_symbol)
    if entry and now - entry[0] < TTL:
        return entry[1]
    data = _fetch_one(yf_symbol)
    if data is not None:
        _cache[yf_symbol] = (now, data)
    return data


def sparkline(closes: List[float], w: int = 100, h: int = 30) -> dict:
    """30개 종가 → SVG path d strings (area, line) + 상승여부."""
    if not closes or len(closes) < 2:
        return {"area": "", "line": "", "w": w, "h": h, "up": True}
    n = len(closes)
    mn, mx = min(closes), max(closes)
    span = max(mx - mn, 1e-9)
    pad_top = 2.0
    pad_bot = 2.0

    def x(i: int) -> float:
        return i / (n - 1) * w

    def y(v: float) -> float:
        return pad_top + (1 - (v - mn) / span) * (h - pad_top - pad_bot)

    pts = [(x(i), y(v)) for i, v in enumerate(closes)]
    line_d = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area_d = (
        f"M {pts[0][0]:.1f},{h:.1f} L "
        + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        + f" L {pts[-1][0]:.1f},{h:.1f} Z"
    )
    return {
        "area": area_d,
        "line": line_d,
        "w": w,
        "h": h,
        "up": closes[-1] >= closes[0],
    }


def get_all_indices(w: int = 100, h: int = 30) -> List[dict]:
    """모든 지표 병렬 fetch. 실패한 항목도 카드는 자리 차지."""
    results: Dict[str, Optional[dict]] = {}
    if yf is None:
        # yfinance 미설치 — 모두 빈 카드
        return [
            {"label": lbl, "symbol": sym, "code": code, "ok": False}
            for lbl, sym, code in INDICES
        ]
    with ThreadPoolExecutor(max_workers=len(INDICES)) as ex:
        futs = {ex.submit(_get_one_cached, sym): sym for _, sym, _ in INDICES}
        for fut in futs:
            sym = futs[fut]
            try:
                results[sym] = fut.result(timeout=10)
            except Exception as e:
                print(f"[market_index] timeout/error {sym}: {e}")
                results[sym] = None

    out: List[dict] = []
    for lbl, sym, code in INDICES:
        data = results.get(sym)
        if data is None:
            out.append({"label": lbl, "symbol": sym, "code": code, "ok": False})
            continue
        spark = sparkline(data["closes"], w=w, h=h)
        out.append({
            "label": lbl,
            "symbol": sym,
            "code": code,
            "ok": True,
            **data,
            "spark": spark,
        })
    return out
