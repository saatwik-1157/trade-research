export type Role = "user" | "trader" | "admin";

export const ROLE_RANK: Record<Role, number> = { user: 0, trader: 1, admin: 2 };

export function roleAtLeast(have: Role, need: Role): boolean {
  return ROLE_RANK[have] >= ROLE_RANK[need];
}
