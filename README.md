# ezadmin

ezgain / ezinvest / ezsplit 포트폴리오의 계좌별 보유종목, 잔고, 손익을 통합 조회하고
매도 주문까지 처리할 수 있는 Flask 웹 대시보드.

## 구조

```
ezadmin/
├── src/
│   ├── app.py                   # Flask 웹서버 (port 9900)
│   ├── config_loader.py         # ezgain/ezinvest/ezsplit portfolio config 스캔 및 로드
│   ├── kis_client.py            # KIS Open API 잔고/주문 조회 (국내/해외)
│   └── kw_client.py             # 키움증권 REST API 잔고 조회 (경량 버전)
├── requirements.txt
├── run.sh                       # `python src/app.py` 래퍼
├── scripts/
│   └── gen_password_hash.py     # Basic Auth 비밀번호 해시 생성기
└── templates/
    ├── base.html                # 공통 레이아웃 (Bootstrap 5 기반 Apple 디자인)
    ├── index.html               # 포트폴리오 목록 (유저별 그룹 + 요약 카드)
    └── portfolio.html           # 보유종목 + 미체결 주문 + 매도 주문 UI
```

## 실행

```bash
pip install -r requirements.txt
python src/app.py         # 또는 ./run.sh
# http://localhost:9900
```

## 기능

### 포트폴리오 스캔 / 표시
- ezgain(`~/ez/ezgain/config/portfolio-*.yaml`),
  ezinvest(`~/ez/ezinvest/config/portfolio-*.yaml`),
  ezsplit(`~/ez/ezsplit/config/config-*.yaml`) 포트폴리오 자동 스캔
- 유저별(bmchae, hitomato, 0eh, 9bong, hayeon) 그룹 표시 및 유저별 총자산 합계
- 계좌(broker + CANO + PRDT) 중복 제거 — ezsplit 설정 우선
- 전 항목 주석처리된 YAML은 자동 제외

### 잔고 조회
- **KIS Open API**: 국내(`TTTC8434R`) / 해외(`CTRP6504R`) 실시간 잔고 조회, 페이지네이션 지원
- **키움증권 REST API**: 국내/해외 잔고 조회 (주문/취소/호가/미체결은 미지원)
- 보유종목: 종목명, 수량, 매수평균가, 현재가, 매수금액, 평가금액, 손익, 수익률, 비중, 목표비중, 비중차이
- 수익률 높은 순 정렬, 손익/수익률 색상 표시 (양: 빨강, 음: 파랑)
- 해외 계좌는 원화 환산값을 병기해 국내와 통일된 기준으로 요약 표시
- 목록 카드 요약은 `ThreadPoolExecutor` 로 병렬 조회 + 계좌별 TTL 60초 캐시

### 주문 (KIS 전용)
- 미체결 주문 전체(매수 + 매도) 조회 및 취소
- 매도 주문 (국내: `place_sell_order` / 해외: `place_sell_order_overseas`)
- 호가 조회 — 매도 가격 추천

### 토큰 관리
- KIS 토큰: ezgain/ezinvest/ezsplit 의 `token/KIS-{config_name}-{YYYYMMDD}` 파일을 재사용,
  없거나 만료된 경우에만 `/oauth2/tokenP` 호출. 401 응답 시 강제 재발급 후 1회 재시도
- Kiwoom 토큰: 각 프로젝트의 `token/KW-{config_name}.json`

### 외부 접근 보호 (Basic Auth)
- LAN(localhost / RFC1918 사설 대역 / link-local)은 인증 면제
- WAN 은 `.env` 의 `WEB_AUTH_USER`, `WEB_AUTH_PASSWORD_HASH` 기반 Basic Auth 필수
- 인증 설정이 비어 있는 상태에서 외부 접근 시 503 으로 차단 (안전한 기본값)
- 리버스 프록시 뒤에서 운영 시 `TRUST_PROXY=1` 로 `X-Forwarded-For` 첫 IP를 사용

#### Basic Auth 자격증명 생성

```bash
python scripts/gen_password_hash.py
# 출력된 WEB_AUTH_USER / WEB_AUTH_PASSWORD_HASH 두 줄을 .env 에 추가
# (해시에 '$' 가 포함되므로 반드시 작은따옴표로 감싼다)
```

`.env` 예시:

```env
WEB_AUTH_USER=admin
WEB_AUTH_PASSWORD_HASH='scrypt:32768:8:1$...$...'
TRUST_PROXY=0
```

## API / 라우트

| Method | Path | 설명 |
|--------|------|------|
| GET  | `/` | 포트폴리오 목록 + 유저별 요약 |
| GET  | `/portfolio/<name>` | 보유종목 + 미체결 주문 상세 |
| POST | `/portfolio/<name>/sell` | 매도 주문 (KIS) |
| POST | `/portfolio/<name>/cancel` | 미체결 주문 취소 (KIS) |
| GET  | `/portfolio/<name>/askprice` | 호가 조회 (KIS) |
| GET  | `/reload` | 포트폴리오 목록 + 요약 캐시 무효화 후 재로드 |

## 주요 설계 결정

- KIS API 클라이언트는 ezgain/ezinvest 의 전역 상태 기반 모듈을 직접 import 하지 않고
  독립 구현 — 여러 계좌를 순차 조회할 때의 상태 충돌을 피하기 위해서다.
- Kiwoom 은 잔고 조회만 지원하며 주문/취소/호가/미체결 등 쓰기성 작업은 의도적으로 제외했다.
- 해외 계좌는 원화 환산값을 우선 사용해 국내와 동일한 통화 기준으로 요약을 통일한다
  (Kiwoom 해외는 환율 정보가 없어 USD 값을 그대로 사용하므로 참고용).
- Holdings / Summary dict 는 백엔드·템플릿 전역에서 한국어 키명(`종목코드`, `평가금액`,
  `수익률`, `D+2예수금` 등)을 그대로 사용한다.
- Owner 감지는 파일명 기반 — `config_loader.py` 의 `KNOWN_OWNERS` 리스트로 관리한다.
