"use client"

import { signIn } from "next-auth/react"

export default function SignIn() {
  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <div className="max-w-md w-full">
        <div className="text-center mb-12">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-court mb-6">
            <svg viewBox="0 0 24 24" className="w-6 h-6 text-canvas" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="9" />
              <path d="M3 12 a 9 9 0 0 1 18 0" strokeLinecap="round" />
              <path d="M21 12 a 9 9 0 0 1 -18 0" strokeLinecap="round" />
            </svg>
          </div>
          <h1 className="font-display text-4xl font-medium tracking-tight mb-3">
            Court Checker
          </h1>
          <p className="text-ink2 text-base leading-relaxed">
            Watch Crystal Sports tennis court openings.
            <br />
            Get an email the moment a slot frees up.
          </p>
        </div>

        <button
          onClick={() => signIn("github", { callbackUrl: "/" })}
          className="w-full flex items-center justify-center gap-3 bg-ink text-canvas py-3.5 px-6 rounded-lg font-medium hover:bg-courtdark transition-colors"
        >
          <svg viewBox="0 0 24 24" className="w-5 h-5" fill="currentColor" aria-hidden="true">
            <path d="M12 .5C5.4.5 0 5.9 0 12.5c0 5.3 3.4 9.8 8.2 11.4.6.1.8-.3.8-.6v-2.1c-3.3.7-4-1.6-4-1.6-.5-1.4-1.3-1.7-1.3-1.7-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2.9-.3 2-.4 3-.4s2.1.1 3 .4C17 4.5 18 4.8 18 4.8c.7 1.7.2 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6C20.6 22.3 24 17.8 24 12.5 24 5.9 18.6.5 12 .5z" />
          </svg>
          Sign in with GitHub
        </button>

        <p className="text-center text-xs text-ink3 mt-6">
          Access is restricted to allowlisted GitHub usernames.
        </p>
      </div>
    </div>
  )
}
