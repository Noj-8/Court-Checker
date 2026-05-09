import { getServerSession } from "next-auth"
import { authOptions } from "@/lib/auth"
import { getFile } from "@/lib/github"
import type { Config, State } from "@/lib/types"
import SignIn from "./SignIn"
import Dashboard from "./Dashboard"

export const dynamic = "force-dynamic"

export default async function Home() {
  const session = await getServerSession(authOptions)

  if (!session) {
    return <SignIn />
  }

  // Load initial data on the server so the dashboard renders with content immediately
  let initialConfig: Config = { targets: [] }
  let initialState: State = {}
  let loadError: string | null = null

  try {
    const [configFile, stateFile] = await Promise.all([
      getFile("config.json"),
      getFile("state.json"),
    ])
    if (configFile) initialConfig = JSON.parse(configFile.content)
    if (stateFile) initialState = JSON.parse(stateFile.content)
  } catch (e: any) {
    loadError = e?.message || "Failed to load data from GitHub"
  }

  return (
    <Dashboard
      initialConfig={initialConfig}
      initialState={initialState}
      loadError={loadError}
      user={{
        name: session.user?.name || (session.user as any)?.login || "user",
        login: (session.user as any)?.login || "",
        image: session.user?.image || null,
      }}
    />
  )
}
