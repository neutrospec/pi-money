# Money Market Intelligence System

한국 money market을 중심으로 무료 금융·경제 데이터를 SQLite 한 곳에 모으고, 웹·REST·MCP·pi가 같은 캐시를 읽는 개인용 모니터링 시스템입니다.

## 빠른 시작

```bash
uv sync
cp .env.example .env              # 키 값 입력, 커밋 금지
uv run python -m app.doctor       # 키 동작·KRX 승인 현황 점검
uv run uvicorn app.main:app --host 127.0.0.1 --port 8077
```

키 발급 절차와 KRX 서비스 신청 항목은 [docs/sources/setup.md](docs/sources/setup.md)에 있습니다. 필수는 ECOS·FRED 두 개뿐이고 약 10분이면 끝납니다.

수동 수집과 검증:

```bash
uv run python -m app.collect              # 주기·최신성 존중
uv run python -m app.collect --repair     # 결측 감사 후 누락 항목만 보상 수집
uv run python -m app.collect --history    # 자원 제한 2차 과거 완전성 배치
uv run python -m app.collect --force      # 전체 강제 수집
uv run python -m unittest -v
uv run python -m app.backup
```

표준 MCP 서버는 별도 웹 서버 없이 SQLite 캐시를 직접 읽습니다.

```bash
uv run python -m app.mcp_server
```

## 현재 범위

- 공식 출처가 확인된 2026년 주요 경제 일정(KST 변환 및 원문 시각 보존)
- ECOS·ECOS 원표·FRED·Yahoo·BoE·ECB 기반 164개 지표, 글로벌 지수 24개, 관심 종목 34개
- 서버 렌더 시장 상황판과 시장 심리 게이지를 첫 화면으로, 지표·지수·상관·연결성·기술·위험·파생 진단 웹 화면과 JSON API
- 프로젝트 로컬 pi 확장·표준 MCP 도구 각 19개와 해석 품질을 통제하는 프로젝트 스킬
- SQLite schema v8, 거래소 시간대 기준 관측일·계열별 날짜 의미·공급자 정산 세션, 결측 등급을 구분하는 커버리지 감사, manifest 기반 유한 과거 복구, 일관성 백업

상세 현황과 남은 범위는 [docs/README.md](docs/README.md)와 [docs/state.md](docs/state.md)를 참고하세요.
