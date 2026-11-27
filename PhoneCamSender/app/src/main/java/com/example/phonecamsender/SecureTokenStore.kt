package com.example.phonecamsender

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Stores the optional CamFlow LAN ingest token encrypted with an Android Keystore AES key.
 * The token is never written to the ordinary phonecam preferences or logged.
 */
internal object SecureTokenStore {
    private const val KEY_ALIAS = "camflow_ingest_token_v1"
    private const val PREFS_NAME = "phonecam_secure"
    private const val IV_KEY = "ingest_token_iv"
    private const val CIPHERTEXT_KEY = "ingest_token_ciphertext"
    private const val MIN_TOKEN_LENGTH = 32
    private const val MAX_TOKEN_LENGTH = 256

    fun authorizationHeader(token: String?): String? {
        if (token == null || token.length !in MIN_TOKEN_LENGTH..MAX_TOKEN_LENGTH) return null
        if (token.any { it !in '!'..'~' }) return null
        return "Bearer $token"
    }

    fun save(context: Context, token: String) {
        require(authorizationHeader(token) != null) {
            "Token must be 32-256 visible ASCII characters without whitespace"
        }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val encrypted = cipher.doFinal(token.toByteArray(StandardCharsets.UTF_8))
        val preferences = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        preferences.edit()
            .putString(IV_KEY, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putString(CIPHERTEXT_KEY, Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .apply()
    }

    fun readToken(context: Context): String? {
        val preferences = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val encodedIv = preferences.getString(IV_KEY, null)
        val encodedCiphertext = preferences.getString(CIPHERTEXT_KEY, null)
        if (encodedIv == null || encodedCiphertext == null) {
            if (encodedIv != null || encodedCiphertext != null) clear(context)
            return null
        }
        return try {
            val iv = Base64.decode(encodedIv, Base64.NO_WRAP)
            require(iv.size == 12) { "Invalid AES-GCM IV" }
            val ciphertext = Base64.decode(encodedCiphertext, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(128, iv))
            String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8).takeIf {
                authorizationHeader(it) != null
            } ?: run {
                clear(context)
                null
            }
        } catch (_: Exception) {
            clear(context)
            null
        }
    }

    fun clear(context: Context) {
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .remove(IV_KEY)
            .remove(CIPHERTEXT_KEY)
            .apply()
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            "AndroidKeyStore"
        )
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }
}
