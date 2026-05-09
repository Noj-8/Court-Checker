export type LocId = "LOC001" | "LOC002"

export type Target = {
  name: string
  date: string                    // YYYY-MM-DD
  locations: LocId[]
  times: string[]                 // ["20:00", "21:00", ...]
}

export type Config = {
  targets: Target[]
}

export type State = {
  last_check?: string             // ISO datetime
  currently_open_count?: number
  known_open?: Record<string, true>
  last_session_alert?: number
}

export const LOCATIONS: Record<LocId, string> = {
  LOC001: "Crystal Sports",
  LOC002: "Crystal Sports G",
}

// 06:00..23:00 — slots the booking site offers
export const ALL_TIMES: string[] =
  Array.from({ length: 18 }, (_, i) => `${String(i + 6).padStart(2, "0")}:00`)
