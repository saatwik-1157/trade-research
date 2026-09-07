"use client";

import { useEffect, useRef } from "react";
import { Unavailable } from "@/components/ui";

/**
 * Candlestick surface (lightweight-charts, already a dependency — no second
 * charting library is introduced).
 *
 * It mounts with an empty series and an overlay saying so. An empty chart is
 * not a fake chart: it draws no data. The series handle is kept so L08 can
 * feed bars, and the same instance will carry volume, indicators, signal
 * markers and position lines when those levels land.
 */
export function PriceChart({
  symbol = "EURUSD",
  timeframe = "H1",
}: {
  symbol?: string;
  timeframe?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let cancelled = false;
    let remove: (() => void) | undefined;

    import("lightweight-charts").then(({ createChart, CandlestickSeries, ColorType }) => {
      if (cancelled) return;
      const chart = createChart(el, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: "#1a1a19" },
          textColor: "#898781",
          attributionLogo: false,
        },
        grid: { vertLines: { color: "#2c2c2a" }, horzLines: { color: "#2c2c2a" } },
        rightPriceScale: { borderColor: "#383835" },
        timeScale: { borderColor: "#383835" },
        crosshair: { mode: 0 },
      });
      chart.addSeries(CandlestickSeries, {
        upColor: "#0ca30c",
        downColor: "#d03b3b",
        wickUpColor: "#0ca30c",
        wickDownColor: "#d03b3b",
        borderVisible: false,
      });
      remove = () => chart.remove();
    });

    return () => {
      cancelled = true;
      remove?.();
    };
  }, []);

  return (
    <div className="relative h-full min-h-[320px]">
      <div className="absolute left-3 top-2 z-10 font-mono text-body text-ink-2">
        {symbol} · {timeframe}
      </div>
      <div ref={ref} className="absolute inset-0" data-testid="price-chart-surface" />
      <Unavailable
        overlay
        level={8}
        reason="No market data feed. The chart draws nothing until bars arrive from a real source."
        today="python tools/rule_backtest.py (MT5 history)"
      />
    </div>
  );
}
