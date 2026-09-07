"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchHealth } from "@/lib/api";

/** Live view of the backend's mode and gates. Polls every 10 s. */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  });
}
