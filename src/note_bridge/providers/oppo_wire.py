"""Current OPPO note envelope. Published vendor RSA public key; fresh AES key per request."""

import base64
import json
import secrets

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from ..errors import BridgeError

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlh6qqqYM/E/ruZ+nb/epfhCWMlVKJqFpzc9AL7PIl5mdk/HN1U57Z4kFMvT+m81kYle3foPWrl02bZ93BtmYPW5fEEHw2gRgTNum9BMhKEVSuvkBTLxqVoOA+iOwPsSZMUNAOg6eTWK93JeUSKBa2VZTNegBvVXZyq1nXrq40QuixZlViX6eR0QgHNVzb3q/qmRdXulRWtVr48plzVr2oRnVb+iPapswfeAuUT173IiIDDShdAU52wcBuvW6LKBw3y7XqfOm8yPBumSQ5px/4XyI6lcJnIWybTPlSW9BEiuaZzQLvUSj2lvPv4JmZ5omzXO52OlGx0n/HdCoe4eGfQIDAQAB
-----END PUBLIC KEY-----"""


class OppoRequest:
    def __init__(self, data: dict, public_key=PUBLIC_KEY):
        self._key = secrets.token_hex(16).encode("ascii")
        iv = secrets.token_hex(8).encode("ascii")
        wrapped = PKCS1_v1_5.new(RSA.import_key(public_key)).encrypt(self._key)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.payload = {
            "key": base64.b64encode(wrapped).decode("ascii"),
            "iv": iv.decode("ascii"),
            "encryptContent": base64.b64encode(
                AES.new(self._key, AES.MODE_CBC, iv).encrypt(pad(payload, 16))
            ).decode("ascii"),
        }

    def decrypt(self, data: dict):
        try:
            iv = data["iv"].encode("ascii")
            ciphertext = base64.b64decode(data["encryptContent"], validate=True)
            return json.loads(unpad(AES.new(self._key, AES.MODE_CBC, iv).decrypt(ciphertext), 16))
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BridgeError("decode_failed", "OPPO 正文响应无法正确解码，已停止处理。") from None

    def close(self):
        self._key = b""
        self.payload.clear()
