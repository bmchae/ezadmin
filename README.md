# ezadmin

ezgain / ezinvest / ezsplit 포트폴리오의 계좌별 보유종목·잔고·손익을 통합 조회하고
KIS·키움 매수/매도/취소/호가/일봉 조회를 지원하는 **FastAPI(uvicorn)** 단일 프로세스 대시보드.

## 구조

```
ezadmin/
├── src/
│   ├── app.py                   # FastAPI 웹서버 (port 9900, uvicorn 으로 실행)
│   ├── config_loader.py         # 4개 프로젝트(ezgain/ezinvest/ezsplit/ezadmin) yaml 스캔
│   ├── db.py                    # SQLite — 일별 portfolio_daily 스냅샷
│   ├── kis_client.py            # KIS Open API (잔고/주문/취소/호가/일봉/체결내역)
│   ├── kw_client.py             # 키움증권 REST API (잔고/주문/취소/호가/일봉/체결내역)
│   └── upbit_client.py          # Upbit REST (잔고 조회만)
├── config/
│   ├── owners.yaml              # 오너 그룹 리스트 (gitignored, 로컬 파일)
│   └── owners.yaml.example      # 템플릿
├── data/
│   └── ezadmin.db               # SQLite (포트폴리오 일별 스냅샷)
├── templates/
│   ├── base.html                # 공통 레이아웃 (Bootstrap 5 + Apple 디자인 토큰)
│   ├── index.html               # 포트폴리오 목록 (오너 헤더 + 1년 차트 + 카드)
│   ├── login.html               # 로그인 폼
│   └── portfolio.html           # 종목 + 미체결 + 매매내역 + 클릭 드로어 (호가/주문/일봉)
├── scripts/
│   └── gen_password_hash.py
├── reload.sh                    # 9900 포트 점유 프로세스 재기동
├── run.sh
└── requirements.txt
```

## 실행

```bash
pip install -r requirements.txt
cp config/owners.yaml.example config/owners.yaml   # 오너 리스트 편집
python src/app.py                                   # 또는 ./run.sh (내부에서 uvicorn 호출)
# http://localhost:9900
```

`python src/app.py` 가 `uvicorn.run(app, host="0.0.0.0", port=9900)` 을 호출합니다.
별도 ASGI 서버 명령으로 띄우려면:
```bash
PYTHONPATH=src uvicorn app:app --host 0.0.0.0 --port 9900 --reload
```

## 주요 기능

### 포트폴리오 자동 감지 (`config_loader.py`)
파일명 규칙에 의존하지 않고 **YAML 내용(shape)** 으로 분류:

- `ezsplit` (통합형): 최상위 `kis`/`kw`/`upbit` + credentials → `config-*.yaml`
- `portfolio-ref`: `account_config:` 로 외부 KIS 계정 yaml 참조 → ezinvest/ezgain
- `bog`: `env + broker + account + bog` 4-섹션 → ezgain bog 모듈
- `kis-account`: 단독 KIS 계정 파일 (참조시만 사용, 단독 skip)
- `unknown`: 무관 yaml (예: `investingcom.yaml`) skip

스캔 디렉토리: `~/ez/ezgain/config/`, `~/ez/ezinvest/config/`, `~/ez/ezsplit/config/`,
`~/ez/ezadmin/config/`. **`-` 로 시작하거나 `example` 이 포함된 yaml 은 자동 제외**.

계좌 중복 제거: `(broker, CANO, PRDT)` 키 기준, ezsplit 우선.

### 오너 그룹 (`config/owners.yaml`)
하드코드 제거. yaml 의 `owners:` 리스트 순서대로 헤더 배치.
파일명 외에 `description` / `name` / `my_htsid` 도 substring 매칭으로 오너 자동 감지.

```yaml
# config/owners.yaml.example
owners:
  - bmchae
  - hitomato
  - hayeon
```

### 카드 표시 정규화 (`(project) <라벨>`)
- ezgain: yaml description 본문 사용 (앞 `(xxx)` prefix 제거)
- ezinvest/그 외: 파일명에서 `portfolio-`, `kis-/kw-/upbit-` 토큰 제거한 짧은 식별자
- ezsplit/ezadmin: yaml `name` 필드

### 오너 헤더 (다크 배너)
- 컬러 그라디언트 배너 + 이니셜 아바타 + 워터마크
- 우측에 **1년 자산변화 미니 차트** (`width: 65%`, h=80px)
- `portfolio_daily` 합산 → SVG 캔들+바, hover 툴팁

### 카드 (포트폴리오 단위)
- xxl/xl/lg/md/sm 별 5/4/3/2/1 자동 (Bootstrap `row-cols-*`)
- 30일 추이 SVG 차트 (자산 area+line + 실현손익 bar) + hover 툴팁
- 당일 실현손익 강조 (font-weight 800, 17px)
- 정렬: `?sort=portfolio|asset|cash|realized`
  - `portfolio` (기본): 프로젝트 순(ezgain → ezinvest → ezsplit → ezadmin) → 총자산 desc + 프로젝트 변경 시 줄바꿈
  - 그 외: 해당 메트릭 desc

### 종목 상세 (드로어 + 정렬)
보유종목 행 클릭 → 행 아래에 펼침 (KIS·KW 모두 지원, Upbit 제외):
- **호가판**: 5매도호가 + 5매수호가 (가격 클릭 시 해당 탭 가격 input 자동 입력)
- **주문 패널 3탭**: 매수(빨강) / 매도(파랑) / 정정·취소 (탭별 prefill: 매수=매도1호가, 매도=매수1호가)
- **일봉 차트**: TradingView lightweight-charts v4.2.0 (CDN), 캔들+거래량
- 그리드 비율: `1.5fr 1.5fr 7fr` (호가 + 주문 = 30%, 차트 = 70%)
- ESC 또는 같은 행 재클릭으로 닫기

종목 테이블 14개 컬럼 모두 클릭 정렬 (▲/▼ 표시, 빈 값은 항상 끝).

### 금일 미체결 주문 + 매매내역
종목 리스트 위에 토글 섹션(`<details>`):
- **금일 미체결주문**: KIS `TTTC8001R` (CCLD_DVSN=02, 페이징) + KW `ka10075` (기본 펼침)
- **금일 매매내역**: KIS `TTTC8001R` / `TTTS3035R` + KW `ka10076` (기본 닫힘, 시간 역순)
- 매수=빨강, 매도=파랑 뱃지 통일

### 토큰 관리 (rate limit 회피)
- 파일명: `kis_<account>.token` / `kw_<account>.token` (실전), `_vps`/`_mock` 접미사
- 위치: `~/ez/tokens/` 또는 yaml 의 `token_dir:` 항목 (~ 확장)
- **app_key_hash 인-파일 저장**: 같은 KIS Open API 앱을 여러 계좌가 공유할 때
  형제 토큰 파일에서 hash 매칭 → 토큰 재사용. KIS 의 `EGW00133` (1분당 1회) 회피.
- `threading.Lock` 으로 동시 발급 직렬화, 401/EGW00123 시 강제 재발급 후 1회 재시도

### 일별 스냅샷 (SQLite)
- 테이블 `portfolio_daily`: `(portfolio_name, date, total_asset, realized_pl)`, PK `(portfolio_name, date)`
- 페이지 진입 시 자동 `upsert_today()` (KST 기준)
- 30일 차트(카드) / 1년 차트(오너 헤더) 가 같은 테이블을 합산해 그림

### 인증 (JWT 쿠키 세션)
- LAN(localhost / RFC1918 / link-local) 인증 면제
- WAN: `.env` 의 `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD_HASH` 로 폼 로그인
- HS256 JWT 쿠키 (24시간 만료) + **슬라이딩 갱신**:
  만료까지 12시간 미만 남았을 때만 새 토큰 발급 (12시간 안에 활동 시 사실상 무기한)
- 인증 설정 미입력 + WAN 접근 → 503 차단 (안전 기본)

#### 자격증명 생성
```bash
python scripts/gen_password_hash.py
# 출력값을 .env 에 추가 (해시에 '$' 가 있으므로 작은따옴표로 감쌈)
```

```env
WEB_AUTH_USER=admin
WEB_AUTH_PASSWORD_HASH='scrypt:32768:8:1$...$...'
WEB_AUTH_SECRET=...
TRUST_PROXY=0
```

## API / 라우트

| Method | Path | 설명 |
|--------|------|------|
| GET  | `/` | 포트폴리오 목록 (`?sort=portfolio\|asset\|cash\|realized`) |
| GET  | `/portfolio/<name>` | 종목 + 미체결 + 매매내역 (진입시마다 yaml 재스캔) |
| POST | `/portfolio/<name>/buy` | 매수 (KIS 국내·해외 / KW 국내) |
| POST | `/portfolio/<name>/sell` | 매도 (KIS 국내·해외 / KW 국내) |
| POST | `/portfolio/<name>/cancel` | 미체결 취소 (KIS 국내·해외 / KW 국내) |
| GET  | `/portfolio/<name>/askprice` | 매도호가 1단계 (KIS) |
| GET  | `/portfolio/<name>/orderbook` | 10단계 호가판 + 현재가/시고저 (KIS·KW 국내, 해외는 1단계) |
| GET  | `/portfolio/<name>/chart` | 일봉 히스토리 (KIS·KW, `?days=120`) |
| GET  | `/login` `POST /login` `GET /logout` | 로그인/로그아웃 |
| GET  | `/reload` | 캐시/포트폴리오 무효화 후 재스캔 |

## 주요 설계 결정

- **단일 프로세스 (FastAPI + uvicorn)**: 외부 의존(Next.js/Node) 없이 Jinja2 +
  바닐라 JS + Bootstrap 5 + lightweight-charts CDN 만 사용
- 라우트 핸들러는 동기 함수 (KIS/KW/Upbit 클라이언트가 `requests` 기반 동기) —
  FastAPI 가 자동으로 threadpool 에서 실행하므로 추가 작업 없이 병렬 처리됨
- KIS API 클라이언트는 ezgain/ezinvest 의 전역 상태 기반 모듈을 직접 import 하지
  않고 독립 구현
- 인증은 단일 `@app.middleware("http")` 로 before/after_request 합침,
  로그인 폼은 `Form(...)` 의존성으로 처리
- Holdings / Summary dict 는 백엔드·템플릿 전역에서 한국어 키명(`종목코드`,
  `평가금액`, `수익률`, `D+2예수금` 등)을 그대로 사용
- 해외 계좌는 원화 환산값을 우선 사용 (Kiwoom 해외는 환율 미제공 → USD 그대로, 참고용)
- 토큰 파일은 사용자 가독성을 위해 계좌번호 기반 명명 유지하되, 파일 내부
  `app_key_hash` 로 형제 공유 — 추가 인덱스 파일/심볼릭 링크 불필요

## 환경

- Python 3.10+
- FastAPI 0.115+, uvicorn 0.30+, Jinja2 3.1+, python-multipart 0.0.9+
- Werkzeug 3.x (`check_password_hash` 만 사용), PyYAML 6.x, requests 2.32+, PyJWT 2.10+
- SQLite (stdlib)
- TradingView lightweight-charts v4.2.0 (CDN)
- Bootstrap 5.3 (CDN)
