# ezadmin

ezgain / ezinvest 포트폴리오의 계좌별 보유종목, 잔고, 손익을 조회하는 웹 대시보드.

## 구조

```
ezadmin/
├── app.py              # Flask 웹서버
├── config_loader.py    # ezgain/ezinvest portfolio config 스캔 및 로드
├── kis_client.py       # KIS Open API 잔고조회 (국내/해외)
├── requirements.txt
└── templates/
    ├── base.html       # 공통 레이아웃 (Bootstrap 5)
    ├── index.html      # 포트폴리오 목록 (유저별 그룹)
    └── portfolio.html  # 보유종목 상세 테이블
```

## 실행

```bash
pip install -r requirements.txt
python app.py
# http://localhost:9000
```

## 기능

- ezgain(`~/ez/ezgain/config/portfolio-*.yaml`) 및 ezinvest(`~/ez/ezinvest/config/portfolio-*.yaml`) 포트폴리오 자동 스캔
- 유저별(bmchae, hitomato, 0eh, 9bong) 그룹 표시
- KIS Open API를 통한 실시간 잔고 조회 (국내주식 / 해외주식)
- 보유종목: 종목명, 수량, 매수평균가, 현재가, 매수금액, 평가금액, 손익, 수익률, 비중, 비중차이
- 평가금액 큰 순 정렬, 손익/수익률 색상 표시 (양: 빨강, 음: 파랑)
- ezgain/ezinvest 프로젝트의 기존 토큰 재사용 (불필요한 토큰 재발급 방지)

## API

- `GET /` - 포트폴리오 목록
- `GET /portfolio/<name>` - 포트폴리오 상세 잔고 조회
- `GET /reload` - 설정 파일 다시 로드
