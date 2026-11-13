"use client"

import { useState, useEffect, useMemo } from "react"
import type { ReactNode } from "react"
import { signOut } from "next-auth/react"
import type { Config, State, Target, LocId, TimeSlot, AutobookMode } from "@/lib/types"
import { LOCATIONS, ALL_TIMES } from "@/lib/types"

const BOOKING_URL = "https://crystalsports-booking.kegroup.co.th/booking"
const STALE_AFTER_MINUTES = 10

// What starring means right now, from the mode the monitor's latest run
// reported in state.json. No mode reported yet ("unknown") gets the
// cautious, live-style wording: assume it may book.
type ModeView = AutobookMode | "unknown"

function modeView(state: State): ModeView {
  const m = state.autobook_mode
  return m === "off" || m === "dry_run" || m === "live" ? m : "unknown"
}

const DANGER = "bg-claylight text-clay border border-clay/30"
const CAUTION = "bg-amberlight text-amber border border-amber/20"

function modeText(mode: ModeView, halted: boolean, blocked: boolean) {
  switch (mode) {
    case "off":
      return {
        banner: halted
          ? "Auto-book is HALTED: state.json couldn't be trusted, so the monitor books nothing until the " +
            "\"autobook_halted\" entry is removed (run restore_state.py — see the email). Starred times get a " +
            "normal alert — book manually."
          : blocked
            ? "Auto-book is PAUSED: a run's state.json couldn't be saved to the repo, so the monitor books " +
              "nothing until a later run saves it (it resumes by itself). Starred times get a normal alert — " +
              "book manually."
            : "Auto-book is off. Starred times get a normal alert — book manually.",
        bannerClass: CAUTION,
        label: "starred — notify only (auto-book off)",
        labelClass: "bg-amberlight text-amber",
        star: "Starred — notify only (auto-book off)",
      }
    case "dry_run":
      return {
        banner:
          "Auto-book is in DRY RUN: for starred times it checks everything and emails what it would book, " +
          "but books nothing. Starred times still get a normal alert — book manually.",
        bannerClass: CAUTION,
        label: "starred — dry run (emails what it would book, books nothing)",
        labelClass: "bg-amberlight text-amber",
        star: "Starred — dry run (emails what it would book, books nothing)",
      }
    case "live":
      return {
        banner:
          "Auto-book is LIVE: starred times are booked automatically and paid from your Wallet as soon as " +
          "they open. To stop it, set the AUTOBOOK_MODE repository variable to off.",
        bannerClass: DANGER,
        label: "AUTO-BOOK — books with Wallet when it opens",
        labelClass: `${DANGER} font-semibold`,
        star: "AUTO-BOOK — books with Wallet when it opens",
      }
    default:
      return {
        banner:
          "Auto-book mode unknown — the monitor hasn't reported it yet. Assume starred times may be booked " +
          "automatically and paid from your Wallet as soon as they open.",
        bannerClass: DANGER,
        label: "AUTO-BOOK (mode unknown) — may book with Wallet when it opens",
        labelClass: `${DANGER} font-semibold`,
        star: "AUTO-BOOK (mode unknown) — may book with Wallet when it opens",
      }
  }
}

type Props = {
  initialConfig: Config
  initialState: State
  loadError: string | null
  user: { name: string; login: string; image: string | null }
}

export default function Dashboard({ initialConfig, initialState, loadError, user }: Props) {
  const [config, setConfig] = useState<Config>(initialConfig)
  const [state, setState] = useState<State>(initialState)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [savingMsg, setSavingMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(loadError)
  const [now, setNow] = useState(() => Date.now())
  const mode = modeView(state)
  // Halted = the key is present, whatever its value (null included).
  const modeInfo = modeText(mode, "autobook_halted" in state, !!state.autobook_blocked)

  // Tick clock so "last check: X min ago" stays fresh
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(t)
  }, [])

  // Auto-refresh state every 30s so the user sees fresh data
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const r = await fetch("/api/state", { cache: "no-store" })
        if (r.ok) setState(await r.json())
      } catch {
        /* swallow */
      }
    }, 30_000)
    return () => clearInterval(t)
  }, [])

  // Counts
  const todayStr = new Date().toISOString().slice(0, 10)
  const activeTargets = config.targets.filter((t) => t.date >= todayStr)
  const currentlyOpen = state.currently_open_count ?? 0
  const lastCheckStr = state.last_check
    ? formatTimeAgo(state.last_check, now)
    : "never"
  const status = state.last_check
    ? minutesSince(state.last_check, now) < STALE_AFTER_MINUTES
      ? "active"
      : "stale"
    : "stale"

  async function saveConfig(next: Config) {
    setSavingMsg("Saving…")
    setErrorMsg(null)
    try {
      const r = await fetch("/api/config", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(next),
      })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        throw new Error(j.error || `HTTP ${r.status}`)
      }
      setConfig(next)
      setSavingMsg("Saved")
      setTimeout(() => setSavingMsg(null), 1500)
    } catch (e: any) {
      setErrorMsg(e.message || "Failed to save")
      setSavingMsg(null)
    }
  }

  function deleteTarget(idx: number) {
    if (!confirm("Delete this target?")) return
    const next = { ...config, targets: config.targets.filter((_, i) => i !== idx) }
    saveConfig(next)
  }

  function upsertTarget(target: Target, replaceIndex: number | null) {
    const next: Config = { ...config, targets: [...config.targets] }
    if (replaceIndex !== null) {
      next.targets[replaceIndex] = target
    } else {
      next.targets.push(target)
    }
    saveConfig(next)
    setShowAdd(false)
    setEditingIndex(null)
  }

  // Map known-open slots → which target they belong to (for the "X open" badges)
  const openByTargetIndex = useMemo(() => {
    const m = new Map<number, number>()
    const known = state.known_open || {}
    for (const k of Object.keys(known)) {
      const [date, locId, , timeName] = k.split("|")
      activeTargets.forEach((t, i) => {
        if (t.date !== date) return
        if (!t.locations.includes(locId as LocId)) return
        if (!t.times.some((x) => x.time === timeName)) return
        m.set(i, (m.get(i) || 0) + 1)
      })
    }
    return m
  }, [state.known_open, activeTargets])

  return (
    <div className="min-h-screen pb-20">
      <div className="max-w-3xl mx-auto px-6 pt-10">
        {/* HEADER */}
        <header className="flex items-center justify-between mb-12">
          <div>
            <h1 className="font-display text-3xl font-medium tracking-tight leading-none">
              Court Checker
            </h1>
            <p className="text-sm text-ink3 mt-1.5 tabular">
              Logged in as{" "}
              <span className="text-ink2">@{user.login || user.name}</span>
            </p>
          </div>
          <button
            onClick={() => signOut({ callbackUrl: "/" })}
            className="text-sm text-ink3 hover:text-ink2 transition-colors"
          >
            Sign out
          </button>
        </header>

        {/* TOAST */}
        {errorMsg && (
          <div className="mb-6 px-4 py-3 bg-claylight border border-clay/20 rounded-lg text-sm text-clay flex items-start justify-between gap-3">
            <div>{errorMsg}</div>
            <button onClick={() => setErrorMsg(null)} className="text-clay/60 hover:text-clay">
              ✕
            </button>
          </div>
        )}
        {savingMsg && (
          <div className="fixed bottom-6 right-6 px-4 py-2.5 bg-ink text-canvas rounded-lg text-sm shadow-lg z-50 tabular">
            {savingMsg}
          </div>
        )}

        {/* STATUS BAR */}
        <div className="flex items-center gap-3 mb-8 text-sm">
          <span
            className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full font-medium tabular ${
              status === "active"
                ? "bg-courtlight text-court"
                : "bg-amberlight text-amber"
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                status === "active" ? "bg-court" : "bg-amber"
              }`}
            />
            {status === "active" ? "Active" : "Idle"}
          </span>
          <span className="text-ink3 tabular">Last check {lastCheckStr}</span>
        </div>

        {/* STATS */}
        <section className="grid grid-cols-3 gap-3 mb-10">
          <Stat label="Targets watching" value={activeTargets.length} />
          <Stat label="Open time slots" value={currentlyOpen} accent={currentlyOpen > 0} />
          <Stat
            label="Total time slots"
            value={activeTargets.reduce(
              (s, t) => s + t.times.length * t.locations.length,
              0,
            )}
          />
        </section>

        {/* WATCH LIST */}
        <section>
          <div className="flex items-center justify-between mb-5">
            <h2 className="font-display text-xl font-medium tracking-tight">
              Watch list
            </h2>
            {!showAdd && editingIndex === null && (
              <button
                onClick={() => setShowAdd(true)}
                className="text-sm font-medium text-court hover:text-courtdark inline-flex items-center gap-1.5 transition-colors"
              >
                <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 5v14M5 12h14" strokeLinecap="round" />
                </svg>
                Add target
              </button>
            )}
          </div>

          <p
            className={`mb-5 px-4 py-3 rounded-lg text-sm leading-relaxed ${modeInfo.bannerClass}`}
            data-autobook-mode={mode}
          >
            {modeInfo.banner}
          </p>

          {/* ADD FORM */}
          {showAdd && (
            <TargetForm
              initial={null}
              mode={mode}
              onSubmit={(t) => upsertTarget(t, null)}
              onCancel={() => setShowAdd(false)}
            />
          )}

          {/* TARGETS */}
          {activeTargets.length === 0 && !showAdd ? (
            <div className="bg-white border border-line rounded-xl p-8 text-center">
              <p className="text-ink3 text-sm mb-4">No targets yet.</p>
              <button
                onClick={() => setShowAdd(true)}
                className="text-sm font-medium text-court hover:text-courtdark"
              >
                Add your first one →
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              {config.targets.map((t, idx) => {
                if (t.date < todayStr) return null
                if (editingIndex === idx) {
                  return (
                    <TargetForm
                      key={idx}
                      initial={t}
                      mode={mode}
                      onSubmit={(updated) => upsertTarget(updated, idx)}
                      onCancel={() => setEditingIndex(null)}
                    />
                  )
                }
                return (
                  <TargetRow
                    key={idx}
                    target={t}
                    mode={mode}
                    openCount={openByTargetIndex.get(idx) || 0}
                    onEdit={() => setEditingIndex(idx)}
                    onDelete={() => deleteTarget(idx)}
                  />
                )
              })}
            </div>
          )}

          {/* PAST TARGETS (auto-skipped, surfaced for cleanup) */}
          {config.targets.some((t) => t.date < todayStr) && (
            <details className="mt-8">
              <summary className="text-sm text-ink3 cursor-pointer hover:text-ink2">
                {config.targets.filter((t) => t.date < todayStr).length} past target(s) — click to clean up
              </summary>
              <div className="mt-3 space-y-2">
                {config.targets.map(
                  (t, idx) =>
                    t.date < todayStr && (
                      <div
                        key={idx}
                        className="flex items-center justify-between text-sm py-2 px-4 bg-white border border-line rounded-lg opacity-60"
                      >
                        <span>
                          {t.name} <span className="text-ink3 tabular">· {t.date}</span>
                        </span>
                        <button
                          onClick={() => deleteTarget(idx)}
                          className="text-clay/70 hover:text-clay text-xs"
                        >
                          Remove
                        </button>
                      </div>
                    ),
                )}
              </div>
            </details>
          )}
        </section>

        {/* BOT SESSION (read-only) */}
        <BotSessionPanel state={state} now={now} />

        <footer className="mt-20 pt-6 border-t border-line text-xs text-ink3 flex items-center justify-between">
          <span>Triggered by cron-job.org via GitHub Actions · 4 polls per run</span>
          <a
            href={BOOKING_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-ink2"
          >
            Booking site →
          </a>
        </footer>
      </div>
    </div>
  )
}

// ============================================================================
// Sub-components
// ============================================================================

function Stat({ label, value, accent = false }: { label: string; value: number; accent?: boolean }) {
  return (
    <div className="bg-white border border-line rounded-xl px-4 py-4">
      <div className="text-xs text-ink3 uppercase tracking-wider mb-1.5 font-medium">
        {label}
      </div>
      <div
        className={`font-display text-3xl font-medium tabular leading-none ${
          accent ? "text-court" : "text-ink"
        }`}
      >
        {value}
      </div>
    </div>
  )
}

function TargetRow({
  target,
  mode,
  openCount,
  onEdit,
  onDelete,
}: {
  target: Target
  mode: ModeView
  openCount: number
  onEdit: () => void
  onDelete: () => void
}) {
  const locStr =
    target.locations.length === 2
      ? "Either court"
      : LOCATIONS[target.locations[0] as LocId]
  const timeStr =
    target.times.length <= 3
      ? target.times.map((t) => t.time).join(", ")
      : `${target.times[0].time}–${target.times[target.times.length - 1].time} (${target.times.length} slots)`
  const starredTimes = target.times.filter((t) => t.autoBook).map((t) => t.time)
  const modeInfo = modeText(mode, false, false)

  return (
    <div className="bg-white border border-line rounded-xl p-4 flex items-center justify-between gap-4 hover:border-line2 transition-colors">
      <div className="flex-1 min-w-0">
        <div className="font-medium text-ink leading-snug mb-1">{target.name}</div>
        <div className="text-sm text-ink3 tabular">
          {target.date} · {timeStr} · {locStr}
        </div>
        {starredTimes.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {starredTimes.map((t) => (
              <span
                key={t}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 text-[11px] font-medium rounded tabular ${modeInfo.labelClass}`}
              >
                ★ {t} {modeInfo.label}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {openCount > 0 ? (
          <span
            className="px-2.5 py-1 bg-courtlight text-court text-xs font-medium rounded-md tabular"
            title={`${openCount} watched time slot${openCount === 1 ? "" : "s"} open right now`}
          >
            {openCount} {openCount === 1 ? "time" : "times"} open
          </span>
        ) : (
          <span className="px-2.5 py-1 bg-line/40 text-ink3 text-xs font-medium rounded-md">
            Monitoring
          </span>
        )}
        <button
          onClick={onEdit}
          className="p-2 text-ink3 hover:text-ink2 hover:bg-line/40 rounded-md transition-colors"
          aria-label="Edit"
        >
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M17 3a2.828 2.828 0 114 4L7.5 20.5 2 22l1.5-5.5L17 3z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        <button
          onClick={onDelete}
          className="p-2 text-ink3 hover:text-clay hover:bg-claylight rounded-md transition-colors"
          aria-label="Delete"
        >
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14z" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </div>
  )
}

function TargetForm({
  initial,
  mode,
  onSubmit,
  onCancel,
}: {
  initial: Target | null
  mode: ModeView
  onSubmit: (t: Target) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(initial?.name || "")
  const [date, setDate] = useState(
    initial?.date || new Date(Date.now() + 86400000).toISOString().slice(0, 10),
  )
  const [locations, setLocations] = useState<LocId[]>(
    initial?.locations || ["LOC001", "LOC002"],
  )
  const [times, setTimes] = useState<TimeSlot[]>(initial?.times || [])
  const modeInfo = modeText(mode, false, false)
  const [submitting, setSubmitting] = useState(false)

  function toggleLoc(loc: LocId) {
    setLocations((prev) =>
      prev.includes(loc) ? prev.filter((l) => l !== loc) : [...prev, loc],
    )
  }

  function toggleTime(t: string) {
    setTimes((prev) =>
      prev.some((x) => x.time === t)
        ? prev.filter((x) => x.time !== t)
        : [...prev, { time: t, autoBook: false }].sort((a, b) => a.time.localeCompare(b.time)),
    )
  }

  function toggleStar(t: string) {
    setTimes((prev) =>
      prev.map((x) => (x.time === t ? { ...x, autoBook: !x.autoBook } : x)),
    )
  }

  function setTimeRange(from: number, to: number) {
    // Carry over autoBook for times that stay selected across the preset change
    const existingByTime = new Map(times.map((x) => [x.time, x.autoBook]))
    const range: TimeSlot[] = []
    for (let h = from; h <= to; h++) {
      const t = `${String(h).padStart(2, "0")}:00`
      range.push({ time: t, autoBook: existingByTime.get(t) ?? false })
    }
    setTimes(range)
  }

  function submit() {
    if (!name.trim() || !date || locations.length === 0 || times.length === 0) {
      return
    }
    setSubmitting(true)
    onSubmit({
      name: name.trim(),
      date,
      locations: [...locations].sort() as LocId[],
      times: [...times].sort((a, b) => a.time.localeCompare(b.time)),
    })
  }

  const valid = name.trim() && date && locations.length > 0 && times.length > 0

  return (
    <div className="bg-white border border-court/30 rounded-xl p-5 space-y-4 mb-3">
      <div>
        <label className="block text-xs font-medium uppercase tracking-wider text-ink3 mb-2">
          Name
        </label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Saturday morning"
          className="w-full px-3 py-2 border border-line rounded-md bg-canvas focus:border-court focus:bg-white text-ink placeholder:text-ink3 transition-colors"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium uppercase tracking-wider text-ink3 mb-2">
            Date
          </label>
          <input
            type="date"
            value={date}
            min={new Date().toISOString().slice(0, 10)}
            onChange={(e) => setDate(e.target.value)}
            className="w-full px-3 py-2 border border-line rounded-md bg-canvas focus:border-court focus:bg-white text-ink tabular transition-colors"
          />
        </div>
        <div>
          <label className="block text-xs font-medium uppercase tracking-wider text-ink3 mb-2">
            Courts
          </label>
          <div className="flex gap-2">
            {(["LOC001", "LOC002"] as LocId[]).map((loc) => (
              <button
                key={loc}
                type="button"
                onClick={() => toggleLoc(loc)}
                className={`flex-1 px-3 py-2 text-sm rounded-md border transition-all ${
                  locations.includes(loc)
                    ? "bg-court text-canvas border-court"
                    : "bg-canvas text-ink2 border-line hover:border-line2"
                }`}
              >
                {LOCATIONS[loc]}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="block text-xs font-medium uppercase tracking-wider text-ink3">
            Times <span className="text-ink3/60 normal-case tracking-normal">({times.length} selected)</span>
          </label>
          <div className="flex gap-2 text-xs">
            <button
              type="button"
              onClick={() => setTimeRange(7, 12)}
              className="text-court hover:text-courtdark"
            >
              Morning
            </button>
            <span className="text-ink3">·</span>
            <button
              type="button"
              onClick={() => setTimeRange(13, 17)}
              className="text-court hover:text-courtdark"
            >
              Afternoon
            </button>
            <span className="text-ink3">·</span>
            <button
              type="button"
              onClick={() => setTimeRange(18, 22)}
              className="text-court hover:text-courtdark"
            >
              Evening
            </button>
            <span className="text-ink3">·</span>
            <button
              type="button"
              onClick={() => setTimeRange(7, 22)}
              className="text-court hover:text-courtdark"
            >
              All day
            </button>
            <span className="text-ink3">·</span>
            <button
              type="button"
              onClick={() => setTimes([])}
              className="text-ink3 hover:text-ink2"
            >
              Clear
            </button>
          </div>
        </div>
        <div className="grid grid-cols-9 gap-1.5">
          {ALL_TIMES.map((t) => {
            const slot = times.find((x) => x.time === t)
            const active = !!slot
            const starred = slot?.autoBook ?? false
            return (
              <button
                key={t}
                type="button"
                onClick={() => toggleTime(t)}
                className={`relative px-2 py-1.5 text-xs rounded-md border transition-all tabular ${
                  active
                    ? "bg-court text-canvas border-court"
                    : "bg-canvas text-ink2 border-line hover:border-line2"
                }`}
              >
                {active && (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleStar(t)
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault()
                        e.stopPropagation()
                        toggleStar(t)
                      }
                    }}
                    className={`absolute -top-1.5 -right-1.5 w-4 h-4 flex items-center justify-center rounded-full text-[10px] leading-none transition-colors ${
                      starred
                        ? mode === "live" || mode === "unknown"
                          ? "bg-clay text-canvas"
                          : "bg-amber text-canvas"
                        : "bg-white text-ink3 border border-line hover:border-line2"
                    }`}
                    aria-label={starred ? `Unstar ${t}` : `Star ${t}`}
                    title={starred ? modeInfo.star : `Not starred — click to star (${modeInfo.star})`}
                  >
                    {starred ? "★" : "☆"}
                  </span>
                )}
                {t}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-ink3 mt-2">
          Click a time to include it · click its star to mark it starred: {modeInfo.star}
        </p>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-sm text-ink2 hover:text-ink rounded-md transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={submit}
          disabled={!valid || submitting}
          className={`px-5 py-2 text-sm font-medium rounded-md transition-colors ${
            valid && !submitting
              ? "bg-court text-canvas hover:bg-courtdark"
              : "bg-line/60 text-ink3 cursor-not-allowed"
          }`}
        >
          {submitting ? "Saving…" : initial ? "Save changes" : "Add target"}
        </button>
      </div>
    </div>
  )
}

function BotSessionPanel({ state, now }: { state: State; now: number }) {
  const alert = state.session_alert
  const mins = state.last_check ? minutesSince(state.last_check, now) : null
  const stale = mins === null || mins >= STALE_AFTER_MINUTES

  let tone: "ok" | "warn" | "bad"
  let label: string
  let detail: string
  if (alert) {
    tone = "bad"
    label = "Needs reseed"
    detail =
      `${alert.kind === "lost" ? "Session likely lost (a rotated cookie couldn't be saved)" : "Session expired (login redirect)"}` +
      ` — detected ${formatBangkok(alert.sent_at)} (${formatTimeAgo(alert.sent_at, now)}).` +
      " The monitor sends no requests until you reseed."
  } else if (stale) {
    tone = "warn"
    label = "Stale"
    detail = state.last_check
      ? `No booking page read since ${formatBangkok(state.last_check)} (${formatTimeAgo(state.last_check, now)}).`
      : "The monitor hasn't recorded a successful check yet."
    detail += state.unreadable_since
      ? ` It hasn't been able to read the booking site since ${formatBangkok(state.unreadable_since)}.`
      : " Check that cron-job.org is running and the Actions runs are green."
  } else {
    tone = "ok"
    label = "OK"
    detail = `Last booking page read ${formatTimeAgo(state.last_check as string, now)}.`
  }

  const pill = {
    ok: "bg-courtlight text-court",
    warn: "bg-amberlight text-amber",
    bad: "bg-claylight text-clay",
  }[tone]
  const dot = { ok: "bg-court", warn: "bg-amber", bad: "bg-clay" }[tone]

  return (
    <section className="mt-16 pt-8 border-t border-line">
      <div className="flex items-center gap-3 mb-2">
        <h2 className="font-display text-xl font-medium tracking-tight">Bot session</h2>
        <span className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-full text-xs font-medium ${pill}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
          {label}
        </span>
      </div>
      <p className="text-sm text-ink2 leading-relaxed mb-4">
        The monitor logs in with its own Crystal Sports session, kept encrypted in{" "}
        <Code>bot_session.enc</Code> (key: the <Code>BOT_JAR_KEY</Code> secret). {detail}
      </p>
      <details className="bg-white border border-line rounded-xl px-5 py-4 text-sm text-ink2" open={tone === "bad"}>
        <summary className="cursor-pointer font-medium text-ink">Reseed steps (on the Mac)</summary>
        <ol className="mt-3 space-y-2.5 list-decimal pl-5 leading-relaxed">
          <li>
            Open an <strong>Incognito</strong> window and log in to the booking site. Don't use your everyday
            browser session.
          </li>
          <li>
            In <Code>court-checker-live</Code>, delete the old session:{" "}
            <Code>git pull --rebase &amp;&amp; git rm -q bot_session.enc &amp;&amp; rm -f .bot_session.json.handed-off-*</Code>
          </li>
          <li>
            DevTools → Application → Cookies: copy <Code>cs.did</Code>, then run{" "}
            <Code>export CS_DID="$(pbpaste)"</Code>; copy <Code>cs.member</Code> <em>last</em>, then run{" "}
            <Code>export CS_MEMBER="$(pbpaste)"</Code>.
          </li>
          <li>Close the Incognito window — don't log out.</li>
          <li>
            Seed and check it once: <Code>python3 probe_new_site.py; unset CS_DID CS_MEMBER</Code>
          </li>
          <li>
            <Code>python3 seal_session.py</Code> — encrypts the session and puts a new key on the clipboard.
          </li>
          <li>
            Paste the key into the <Code>BOT_JAR_KEY</Code> secret <strong>before copying anything else</strong>:{" "}
            <Code>pbpaste | gh secret set BOT_JAR_KEY; pbcopy &lt;/dev/null</Code> (or GitHub → Settings →
            Secrets and variables → Actions).
          </li>
          <li>
            <Code>git add bot_session.enc &amp;&amp; git commit -m "Reseed bot session" &amp;&amp; git push</Code>
          </li>
        </ol>
      </details>
    </section>
  )
}

function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-xs bg-line/40 px-1.5 py-0.5 rounded break-all">{children}</code>
}

// ============================================================================
// Helpers
// ============================================================================

function minutesSince(iso: string, now: number): number {
  const t = new Date(iso).getTime()
  return Math.floor((now - t) / 60000)
}

function formatBangkok(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString("en-GB", {
    timeZone: "Asia/Bangkok",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  })
}

function formatTimeAgo(iso: string, now: number): string {
  const mins = minutesSince(iso, now)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins} min ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
