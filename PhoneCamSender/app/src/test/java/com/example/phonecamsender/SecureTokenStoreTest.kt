package com.example.phonecamsender

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SecureTokenStoreTest {
    @Test
    fun authorizationHeaderAcceptsVisibleAsciiToken() {
        val token = "A".repeat(64)
        assertEquals("Bearer $token", SecureTokenStore.authorizationHeader(token))
    }

    @Test
    fun authorizationHeaderRejectsMissingShortAndWhitespaceTokens() {
        assertNull(SecureTokenStore.authorizationHeader(null))
        assertNull(SecureTokenStore.authorizationHeader("short"))
        assertNull(SecureTokenStore.authorizationHeader("A".repeat(32) + " "))
        assertNull(SecureTokenStore.authorizationHeader("A".repeat(257)))
    }
}
