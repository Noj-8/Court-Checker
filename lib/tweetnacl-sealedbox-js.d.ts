declare module "tweetnacl-sealedbox-js" {
  /**
   * Encrypts a message using libsodium's `crypto_box_seal` (X25519 sealed box).
   * @param message Plaintext bytes to encrypt
   * @param publicKey Recipient's 32-byte X25519 public key
   * @returns Sealed-box ciphertext (Uint8Array)
   */
  export function seal(message: Uint8Array, publicKey: Uint8Array): Uint8Array

  /**
   * Decrypts a sealed-box message. Not used by this app, but exposed for completeness.
   */
  export function open(
    ciphertext: Uint8Array,
    publicKey: Uint8Array,
    secretKey: Uint8Array,
  ): Uint8Array | null

  const _default: { seal: typeof seal; open: typeof open }
  export default _default
}
