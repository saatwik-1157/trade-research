import type { Role } from "./roles";

/** Shape of GET /health from the backend. Nothing here is a secret. */
export type TradingMode = "paper" | "demo" | "live";

export interface Health {
  status: string;
  time: string;
  service: string;
  version: string;
  environment: string;
  trading_mode: TradingMode;
  live_trading: boolean;
  live_execution_allowed: boolean;
  live_execution_blockers: string[];
}

/** Shape of GET /auth/me. The API never sends a password hash. */
export interface User {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}
