package com.androidagent.companion

import android.content.Context
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.util.concurrent.atomic.AtomicBoolean

/**
 * A newline-delimited JSON request/response server on an abstract namespace
 * LocalSocket. Termux reaches it from Python with socket.AF_UNIX and a
 * leading NUL byte in the address, which is the same mechanism Termux:API
 * itself uses.
 */
class Bridge(
    private val context: Context,
    private val handler: (op: String, args: JSONObject) -> JSONObject,
) {
    companion object {
        const val SOCKET_NAME = "com.androidagent.companion.bridge"
        private const val TAG = "AgentBridge"
        private const val MAX_REQUEST_BYTES = 64 * 1024
        const val PROTOCOL_VERSION = 1
    }

    private val running = AtomicBoolean(false)
    private var server: LocalServerSocket? = null
    private var thread: Thread? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        thread = Thread({ serve() }, "agent-bridge").apply { isDaemon = true; start() }
    }

    fun stop() {
        running.set(false)
        try { server?.close() } catch (e: Exception) { Log.w(TAG, "close failed", e) }
        server = null
    }

    private fun serve() {
        try {
            server = LocalServerSocket(SOCKET_NAME)
        } catch (e: Exception) {
            Log.e(TAG, "cannot bind $SOCKET_NAME", e)
            running.set(false)
            return
        }
        Log.i(TAG, "listening on abstract:$SOCKET_NAME")
        while (running.get()) {
            val client = try {
                server?.accept() ?: break
            } catch (e: Exception) {
                if (running.get()) Log.w(TAG, "accept failed", e)
                break
            }
            try {
                client.use { handleOne(it) }
            } catch (e: Exception) {
                Log.w(TAG, "connection failed", e)
            }
        }
        Log.i(TAG, "bridge stopped")
    }

    private fun handleOne(client: LocalSocket) {
        val writer = OutputStreamWriter(client.outputStream)
        val reply: JSONObject = try {
            val reader = BufferedReader(InputStreamReader(client.inputStream), 8192)
            val line = readBounded(reader) ?: return
            dispatch(client, line)
        } catch (e: Exception) {
            error("internal_error", e.message ?: e.javaClass.simpleName)
        }
        writer.write(reply.toString())
        writer.write("\n")
        writer.flush()
    }

    /** Reads one line, refusing anything oversized rather than buffering it. */
    private fun readBounded(reader: BufferedReader): String? {
        val sb = StringBuilder()
        while (true) {
            val ch = reader.read()
            if (ch < 0) return if (sb.isEmpty()) null else sb.toString()
            if (ch == '\n'.code) return sb.toString()
            sb.append(ch.toChar())
            if (sb.length > MAX_REQUEST_BYTES) throw IllegalArgumentException("request too large")
        }
    }

    private fun dispatch(client: LocalSocket, line: String): JSONObject {
        val request = try {
            JSONObject(line)
        } catch (e: Exception) {
            return error("bad_request", "payload is not JSON")
        }

        if (request.optInt("v", 0) != PROTOCOL_VERSION) {
            return error("version_mismatch", "this companion speaks protocol v$PROTOCOL_VERSION")
        }

        val expected = Pairing.token(context)
        if (!Pairing.tokenMatches(expected, request.optString("token", ""))) {
            Log.w(TAG, "rejected request with bad token")
            return error("unauthorised", "pairing token does not match")
        }

        val peerUid = try { client.peerCredentials.uid } catch (e: Exception) { Pairing.NO_UID }
        val pinned = Pairing.pairedUid(context)
        if (pinned == Pairing.NO_UID) {
            if (peerUid == Pairing.NO_UID) return error("unauthorised", "cannot read peer uid")
            Pairing.pinUid(context, peerUid)
            Log.i(TAG, "paired with uid $peerUid")
        } else if (peerUid != pinned) {
            Log.w(TAG, "rejected uid $peerUid, paired to $pinned")
            return error("unauthorised", "this companion is paired to a different app")
        }

        val op = request.optString("op", "")
        if (op.isEmpty()) return error("bad_request", "missing op")
        val args = request.optJSONObject("args") ?: JSONObject()
        return try {
            handler(op, args)
        } catch (e: Exception) {
            Log.w(TAG, "op $op failed", e)
            error("internal_error", e.message ?: e.javaClass.simpleName)
        }
    }
}

fun ok(data: JSONObject): JSONObject =
    JSONObject().put("ok", true).put("data", data)

fun error(code: String, message: String): JSONObject =
    JSONObject().put("ok", false).put("code", code).put("error", message)
