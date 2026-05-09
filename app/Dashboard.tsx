"use client"

import { useState, useEffect, useMemo } from "react"
import { signOut } from "next-auth/react"
import type { Config, State, Target, LocId } from "@/lib/types"
import { LOCATIONS, ALL_TIMES } from "@/lib/types"

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
  const [showCookie, setShowCookie] = useState(false)
  const [savingMsg, setSavingMsg] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string | null>(loadError)
  const [now, setNow] = useState(() => Date.now())

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
    ? minutesSince(state.last_check, now) < 10
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
        if (!t.times.includes(timeName)) return
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
          <Stat label="Currently open" value={currentlyOpen} accent={currentlyOpen > 0} />
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

          {/* ADD FORM */}
          {showAdd && (
            <TargetForm
              initial={null}
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
                      onSubmit={(updated) => upsertTarget(updated, idx)}
                      onCancel={() => setEditingIndex(null)}
                    />
                  )
                }
                return (
                  <TargetRow
                    key={idx}
                    target={t}
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

        {/* COOKIE REFRESH */}
        <section className="mt-16 pt-8 border-t border-line">
          <h2 className="font-display text-xl font-medium tracking-tight mb-2">
            Session cookie
          </h2>
          <p className="text-sm text-ink2 leading-relaxed mb-4">
            The monitor uses your browser's <code className="font-mono text-xs bg-line/40 px-1.5 py-0.5 rounded">PHPSESSID</code> cookie to read court availability.
            It expires periodically — when it does, you'll get an email and need to refresh it here.
          </p>
          {!showCookie ? (
            <button
              onClick={() => setShowCookie(true)}
              className="text-sm font-medium text-court hover:text-courtdark"
            >
              Update PHPSESSID →
            </button>
          ) : (
            <CookieRefreshForm
              onCancel={() => setShowCookie(false)}
              onError={(msg) => setErrorMsg(msg)}
            />
          )}
        </section>

        <footer className="mt-20 pt-6 border-t border-line text-xs text-ink3 flex items-center justify-between">
          <span>Cron runs every 5 min · 4 polls per run · ~60s effective</span>
          <a
            href="https://crystalsports-booking.kegroup.co.th/booking.php"
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
  openCount,
  onEdit,
  onDelete,
}: {
  target: Target
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
      ? target.times.join(", ")
      : `${target.times[0]}–${target.times[target.times.length - 1]} (${target.times.length} slots)`

  return (
    <div className="bg-white border border-line rounded-xl p-4 flex items-center justify-between gap-4 hover:border-line2 transition-colors">
      <div className="flex-1 min-w-0">
        <div className="font-medium text-ink leading-snug mb-1">{target.name}</div>
        <div className="text-sm text-ink3 tabular">
          {target.date} · {timeStr} · {locStr}
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {openCount > 0 ? (
          <span className="px-2.5 py-1 bg-courtlight text-court text-xs font-medium rounded-md tabular">
            {openCount} open
          </span>
        ) : (
          <span className="px-2.5 py-1 bg-line/40 text-ink3 text-xs font-medium rounded-md">
            booked
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
  onSubmit,
  onCancel,
}: {
  initial: Target | null
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
  const [times, setTimes] = useState<string[]>(initial?.times || [])
  const [submitting, setSubmitting] = useState(false)

  function toggleLoc(loc: LocId) {
    setLocations((prev) =>
      prev.includes(loc) ? prev.filter((l) => l !== loc) : [...prev, loc],
    )
  }

  function toggleTime(t: string) {
    setTimes((prev) =>
      prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t].sort(),
    )
  }

  function setTimeRange(from: number, to: number) {
    const range: string[] = []
    for (let h = from; h <= to; h++) range.push(`${String(h).padStart(2, "0")}:00`)
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
      times: [...times].sort(),
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
            const active = times.includes(t)
            return (
              <button
                key={t}
                type="button"
                onClick={() => toggleTime(t)}
                className={`px-2 py-1.5 text-xs rounded-md border transition-all tabular ${
                  active
                    ? "bg-court text-canvas border-court"
                    : "bg-canvas text-ink2 border-line hover:border-line2"
                }`}
              >
                {t}
              </button>
            )
          })}
        </div>
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

function CookieRefreshForm({
  onCancel,
  onError,
}: {
  onCancel: () => void
  onError: (msg: string) => void
}) {
  const [value, setValue] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [success, setSuccess] = useState(false)

  async function submit() {
    if (!value.trim()) return
    setSubmitting(true)
    try {
      const r = await fetch("/api/cookie", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value: value.trim() }),
      })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        throw new Error(j.error || `HTTP ${r.status}`)
      }
      setSuccess(true)
      setTimeout(() => {
        onCancel()
      }, 2500)
    } catch (e: any) {
      onError(e.message || "Failed to update cookie")
      setSubmitting(false)
    }
  }

  if (success) {
    return (
      <div className="bg-courtlight border border-court/30 rounded-xl p-4 text-sm text-court">
        ✓ PHPSESSID updated. Next monitor run (within 5 min) will use the new value.
      </div>
    )
  }

  return (
    <div className="bg-white border border-line rounded-xl p-5 space-y-3">
      <p className="text-xs text-ink3 leading-relaxed">
        Log in to the booking site, then DevTools → Application → Cookies → copy the value of <code className="font-mono bg-line/40 px-1 py-0.5 rounded">PHPSESSID</code> and paste below.
      </p>
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="68sj0h1vppr8i7qc2tpg915n5f"
        className="w-full px-3 py-2 border border-line rounded-md bg-canvas focus:border-court focus:bg-white text-ink placeholder:text-ink3 font-mono text-sm tabular transition-colors"
        spellCheck={false}
        autoComplete="off"
      />
      <div className="flex items-center justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 text-sm text-ink2 hover:text-ink rounded-md transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={submit}
          disabled={!value.trim() || submitting}
          className={`px-5 py-2 text-sm font-medium rounded-md transition-colors ${
            value.trim() && !submitting
              ? "bg-court text-canvas hover:bg-courtdark"
              : "bg-line/60 text-ink3 cursor-not-allowed"
          }`}
        >
          {submitting ? "Updating…" : "Update cookie"}
        </button>
      </div>
    </div>
  )
}

// ============================================================================
// Helpers
// ============================================================================

function minutesSince(iso: string, now: number): number {
  const t = new Date(iso).getTime()
  return Math.floor((now - t) / 60000)
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
