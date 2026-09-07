/** Symbols and timeframes the terminal offers. */

/** The seven majors the overnight session trades, plus the two the ledger holds. */
export const SYMBOLS = [
  "EURUSD",
  "GBPUSD",
  "USDJPY",
  "USDCAD",
  "AUDUSD",
  "USDCHF",
  "NZDUSD",
  "DE40",
  "XAUUSD",
] as const;

export const TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4", "D1"] as const;

export type Symbol = (typeof SYMBOLS)[number];
export type Timeframe = (typeof TIMEFRAMES)[number];

export const DEFAULT_SYMBOL: Symbol = "EURUSD";
export const DEFAULT_TIMEFRAME: Timeframe = "H1";
