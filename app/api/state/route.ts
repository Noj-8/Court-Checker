import { getServerSession } from "next-auth"
import { NextResponse } from "next/server"
import { authOptions } from "@/lib/auth"
import { getFile } from "@/lib/github"
import type { State } from "@/lib/types"

export const dynamic = "force-dynamic"

export async function GET() {
  const session = await getServerSession(authOptions)
  if (!session) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 })
  }
  const file = await getFile("state.json")
  const state: State = file ? JSON.parse(file.content) : {}
  return NextResponse.json(state)
}
