import { Octokit } from "@octokit/rest"
import sealedbox from "tweetnacl-sealedbox-js"

function octo() {
  const token = process.env.GITHUB_TOKEN
  if (!token) throw new Error("GITHUB_TOKEN env var is not set")
  return new Octokit({ auth: token })
}

function repoConfig() {
  const owner = process.env.GITHUB_OWNER
  const repo = process.env.GITHUB_REPO
  if (!owner || !repo) throw new Error("GITHUB_OWNER / GITHUB_REPO env vars not set")
  return { owner, repo }
}

export async function getFile(path: string): Promise<{ content: string; sha: string } | null> {
  const { owner, repo } = repoConfig()
  try {
    const { data } = await octo().repos.getContent({ owner, repo, path })
    if (Array.isArray(data) || data.type !== "file") {
      throw new Error(`${path} is not a file`)
    }
    const content = Buffer.from(data.content, "base64").toString("utf-8")
    return { content, sha: data.sha }
  } catch (e: any) {
    if (e.status === 404) return null
    throw e
  }
}

export async function putFile(path: string, content: string, message: string): Promise<void> {
  const { owner, repo } = repoConfig()
  const existing = await getFile(path)
  await octo().repos.createOrUpdateFileContents({
    owner,
    repo,
    path,
    message,
    content: Buffer.from(content, "utf-8").toString("base64"),
    sha: existing?.sha,
  })
}

export async function updateSecret(name: string, value: string): Promise<void> {
  const { owner, repo } = repoConfig()

  // 1. Fetch the repo's public key (base64-encoded X25519 key)
  const { data: pubKey } = await octo().actions.getRepoPublicKey({ owner, repo })

  // 2. Encrypt the secret value with libsodium-compatible sealed box
  const messageBytes = Buffer.from(value, "utf-8")
  const keyBytes = Buffer.from(pubKey.key, "base64")
  const encrypted = sealedbox.seal(messageBytes, keyBytes)
  const encryptedBase64 = Buffer.from(encrypted).toString("base64")

  // 3. Upload
  await octo().actions.createOrUpdateRepoSecret({
    owner,
    repo,
    secret_name: name,
    encrypted_value: encryptedBase64,
    key_id: pubKey.key_id,
  })
}
