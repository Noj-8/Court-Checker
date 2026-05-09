import type { NextAuthOptions } from "next-auth"
import GitHub from "next-auth/providers/github"

const allowedUsers = (process.env.ALLOWED_GITHUB_USERS || "")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean)

export const authOptions: NextAuthOptions = {
  providers: [
    GitHub({
      clientId: process.env.GITHUB_OAUTH_CLIENT_ID || "",
      clientSecret: process.env.GITHUB_OAUTH_CLIENT_SECRET || "",
    }),
  ],
  session: { strategy: "jwt" },
  callbacks: {
    async signIn({ profile }) {
      const username = ((profile as any)?.login as string | undefined)?.toLowerCase()
      if (!username) return false
      if (allowedUsers.length === 0) {
        // No allowlist configured — only allow nobody, force user to set it
        return false
      }
      return allowedUsers.includes(username)
    },
    async jwt({ token, profile }) {
      if (profile) {
        ;(token as any).login = (profile as any).login
      }
      return token
    },
    async session({ session, token }) {
      if (session.user) {
        ;(session.user as any).login = (token as any).login
      }
      return session
    },
  },
}
