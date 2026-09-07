"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchMe, logout } from "@/lib/api";

export const ME_KEY = ["me"] as const;

/** The signed-in user, or a 401 error when there is no session. */
export function useMe() {
  return useQuery({
    queryKey: ME_KEY,
    queryFn: fetchMe,
    retry: (count, err) => !(err instanceof ApiError && err.status === 401) && count < 1,
    staleTime: 30_000,
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSettled: () => qc.invalidateQueries({ queryKey: ME_KEY }),
  });
}
