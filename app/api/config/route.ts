import { getServerSession } from "next-auth"
import { NextResponse } from "next/server"
import { authOptions } from "@/lib/auth"
import { getFile, putFile } from "@/lib/github"
import type { Config } from "@/lib/types"

export const dynamic = "force-dynamic"

async function requireAuth() {
  const session = await getServerSession(authOptions)
  if (!session) return null
  return session
}

export async function GET() {
  if (!(await requireAuth())) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 })
  }
  const file = await getFile("config.json")
  const config: Config = file ? JSON.parse(file.content) : { targets: [] }
  return NextResponse.json(config)
}

export async function PUT(req: Request) {
  const session = await requireAuth()
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 })
  }

  let body: Config
  try {
    body = await req.json()
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 })
  }

  if (!body || !Array.isArray(body.targets)) {
    return NextResponse.json({ error: "missing targets array" }, { status: 400 })
  }

  // Light validation
  for (const t of body.targets) {
    if (typeof t.name !== "string" || !t.name.trim()) {
      return NextResponse.json({ error: "each target needs a name" }, { status: 400 })
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(t.date)) {
      return NextResponse.json({ error: `invalid date: ${t.date}` }, { status: 400 })
    }
    if (!Array.isArray(t.locations) || t.locations.length === 0) {
      return NextResponse.json({ error: "each target needs at least 1 location" }, { status: 400 })
    }
    if (!Array.isArray(t.times) || t.times.length === 0) {
      return NextResponse.json({ error: "each target needs at least 1 time" }, { status: 400 })
    }
  }

  const username = (session.user as any)?.login || "user"
  await putFile(
    "config.json",
    JSON.stringify(body, null, 2) + "\n",
    `Update config (via dashboard, by @${username})`,
  )

  return NextResponse.json({ ok: true })
}
