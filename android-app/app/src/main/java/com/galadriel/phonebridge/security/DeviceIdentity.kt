package com.galadriel.phonebridge.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Signature
import java.security.spec.ECGenParameterSpec

class DeviceIdentity {
    companion object {
        private const val KEY_ALIAS = "phone_bridge_device_identity_v1"
        private const val AUTH_CONTEXT = "galadriel-phone-bridge-v1:"
    }

    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun ensureKey() {
        if (keyStore.containsAlias(KEY_ALIAS)) return
        val generator = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC,
            "AndroidKeyStore",
        )
        generator.initialize(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
            )
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .build(),
        )
        generator.generateKeyPair()
    }

    fun publicKey(): String {
        ensureKey()
        val certificate = keyStore.getCertificate(KEY_ALIAS)
        return Base64.encodeToString(certificate.publicKey.encoded, Base64.NO_WRAP)
    }

    fun signChallenge(nonce: String): String {
        ensureKey()
        val entry = keyStore.getEntry(KEY_ALIAS, null) as KeyStore.PrivateKeyEntry
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(entry.privateKey)
        signer.update((AUTH_CONTEXT + nonce).toByteArray(StandardCharsets.US_ASCII))
        return Base64.encodeToString(signer.sign(), Base64.NO_WRAP)
    }
}
