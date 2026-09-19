export type LocId = "LOC001" | "LOC002"

export type TimeSlot = {
  time: string                    // "20:00"
  autoBook: boolean               // starred — auto-book to pending payment when this slot opens
}

export type Target = {
  name: string
  date: string                    // YYYY-MM-DD
  locations: LocId[]
  times: TimeSlot[]
}

// Accepts either a legacy plain time string or a {time, autoBook} object per
// entry, and always returns the latter. Mirrors monitor.py's
// normalize_times() so config.json's shape is interpreted identically
// regardless of which side last touched it.
export function normalizeTimes(times: unknown[]): TimeSlot[] {
  return times.map((t) =>
    typeof t === "string"
      ? { time: t, autoBook: false }
      : { time: (t as TimeSlot).time, autoBook: !!(t as TimeSlot).autoBook },
  )
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
