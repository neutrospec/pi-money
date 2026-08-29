/**
 * Money Market Intelligence — pi 확장
 *
 * 이 프로젝트에서 작업하는 pi에게 우리 시스템의 데이터를 조회하는 도구를 제공한다.
 * 로컬 FastAPI 서버 (http://localhost:8077) 를 호출한다.
 *
 * 프로젝트 로컬 확장이므로 이 프로젝트에서만 로드된다.
 * 안정화 후 ~/.pi/agent/extensions/ 로 옮기면 전역 적용된다.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const BASE = "http://localhost:8077";

async function apiGet(path: string): Promise<any> {
	const res = await fetch(`${BASE}${path}`);
	const body = await res.text();
	let data: any = {};
	try {
		data = body ? JSON.parse(body) : {};
	} catch {
		data = { detail: body || "응답 본문 없음" };
	}
	if (!res.ok) {
		const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
		throw new Error(`Money API ${res.status}: ${detail}`);
	}
	return data;
}

export default function (pi: ExtensionAPI) {
	// 캐시·수집 상태
	pi.registerTool({
		name: "market_health",
		label: "시장 데이터 상태",
		description:
			"데이터가 없거나 오래돼 보일 때 SQLite 무결성, 마지막 수집 시각, partial/error 수집기를 확인한다. 수집을 실행하지 않는다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/health");
			const issues = data.collector_issues || [];
			const issueLines = issues.map((item: any) => `- ${item.name}: ${item.status}${item.error ? ` — ${item.error}` : ""}`);
			const done = data.completeness || {};
			const core = done.core || {};
			const text =
				`캐시 상태: ${data.status} / DB: ${data.database_integrity} / schema: ${data.schema_version || "N/A"}\n` +
				`수집 완전성: ${done.status || "N/A"} (핵심 ${core.ready ?? "?"}/${core.total ?? "?"} 준비, 미해소 ${done.unresolved ?? "?"})\n` +
				`마지막 전체 수집: ${data.last_collect || "기록 없음"}` +
				(issueLines.length ? `\n수집 이슈:\n${issueLines.join("\n")}` : "\n수집 이슈 없음");
			return { content: [{ type: "text", text }], details: data };
		},
	});

	// 시장 상황 한 번에 — 개별 계열로 들어가기 전 개관
	pi.registerTool({
		name: "market_situation",
		label: "시장 상황판",
		description:
			"국면 판정, 핵심 금리·환율·위험 수준, 파생 스프레드, 이번 주 주요 발표, 수집 신선도를 한 번에 조회한다. '지금 시장이 어떤가'를 물을 때 개별 계열을 여러 번 부르는 대신 이것을 쓴다.",
		parameters: Type.Object({}),
		async execute() {
			const d = await apiGet("/api/situation");
			const r = d.regime || {};
			const f = d.freshness || {};
			const tiles = (d.groups || []).map(
				(g: any) =>
					`[${g.title}] ` +
					g.tiles.map((t: any) => `${t.label} ${t.value}${t.unit} (${t.date})`).join(", "),
			);
			const idx = (d.indices || []).map(
				(i: any) => `${i.name} ${i.value}${i.change_pct != null ? ` ${i.change_pct > 0 ? "+" : ""}${i.change_pct}%` : ""} (${i.session_date})`,
			);
			const risk = (d.risk?.items || []).map((i: any) => `${i.label} ${i.value}${i.unit} (${i.date})`);
			const events = (d.events || []).map((e: any) => `${e.date} ${e.time || "시간 미정"} ${e.title}`);
			const kr = d.korea_regime || {};
			const krPending = (kr.pending || []).map((p: any) => `${p.label} 제외(${p.reason})`);
			const text =
				`한국 국면: ${kr.regime} (구성요소 ${kr.component_count}/${kr.component_total}, 순점수 ${kr.score}) — ${(kr.reasons || []).join(" · ")}\n` +
				(krPending.length ? `한국 국면 제외 항목: ${krPending.join(", ")}\n` : "") +
				`미국 국면: ${r.regime} — ${(r.reasons || []).join(" · ")}\n` +
				`관측일 한국 ${kr.as_of || "-"} / VIX ${r.as_of?.vix || "-"} / 스프레드 ${r.as_of?.credit_spread || "-"} / S&P ${r.as_of?.sp500 || "-"}\n` +
				`두 분류기는 서로 다른 시장의 입력을 쓴다. 갈릴 때는 둘 다 보고할 것.\n\n` +
				`${tiles.join("\n")}\n\n지수: ${idx.join(", ")}\n\n파생: ${risk.join(", ")}\n\n` +
				(events.length ? `이번 주 주요 발표:\n${events.join("\n")}\n\n` : "") +
				`수집: ${f.status} · 핵심 ${f.core_ready}/${f.core_total} 최신 · 공급자 지연 ${f.stale}`;
			return { content: [{ type: "text", text }], details: d };
		},
	});

	// KRX 데이터셋 목록
	pi.registerTool({
		name: "market_datasets",
		label: "KRX 데이터셋",
		description:
			"캐시에 보유한 KRX 일별 표 목록과 각 표의 종목 수·행 수·보유 기간을 조회한다. market_daily 로 내려가기 전에 어떤 표가 있는지 확인할 때 쓴다.",
		parameters: Type.Object({}),
		async execute() {
			const d = await apiGet("/api/market/datasets");
			const lines = d.datasets.map(
				(x: any) =>
					`  ${x.dataset} (${x.label}) — ${x.instruments.toLocaleString()}종목 ${x.rows.toLocaleString()}행 ${x.first_date || "-"}~${x.latest_date || "-"}`,
			);
			return {
				content: [{ type: "text", text: `KRX 데이터셋 ${d.count}개 (범위 ${d.scope})\n${lines.join("\n")}` }],
				details: d,
			};
		},
	});

	// KRX 일별시세 (옵션·선물·ETF·채권 등)
	pi.registerTool({
		name: "market_daily",
		label: "KRX 일별시세",
		description:
			"KRX 일별 표 한 개의 캐시된 시세 행을 조회한다. 표가 크므로 dataset 지정이 필수이고, 가능하면 symbol 또는 date 로 좁힌다.",
		parameters: Type.Object({
			dataset: Type.String({ maxLength: 40, description: "예: opt_bydd_trd, stk_bydd_trd" }),
			symbol: Type.Optional(Type.String({ maxLength: 40 })),
			date: Type.Optional(Type.String({ maxLength: 10, description: "YYYY-MM-DD" })),
			limit: Type.Optional(Type.Number({ minimum: 1, maximum: 500 })),
		}),
		async execute(_id, params) {
			const query = new URLSearchParams({ source: "krx", dataset: params.dataset });
			if (params.symbol) query.set("symbol", params.symbol);
			if (params.date) query.set("date", params.date);
			query.set("limit", String(params.limit ?? 50));
			const d = await apiGet(`/api/market/daily?${query}`);
			const lines = d.rows.map(
				(r: any) =>
					`  ${r.date} ${r.symbol} ${r.name}: ${r.close ?? "-"}${r.change_pct != null ? ` (${r.change_pct}%)` : ""}`,
			);
			return {
				content: [{ type: "text", text: `${params.dataset} ${d.count}행\n${lines.join("\n")}` }],
				details: d,
			};
		},
	});

	// 시장 심리 게이지
	pi.registerTool({
		name: "market_sentiment",
		label: "시장 심리 지수",
		description:
			"우리가 수집한 입력만으로 계산한 한국 시장 위험선호 점수(0~100)와 구성요소별 점수를 조회한다. 구성요소가 서로 어긋나는 지점이 가장 유용하다. 매수·매도 신호가 아니다.",
		parameters: Type.Object({}),
		async execute() {
			const d = await apiGet("/api/analysis/sentiment");
			if (d.status !== "ok") {
				return { content: [{ type: "text", text: `측정 불가: ${d.reason || "구성요소 부족"}` }], details: d };
			}
			const rows = (d.components || []).map(
				(c: any) => `  ${c.label} ${c.score} — ${c.detail}`,
			);
			const waiting = (d.pending || []).map((p: any) => `  (대기) ${p.key}: ${p.reason}`);
			const text =
				`시장 심리 ${d.score} / 100 — ${d.band_label}\n` +
				`구성요소 ${d.component_count}/${d.component_total} · 기준일 ${d.as_of}\n\n` +
				`${rows.join("\n")}\n` +
				(waiting.length ? `${waiting.join("\n")}\n` : "") +
				`\n${d.warning}`;
			return { content: [{ type: "text", text }], details: d };
		},
	});

	// 수집 완전성 — 결측 관측치와 복구 가능성
	pi.registerTool({
		name: "market_coverage",
		label: "수집 완전성",
		description:
			"결측된 관측일과 그 결측이 복구 가능한지 조회한다. 값이 없다는 사실을 시장 사실로 서술하기 전에 먼저 확인한다. 수집을 실행하지 않는다.",
		parameters: Type.Object({
			key: Type.Optional(Type.String({ maxLength: 60, description: "지표 키 또는 지수 심볼. 생략하면 전체 요약" })),
		}),
		async execute(_id, params) {
			if (params.key) {
				const rows = await apiGet("/api/coverage?detail=indicators");
				const found =
					rows.indicators.find((row: any) => row.key === params.key) ??
					(await apiGet("/api/coverage?detail=indices")).indices.find(
						(row: any) => row.symbol === params.key,
					);
				if (!found) {
					throw new Error(`알 수 없는 계열: ${params.key}`);
				}
				const gaps = found.gaps || {};
				const text =
					`${found.label || found.name} (${found.key || found.symbol})\n` +
					`관측 ${found.observations}개 · 최신 ${found.latest_date || "-"}` +
					(found.provider_session ? ` · 공급자 정산 ${found.provider_session}` : "") +
					`\n상태: ${found.tail} · 결측 근거: ${gaps.basis} · 결측 ${gaps.missing_count ?? 0}개` +
					(gaps.missing_sample?.length ? `\n예: ${gaps.missing_sample.slice(0, 6).join(", ")}` : "");
				return { content: [{ type: "text", text }], details: found };
			}
			const data = await apiGet("/api/coverage");
			const ind = data.indicators || {};
			const idx = data.indices || {};
			const text =
				`수집 완전성: ${data.status} (미해소 ${data.unresolved})\n` +
				`지표 ${ind.total}개 — 확정결측 ${ind.confirmed_gap_series} · 후보결측 ${ind.candidate_gap_series} · 판정불가 ${ind.unverifiable_series}\n` +
				`지수 ${idx.total}개 — 확정결측 ${idx.confirmed_gap_symbols}\n` +
				`핵심 계열 ${data.core.ready}/${data.core.total} 준비 (${data.core_ready_pct}%)\n` +
				"confirmed=공급자에 있는데 우리가 없음, candidate=주기상 기대되나 미발표 가능, unverifiable=공급자 세션 목록 미확보";
			return { content: [{ type: "text", text }], details: data };
		},
	});

	// 주식 시세 (관심 종목)
	pi.registerTool({
		name: "market_quotes",
		label: "주식 시세",
		description:
			"Yahoo에서 마지막으로 수집해 둔 관심 종목 시세와 수집 시각을 조회한다. 실시간 시세라고 가정하지 않는다.",
		parameters: Type.Object({
			category: Type.Optional(Type.String({ maxLength: 40, description: "관심 종목 카테고리 필터" })),
		}),
		async execute(_id, params) {
			const suffix = params.category ? `?category=${encodeURIComponent(params.category)}` : "";
			const data = await apiGet(`/api/quotes${suffix}`);
			const lines = data.quotes.map(
				(q: any) =>
					`${q.group_name} ${q.label} (${q.symbol}): ${q.price} ${q.change_pct != null ? q.change_pct.toFixed(2) + "%" : "-"}`,
			);
			return {
				content: [
					{
						type: "text",
						text:
							lines.length
								? `관심 종목 시세 (${data.count}개, 수집 ${data.as_of || "시각 미상"}):\n` + lines.join("\n")
								: "시세가 없습니다.",
					},
				],
				details: data,
			};
		},
	});

	// 글로벌 지수
	pi.registerTool({
		name: "market_indices",
		label: "글로벌 지수",
		description:
			"허용 목록의 글로벌 지수 최신 캐시를 조회한다. observation_date는 시장 관측일, updated_at은 조회·수집 시각이다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/indices");
			const lines = data.indices.map(
				(i: any) =>
					`${i.region} ${i.name}: ${i.value} ${i.change_pct != null ? i.change_pct.toFixed(2) + "%" : "-"}`,
			);
			return {
				content: [
					{
						type: "text",
							text: lines.length ? `글로벌 지수 (${data.count}개, 관측일 ${data.as_of || "혼재/미상"}):\n` + lines.join("\n") : "지수 데이터가 없습니다.",
					},
				],
				details: data,
			};
		},
	});

	// 경제 일정
	pi.registerTool({
		name: "market_events",
		label: "경제 일정",
		description:
			"다음 30일의 주요 경제 일정을 조회한다. FOMC, 금통위, ECB, BOJ, BOE 금리결정, 미국 CPI·고용 등.",
		parameters: Type.Object({
			days: Type.Optional(Type.Integer({ minimum: 1, maximum: 365, description: "조회할 일수 (기본 30)" })),
			country: Type.Optional(Type.String({ minLength: 2, maxLength: 2, description: "ISO 2자리 국가 필터 (예: KR, US)" })),
		}),
		async execute(_id, params) {
			const days = Math.max(1, Math.min(365, params.days || 30));
			const country = params.country ? `&country=${encodeURIComponent(params.country.toUpperCase())}` : "";
			const data = await apiGet(`/api/events?days=${days}${country}`);
			const lines = data.events.map(
				(e: any) => `${e.date} [${e.country}] ${e.title} (${e.impact})${e.note ? " — " + e.note : ""}`,
			);
			return {
				content: [
					{
						type: "text",
						text: lines.length ? `경제 일정 (${data.count}건):\n` + lines.join("\n") : "일정이 없습니다.",
					},
				],
				details: data,
			};
		},
	});

	// 지표 조회
	pi.registerTool({
		name: "market_indicator",
		label: "경제 지표",
		description:
			"정확한 지표 키의 캐시 시계열과 관측일·출처·빈도를 조회한다. 키를 모르면 market_indicator_list를 먼저 사용한다.",
		parameters: Type.Object({
			key: Type.String({ description: "지표 키 (예: kr_base_rate, us_cpi, gold, wti, kr_usd, eu_rate, jp_rate, kr_unemployment, us_house_price)" }),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000, description: "최근 관측 수 (기본 24)" })),
		}),
		async execute(_id, params) {
			const data = await apiGet(`/api/indicator/${params.key}?limit=${params.limit || 24}`);
			if (data.error) {
				return { content: [{ type: "text", text: data.error }] };
			}
			const pts = data.points;
			const last = pts.length ? pts[pts.length - 1] : null;
			const desc = data.desc ? `\n설명: ${data.desc}` : "";
			const text = last
				? `${data.label}: 현재 ${last.value} ${data.unit} (${last.date})\n출처: ${data.source === "fred" ? "FRED" : data.source === "ecos" ? "ECOS" : "Yahoo"}${desc}`
				: `${data.label}: 데이터 없음${desc}`;
			return { content: [{ type: "text", text }], details: data };
		},
	});

	// 지표 목록 (투자 보조 시 어떤 지표가 있는지)
	pi.registerTool({
		name: "market_indicator_list",
		label: "지표 목록",
		description: "조회 가능한 지표 키·설명·출처·빈도·최신 관측일을 찾는다. 키를 추측하지 말고 이 도구로 발견한다.",
		parameters: Type.Object({
			category: Type.Optional(Type.String({ description: "카테고리 필터 (금리, 환율, 물가, 상품 등)" })),
		}),
		async execute(_id, params) {
			const cat = params.category ? `?category=${encodeURIComponent(params.category)}` : "";
			const data = await apiGet(`/api/indicators${cat}`);
			const lines = data.items.map((i: any) => `${i.key} (${i.category}) — ${i.label}${i.desc ? ": " + i.desc : ""}`);
			return {
				content: [
					{
						type: "text",
						text: `지표 목록 (${data.count}개):\n` + lines.join("\n"),
					},
				],
				details: data,
			};
		},
	});

	// 공급자가 제공한 전체 종목 유니버스
	pi.registerTool({
		name: "market_universe",
		label: "수집 종목 유니버스",
		description:
			"KRX 등 공급자 데이터셋에서 자동 발견되어 SQLite에 저장된 전체 종목·지수·ETF/ETN 유니버스를 검색한다. 관심종목 allowlist와 다르다.",
		parameters: Type.Object({
			query: Type.Optional(Type.String({ maxLength: 80, description: "종목코드 또는 이름 부분 검색" })),
			source: Type.Optional(Type.String({ maxLength: 20, description: "공급자 (기본 krx)" })),
			dataset: Type.Optional(Type.String({ maxLength: 60, description: "데이터셋 필터" })),
			assetType: Type.Optional(Type.String({ maxLength: 40, description: "자산 유형 필터" })),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 5000, description: "최대 결과 수 (기본 100)" })),
		}),
		async execute(_id, params) {
			const query = new URLSearchParams({
				source: params.source || "krx",
				limit: String(params.limit || 100),
			});
			if (params.query) query.set("q", params.query);
			if (params.dataset) query.set("dataset", params.dataset);
			if (params.assetType) query.set("asset_type", params.assetType);
			const data = await apiGet(`/api/market/universe?${query}`);
			const lines = data.instruments.map(
				(item: any) => `${item.symbol} — ${item.name} [${item.asset_type}/${item.dataset}] (최근 ${item.last_seen})`,
			);
			const overview = data.overview || {};
			const text = lines.length
				? `저장 유니버스 ${overview.instruments ?? "?"}개 중 ${data.count}개:\n${lines.join("\n")}`
				: `조건에 맞는 저장 종목이 없습니다. KRX 수집 상태를 market_health로 확인하세요.`;
			return { content: [{ type: "text", text }], details: data };
		},
	});

	// 상관관계
	pi.registerTool({
		name: "market_correlation",
		label: "지수 상관관계",
		description:
			"두 지수 간 동시 상관과 시차 상관(lead-lag)을 조회한다. 시차 상관은 예측력이나 인과관계의 증거가 아니다. 지수 이름은 한글 (코스피, S&P 500, 닛케이, 항셍 등).",
		parameters: Type.Object({
			a: Type.String({ description: "첫 번째 지수 이름 (예: 코스피)" }),
			b: Type.String({ description: "두 번째 지수 이름 (예: S&P 500)" }),
			window: Type.Optional(Type.Integer({ minimum: 20, maximum: 252, description: "롤링 창 (기본 60 공통 거래일)" })),
			maxLag: Type.Optional(Type.Integer({ minimum: 0, maximum: 20, description: "최대 시차 (기본 10 공통 세션)" })),
		}),
		async execute(_id, params) {
			const window = params.window || 60;
			const maxLag = params.maxLag ?? 10;
			const rolling = await apiGet(
				`/api/correlation/rolling?a=${encodeURIComponent(params.a)}&b=${encodeURIComponent(params.b)}&window=${window}`,
			);
			const leadlag = await apiGet(
				`/api/correlation/leadlag?a=${encodeURIComponent(params.a)}&b=${encodeURIComponent(params.b)}&max_lag=${maxLag}`,
			);
			const lastRoll = rolling.values.length ? rolling.values[rolling.values.length - 1] : null;
			const text =
				`${params.a} × ${params.b} 상관분석:\n` +
				`- 최근 ${window} 공통 세션 롤링 상관 (${rolling.as_of || "관측일 미상"}): ${lastRoll ?? "N/A"}\n` +
				`- 가장 큰 절대 시차 상관: lag ${leadlag.best?.lag ?? "N/A"}, r=${leadlag.best?.correlation ?? "N/A"}, p≈${leadlag.best?.p_value ?? "N/A"}\n` +
				`- 주의: ${leadlag.interpretation}`;
			return { content: [{ type: "text", text }], details: { rolling, leadlag } };
		},
	});

	// 전이효과
	pi.registerTool({
		name: "market_spillover",
		label: "전이효과 네트워크",
		description:
			"전 세계 지수 수익률의 VAR 기반 일반화 예측오차 분산 연결성을 조회한다. 방향성 연결성은 인과관계의 증거가 아니다.",
		parameters: Type.Object({
			region: Type.Optional(Type.String({ description: "정확한 지역명 필터 (예: 미국, 한국)" })),
			maxlags: Type.Optional(Type.Integer({ minimum: 1, maximum: 10, description: "VAR 최대 시차 (기본 2)" })),
			horizon: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, description: "FEVD 예측 지평 (기본 10)" })),
		}),
		async execute(_id, params) {
			const query = new URLSearchParams({
				maxlags: String(params.maxlags || 2),
				horizon: String(params.horizon || 10),
			});
			if (params.region) query.set("region", params.region);
			const data = await apiGet(`/api/spillover?${query}`);
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const sorted = [...data.nodes].sort((a, b) => b.net - a.net);
			const lines = sorted.map(
				(n: any) => `${n.name}: net ${n.net.toFixed(3)} (to ${n.to.toFixed(3)} / from ${n.from.toFixed(3)})`,
			);
			return {
				content: [
					{
						type: "text",
						text:
							`총 연결성: ${(data.total_connectedness * 100).toFixed(1)}% (${data.start}~${data.end}, ${data.observations}개 공통 관측)\n` +
							`지수별 순 전이 (net):\n` +
							lines.join("\n") +
							`\n주의: ${data.causality_warning}`,
					},
				],
				details: data,
			};
		},
	});

	// 수익률 곡선
	pi.registerTool({
		name: "market_yield_curve",
		label: "수익률 곡선",
		description:
			"캐시된 공통 관측일의 장단기 금리차를 조회한다. 역전은 참고 신호이며 단독 경기침체 예측으로 해석하지 않는다.",
		parameters: Type.Object({
			country: Type.Optional(Type.Union([Type.Literal("us"), Type.Literal("kr")], { description: "us(기본) 또는 kr" })),
		}),
		async execute(_id, params) {
			const data = await apiGet(`/api/analysis/yield_curve?country=${params.country || "us"}`);
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const text =
				`${data.country} 수익률 곡선 (${data.date}, ${data.source}):\n` +
				`- 단기(${data.short}) / 장기(${data.long})\n` +
				`- 장단기 스프레드: ${data.spread}\n` +
				`- ${data.inverted ? "역전" : "정상 (우상향)"}\n` +
				`- 주의: ${data.warning}`;
			return { content: [{ type: "text", text }], details: data };
		},
	});

	// 추세·변동성 분석
	pi.registerTool({
		name: "market_index_analysis",
		label: "지수 추세·변동성 분석",
		description:
			"허용 목록 지수의 이동평균·변동성·최대 낙폭을 캐시 이력으로 기술한다. 결과를 매매 신호로 단정하지 않는다.",
		parameters: Type.Object({
			symbol: Type.String({ description: "지수 심볼 (예: ^KS11, ^GSPC)" }),
			years: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, description: "분석 이력 연수 (기본 2)" })),
		}),
		async execute(_id, params) {
			const data = await apiGet(`/api/analysis/trend?symbol=${encodeURIComponent(params.symbol)}&years=${params.years || 2}`);
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const lines: string[] = [];
			lines.push(`${data.name} (${data.symbol}) 현재: ${data.current} (${data.date})`);
			for (const s of data.signals) {
				lines.push(`- MA${s.window}: ${s.ma} (현재가 ${s.above ? "위" : "아래"}, ${s.pct_from_ma}%)`);
			}
			if (data.cross) lines.push(`- 크로스: ${data.cross === "golden" ? "골든 (상승 전환)" : "데드 (하락 전환)"}`);
			if (data.volatility && data.volatility.recent_vol_annualized) {
				lines.push(
					`- 변동성: 최근 ${data.volatility.recent_vol_annualized}% (전체 ${data.volatility.full_vol_annualized}%)` +
						` ${data.volatility.elevated ? "⚠️ 상승" : ""}`,
				);
			}
			if (data.max_drawdown && data.max_drawdown.max_drawdown_pct) {
				lines.push(`- 최대 낙폭: ${data.max_drawdown.max_drawdown_pct}%`);
			}
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});
	// 기술적 지표 (RSI, MACD, 볼린저)
	pi.registerTool({
		name: "market_technical",
		label: "기술적 지표",
		description:
			"허용 목록 지수의 RSI·MACD·볼린저 값을 캐시 이력으로 기술한다. 후행 통계이며 지지·저항 또는 매매 신호가 아니다.",
		parameters: Type.Object({
			symbol: Type.String({ description: "지수 심볼 (예: ^KS11)" }),
			years: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, description: "분석 이력 연수 (기본 2)" })),
		}),
		async execute(_id, params) {
			const data = await apiGet(`/api/analysis/technical?symbol=${encodeURIComponent(params.symbol)}&years=${params.years || 2}`);
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const lines: string[] = [];
			lines.push(`${data.name} (${data.symbol}) 기술적 지표 (${data.as_of || "관측일 미상"}):`);
			if (data.rsi != null) {
				const state = data.rsi > 70 ? "과매수" : data.rsi < 30 ? "과매도" : "중립";
				lines.push(`- RSI: ${data.rsi} (${state})`);
			}
			if (data.macd) {
				lines.push(`- MACD: ${data.macd.macd} / signal ${data.macd.signal} (${data.macd.bullish ? "상승 모멘텀" : "하락 모멘텀"})`);
			}
			if (data.bollinger) {
				lines.push(`- 볼린저 %B: ${data.bollinger.pct_b} (상단 ${data.bollinger.upper} / 하단 ${data.bollinger.lower})`);
			}
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});

	// 위험 지표 (샤프, VaR, 낙폭)
	pi.registerTool({
		name: "market_risk",
		label: "위험 지표",
		description:
			"허용 목록 지수의 샤프·과거 1일 VaR/ES·최대 낙폭을 조회한다. VaR를 최대 예상 손실로 표현하지 않는다.",
		parameters: Type.Object({
			symbol: Type.String({ description: "지수 심볼 (예: ^KS11)" }),
			years: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, description: "분석 이력 연수 (기본 2)" })),
		}),
		async execute(_id, params) {
			const data = await apiGet(`/api/analysis/risk?symbol=${encodeURIComponent(params.symbol)}&years=${params.years || 2}`);
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const lines: string[] = [];
			lines.push(`${data.name} (${data.symbol}) 위험 지표 (${data.as_of || "관측일 미상"}):`);
			if (data.sharpe) lines.push(`- 샤프 비율: ${data.sharpe.sharpe_annualized} (수익 ${data.sharpe.annual_return_pct}% / 변동성 ${data.sharpe.annual_vol_pct}%)`);
			if (data.var) lines.push(`- 과거 VaR (${data.var.confidence * 100}%): 1일 손실 분위수 ${data.var.var_1day_pct}% / ES ${data.var.expected_shortfall_1day_pct}%`);
			if (data.max_drawdown) lines.push(`- 최대 낙폭: ${data.max_drawdown.max_drawdown_pct}%`);
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});

	// 브리핑
	pi.registerTool({
		name: "market_brief",
		label: "브리핑",
		description:
			"이번 주 자기 분포에서 이동한 계열, 지금 실제로 어긋나는 판정들, 그리고 어느 구성요소가 바뀌면 국면 판정이 달라지는지를 조회한다. 뒤집기 조건은 이미 던져진 표에 대한 산술이며 예측도 권유도 아니다.",
		parameters: Type.Object({}),
		async execute() {
			const d = await apiGet("/api/brief");
			if (d.error) return { content: [{ type: "text", text: d.error }] };
			const kr = d.korea_regime || {}, us = d.regime || {}, g = d.sentiment || {};
			const lines: string[] = [
				`한국 ${kr.regime} (순점수 ${kr.score}, ${kr.component_count}/${kr.component_total}) · 미국 ${us.regime} (순점수 ${us.score}) · 심리 ${g.score} ${g.band_label}`,
			];
			if ((d.disagreements || []).length) {
				lines.push("", "어긋나는 것:");
				d.disagreements.forEach((x: any) => lines.push(`  [${x.kind}] ${x.title} — ${x.detail}`));
			}
			if ((d.flip_conditions || []).length) {
				lines.push("", "무엇이 바뀌면 판정이 달라지나:");
				d.flip_conditions.forEach((f: any) => lines.push(
					f.unreachable ? `  ${f.detail}`
						: `  ${f.label}: ${f.from_score} → ${f.to_score} 이면 ${f.verdict}` +
						  (f.percentile_gap != null ? ` (백분위 ${f.percentile} → ${Math.round(f.percentile + f.percentile_gap)})` : "")));
			}
			if ((d.movers || []).length) {
				lines.push("", `최근 ${d.lookback_days}일 분포 이동:`);
				d.movers.forEach((m: any) => lines.push(
					`  ${m.label} ${m.then}→${m.now} (${m.change > 0 ? "+" : ""}${m.change}p, ${m.from_date}~${m.as_of})` +
					((m.watch || []).length ? ` · 함께 볼 것: ${m.watch.map((w: any) => w.label).join(", ")}` : "")));
			}
			if ((d.unresolved || []).length) {
				lines.push("", "투표하지 않은 근거:");
				d.unresolved.forEach((u: any) => lines.push(`  [${u.source}] ${u.label} — ${u.reason}`));
			}
			lines.push("", `주의: ${d.warning}`);
			return { content: [{ type: "text", text: lines.join("\n") }], details: d };
		},
	});

	// 시장 상태 분류
	pi.registerTool({
		name: "market_regime",
		label: "시장 상태 분류",
		description:
			"두 개의 규칙형 국면 분류를 함께 조회한다. 미국은 VIX·신용 스프레드·S&P 200일선의 임계값 규칙이고, 한국은 VKOSPI·회사채 스프레드·CP−CD·코스피 추세·낙폭을 각자의 분포 백분위로 채점한다. 객관적 현재 상태나 투자 판단이 아니다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/analysis/regime");
			if (data.error) return { content: [{ type: "text", text: data.error }] };
			const name = (v: string) => v === "risk_on" ? "위험 선호 (Risk-on)" : v === "risk_off" ? "위험 회피 (Risk-off)" : v === "neutral" ? "중립" : "알 수 없음";
			const kr = data.korea_regime || {};
			const krLines = (kr.components || []).map((c: any) => `  ${c.label}: ${c.score > 0 ? "위험 선호" : c.score < 0 ? "위험 회피" : "중립"}${c.percentile != null ? ` (${c.percentile}점)` : ""} — ${c.detail}`);
			const krPending = (kr.pending || []).map((p: any) => `  ${p.label}: 제외 — ${p.reason}`);
			return {
				content: [
					{
						type: "text",
						text:
							`한국 국면: ${name(kr.regime)} (순점수 ${kr.score}, 구성요소 ${kr.component_count}/${kr.component_total}, 관측일 ${kr.as_of || "-"})\n` +
							krLines.join("\n") + (krPending.length ? `\n${krPending.join("\n")}` : "") +
							`\n\n미국 국면: ${name(data.regime)} (점수 ${data.score})\n관측일: ${JSON.stringify(data.as_of)}\n` +
							data.reasons.map((x: string) => `  ${x}`).join("\n") +
							`\n\n산식: ${kr.method_note || "-"}\n` +
							`두 분류기는 서로 다른 시장의 입력을 쓴다. 미국 국면을 한국 자산 판단에 그대로 옮기지 말 것.\n주의: ${data.warning}`,
					},
				],
				details: data,
			};
		},
	});

	// 파생 거시·교차자산 지표
	pi.registerTool({
		name: "market_derived_metrics",
		label: "파생 시장 지표",
		description:
			"캐시된 원천 계열을 날짜 정렬해 한국 신용·실질금리, 미국 순유동성, 물가 증감, 교차자산 상대강도를 계산한다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/analysis/derived");
			const macro = data.macro || {};
			const lines = [
				`한국 3년 신용스프레드: ${macro.kr_credit_spread_3y?.value ?? "N/A"}%p`,
				`한국 실질 정책금리: ${macro.kr_real_policy_rate?.value_pct ?? "N/A"}%`,
				`미국 순유동성 proxy: ${macro.us_net_liquidity?.net_liquidity_million_usd ?? "N/A"} 백만달러`,
				`미국 NFP 월간 증감: ${macro.us_nfp_monthly_change?.change_thousand ?? "N/A"} 천명`,
			];
			if (data.missing_inputs?.length) lines.push(`미수집 입력: ${data.missing_inputs.join(", ")}`);
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});

	// 다섯 층별 근거
	pi.registerTool({
		name: "market_layers",
		label: "층별 근거",
		description:
			"정책·경기·유동성·신용·시장폭 다섯 층으로 카탈로그를 다시 잘라 각 층이 " +
			"무엇을 말하는지와 그 판정이 몇 개의 근거 위에 서 있는지를 보고한다. " +
			"정책 층은 판정하지 않는다 — 금리·물가 계열이 상승의 의미를 선언하지 " +
			"않았고, 금리 상승이 긴축인지 경기 회복인지는 백분위가 답할 수 없기 " +
			"때문이다. 그 침묵을 중립으로 읽지 말 것.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/layers");
			const lines = data.layers.map((layer: any) =>
				`${layer.label}: ${layer.stance}${layer.mode === "stance" ? ` (순점수 ${layer.score})` : ""}` +
				` · 신뢰도 ${layer.confidence.level} (${layer.confidence.voted}/${layer.confidence.expected}` +
				`${layer.confidence.stale ? `, 지연 ${layer.confidence.stale}` : ""})` +
				`${layer.split ? " · 내부 갈림" : ""}\n  ${layer.reading}`,
			);
			return {
				content: [{ type: "text", text: [data.agreement, ...lines].join("\n") }],
				details: data,
			};
		},
	});

	// 신호 검증
	pi.registerTool({
		name: "market_backtest",
		label: "신호 검증",
		description:
			"국면 판정이 2023-12-18~2026-08-28 655거래일 동안 실제로 무슨 정보를 " +
			"담았는지. precision 보다 lift 를 먼저 볼 것 — lift 는 정밀도에서 " +
			"기저율을 뺀 값이고, 0 이하면 그 판정은 정보를 담지 않았다는 뜻이다. " +
			"limits 는 결과와 함께 인용해야 한다: 선견 누출은 통제, 개정 누출은 " +
			"미통제, 임계값은 평가 대상이지 조정 대상이 아니다.",
		parameters: Type.Object({
			market: Type.Optional(
				Type.String({ description: "korea (기본) 또는 us" }),
			),
		}),
		async execute({ market }: { market?: string }) {
			const data = await apiGet(
				`/api/backtest${market ? `?market=${encodeURIComponent(market)}` : ""}`,
			);
			if (!data.available) {
				return { content: [{ type: "text", text: data.reason }], details: data };
			}
			const c = data.contingency;
			const lines = [
				`${data.benchmark} ${data.window.start}~${data.window.end} (${data.window.days}거래일) · ` +
					`사건 정의 ${data.declared.drawdown_pct}%/${data.declared.horizon_days}거래일`,
				`적중 ${c.hit} · 오탐 ${c.false_alarm} · 미탐 ${c.miss} · 정상기각 ${c.correct_rejection}`,
				`정밀도 ${c.precision ?? "—"}% · 기저율 ${c.base_rate}% · lift ${c.lift ?? "—"}`,
				...Object.entries(data.conditional).map(
					([name, d]: [string, any]) =>
						`  ${name}: n=${d.forward_return.count} 전방수익 중앙 ${d.forward_return.median}% ` +
						`낙폭 중앙 ${d.forward_drawdown.median}%`,
				),
				`churn 전환 ${data.churn.changes}회 · 지속 중앙값 ${data.churn.median_run_days}일`,
				...data.limits.map((l: string) => `· ${l}`),
			];
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});

	// 과거 시점 재생
	pi.registerTool({
		name: "market_replay",
		label: "과거 시점 재생",
		description:
			"그날의 데이터만으로 같은 판정 코드를 돌린다. observed 는 관측일만 " +
			"잘라 선견 누출을 막고 모든 표가 지원한다. vintage 는 그 순간까지 실제로 " +
			"받은 값만 써서 개정 누출까지 잡지만 빈티지 원장(2026-08-23 시작)만 " +
			"지원한다. vintage 판정을 인용하기 전에 market_replay_readiness 를 " +
			"먼저 볼 것 — 원장이 얇으면 구성요소가 판정하지 않고 보류로 남는다.",
		parameters: Type.Object({
			date: Type.String({ description: "재생할 날짜 (YYYY-MM-DD, KST)" }),
			mode: Type.Optional(
				Type.String({ description: "observed (기본) 또는 vintage" }),
			),
		}),
		async execute({ date, mode }: { date: string; mode?: string }) {
			const data = await apiGet(
				`/api/replay?date=${encodeURIComponent(date)}` +
					(mode ? `&mode=${encodeURIComponent(mode)}` : ""),
			);
			const text = [
				`${data.as_of} (${data.mode}) — 한국 ${data.korea_regime.regime} ` +
					`(순점수 ${data.korea_regime.score}, ${data.korea_regime.component_count}/${data.korea_regime.component_total})` +
					` · 미국 ${data.regime.regime} (순점수 ${data.regime.score})`,
				data.coverage
					? `빈티지 커버리지 ${data.coverage.usable}/${data.coverage.requested}` +
						(data.coverage.complete ? "" : " — 부분 재생이다")
					: "",
				data.warning,
			].filter(Boolean);
			return { content: [{ type: "text", text: text.join("\n") }], details: data };
		},
	});

	pi.registerTool({
		name: "market_replay_readiness",
		label: "재생 준비도",
		description:
			"계열마다 언제부터 재생 가능한지. 추정이 아니라 원장에 이미 적힌 " +
			"사실이다 — N번째 관측이 도착한 시각이 곧 깊이가 채워진 시각이고, " +
			"사후 백필은 그 이후의 모든 날짜를 재생 가능하게 만든다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/replay/readiness");
			const lines = data.series.map(
				(s: any) => `${s.key}: 관측 ${s.observations}/${s.minimum} — ${s.note}`,
			);
			const head =
				`원장 ${data.ledger_began} 시작 · 오늘 재생 가능 ${data.usable}/${data.requested}` +
				(data.complete_from
					? ` · 전체 재생 ${data.complete_from}부터`
					: ` · 전체 재생 불가 (대기 ${data.waiting.length}개)`);
			return {
				content: [{ type: "text", text: [head, ...lines].join("\n") }],
				details: data,
			};
		},
	});

	// KRX 시장폭
	pi.registerTool({
		name: "market_breadth",
		label: "KRX 시장폭",
		description:
			"캐시된 KRX 전 종목 일봉에서 KOSPI·KOSDAQ 등락종목, 거래대금, 집중도, 20일 시장폭을 계산한다.",
		parameters: Type.Object({}),
		async execute() {
			const data = await apiGet("/api/analysis/krx-breadth");
			const lines = data.markets.map((market: any) =>
				market.status === "ok"
					? `${market.market} (${market.as_of}): 상승 ${market.advances} / 하락 ${market.declines}, A/D ${market.advance_decline_ratio}, 상위10 시총 ${market.top10_market_cap_pct}%`
					: `${market.market}: ${market.reason}`,
			);
			return { content: [{ type: "text", text: lines.join("\n") }], details: data };
		},
	});

}
