"""
시장 지표 카드용 시세 fetcher.
yfinance 일봉을 가져와 현재가/전일대비/스파크라인/MA(50) 편차/디테일 차트를 만든다.
메모리 캐시 TTL 5분 (장중에도 자주 호출하지 않도록).
"""
import datetime as _dt
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional

try:
    import yfinance as yf
except ImportError:
    yf = None

# /indices 페이지 디테일 차트의 시작 날짜
CHART_START_DATE = "2025-01-01"

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
    """CHART_START_DATE 이전 ~90일 버퍼부터 일봉 fetch.
    → 30일 스파크라인 + MA(50) 편차 (최근 7일 + 2025-01-01 이후 전체 history)
    + 디테일 차트용 종가."""
    if yf is None:
        return None
    try:
        # MA(50) 계산을 위해 CHART_START_DATE 보다 ~90 캘린더일 (≈60 거래일) 앞서 가져옴
        chart_start_dt = _dt.date.fromisoformat(CHART_START_DATE)
        fetch_start = (chart_start_dt - _dt.timedelta(days=90)).isoformat()
        df = yf.Ticker(yf_symbol).history(
            start=fetch_start, interval="1d", auto_adjust=False
        )
        if df is None or df.empty:
            return None

        # 날짜와 종가 페어 추출 (NaN 제거)
        pairs = []
        for ts, c in zip(df.index, df["Close"].tolist()):
            if c == c and c is not None:
                pairs.append((ts.date().isoformat(), float(c)))
        if len(pairs) < 2:
            return None

        dates = [p[0] for p in pairs]
        closes_full = [p[1] for p in pairs]

        last = closes_full[-1]
        prev = closes_full[-2]
        chg = last - prev
        rate = (chg / prev * 100) if prev else 0.0

        # 스파크라인용 최근 30일
        closes = closes_full[-30:]

        # MA(50) 편차 전 일자 계산
        ma_dev_full = []
        for i in range(len(closes_full)):
            if i + 1 >= MA_WINDOW:
                window = closes_full[i - MA_WINDOW + 1 : i + 1]
                ma = sum(window) / MA_WINDOW
            else:
                # 데이터 부족 구간 (시작 50일 이전) 은 가용 윈도우 사용
                window = closes_full[: i + 1]
                ma = sum(window) / len(window)
            dev = ((closes_full[i] - ma) / ma * 100) if ma else 0.0
            ma_dev_full.append(dev)

        # 카드용 최근 7일
        ma_dev = ma_dev_full[-MA_DEV_DAYS:] if len(ma_dev_full) >= MA_DEV_DAYS else ma_dev_full

        # 디테일 차트용 슬라이스: CHART_START_DATE 이후
        chart_idx = [i for i, d in enumerate(dates) if d >= CHART_START_DATE]
        if chart_idx:
            s = chart_idx[0]
            chart_dates = dates[s:]
            chart_devs = ma_dev_full[s:]
            chart_closes = closes_full[s:]
        else:
            chart_dates, chart_devs, chart_closes = [], [], []

        return {
            "last": last,
            "prev": prev,
            "chg": chg,
            "rate": rate,
            "closes": closes,
            "ma_dev": ma_dev,
            "ma_dev_full": ma_dev_full,
            "ma_dev_dates": dates,
            "chart_dates": chart_dates,
            "chart_devs": chart_devs,
            "chart_closes": chart_closes,
        }
    except Exception as e:
        print(f"[market_index] fetch {yf_symbol} failed: {e}")
        return None


def _y_scale(values: List[float], pad_y: float, plot_h: float):
    """주어진 값들로 y-축 스케일 함수 반환. 0 을 항상 포함, 10% 여유."""
    lo = min(min(values), 0)
    hi = max(max(values), 0)
    pad = max((hi - lo) * 0.10, 1.0)
    y_min = lo - pad
    y_max = hi + pad
    span = y_max - y_min if y_max != y_min else 1.0

    def y(v: float) -> float:
        return pad_y + (1 - (v - y_min) / span) * plot_h

    return y, y_min, y_max


def ma_dev_chart(
    devs: List[float],
    dates: List[str],
    closes: List[float],
    w: int = 720,
    h: int = 180,
) -> Optional[dict]:
    """MA(50) 편차(%) + 종가 변동률(%) dual-axis 차트.
    편차와 가격은 각자 독립 y-축 스케일 사용 → 두 시계열 변동 모두 잘 보이게."""
    if not devs or len(devs) < 2:
        return None
    n = len(devs)

    has_price = len(closes) == n and closes[0]
    price_pct: List[float] = []
    if has_price:
        base = closes[0]
        price_pct = [(c / base - 1) * 100 for c in closes]

    pad_x_left = 4
    pad_x_right = 40        # 우측 임계선 라벨용 여백
    pad_y = 8
    plot_w = w - pad_x_left - pad_x_right
    plot_h = h - pad_y * 2

    def x(i: int) -> float:
        return pad_x_left + (i / (n - 1) * plot_w if n > 1 else plot_w / 2)

    # 편차 y-축 (좌측 기준선용)
    y_dev_fn, dev_min, dev_max = _y_scale(devs, pad_y, plot_h)
    # 가격 y-축 (별도 스케일)
    y_price_fn = None
    if price_pct:
        y_price_fn, _, _ = _y_scale(price_pct, pad_y, plot_h)

    # 편차 라인 + 0 기준 영역
    dev_pts = [(x(i), y_dev_fn(v)) for i, v in enumerate(devs)]
    dev_line = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in dev_pts)
    dev_area = (
        f"M {dev_pts[0][0]:.1f},{y_dev_fn(0):.1f} L "
        + " L ".join(f"{px:.1f},{py:.1f}" for px, py in dev_pts)
        + f" L {dev_pts[-1][0]:.1f},{y_dev_fn(0):.1f} Z"
    )

    # 가격 라인 (별도 스케일)
    price_line = ""
    if price_pct and y_price_fn:
        price_pts = [(x(i), y_price_fn(v)) for i, v in enumerate(price_pct)]
        price_line = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in price_pts)

    # 편차 임계선 (+30, 0, -30) 중 차트 범위 내인 것만
    thresholds = []
    for tv in [30, 0, -30]:
        if dev_min <= tv <= dev_max:
            thresholds.append({
                "value": tv,
                "y": y_dev_fn(tv),
                "color": "#ff3b30" if tv > 0 else ("#0d6efd" if tv < 0 else "#86868b"),
            })

    last_v = devs[-1]
    avg_v = sum(devs) / len(devs)
    last_x, last_y = dev_pts[-1]
    last_color = "#ff3b30" if last_v > 30 else ("#0d6efd" if last_v < -30 else "#1d1d1f")

    # 마우스오버용 points
    points = []
    for i in range(n):
        pt = {
            "x": round(x(i), 1),
            "y_dev": round(y_dev_fn(devs[i]), 1),
            "date": dates[i] if i < len(dates) else "",
            "dev": round(devs[i], 2),
        }
        if price_pct and y_price_fn:
            pt["price"] = round(closes[i], 2)
            pt["price_pct"] = round(price_pct[i], 2)
            pt["y_price"] = round(y_price_fn(price_pct[i]), 1)
        points.append(pt)

    return {
        "w": w,
        "h": h,
        "dev_line": dev_line,
        "dev_area": dev_area,
        "price_line": price_line,
        "has_price": bool(price_line),
        "thresholds": thresholds,
        "last_x": last_x,
        "last_y": last_y,
        "last_v": last_v,
        "last_color": last_color,
        "max": max(devs),
        "min": min(devs),
        "avg": avg_v,
        "first_date": dates[0] if dates else "",
        "last_date": dates[-1] if dates else "",
        "n": n,
        "points": points,
    }


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
        dev_chart = ma_dev_chart(
            data.get("chart_devs") or [],
            data.get("chart_dates") or [],
            data.get("chart_closes") or [],
            w=720, h=180,
        )
        out.append({
            "label": lbl,
            "symbol": sym,
            "code": code,
            "ok": True,
            **data,
            "spark": spark,
            "dev_chart": dev_chart,
        })
    return out
