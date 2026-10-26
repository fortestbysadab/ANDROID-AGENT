package com.androidagent.companion

import android.content.Context
import java.security.SecureRandom
import android.util.Base64

/**
 * The pairing secret and the UID it was claimed by.
 *
 * The bridge listens on an abstract LocalSocket, which any app on the device
 * can connect to. Two things keep that safe:
 *
 *  1. every request must carry the pairing token, compared in constant time;
 *  2. the first caller to present a valid token pins its UID, and every later
 *     connection must come from that same UID.
 *
 * So a token leak alone does not grant access to a second app, and an app
 * guessing UIDs cannot get in without the token.
 */
object Pairing {
    private const val PREFS = "pairing"
    private const val KEY_TOKEN = "token"
    private const val KEY_UID = "paired_uid"
    const val NO_UID = -1

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Returns the current token, minting one on first use. */
    fun token(context: Context): String {
        val store = prefs(context)
        store.getString(KEY_TOKEN, null)?.let { return it }
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        val minted = Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        store.edit().putString(KEY_TOKEN, minted).putInt(KEY_UID, NO_UID).apply()
        return minted
    }

    /** Invalidates the token and the pinned UID, forcing a re-pair. */
    fun reset(context: Context) {
        prefs(context).edit().remove(KEY_TOKEN).remove(KEY_UID).apply()
    }

    fun pairedUid(context: Context): Int = prefs(context).getInt(KEY_UID, NO_UID)

    fun pinUid(context: Context, uid: Int) {
        prefs(context).edit().putInt(KEY_UID, uid).apply()
    }

    /** Length-independent, early-exit-free comparison. */
    fun tokenMatches(expected: String, supplied: String): Boolean {
        val a = expected.toByteArray()
        val b = supplied.toByteArray()
        var diff = a.size xor b.size
        for (i in a.indices) {
            diff = diff or (a[i].toInt() xor b[if (i < b.size) i else 0].toInt())
        }
        return diff == 0
    }
}
