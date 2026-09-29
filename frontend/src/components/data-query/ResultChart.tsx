import { useState } from "react";

import type { ChartSuggestion, QueryResult, ValueFormat } from "@/lib/api/agent-data-query";
import { Notice } from "@/components/ui/Notice";

import { formatAxisValue, formatCategoryLabel, formatValue, toFiniteNumber } from "./format";
import { getChartFieldLabel } from "./chart-labels";
import styles from "./data-query.module.css";

const WIDTH = 720;
const HEIGHT = 300;
const PAD = { top: 16, right: 24, bottom: 40, left: 80 };
const Y_TICK_COUNT = 4;
const MAX_X_LABELS = 12;
const SERIES_COLORS = ["var(--primary)", "#d97706", "#0f766e", "#7c3aed"];

type Props = { result: QueryResult; suggestion: ChartSuggestion | null };
type Point = { label: string; value: number };
type ChartSeries = { field: string; points: Point[] };
type ChartMode = "line" | "bar";

/** 图表是智能问数的主输出；字段或数值不满足约束时安全降级到明细表。 */
export function ResultChart({ result, suggestion }: Props) {
  const [override, setOverride] = useState<{ key: string; type: ChartMode } | null>(null);
  const suggestionKey = `${suggestion?.title ?? ""}:${suggestion?.chart_type ?? ""}:${suggestion?.x_field ?? ""}`;
  const overrideType = override?.key === suggestionKey ? override.type : null;

  const decision = evaluate(result, suggestion, overrideType);
  if (decision.kind === "notice") {
    return (
      <div>
        <Notice tone="neutral" tag="提示">{decision.text}</Notice>
        {suggestion ? <p className={styles.chartReason}>图表建议依据：{suggestion.reason}</p> : null}
      </div>
    );
  }

  const canSwitch = suggestion?.chart_type === "line" || suggestion?.chart_type === "bar";
  return (
    <div>
      <div className={styles.chartHeader}>
        <div>
          <p className={styles.chartTitle}>{suggestion?.title}</p>
          <p className={styles.chartReason}>图表建议依据：{suggestion?.reason}</p>
        </div>
        {canSwitch ? (
          <div className={styles.chartControls} aria-label="切换图表类型">
            {(["line", "bar"] as const).map((type) => (
              <button
                key={type}
                type="button"
                className={styles.chartControl}
                data-active={decision.type === type}
                onClick={() => setOverride({ key: suggestionKey, type })}
              >
                {type === "line" ? "折线" : "柱状"}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      {decision.type === "line" ? (
        <LineChart series={decision.series} format={suggestion?.value_format ?? null} />
      ) : (
        <BarChart series={decision.series} format={suggestion?.value_format ?? null} />
      )}
    </div>
  );
}

type Decision =
  | { kind: "chart"; type: ChartMode; series: ChartSeries[] }
  | { kind: "notice"; text: string };

function evaluate(result: QueryResult, suggestion: ChartSuggestion | null, overrideType: ChartMode | null): Decision {
  if (!suggestion) return { kind: "notice", text: "没有收到图表建议，已改为只显示下方明细表。" };
  if (suggestion.chart_type === "none") return { kind: "notice", text: "本次查询没有可绘制的数据。" };
  if (suggestion.chart_type === "table") return { kind: "notice", text: "当前结果字段不满足受控图表规则，建议查看下方明细表。" };

  const xField = suggestion.x_field;
  const fields = Array.from(new Set(
    suggestion.y_fields?.length ? suggestion.y_fields : suggestion.y_field ? [suggestion.y_field] : [],
  ));
  if (!xField || fields.length === 0) {
    return { kind: "notice", text: "图表建议没有指明维度或度量字段，已改为只显示下方明细表。" };
  }
  if (!result.columns.includes(xField) || fields.some((field) => !result.columns.includes(field))) {
    return { kind: "notice", text: "图表建议引用的字段不在本次查询结果中，已改为只显示下方明细表。" };
  }
  if (result.rows.length === 0) return { kind: "notice", text: "本次查询没有返回数据行。" };

  const series = fields.map((field) => ({
    field,
    points: result.rows.map((row) => ({
      label: formatCategoryLabel(row[xField]),
      value: toFiniteNumber(row[field]) ?? Number.NaN,
    })),
  }));
  if (series.some((item) => item.points.some((point) => !Number.isFinite(point.value)))) {
    return { kind: "notice", text: "结果中含有无法作为数值绘制的行，已改为只显示下方明细表。" };
  }

  const type = overrideType ?? suggestion.chart_type;
  if (type === "bar" && series.some((item) => item.points.some((point) => point.value < 0))) {
    return { kind: "notice", text: "结果中含有负值，当前柱状图不支持，已改为只显示下方明细表。" };
  }
  return { kind: "chart", type, series };
}

function Legend({ series, format }: { series: ChartSeries[]; format: ValueFormat | null }) {
  return (
    <div className={styles.chartLegend} aria-label="图例">
      {series.map((item, index) => (
        <span key={item.field} className={styles.legendItem}>
          <span className={styles.legendDot} style={{ backgroundColor: SERIES_COLORS[index % SERIES_COLORS.length] }} />
          {getChartFieldLabel(item.field)}
          {item.points.length === 1 ? ` · ${formatValue(item.points[0].value, format)}` : null}
        </span>
      ))}
    </div>
  );
}

function LineChart({ series, format }: { series: ChartSeries[]; format: ValueFormat | null }) {
  const values = series.flatMap((item) => item.points.map((point) => point.value));
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const span = rawMax - rawMin;
  const breathing = span === 0 ? Math.max(Math.abs(rawMax) * 0.1, 1) : span * 0.08;
  const min = rawMin - breathing;
  const max = rawMax + breathing;
  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const pointCount = series[0]?.points.length ?? 0;
  const xAt = (index: number) => pointCount === 1 ? PAD.left + plotWidth / 2 : PAD.left + (index / (pointCount - 1)) * plotWidth;
  const yAt = (value: number) => PAD.top + plotHeight - ((value - min) / (max - min)) * plotHeight;
  const ticks = Array.from({ length: Y_TICK_COUNT }, (_, index) => min + ((max - min) / (Y_TICK_COUNT - 1)) * index);
  const labelEvery = Math.max(1, Math.ceil(pointCount / MAX_X_LABELS));

  return (
    <div>
      <Legend series={series} format={format} />
      <svg className={styles.svgChart} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`折线图，共 ${pointCount} 个数据点`}>
        {ticks.map((tick) => (
          <g key={tick}>
            <line className={styles.gridLine} x1={PAD.left} x2={WIDTH - PAD.right} y1={yAt(tick)} y2={yAt(tick)} />
            <text className={styles.axisText} x={PAD.left - 10} y={yAt(tick) + 4} textAnchor="end">{formatAxisValue(tick)}</text>
          </g>
        ))}
        <line className={styles.axisLine} x1={PAD.left} x2={WIDTH - PAD.right} y1={PAD.top + plotHeight} y2={PAD.top + plotHeight} />
        {series.map((item, seriesIndex) => (
          <g key={item.field}>
            <polyline className={styles.linePath} style={{ stroke: SERIES_COLORS[seriesIndex % SERIES_COLORS.length] }} points={item.points.map((point, index) => `${xAt(index)},${yAt(point.value)}`).join(" ")} />
            {item.points.map((point, index) => (
              <circle key={`${item.field}-${index}`} className={styles.linePoint} style={{ fill: SERIES_COLORS[seriesIndex % SERIES_COLORS.length] }} cx={xAt(index)} cy={yAt(point.value)} r={3.5}>
                <title>{`${getChartFieldLabel(item.field)} · ${point.label}：${formatValue(point.value, format)}`}</title>
              </circle>
            ))}
          </g>
        ))}
        {series[0]?.points.map((point, index) => index % labelEvery === 0 ? (
          <text key={`label-${index}`} className={styles.axisText} x={xAt(index)} y={PAD.top + plotHeight + 18} textAnchor="middle">{point.label}</text>
        ) : null)}
      </svg>
    </div>
  );
}

function BarChart({ series, format }: { series: ChartSeries[]; format: ValueFormat | null }) {
  const max = Math.max(...series.flatMap((item) => item.points.map((point) => point.value)), 0);
  const base = max > 0 ? max : 1;
  return (
    <div>
      <Legend series={series} format={format} />
      {series.map((item, seriesIndex) => (
        <section key={item.field} className={styles.barSeries} aria-label={`${getChartFieldLabel(item.field)}柱状图`}>
          {series.length > 1 ? <h3 className={styles.barSeriesTitle}>{getChartFieldLabel(item.field)}</h3> : null}
          <ul className={styles.barList}>
            {item.points.map((point, index) => (
              <li key={`${item.field}-${index}-${point.label}`} className={styles.barRow}>
                <span className={styles.barLabel} title={point.label}>{point.label}</span>
                <span className={styles.barTrack}>
                  <span className={styles.barFill} style={{ width: `${(point.value / base) * 100}%`, background: SERIES_COLORS[seriesIndex % SERIES_COLORS.length] }} />
                </span>
                <span className={styles.barValue}>{formatValue(point.value, format)}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
