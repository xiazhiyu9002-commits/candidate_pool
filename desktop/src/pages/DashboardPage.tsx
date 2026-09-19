import { useRef, type RefObject } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { DashboardByJd, DashboardFilters, DashboardOverview, DashboardTrendItem, DailyFollowupToday, JdListItem } from "../App";
import { Button } from "../components/ui";

function exportSvgAsPng(svg: SVGSVGElement, filename: string) {
  const width = svg.width.baseVal.value || svg.viewBox.baseVal.width || 800;
  const height = svg.height.baseVal.value || svg.viewBox.baseVal.height || 400;
  const scale = 3; // 3 倍分辨率导出，保证文字清晰
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", String(width * scale));
  clone.setAttribute("height", String(height * scale));
  const xml = new XMLSerializer().serializeToString(clone);
  const svgBlob = new Blob([xml], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(svgBlob);
  const image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = width * scale;
    canvas.height = height * scale;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      URL.revokeObjectURL(url);
      return;
    }
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);
    canvas.toBlob(async (blob) => {
      if (!blob) return;
      try {
        const buffer = await blob.arrayBuffer();
        const content = Array.from(new Uint8Array(buffer));
        await invoke("save_file", { filename, content });
      } catch {
        // 浏览器/无 Tauri 环境：回退到 <a download> 原生下载。
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    }, "image/png");
  };
  image.onerror = () => URL.revokeObjectURL(url);
  image.src = url;
}

function renderTrendChart(data: DashboardTrendItem[], ref: RefObject<SVGSVGElement | null>, granularity: string) {
  const width = 720;
  const height = 320;
  const padLeft = 48;
  const padBottom = 64;
  const padTop = 22;
  const padRight = 16;
  const chartWidth = width - padLeft - padRight;
  const chartHeight = height - padTop - padBottom;
  const maxValue = Math.max(1, ...data.map((d) => Math.max(d.recommendation, d.offer)));
  const groupWidth = data.length ? chartWidth / data.length : chartWidth;
  const barWidth = Math.min(40, Math.max(8, groupWidth * 0.3));
  const labelStep = Math.max(1, Math.ceil(data.length / 12));

  return (
    <svg ref={ref} className="chart-svg" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="推荐量与 offer 量趋势">
      {data.map((d, i) => {
        const cx = padLeft + groupWidth * i + groupWidth / 2;
        const baseline = padTop + chartHeight;
        const recHeight = (d.recommendation / maxValue) * chartHeight;
        const offerHeight = (d.offer / maxValue) * chartHeight;
        return (
          <g key={d.period}>
            <rect x={cx - barWidth - 2} y={baseline - recHeight} width={barWidth} height={recHeight} rx="3" fill="#3b82f6" />
            <rect x={cx + 2} y={baseline - offerHeight} width={barWidth} height={offerHeight} rx="3" fill="#10b981" />
            <text x={cx - barWidth / 2 - 2} y={baseline - recHeight - 4} textAnchor="middle" fontSize="9" fill="#3b82f6">{d.recommendation}</text>
            <text x={cx + barWidth / 2 + 2} y={baseline - offerHeight - 4} textAnchor="middle" fontSize="9" fill="#10b981">{d.offer}</text>
            {i % labelStep === 0 && (
              <text x={cx} y={baseline + 18} textAnchor="end" fontSize="10" fill="#64748b" transform={`rotate(-40 ${cx} ${baseline + 18})`}>
                {granularity === "week" ? d.period.replace(/^\d{4}-/, "") : d.period}
              </text>
            )}
          </g>
        );
      })}
      <line x1={padLeft} y1={padTop} x2={padLeft} y2={padTop + chartHeight} stroke="#cbd5e1" />
      <line x1={padLeft} y1={padTop + chartHeight} x2={width - padRight} y2={padTop + chartHeight} stroke="#cbd5e1" />
      <text x={padLeft - 6} y={padTop + 4} textAnchor="end" fontSize="10" fill="#64748b">{maxValue}</text>
      <text x={padLeft - 6} y={padTop + chartHeight} textAnchor="end" fontSize="10" fill="#64748b">0</text>
      <g transform={`translate(${padLeft + 8}, ${height - 16})`}>
        <rect width="10" height="10" fill="#3b82f6" />
        <text x="14" y="9" fontSize="10" fill="#334155">推荐量</text>
        <rect x="64" width="10" height="10" fill="#10b981" />
        <text x="78" y="9" fontSize="10" fill="#334155">offer 量</text>
      </g>
    </svg>
  );
}

function renderPassRateChart(data: DashboardByJd[], ref: RefObject<SVGSVGElement | null>) {
  const labelWidth = 150;
  const barWidth = 360;
  const valueWidth = 70;
  const rowHeight = 24;
  const titleHeight = 22;
  const groupGap = 20;
  const width = labelWidth + barWidth + valueWidth + 24;

  const groups = data
    .filter((jd) => jd.rounds.length > 0)
    .map((jd) => ({
      title: `${jd.company} · ${jd.title}`,
      rows: [
        ...jd.rounds.map((r) => ({ label: r.round_name, value: r.pass_rate, highlight: false })),
        { label: "最终 offer 率", value: jd.final_offer_rate, highlight: true },
      ],
    }));

  let height = 12;
  const layouts = groups.map((g) => {
    const start = height;
    height += titleHeight + g.rows.length * rowHeight + groupGap;
    return { ...g, start };
  });
  height = Math.max(height, 40);

  return (
    <svg ref={ref} className="chart-svg" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="每岗位每轮通过率">
      {layouts.map((g, gi) => (
        <g key={gi}>
          <text x={8} y={g.start + 14} fontSize="12" fontWeight="bold" fill="#0f172a">{g.title}</text>
          {g.rows.map((row, ri) => {
            const ry = g.start + titleHeight + ri * rowHeight;
            const pct = Math.min(1, Math.max(0, row.value ?? 0));
            return (
              <g key={ri}>
                <text x={8} y={ry + 16} fontSize="11" fill="#334155">{row.label}</text>
                <rect x={labelWidth} y={ry + 4} width={barWidth} height={16} rx={3} fill="#e2e8f0" />
                <rect x={labelWidth} y={ry + 4} width={pct * barWidth} height={16} rx={3} fill={row.highlight ? "#f59e0b" : "#6366f1"} />
                <text x={labelWidth + barWidth + 8} y={ry + 16} fontSize="11" fill="#334155">
                  {row.value === null ? "—" : `${(row.value * 100).toFixed(0)}%`}
                </text>
              </g>
            );
          })}
        </g>
      ))}
    </svg>
  );
}

export interface DashboardPageProps {
  dashboard: DashboardOverview | null;
  dashboardByJdData: DashboardByJd[];
  dashboardTrend: DashboardTrendItem[];
  trendGranularity: string;
  dashboardDraft: DashboardFilters;
  dashboardBusy: boolean;
  dailyFollowup: DailyFollowupToday | null;
  jds: JdListItem[];
  onDashboardDraftChange: (draft: DashboardFilters) => void;
  onApplyFilters: (filters: DashboardFilters) => void;
  onReload: () => void;
  onReloadTrend: (granularity: string) => void;
  onExportExcel: () => void;
}

export function DashboardPage({
  dashboard,
  dashboardByJdData,
  dashboardTrend,
  trendGranularity,
  dashboardDraft,
  dashboardBusy,
  dailyFollowup,
  jds,
  onDashboardDraftChange,
  onApplyFilters,
  onReload,
  onReloadTrend,
  onExportExcel,
}: DashboardPageProps) {
  const trendSvgRef = useRef<SVGSVGElement | null>(null);
  const passRateSvgRef = useRef<SVGSVGElement | null>(null);

  return (
    <section className="jd-panel">
      {dailyFollowup && (
        <div className="results-card followup-card">
          <div className="section-heading">
            <h2>今日待办</h2>
            <span>追反馈与待面试按上海时间汇总</span>
          </div>
          <div className="followup-grid">
            <div className="followup-col">
              <h3>追反馈 <span className="followup-count">{dailyFollowup.followup.length}</span></h3>
              {dailyFollowup.followup.length === 0 ? (
                <p className="followup-empty">暂无待反馈</p>
              ) : (
                <ul className="followup-list">
                  {dailyFollowup.followup.map((item, index) => (
                    <li key={`f-${index}`}>{item.name} · {item.company} · {item.title}<span className="followup-time">{item.date}</span></li>
                  ))}
                </ul>
              )}
            </div>
            <div className="followup-col">
              <h3>待面试 <span className="followup-count">{dailyFollowup.interview.length}</span></h3>
              {dailyFollowup.interview.length === 0 ? (
                <p className="followup-empty">暂无待面试</p>
              ) : (
                <ul className="followup-list">
                  {dailyFollowup.interview.map((item, index) => (
                    <li key={`i-${index}`}>{item.name} · {item.company} · {item.title}<span className="followup-time">{item.time}</span></li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
      <form className="case-filter-bar dashboard-filters" onSubmit={(event) => { event.preventDefault(); onApplyFilters(dashboardDraft); }}>
        <label className="case-filter-field"><span>公司</span><select aria-label="看板公司" value={dashboardDraft.company || ""} onChange={(event) => onDashboardDraftChange({ ...dashboardDraft, company: event.target.value || undefined, jd_id: undefined })}>
          <option value="">全部公司</option>
          {[...new Set(jds.map((jd) => jd.company))].sort().map((company) => <option key={company}>{company}</option>)}
        </select></label>
        <label className="case-filter-field"><span>岗位</span><select aria-label="看板岗位" value={dashboardDraft.jd_id || ""} onChange={(event) => onDashboardDraftChange({ ...dashboardDraft, jd_id: event.target.value || undefined })}>
          <option value="">全部岗位</option>
          {jds.filter((jd) => !dashboardDraft.company || jd.company === dashboardDraft.company).map((jd) => <option key={jd.jd_id} value={jd.jd_id}>{jd.company} · {jd.title}</option>)}
        </select></label>
        <label className="case-filter-field"><span>开始日期</span><input aria-label="开始日期" type="date" value={dashboardDraft.date_from || ""} onChange={(event) => onDashboardDraftChange({ ...dashboardDraft, date_from: event.target.value || undefined })} /></label>
        <label className="case-filter-field"><span>结束日期</span><input aria-label="结束日期" type="date" value={dashboardDraft.date_to || ""} onChange={(event) => onDashboardDraftChange({ ...dashboardDraft, date_to: event.target.value || undefined })} /></label>
        <Button type="submit" variant="primary" disabled={dashboardBusy}>应用筛选</Button>
        <Button type="button" variant="secondary" disabled={dashboardBusy} onClick={onReload}>刷新看板</Button>
        <Button type="button" variant="ghost" disabled={dashboardBusy || !dashboard} onClick={onExportExcel}>导出 Excel</Button>
      </form>
      <p className="dashboard-hint">统计按上海自然日；导出和趋势使用最近一次已应用的筛选。{dashboardBusy ? "正在加载…" : ""}</p>
      {dashboard ? (
        <>
          <div className="health-grid">
            <div className="health-card"><small>候选人总数</small><strong>{dashboard.candidate_total}</strong></div>
            <div className="health-card"><small>每月新增候选人</small><strong>{dashboard.monthly_new_candidates.length ? dashboard.monthly_new_candidates[dashboard.monthly_new_candidates.length - 1].count : 0}</strong></div>
            <div className="health-card"><small>推荐总数</small><strong>{dashboard.recommendation_total}</strong></div>
            <div className="health-card"><small>offer 总数</small><strong>{dashboard.offer_total}</strong></div>
            <div className="health-card"><small>当前有效 offer</small><strong>{dashboard.active_offer_total}</strong></div>
            <div className="health-card"><small>已入职人数</small><strong>{dashboard.onboarded_total}</strong></div>
          </div>

          <div className="results-card chart-card">
            <div className="section-heading">
              <h2>推荐量 / offer 量趋势</h2>
              <div className="case-actions">
                <div className="trend-toggle" role="group" aria-label="趋势粒度">
                  {(["week", "month", "quarter"] as const).map((granularity) => (
                    <button
                      key={granularity}
                      disabled={dashboardBusy}
                      className={trendGranularity === granularity ? "trend-toggle-btn active" : "trend-toggle-btn"}
                      onClick={() => onReloadTrend(granularity)}
                    >
                      {granularity === "week" ? "周" : granularity === "month" ? "月" : "季度"}
                    </button>
                  ))}
                </div>
                <Button variant="ghost" size="sm" onClick={() => trendSvgRef.current && exportSvgAsPng(trendSvgRef.current, "趋势图.png")}>导出图片</Button>
              </div>
            </div>
            <div className="chart-box">
              {renderTrendChart(dashboardTrend, trendSvgRef, trendGranularity)}
            </div>
          </div>

          <div className="results-card chart-card">
            <div className="section-heading">
              <h2>每岗位每轮通过率</h2>
              <Button variant="ghost" size="sm" onClick={() => passRateSvgRef.current && exportSvgAsPng(passRateSvgRef.current, "每轮通过率.png")}>导出图片</Button>
            </div>
            <div className="chart-box">
              {renderPassRateChart(dashboardByJdData, passRateSvgRef)}
            </div>
            {dashboardByJdData.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>岗位</th>
                    <th>轮次</th>
                    <th>进入</th>
                    <th>通过</th>
                    <th>未通过</th>
                    <th>待反馈</th>
                    <th>通过率</th>
                    <th>最终 offer 率</th>
                  </tr>
                </thead>
                <tbody>
                  {dashboardByJdData.flatMap((jd) => {
                    if (jd.rounds.length === 0) {
                      return (
                        <tr key={jd.jd_id}>
                          <td>{jd.company} · {jd.title}</td>
                          <td colSpan={7}>暂无面试数据</td>
                        </tr>
                      );
                    }
                    return jd.rounds.map((round, index) => (
                      <tr key={`${jd.jd_id}-${round.round_key ?? `${round.round_no}-${index}`}`}>
                        {index === 0 && <td rowSpan={jd.rounds.length}>{jd.company} · {jd.title}</td>}
                        <td>{round.round_name}</td>
                        <td>{round.entered}</td>
                        <td>{round.passed}</td>
                        <td>{round.failed}</td>
                        <td>{round.pending}</td>
                        <td>{round.pass_rate === null ? "—" : `${(round.pass_rate * 100).toFixed(1)}%`}</td>
                        {index === 0 && <td rowSpan={jd.rounds.length}>{jd.final_offer_rate === null ? "—" : `${(jd.final_offer_rate * 100).toFixed(1)}%`}</td>}
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            )}
          </div>
        </>
      ) : (
        <div className="empty-state"><strong>加载看板数据</strong><p>点击「刷新看板」查看推荐统计与面试漏斗。</p></div>
      )}
    </section>
  );
}
