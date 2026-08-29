# 운영 가이드

## 기본 실행

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8077
```

ASGI 서버는 DB 마이그레이션 후 즉시 요청을 받습니다. 시작 직후 백그라운드 reconciliation이 캐시 결측을 감사하며, cadence가 남아 있어도 누락된 항목만 보상 수집합니다. 수동 확인은 다음 명령을 사용합니다.

```bash
uv run python -m app.collect
uv run python -m app.collect --repair
uv run python -m app.collect --history
uv run python -m app.collect --job events --force
uv run python -m app.collect --job krx_market --force
curl --fail http://127.0.0.1:8077/api/health
```

## 배포 규칙

- scheduler-enabled 프로세스는 정확히 하나만 둡니다.
- 여러 웹 worker를 쓸 때는 한 프로세스를 제외하고 `MONEY_DISABLE_SCHEDULER=1`을 설정합니다.
- 기본 바인딩은 `127.0.0.1`로 유지합니다.
- 외부 노출은 인증·TLS·요청 제한이 있는 reverse proxy 뒤에서만 합니다. 앱 자체에는 사용자 인증이 없습니다.
- `.env`와 `data/*.db`는 공개 저장소나 정적 파일 경로에 두지 않습니다.

## 상태와 장애 확인

1. `/api/health`에서 `database_integrity`, `collector_errors`, `reconciliation`, `historical_recovery`를 봅니다.
2. `/api/scheduler`에서 1차 결측 감사와 2차 manifest 복구의 마지막 보고서를 봅니다.
3. `/manage`에서 계열별 최신성, 2차 종료 상태, 마지막 성공, 부분 실패 상세를 봅니다.
4. 누락 항목만 수동 보상하려면 `uv run python -m app.collect --repair`를 실행합니다.
5. 전체 수동 재시도는 해당 job만 실행합니다: `uv run python -m app.collect --job <name> --force`.
6. 2차 배치를 한 번 수동 진행하려면 `uv run python -m app.collect --history`를 실행합니다.
7. `blocked`·`exhausted` 원인을 해결한 뒤에만 `uv run python -m app.collect --history-reset`을 실행합니다. 특정 대상은 `--history-kind`·`--history-target`으로 제한합니다.
8. 공급자가 실패해도 DB의 마지막 성공 데이터는 삭제하지 않습니다.

수집기 이름은 `events`, `indicators_daily`, `indicators_monthly`, `indicators_quarterly`, `quotes`, `index_quotes`, `index_history`, `historical_recovery`이며 `KRX_API_KEY`가 있으면 `krx_market`이 추가됩니다.

`krx_market`이 HTTP 401이면 해당 데이터셋은 `blocked` 권한 gate에 들어가 외부 재호출을 중단합니다. KRX Open API 마이페이지에서 서비스를 승인하거나 키를 갱신한 뒤 해당 데이터셋을 명시적으로 reset합니다. 키 fingerprint가 바뀐 경우에는 자동으로 새 세대가 열립니다.

### 결측 보상 설정

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `RECONCILE_INTERVAL` | 300초 | 외부 호출 없이 SQLite 결측을 다시 감사하는 간격 |
| `REPAIR_BACKOFF` | 3600초 | 정상·성공 상태에서 결측이 발견됐을 때 최대 보상 backoff |
| `REPAIR_ERROR_BACKOFF` | 21600초 | `partial/error` 이후 공급자 재시도 최대 backoff |

기존 수집 간격이 위 backoff보다 짧으면 해당 수집 간격을 적용합니다. 정기 cadence는 그대로 유지됩니다. reconciliation은 일정 원문, 지표별 관측 연령, 관심 종목·지수 시세 누락, 지수 이력 길이·최신일, KRX 데이터셋별 최근 거래일 상태를 별도로 검사합니다. 공급자 호출은 웹 요청 경로가 아니라 scheduler의 단일 background worker에서만 일어납니다.

일반 KRX 일시 오류는 `KRX_MARKET_INTERVAL`(기본 24시간)을 오류 backoff로 유지합니다. 인증·승인 오류는 backoff 반복 대신 데이터셋별 `blocked` gate로 종료합니다.

### 2차 과거 완전성 설정

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `HISTORY_RECOVERY_INTERVAL` | 21600초 | 자원 제한 2차 배치 간격 |
| `HISTORY_RECOVERY_MAX_ATTEMPTS` | 3 | 대상별 자동 공급자 호출 상한 |
| `HISTORY_RECOVERY_BACKOFF` | 21600초 | 첫 일시 오류 후 대기 |
| `HISTORY_RECOVERY_MAX_BACKOFF` | 604800초 | 지수 backoff 상한 |
| `HISTORY_EMPTY_CONFIRMATIONS` | 2 | 전체 시계열 빈 응답 확인 후 `exhausted` 처리 횟수 |
| `HISTORY_INDICATOR_CALLS_PER_RUN` | 6 | 실행당 지표 snapshot 상한 |
| `HISTORY_INDEX_CALLS_PER_RUN` | 3 | 실행당 지수 snapshot 상한 |
| `HISTORY_KRX_CALLS_PER_RUN` | 3 | 실행당 KRX 일별 표 상한 |
| `HISTORY_INDEX_YEARS` | 20 | 글로벌 지수 과거 검증 범위 |
| `HISTORY_KRX_BUSINESS_DAYS` | 20 | KRX 고정 과거 세대의 영업일 수 |
| `HISTORY_KRX_MAX_ROWS_PER_RUN` | 200000 | 2차 KRX 실행당 저장 행 상한 |

2차 방어는 달력으로 거래일이나 관측일을 추정하지 않습니다. 첫 공급자 snapshot이 반환한 날짜를 manifest로 확정하고, 이후에는 로컬 행 소실만 재활성화합니다. 정상 빈 KRX 표는 `verified_empty`, 인증 문제는 `blocked`, 최대 시도 초과는 `exhausted`이므로 무한 자동 재시도가 없습니다. 상세 상태 전이는 [과거 복구 계획](../plan/historical-recovery.md)을 참고합니다.

## 백업

SQLite online backup API로 일관성 검사를 포함한 복사본을 만듭니다.

```bash
uv run python -m app.backup                       # 백업 후 보존 정책 적용
uv run python -m app.backup --destination /path/to/backup-volume
uv run python -m app.backup --dry-run             # 정리 대상만 확인
uv run python -m app.backup --prune-only          # 새 백업 없이 정리만
uv run python -m app.backup --no-compress         # 평문 .db 로 저장
```

기본 위치는 `data/backups/money-<UTC timestamp>.db.gz`이며 gzip으로 약 7.5배 압축됩니다. 무결성 검사는 압축 전 평문 사본에 대해 수행합니다.

보존은 계층 + 용량 상한입니다. 최근 3개, 주당 1개씩 4주, 월당 1개씩 6개월을 유지하고, 총 20GB를 넘으면 오래된 것부터 정리합니다. 가장 최근 백업은 예산을 초과해도 지우지 않습니다. 값은 `BACKUP_KEEP_RECENT`·`BACKUP_KEEP_WEEKLY`·`BACKUP_KEEP_MONTHLY`·`BACKUP_MAX_TOTAL_MB`·`BACKUP_COMPRESS`로 조정합니다.

DB는 KRX `all` 범위에서 연 5~6GB로 자랍니다. 압축 후 사본당 약 0.8GB이므로 기본 정책의 최대 13개 사본이 상한 20GB 안에 들어옵니다.

압축본은 복구 전에 풀어야 합니다.

```bash
uv run python -m app.backup --restore data/backups/money-<stamp>.db.gz --into /tmp/check.db
```

복구 전에는 서버를 중지하고 원본 DB를 별도 보존합니다. 먼저 임시 경로에서 다음처럼 검사한 후 운영 경로 교체를 결정합니다.

```bash
MONEY_DB_PATH=/path/to/backup.db uv run python -c "from app import db; db.init_db(); print(db.get_meta('schema_version'))"
sqlite3 /path/to/backup.db 'PRAGMA quick_check;'
```

## 검증

```bash
MONEY_DISABLE_SCHEDULER=1 uv run python -m unittest -v
```

테스트는 임시 SQLite를 사용하고 외부 네트워크를 호출하지 않습니다. 배포 전에는 단위 테스트에 더해 브라우저에서 `/`, `/indices`, `/charts`, `/manage`, `/correlation`, `/spillover`를 확인합니다.
