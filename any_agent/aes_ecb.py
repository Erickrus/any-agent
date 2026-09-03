"""
AES-128-ECB crypto helpers for WeChat CDN upload.
Ported from openclaw-weixin/src/cdn/aes-ecb.ts.

WeChat CDN encrypts media with AES-128-ECB + PKCS7 padding.
"""

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt bytes with AES-128-ECB (PKCS7 padding). Key must be 16 bytes."""
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(pad(plaintext, AES.block_size))


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB (PKCS7 padding). Key must be 16 bytes."""
    cipher = AES.new(key, AES.MODE_ECB)
    return unpad(cipher.decrypt(ciphertext), AES.block_size)


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Ciphertext size after AES-128-ECB with PKCS7 padding: ceil((n+1)/16)*16."""
    return ((plaintext_size + 1 + 15) // 16) * 16
