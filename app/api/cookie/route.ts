import { getServerSession } from "next-auth"
import { NextResponse } from "next/server"
import { authOptions } from "@/lib/auth"
import { updateSecret } from "@/lib/github"

export const dynamic = "force-dynamic"

export async function PUT(req: Request) {
  const session = await getServerSession(authOptions)
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 })
  }

  let body: { value?: string }
  try {
    body = await req.json()
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 })
  }

  const value = body.value?.trim()
  if (!value) {
    return NextResponse.json({ error: "value is required" }, { status: 400 })
  }

  // Sanity check: PHPSESSID values are typically 26-32 alphanumeric chars
  if (!/^[a-zA-Z0-9]{20,64}$/.test(value)) {
    return NextResponse.json(
      { error: "value doesn't look like a valid PHPSESSID (expected 20-64 alphanumeric chars)" },
      { status: 400 },
    )
  }

  try {
    await updateSecret("PHPSESSID", value)
    return NextResponse.json({ ok: true })
  } catch (e: any) {
    return NextResponse.json(
      { error: `failed to update secret: ${e.message || "unknown error"}` },
      { status: 500 },
    )
  }
}
