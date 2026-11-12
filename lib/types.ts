export type LocId = "LOC001" | "LOC002"

export type TimeSlot = {
  time: string                    // "20:00"
  autoBook: boolean               // starred. Auto-book is PAUSED (AUTOBOOK_ENABLED = False in
                                  // monitor.py): starred slots get a normal alert. Kept for stage 3.
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

// Written by monitor.py when the bot session fails (login redirect, or a
// rotated cookie that couldn't be saved). Removed again by the next
// successful page read.
export type SessionAlert = {
  kind: "expired" | "lost"        // "expired": login redirect; "lost": rotated cookie not saved/pushed
  sent_at: string                 // ISO datetime (+07:00) when the "reseed needed" email went out
  jar_sha256: string | null       // sha256 prefix of the bot_session.enc it was sent for
}

export type State = {
  last_check?: string             // ISO datetime of the last pass that READ a booking page
  currently_open_count?: number   // watched TIME SLOTS open right now (per time, not per court)
  known_open?: Record<string, true> // keys "YYYY-MM-DD|LOC00x|any|HH:MM" — one per open time slot
  session_alert?: SessionAlert
  unreadable_since?: string       // start of the current streak of passes that read no page
  unreadable_last?: string        // the latest such pass
  unreadable_passes?: number
  read_alert?: { sent_at: string } // the one "can't read the booking site" email for this streak
}

export const LOCATIONS: Record<LocId, string> = {
  LOC001: "Crystal Sports",
  LOC002: "Crystal Sports G",
}

// 06:00..23:00 — the 18 slots the new booking grid shows (confirmed by the probe, Nov 2026)
export const ALL_TIMES: string[] =
  Array.from({ length: 18 }, (_, i) => `${String(i + 6).padStart(2, "0")}:00`)
