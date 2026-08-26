# Money Market Intelligence System — 문서

한국 money market을 중심으로 무료 금융·경제 데이터를 수집하고, 웹과 에이전트가 같은 SQLite 저장소를 읽는 개인용 모니터링 시스템입니다.

## 문서 맵

| 문서 | 내용 |
|------|------|
| [`state.md`](state.md) | 구현·데이터·API의 현재 상태와 알려진 한계 |
| [`architecture/README.md`](architecture/README.md) | 수집·저장·웹·에이전트 데이터 흐름 |
| [`architecture/operations.md`](architecture/operations.md) | 실행, 백업, 장애 확인, 외부 노출 원칙 |
| [`agent.md`](agent.md) | 표준 MCP 및 pi 연동 |
| [`sources/setup.md`](sources/setup.md) | **처음 설정** — 키 발급, KRX 신청 항목, 점검 (약 10분) |
| [`sources/README.md`](sources/README.md) | 무료 데이터 소스와 인증·한계 |
| [`plan/README.md`](plan/README.md) | M0~M7 계획과 실제 상태 |
| [`plan/historical-recovery.md`](plan/historical-recovery.md) | 1차 최신성·2차 과거 완전성 방어와 유한 종료 조건 |
| [`plan/data-coverage.md`](plan/data-coverage.md) | 더 풍부한 시장 분석을 위한 입력 데이터 우선순위·자원 상한 |
| [`tasks/`](tasks/) | 마일스톤별 검증 체크리스트, M6 분석 입력·브리핑 진행 상태 |
| [`roadmap/README.md`](roadmap/README.md) | 다음 단계와 의도적으로 남긴 범위 |
| [`analysis-methods.md`](analysis-methods.md) | 분석 방법과 해석 제한 |
| [`lessons.md`](lessons.md) | 시행착오와 재발 방지 규칙 |
| [`finance-guide.md`](finance-guide.md) | 금융 기초 가이드 |

## 빠른 시작

```bash
uv sync
cp .env.example .env              # ECOS·FRED 키 입력
uv run python -m app.doctor       # 키가 실제로 동작하는지 점검
uv run uvicorn app.main:app --host 127.0.0.1 --port 8077
```

키 발급 절차와 KRX 서비스 신청 항목은 [`sources/setup.md`](sources/setup.md)에 단계별로 있습니다. API 키는 소스코드나 문서에 기록하지 않습니다.

## 핵심 원칙

- 무료 소스만 사용하고 공급자 한계를 숨기지 않는다.
- 모든 소비자는 SQLite 캐시를 읽고, 외부 호출은 수집기만 수행한다.
- 매 마일스톤마다 실행 가능한 결과와 결정론적 검증을 남긴다.
- 새 의존성과 추상화는 실제 소비자가 생긴 경우에만 추가한다.
