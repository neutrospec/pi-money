# 데이터 소스와 인증

> 처음 설정하신다면 [`setup.md`](setup.md)를 먼저 보세요. 이 문서는 소스별 특성과 한계를 다룹니다.

모든 운영 데이터 소스는 무료입니다. 키는 `.env` 또는 OS keychain에만 저장하고 코드·문서·로그에 출력하지 않습니다.

## 사용 중인 소스

### 한국은행 ECOS

- 용도: 한국 기준금리, 국고채, 환율, 물가, 통화, 성장·고용·소비·건설·무역 계열 81개 (pyecos 큐레이션)
- 인증: `ECOS_API_KEY`
- 가입: <https://ecos.bok.or.kr/>
- 주의: 계열마다 일·월·분기·연간 주기가 다르므로 같은 최신성 기준을 적용하지 않습니다. 공급자 표에 복수 하위 차원이 있으면 `source_options`로 선택 조건을 보존합니다.

### 한국은행 ECOS 원표 (직접 조회)

- 용도: pyecos 큐레이션에 없는 한국 자금시장 핵심 계열 8개 — KOFR, CP 91일, 통안증권 91일, 국고채 1·2·10·30년, 회사채 BBB-
- 인증: `ECOS_API_KEY` (위와 동일한 키)
- 방식: 통계표 코드와 항목 코드로 직접 지정합니다. 카탈로그의 `series`가 `817Y002/010901000` 형식입니다.
- 이유: 국고채 곡선이 3년·5년에서 끊겨 있었고 무위험지표금리(KOFR)와 단기 신용물(CP)이 빠져 있어, 이 프로젝트의 주제인 자금시장을 볼 수 없었습니다.

### FRED

- 용도: 미국 및 일부 주요국 거시계열 59개, SOFR·금리곡선·실질금리·기대인플레이션·광의 달러·NFCI·금융스트레스·연준 유동성
- 인증: `FRED_API_KEY`
- 가입: <https://fred.stlouisfed.org/docs/api/api_key.html>
- 주의: FRED series 정의와 단위를 카탈로그에 보존합니다. `FEDFUNDS`는 정책 목표가 아니라 월평균 실효연방기금금리입니다.
- OECD MEI 경유 계열은 공급자가 2025년 중반 이후 갱신을 멈췄습니다. 유로존·영국 금리는 아래 직접 원천으로 교체했고, 남은 영국·중국 CPI는 `provider_stalled`로 표면화합니다.

### ECB Data Portal

- 용도: 유로존 3개월 EURIBOR 1개
- 인증: 없음
- 형식: SDMX-JSON. `format=jsondata&lastNObservations=N`
- 이유: 같은 계열의 OECD 경유 FRED 값보다 6개월 이상 최신입니다.

### Bank of England IADB

- 용도: 영국 정책금리(Bank Rate)와 SONIA 익일물 2개
- 인증: 없음
- 형식: CSV (`fromshowcolumns.asp`)
- 주의: 영업일에만 게시하므로 `date_kind`는 `trading_day`입니다. 금리 자체는 주말에도 유효하지만 판정 기준은 공급자의 발표 달력입니다.

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
- 자원 보호: 데이터셋당 60,000행, 실행당 400,000행 상한. `all`은 ELW·채권·파생상품까지 포함하므로 DB 증가량 검토 후 사용
- 서비스별 승인: 키 발급과 이용 신청이 별개입니다. 시장폭에는 `sto/stk_bydd_trd`(유가증권)와 `sto/ksq_bydd_trd`(코스닥) 2개만 있으면 충분하며, 이름이 비슷한 `idx/kospi_dd_trd`(지수)와 혼동하지 않도록 주의합니다. 신청 목록과 절차는 [`setup.md`](setup.md#선택--krx-open-api) 참조.
- 현재 운영 상태와 승인 현황: [`../state.md`](../state.md)의 데이터 소스 절 참조. 점검은 `uv run python -m app.doctor`.

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

### BOJ·Eurostat·ONS·중국 NBS 직접 API

- ECB와 Bank of England는 연동을 마쳤습니다(위 참조).
- ONS(영국 통계청): 구 API는 폐지됐고 신규 `api.beta.ons.gov.uk`는 데이터셋 목록까지는 응답하나 관측치 엔드포인트가 빈 값을 반환해 미연동입니다.
- 중국 NBS: `data.stats.gov.cn`이 HTTP 403으로 차단돼 미연동입니다.
- BOJ: 시계열 검색이 세션 기반이라 별도 파서가 필요합니다. 일본 단기금리는 현재 OECD 경유 콜금리를 사용합니다.

### OpenDART·CFTC·미국 재무부

- OpenDART: 한국 기업 실적·공시 연결 후보. 무료 키가 필요하고 정정공시·연결/별도 재무제표 정규화가 선행돼야 합니다.
- CFTC COT: 달러·금리·원자재 주간 포지셔닝 후보. 무료지만 계약 코드와 연속계약 매핑이 필요합니다.
- 미국 재무부: 입찰·발행 일정 후보. 공식 저빈도 일정으로 M6 이벤트 위험 보강에 사용합니다.
- 자동 활성화하지 않으며 [`../plan/data-coverage.md`](../plan/data-coverage.md)의 자원 상한과 우선순위를 따릅니다.

## 환경 설정

```bash
cp .env.example .env
uv run python -m app.doctor
```

`app.doctor`는 각 공급자를 실제로 한 번 호출해 키 동작 여부와 KRX 승인 현황을 보고하고, 남은 조치를 안내합니다. DB에 쓰지 않으므로 첫 수집 전에도 안전합니다.

필수 값은 `ECOS_API_KEY`, `FRED_API_KEY`입니다. 키가 비어 있으면 수집기는 빈 키로 요청하지 않고 명시적인 설정 오류를 기록합니다. `KRX_API_KEY`가 있으면 KRX 수집기는 자동 활성화됩니다. 일시 중지는 `KRX_MARKET_ENABLED=0`, 범위 조정은 `KRX_MARKET_SCOPE=light|balanced|all`을 사용합니다.
