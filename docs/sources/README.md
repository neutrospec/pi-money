# 데이터 소스와 인증

모든 운영 데이터 소스는 무료입니다. 키는 `.env` 또는 OS keychain에만 저장하고 코드·문서·로그에 출력하지 않습니다.

## 사용 중인 소스

### 한국은행 ECOS

- 용도: 한국 기준금리, 국고채, 환율, 물가, 통화, 성장·고용·소비·건설·무역 계열 81개
- 인증: `ECOS_API_KEY`
- 가입: <https://ecos.bok.or.kr/>
- 주의: 계열마다 일·월·분기·연간 주기가 다르므로 같은 최신성 기준을 적용하지 않습니다. 공급자 표에 복수 하위 차원이 있으면 `source_options`로 선택 조건을 보존합니다.

### FRED

- 용도: 미국 및 일부 주요국 거시계열 61개, SOFR·금리곡선·실질금리·기대인플레이션·광의 달러·NFCI·금융스트레스·연준 유동성
- 인증: `FRED_API_KEY`
- 가입: <https://fred.stlouisfed.org/docs/api/api_key.html>
- 주의: FRED series 정의와 단위를 카탈로그에 보존합니다. `FEDFUNDS`는 정책 목표가 아니라 월평균 실효연방기금금리입니다.

### Yahoo Finance

- 용도: 주식·ETF·지수·상품의 현재가와 일별 이력
- 인증: 없음
- 주의: 공식 보장 API가 아닙니다. 요청은 수집기에서만 수행하고, 20년 이력은 명시적 `period1/period2`로 요청합니다. 심볼 장애나 rate limit은 배치 부분 실패로 기록합니다.
- 분석 proxy: RSP(동일가중 시장폭), SMH(반도체), HYG(하이일드채), TLT(장기국채), LQD(IG 회사채), KRE(지역은행), XLY(경기소비재), XLP(필수소비재)를 2년 일봉으로만 수집합니다. 모두 공식 지수·수익률이 아닌 `proxy`로 표시합니다.

### KRX Open API

- 용도: KRX·KOSPI·KOSDAQ·채권·파생 지수 전체, KOSPI/KOSDAQ/KONEX 전 주식, ETF·ETN 전 종목, 금·석유·배출권 일별 표
- 인증: `KRX_API_KEY`와 사용할 API 서비스별 이용 승인
- 가입·승인: <https://openapi.krx.co.kr/>
- 기본 정책: `KRX_MARKET_SCOPE=balanced`, 최근 5거래일 최초 수집 후 하루 1회 증분
- 보존: 정규화 OHLCV·시가총액과 공급자 원본 행을 함께 저장하며 심볼은 응답에서 자동 발견
- 자원 보호: 데이터셋당 20,000행, 실행당 100,000행 상한. `all`은 ELW·채권·파생상품까지 포함하므로 DB 증가량 검토 후 사용
- 현재 운영 상태: 키는 설정돼 있지만 2026-08-24 호출이 HTTP 401을 반환했습니다. 서비스별 신청 승인 또는 키 유효기간 확인이 필요합니다.

## 공식 일정 출처

일정은 제3자 캘린더를 스크래핑하지 않고 다음 기관의 확정 발표 자료에서 큐레이션합니다.

- Federal Reserve FOMC calendar
- BLS release schedule
- BEA release schedule
- ECB Governing Council calendar
- Bank of England MPC dates
- Bank of Japan calendar and meeting schedule
- 한국은행 금융통화위원회 일정

저장 시 원문 `source_date`, `source_time`, `source_timezone`, `source_url`과 KST 변환 `date`, `time`을 함께 둡니다. 기관이 시각을 확정하지 않은 일정은 `time = null`이며 화면에는 “시간 미정”으로 표시합니다. 공식 확정 자료가 없는 2027 일정은 추정해 넣지 않습니다.

## 준비됐지만 미연동인 소스

### ECB·BOJ·Eurostat 직접 API

- 인증: 대체로 없음
- 상태: 정책 일정은 공식 출처를 사용하지만 거시 시계열은 아직 FRED proxy 중심입니다.

### OpenDART·CFTC·미국 재무부

- OpenDART: 한국 기업 실적·공시 연결 후보. 무료 키가 필요하고 정정공시·연결/별도 재무제표 정규화가 선행돼야 합니다.
- CFTC COT: 달러·금리·원자재 주간 포지셔닝 후보. 무료지만 계약 코드와 연속계약 매핑이 필요합니다.
- 미국 재무부: 입찰·발행 일정 후보. 공식 저빈도 일정으로 M6 이벤트 위험 보강에 사용합니다.
- 자동 활성화하지 않으며 [`../plan/data-coverage.md`](../plan/data-coverage.md)의 자원 상한과 우선순위를 따릅니다.

## 환경 설정

```bash
cp .env.example .env
```

필수 값은 `ECOS_API_KEY`, `FRED_API_KEY`입니다. 키가 비어 있으면 수집기는 빈 키로 요청하지 않고 명시적인 설정 오류를 기록합니다. `KRX_API_KEY`가 있으면 KRX 수집기는 자동 활성화됩니다. 일시 중지는 `KRX_MARKET_ENABLED=0`, 범위 조정은 `KRX_MARKET_SCOPE=light|balanced|all`을 사용합니다.
