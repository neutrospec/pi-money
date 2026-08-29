# 에이전트 연동

웹과 에이전트는 수집을 트리거하지 않고 같은 SQLite 캐시를 읽습니다. 사용 환경에 따라 표준 MCP 또는 프로젝트 로컬 pi 확장을 선택하며, 두 인터페이스는 아래 16개 기능을 동일한 이름으로 제공합니다.

## 프로젝트 스킬

`.agents/skills/money-market-intelligence/SKILL.md`는 이 저장소의 에이전트가 데이터 도구를 고르고 결과를 해석하는 기준입니다. 지표 키·지수 이름을 추측하지 않는 발견 절차, 관측일과 수집 시각의 구분, 결측·오래된 캐시 처리, 금융 분석별 과잉 해석 방지 규칙을 포함합니다.

스킬의 핵심 원칙은 다음과 같습니다.

- 최신성이 중요하거나 결과가 비면 `market_health`로 수집 상태부터 확인
- 지표는 `market_indicator_list`, 지수는 `market_indices`, KRX 전체 종목은 `market_universe`로 먼저 발견
- 답변에 관측일·출처·캐시 여부를 명시
- 상관·시차 상관·전이 방향을 예측력 또는 인과관계로 표현하지 않음
- 수집 갱신은 사용자가 명시적으로 요청한 경우에만 실행

## 표준 MCP 2.0

`app/mcp_server.py`는 stdio 서버이며 FastAPI가 떠 있지 않아도 동작합니다.

```bash
uv run python -m app.mcp_server
```

MCP 클라이언트에는 작업 디렉터리를 고정한 명령으로 등록합니다. 클라이언트별 설정 문법은 다르지만 실행 값은 다음과 같습니다.

```text
command: uv
args: --directory /Users/nobocop/projects/money run python -m app.mcp_server
```

| 도구 | 기능 |
|------|------|
| `market_health` | DB 무결성·마지막 수집·partial/error 수집기 |
| `market_brief` | 이번 주 분포 이동·불일치·판정을 뒤집는 조건(투표 산술) |
| `market_events` | KST 기준 향후 공식 경제 일정 |
| `market_quotes` | 관심 종목의 마지막 수집 시세 |
| `market_indices` | 글로벌 지수 심볼·시장 관측일·수집 시각 |
| `market_indicator_list` | 지표 키·출처·빈도·최신 관측일 발견 |
| `market_indicator` | 지표 메타데이터·최근 관측값, `position`(자체 분포 위치·관측 창·위험 방향), `explanation`(설명 층·연관 계열·생성된 현재 값 해석) |
| `market_universe` | KRX 등 공급자에서 자동 발견한 전체 종목 검색 |
| `market_correlation` | 수익률 롤링·시차 상관과 표본 진단 |
| `market_spillover` | generalized FEVD 연결성과 표시용 핵심 간선 |
| `market_yield_curve` | 공통 관측일 기준 미국·한국 장단기 스프레드 |
| `market_index_analysis` | 지수 추세·변동성·최대 낙폭 |
| `market_technical` | RSI·MACD·볼린저·추세 기술 통계 |
| `market_risk` | 샤프·과거 VaR/ES·최대 낙폭 |
| `market_regime` | 한·미 두 규칙형 국면 분류. 미국은 VIX·신용·S&P 임계값, 한국은 VKOSPI·회사채 스프레드·CP−CD·코스피 추세·낙폭의 자체 분포 백분위 |
| `market_derived_metrics` | 날짜 정렬한 신용·실질금리·유동성·물가·상대강도 파생지표 |
| `market_breadth` | 캐시된 KRX 전 종목 표의 상승·하락·쏠림·20일 시장폭 |

도구 목록 smoke test:

```bash
uv run python -c "import anyio; from app.mcp_server import mcp; print([x.name for x in anyio.run(mcp.list_tools)])"
```

## pi 프로젝트 확장

`.pi/extensions/market.ts`가 `http://localhost:8077`의 REST API를 호출합니다. 이 방식은 웹 서버가 실행 중이어야 합니다.

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8077
pi
```

pi도 위 표의 16개 도구를 같은 이름으로 제공합니다. 일정의 `days`는 서버가 KST 오늘을 기준으로 계산하므로 에이전트 프로세스의 로컬 시간대와 무관합니다. API 오류 시 HTTP 상태뿐 아니라 서버의 `detail` 메시지도 표시합니다.

## 해석 원칙

- 모든 답변에 마지막 관측일과 출처를 함께 확인합니다.
- `updated`, `updated_at`, `retrieved_at`은 수집 시각이며 시장 관측일과 구분합니다.
- `us_rate`는 FRED의 실효연방기금금리이며 FOMC 목표 범위와 같다고 표현하지 않습니다.
- `TOPIX ETF (proxy)` 같은 대용치는 기초 지수 자체로 표현하지 않습니다.
- 시차 상관의 가장 큰 값은 표본 내 통계량일 뿐 예측력·인과관계의 증거가 아닙니다.
- generalized FEVD 연결성의 방향도 구조적 충격 식별이나 인과 추론이 아닙니다.
- 과거 VaR는 손실 분위수이지 최대 예상 손실이 아닙니다.
- 데이터가 비었거나 오래됐으면 추정하지 말고 수집 상태를 먼저 확인합니다.
